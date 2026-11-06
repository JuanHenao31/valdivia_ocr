"""Orquesta un documento que llega por WhatsApp: clasifica -> enruta -> procesa -> responde.

- Proceso 1 (planillas)  -> PlanillaService  -> Excel 1 "Mano de obra"
- Proceso 2 (documentos) -> DocumentPipeline -> Excel 2 plantilla Valdivia
- No concluyente         -> se le pide al remitente que aclare (caption "planilla"/"remisión").

Corre síncrono dentro de una BackgroundTask: el webhook ya respondió 200 a YCloud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.infrastructure import storage
from app.infrastructure.files import UploadedDoc
from app.services.classifier_service import (
    RUTA_DOCUMENTOS,
    RUTA_PLANILLAS,
    Clasificacion,
    ClassifierService,
)

logger = logging.getLogger(__name__)

MSG_NO_CLASIFICADO = (
    "No pude identificar qué documento es {archivo}. Reenvíalo con un texto que diga "
    "*planilla* (control diario de trabajadores) o *remisión* / *liquidación*."
)
MSG_ERROR = "Tuve un problema procesando {archivo}. Ya quedó registrado; inténtalo de nuevo en unos minutos."
MSG_AYUDA = (
    "Hola 👋 Envíame la foto o el PDF de una *planilla* de control diario, una *remisión* "
    "o una *liquidación* y te devuelvo el Excel listo."
)


@dataclass
class Resultado:
    clasificacion: Clasificacion
    nombre_excel: str | None = None
    resumen: str = ""
    detalles: dict = field(default_factory=dict)


class InboundService:
    def __init__(self, classifier=None, pipeline=None, planillas=None) -> None:
        self.classifier = classifier or ClassifierService()
        self._pipeline = pipeline
        self._planillas = planillas

    @property
    def pipeline(self):
        if self._pipeline is None:
            from app.api.deps import get_pipeline

            self._pipeline = get_pipeline()
        return self._pipeline

    @property
    def planillas(self):
        if self._planillas is None:
            from app.api.deps import get_planilla_service

            self._planillas = get_planilla_service()
        return self._planillas

    def procesar(self, doc: UploadedDoc, caption: str = "") -> Resultado:
        c = self.classifier.classify(doc, caption)
        logger.info(
            "clasificado %s -> %s (%s, %s, conf=%.2f, evidencia=%r)",
            doc.filename, c.ruta, c.tipo.value, c.metodo, c.confianza, c.evidencia,
        )
        if c.ruta == RUTA_PLANILLAS:
            r = self.planillas.process([doc])
            nombre = storage.guardar(r.xlsx, "planillas_mano_de_obra.xlsx")
            resumen = (
                f"✅ Planilla procesada ({r.rows} filas). "
                f"Celdas a revisar (amarillo): {len(r.yellow_cells)}. Excepciones: {len(r.exceptions)}."
            )
            return Resultado(c, nombre, resumen)
        if c.ruta == RUTA_DOCUMENTOS:
            xlsx, summary = self.pipeline.process([doc])
            nombre = storage.guardar(xlsx, "remisiones_liquidaciones.xlsx")
            tipos = ", ".join(sorted({d.doc_type.value for d in summary.documents})) or c.tipo.value
            resumen = (
                f"✅ Documento procesado ({tipos}). "
                f"Celdas a revisar (amarillo): {len(summary.yellow_cells)}. "
                f"Excepciones: {len(summary.exceptions)}."
            )
            return Resultado(c, nombre, resumen)
        return Resultado(c, None, MSG_NO_CLASIFICADO.format(archivo=doc.filename))

    def procesar_y_responder(self, ycloud, destino: str, media: dict, filename: str, caption: str = "") -> None:
        """Descarga la media, la procesa y responde al remitente. Nunca lanza."""
        try:
            doc = UploadedDoc(filename=filename, content=ycloud.descargar(media))
            res = self.procesar(doc, caption)
            if res.nombre_excel is None:
                ycloud.enviar_texto(destino, res.resumen)
                return
            nombre_publico = res.nombre_excel.split("__", 1)[1]
            ycloud.enviar_documento(destino, storage.enlace(res.nombre_excel), nombre_publico, res.resumen)
        except Exception:
            logger.exception("falló el procesamiento de %s", filename)
            try:
                ycloud.enviar_texto(destino, MSG_ERROR.format(archivo=filename))
            except Exception:
                logger.exception("tampoco se pudo avisar del error a %s", destino)
