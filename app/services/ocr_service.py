"""Servicio de OCR con OpenAI vision.

Proceso 2: un archivo (PDF/imagen) puede traer VARIOS documentos (p. ej. un PDF con las
remisiones CA 138 y CA 139), así que el OCR devuelve una lista de documentos, cada uno con
su `raw_text` y sus campos. Proceso 1 (planillas) usa `extract_json` con su propio prompt.
"""

import json

from app.core.config import settings
from app.domain.models import OCRResult
from app.infrastructure.openai_client import get_openai_client
from app.infrastructure.pdf import to_png_b64

# Remisión de finca (limón o plátano) -> hojas "Plantilla Limón" / "Plantilla Normal".
REMISION_FIELDS = [
    "cultivo",
    "finca",
    "remision_prefijo",
    "remision_num",
    "fecha",
    "cliente",
    "placa",
    "variedad",
    "kg_despachados",
    "canastillas",
    "lotes",
]

# Liquidación de planta de limón (informe Gramaluz "REPORTE PROCESO") -> columnas I–AB.
LIQUIDACION_FIELDS = [
    "liq_remision_prefijo",
    "liq_remision_num",
    "liq_placa",
    "liq_fecha",
    "liq_kg_primera",
    "liq_kg_primera_parejo",
    "liq_precio_exportacion",
    "liq_kg_nacional",
    "liq_precio_nacional",
    "liq_kg_industrial",
    "liq_precio_industrial",
]

# "Liquidación" de plátano: tiquetes de báscula + mensaje de WhatsApp con los kg entregados.
BASCULA_FIELDS = [
    "bas_placa",
    "bas_cliente",
    "bas_entregas",
]

SYSTEM_PROMPT = (
    "Eres un asistente de digitación de documentos agrícolas colombianos de la empresa "
    "Valdivia. Lees documentos escaneados o fotografiados (texto impreso + manuscrito) y "
    "devuelves SIEMPRE un objeto JSON. NUNCA inventes ni completes valores por suposición: "
    "si un dato no aparece o no es legible, valor null y confianza 0. Sé honesto con la "
    "confianza (0-100): si la letra es dudosa o estás adivinando, bájala."
)

USER_PROMPT = """Las imágenes pueden contener UNO O VARIOS documentos (p. ej. un PDF con dos
remisiones, una por página). Devuelve SOLO este JSON, con un elemento por documento:

{"documentos": [
  {
    "tipo": "remision" | "liquidacion_planta" | "liquidacion_bascula" | "otro",
    "raw_text": "<transcripción fiel de todo el texto de ESTE documento>",
    "fields": { ...campos del tipo, cada uno {"valor": ..., "confianza": <0-100>}... }
  }
]}

TIPO "remision" — formato Valdivia "REMISION", título "<FINCA> <CULTIVO> 2026   <PREFIJO> <NUM>":
  "cultivo":          "LIMON" | "PLATANO"           // del título: "CAMELIA LIMON TAHITI" -> LIMON
  "finca":            "<str MAYUS>"                 // del título: "LA PALMA PLATANO 2026" -> "LA PALMA"
  "remision_prefijo": "<str MAYUS>"                 // "MC 125" -> "MC"; "CA 138" -> "CA"
  "remision_num":     <int>                         // "MC 125" -> 125
  "fecha":            "<YYYY-MM-DD>"                // "14-10-2026" -> "2026-10-14"; "16/10/26" -> "2026-10-16"
  "cliente":          "<str MAYUS>"                 // SEÑOR(ES), tal como está escrito
  "placa":            "<str sin guiones/espacios MAYUS>"  // "TrJ 399" -> "TRJ399"
  "variedad":         "<str MAYUS>"                 // LIMON: "LIMON TAHITI". PLATANO: "HARTON" si dice Harton/Hartón
  "kg_despachados":   <int>                         // PESO TOTAL o CANTIDAD en kg. "10.375" -> 10375
  "canastillas":      <int>                         // CAJAS/BULTOS o "264 canastillas" -> 264
  "lotes":            [{"lote": <int>, "canastillas": <int|null>, "kg": <int|null>}]
      // OBSERVACIONES o descripción. "Lote 5-169=3201" -> {"lote":5,"canastillas":169,"kg":3201}
      // "lote 3 = 299" (sin canastillas) -> {"lote":3,"canastillas":null,"kg":299}
      // "Lote 10-120" (sin '=kg') -> {"lote":10,"canastillas":120,"kg":null}. NO calcules kg.

TIPO "liquidacion_planta" — informe de planta "REPORTE PROCESO" / "RESUMEN DE COMPRA":
  "liq_remision_prefijo":   "<str>"   // campo REMISIÓN "MC 125" -> "MC"
  "liq_remision_num":       <int>     // "MC 125" -> 125
  "liq_placa":              "<str sin guiones MAYUS>"  // PLACA "TRJ399"
  "liq_fecha":              "<YYYY-MM-DD>"  // FECHA DE DESCARGUE
  "liq_kg_primera":         <int>     // PESO KG de "(1) Primera". "2.909,0" -> 2909
  "liq_kg_primera_parejo":  <int>     // PESO KG de "(1) Primera Parejo". "363,0" -> 363
  "liq_precio_exportacion": <int>     // KG VLR de (1) Primera. "$ 2.019" -> 2019
  "liq_kg_nacional":        <int>     // PESO KG "(2) Mercado Nacional"
  "liq_precio_nacional":    <int>     // KG VLR Mercado Nacional
  "liq_kg_industrial":      <int>     // PESO KG "(4) Industria Proceso"
  "liq_precio_industrial":  <int>     // KG VLR Industria del RESUMEN DE COMPRA (no del detalle). "$ 694" -> 694
  Usa SIEMPRE la tabla "RESUMEN DE COMPRA" (página 1).

TIPO "liquidacion_bascula" — plátano: tiquetes de báscula (Tigreros, Montenegro...) y/o mensaje
de WhatsApp tipo "Valdivia la palma harton kls 7567 Entregado En Cuatro cielos canastillas Azules 375":
  "bas_placa":    "<str sin guiones MAYUS>"   // placa del tiquete, si se lee
  "bas_cliente":  "<str MAYUS>"               // "Entregado En Cuatro cielos" -> "CUATRO CIELOS"
  "bas_entregas": [{"finca": "<str MAYUS>", "kg": <int>, "canastillas": <int|null>}]
      // una por finca. "BONANZA FIAT KLS 2120. CAMELIA FIAT KLS 1130" -> dos entregas.
      // "Valdivia la palma harton kls 7567 ... canastillas Azules 375" -> [{"finca":"LA PALMA","kg":7567,"canastillas":375}]

IMPORTANTE: TODOS los campos, incluidas las listas ("lotes", "bas_entregas"), van envueltos:
"lotes": {"valor": [{"lote":5,"canastillas":169,"kg":3201}], "confianza": 85}.
Incluye en "fields" SOLO los campos del tipo del documento. Si es "otro", fields = {}.
Devuelve números como números (no strings). No incluyas texto fuera del JSON."""


class OCRService:
    def extract_json(
        self,
        content: bytes,
        system: str,
        user: str,
        model: str | None = None,
        detail: str = "high",
        max_pages: int | None = None,
    ) -> dict:
        """Manda el documento (páginas como imágenes) al modelo de visión y devuelve el JSON."""
        model = model or settings.ocr_model
        client = get_openai_client()
        images = to_png_b64(content, max_pages=max_pages or settings.max_pdf_pages)

        parts: list[dict] = [{"type": "text", "text": user}]
        for b64 in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": detail},
                }
            )

        # La familia gpt-5 / o-series no acepta temperature distinto al default.
        extra = {} if model.startswith(("gpt-5", "o1", "o3", "o4")) else {"temperature": 0}
        resp = client.chat.completions.create(
            model=model,
            **extra,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        )
        return json.loads(resp.choices[0].message.content)

    def extract(self, content: bytes) -> list[OCRResult]:
        """Proceso 2: devuelve un OCRResult por cada documento encontrado en el archivo."""
        data = self.extract_json(content, SYSTEM_PROMPT, USER_PROMPT)
        return [
            OCRResult(
                tipo=d.get("tipo") or "otro",
                raw_text=d.get("raw_text") or "",
                fields=normalize_fields(d.get("fields") or {}),
            )
            for d in (data.get("documentos") or [])
        ]


def normalize_fields(fields: dict) -> dict[str, dict]:
    """Garantiza {name: {"valor", "confianza"}}. Si el modelo devolvió el valor suelto
    (p. ej. `lotes` como lista), se envuelve SIN confianza (=0 -> queda para revisión)."""
    out = {}
    for name, f in fields.items():
        if isinstance(f, dict) and "valor" in f:
            out[name] = f
        else:
            out[name] = {"valor": f, "confianza": 0}
    return out
