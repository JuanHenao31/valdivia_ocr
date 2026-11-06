"""Clasificación de un documento entrante para saber a qué proceso va.

Escalera de menor a mayor costo; se detiene en el primer nivel concluyente:

1. **Reglas** sobre texto gratis: caption del WhatsApp + nombre de archivo + texto embebido
   del PDF. Determinista, sin llamadas externas.
2. **Encabezado OCR**: una lectura barata (modelo pequeño, `detail=low`, solo pág. 1) que
   transcribe el encabezado; se le vuelven a aplicar las reglas.
3. **Jev** (`Choice`) sobre ese encabezado, si está configurado y su confianza supera el umbral.
4. **Sugerencia del modelo** del paso 2 (último recurso, confianza baja).

Si nada concluye, la ruta es `desconocido` y el webhook le pide al usuario que aclare.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from app.core.config import settings
from app.domain.models import DocumentType
from app.infrastructure.files import UploadedDoc
from app.infrastructure.pdf import pdf_text

logger = logging.getLogger(__name__)

RUTA_PLANILLAS = "planillas"  # Proceso 1 -> Excel 1 (Mano de obra)
RUTA_DOCUMENTOS = "documentos"  # Proceso 2 -> Excel 2 (remisiones/liquidaciones)
RUTA_DESCONOCIDA = "desconocido"

RUTA_POR_TIPO = {
    DocumentType.CONTROL_DIARIO: RUTA_PLANILLAS,
    DocumentType.REMISION: RUTA_DOCUMENTOS,
    DocumentType.LIQUIDACION_PLANTA: RUTA_DOCUMENTOS,
    DocumentType.LIQUIDACION_BASCULA: RUTA_DOCUMENTOS,
}

# Patrones sobre texto normalizado (minúsculas, sin tildes).
REGLAS: dict[DocumentType, list[str]] = {
    DocumentType.CONTROL_DIARIO: [
        r"control\s*diario", r"planilla", r"jornal", r"trabajador", r"mano\s*de\s*obra",
    ],
    DocumentType.REMISION: [r"remision", r"\bremi\b"],
    DocumentType.LIQUIDACION_PLANTA: [
        r"reporte\s*(de\s*)?proceso", r"gramaluz", r"liquidacion", r"compra\s*de\s*limon",
    ],
    DocumentType.LIQUIDACION_BASCULA: [r"bascula", r"tiquete", r"\bkls?\b", r"\bpesaje\b"],
}

PROMPT_ENCABEZADO = (
    "Transcribe literalmente el encabezado y los títulos visibles de este documento "
    "(máximo 300 caracteres) y di qué tipo es. Tipos: control_diario (planilla de "
    "trabajadores con cédulas y labores), remision (remisión de fruta de una finca), "
    "liquidacion_planta (reporte/liquidación de planta de limón con kg por calidad), "
    "liquidacion_bascula (tiquete de báscula de plátano), otro. "
    'Responde JSON: {"encabezado": "...", "tipo": "..."}'
)


@dataclass
class Clasificacion:
    tipo: DocumentType
    ruta: str
    metodo: str  # reglas | encabezado_reglas | jev | modelo | ninguno
    confianza: float
    evidencia: str = ""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def por_reglas(texto: str) -> tuple[DocumentType | None, str]:
    """Devuelve el tipo si las reglas son concluyentes, y el patrón que lo decidió.

    Concluyente = el tipo con más coincidencias es único, o todos los tipos que coinciden
    van a la misma ruta (p. ej. remisión + liquidación en un mismo PDF -> Proceso 2).
    """
    t = _norm(texto)
    if not t.strip():
        return None, ""
    hits: dict[DocumentType, list[str]] = {}
    for tipo, patrones in REGLAS.items():
        found = [p for p in patrones if re.search(p, t)]
        if found:
            hits[tipo] = found
    if not hits:
        return None, ""
    best = max(len(v) for v in hits.values())
    top = [k for k, v in hits.items() if len(v) == best]
    rutas = {RUTA_POR_TIPO[k] for k in hits}
    if len(top) == 1 and len(rutas) == 1:
        return top[0], ",".join(hits[top[0]])
    if len(rutas) == 1:  # empate entre tipos del mismo proceso: la ruta es segura
        return top[0], ",".join(sum(hits.values(), []))
    if len(top) == 1 and best >= 2:  # mayoría clara pese a ruido del otro proceso
        return top[0], ",".join(hits[top[0]])
    return None, ""


class ClassifierService:
    def __init__(self, ocr=None, jev=None) -> None:
        # Perezoso: las reglas no necesitan clientes externos (tests, docs con caption).
        self._ocr = ocr
        self._jev = jev

    @property
    def ocr(self):
        if self._ocr is None:
            from app.services.ocr_service import OCRService

            self._ocr = OCRService()
        return self._ocr

    @property
    def jev(self):
        if self._jev is None:
            from app.services.validation_service import JevValidationService

            self._jev = JevValidationService()
        return self._jev

    @staticmethod
    def _hecho(tipo: DocumentType, metodo: str, conf: float, ev: str) -> Clasificacion:
        return Clasificacion(tipo, RUTA_POR_TIPO.get(tipo, RUTA_DESCONOCIDA), metodo, conf, ev)

    def classify(self, doc: UploadedDoc, caption: str = "") -> Clasificacion:
        # 1) Reglas sobre texto gratis.
        gratis = "\n".join([caption or "", doc.filename or "", pdf_text(doc.content)])
        tipo, ev = por_reglas(gratis)
        if tipo:
            return self._hecho(tipo, "reglas", 1.0, ev)

        # 2) Encabezado con un OCR barato.
        try:
            data = self.ocr.extract_json(
                doc.content,
                "Eres un clasificador de documentos agrícolas colombianos.",
                PROMPT_ENCABEZADO,
                model=settings.classifier_model,
                detail="low",
                max_pages=1,
            )
        except Exception:
            logger.exception("falló el OCR de encabezado de %s", doc.filename)
            return Clasificacion(DocumentType.OTRO, RUTA_DESCONOCIDA, "ninguno", 0.0)
        encabezado = str(data.get("encabezado") or "")
        tipo, ev = por_reglas(encabezado)
        if tipo:
            return self._hecho(tipo, "encabezado_reglas", 0.95, ev)

        # 3) Jev sobre el encabezado.
        try:
            jev = self.jev.classify_text(f"{caption}\n{encabezado}")
        except Exception:
            logger.exception("falló la clasificación con Jev de %s", doc.filename)
            jev = None
        if jev and jev[0] in RUTA_POR_TIPO and jev[1] >= settings.jev_support_threshold:
            return self._hecho(jev[0], "jev", jev[1], encabezado[:120])

        # 4) Sugerencia del modelo.
        try:
            sugerido = DocumentType(str(data.get("tipo") or "otro").strip().lower())
        except ValueError:
            sugerido = DocumentType.OTRO
        if sugerido in RUTA_POR_TIPO:
            return self._hecho(sugerido, "modelo", 0.6, encabezado[:120])
        return Clasificacion(DocumentType.OTRO, RUTA_DESCONOCIDA, "ninguno", 0.0, encabezado[:120])
