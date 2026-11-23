"""Servicio de OCR con OpenAI vision.

Proceso 2: un archivo (PDF/imagen) puede traer VARIOS documentos (p. ej. un PDF con las
remisiones CA 138 y CA 139), así que el OCR devuelve una lista de documentos, cada uno con
su `raw_text` y sus campos. Proceso 1 (planillas) usa `extract_json` con su propio prompt
y pasa PLANILLA_SCHEMA explícitamente (planilla_service._ocr_kwargs).
"""

import json
import logging
import random
import time
from types import SimpleNamespace

import httpx
import openai

from app.core.config import settings
from app.domain.models import OCRResult
from app.infrastructure.limites import OPENAI_SEM
from app.infrastructure.openai_client import get_openai_client, httpx_timeout
from app.infrastructure.pdf import to_png_b64
from app.infrastructure.telemetria import registrar_uso

logger = logging.getLogger(__name__)


class OCRError(RuntimeError):
    """El OCR falló tras agotar los reintentos (o por un error no reintentable)."""

    def __init__(self, msg: str, filename: str | None = None) -> None:
        self.filename = filename
        super().__init__(f"{msg} [archivo={filename or '?'}]")


class OCRTruncadoError(OCRError):
    """La salida del modelo quedó truncada (finish_reason=length/timeout) o no es JSON válido."""


# ---------------------------------------------------------------------------
# Esquemas Structured Outputs (strict): todo `required`, additionalProperties=false,
# nulos vía type [..., "null"]. Reproducen la forma que ya pedían los prompts.
# ---------------------------------------------------------------------------
_S, _I, _N = ["string", "null"], ["integer", "null"], ["number", "null"]


def _obj(props: dict, description: str | None = None) -> dict:
    o = {"type": "object", "properties": props, "required": list(props),
         "additionalProperties": False}
    if description:
        o["description"] = description
    return o


def _campo(valor, description: str | None = None) -> dict:
    """{"valor": <valor>, "confianza": int}. `valor` siempre admite null (lista con "null")."""
    v = {"type": valor} if isinstance(valor, list) else dict(valor)
    if description:
        v["description"] = description
    return _obj({"valor": v, "confianza": {"type": "integer", "description": "0-100"}})


# kg y precios como number: to_int (excel_service) redondea; "2.909,0" puede llegar como 2909.0.
_LOTES = {"type": ["array", "null"], "items": _obj(
    {"lote": {"type": _I}, "canastillas": {"type": _I}, "kg": {"type": _N}})}
_ENTREGAS = {"type": ["array", "null"], "items": _obj(
    {"finca": {"type": _S}, "kg": {"type": _N}, "canastillas": {"type": _I}})}

_REMISION_TIPOS = {
    "cultivo": _S, "finca": _S, "remision_prefijo": _S, "remision_num": _I, "fecha": _S,
    "cliente": _S, "placa": _S, "variedad": _S, "kg_despachados": _N, "canastillas": _I,
    "lotes": _LOTES,
}
_LIQUIDACION_TIPOS = {
    "liq_remision_prefijo": _S, "liq_remision_num": _I, "liq_placa": _S, "liq_fecha": _S,
    "liq_kg_primera": _N, "liq_kg_primera_parejo": _N, "liq_precio_exportacion": _N,
    "liq_kg_nacional": _N, "liq_precio_nacional": _N, "liq_kg_industrial": _N,
    "liq_precio_industrial": _N,
}
_BASCULA_TIPOS = {"bas_placa": _S, "bas_cliente": _S, "bas_entregas": _ENTREGAS}


def _documento(tipo: str, campos: dict) -> dict:
    """Un documento completo con `tipo` fijo: strict no tiene condicionales, así que el
    vínculo tipo -> fields se expresa con un anyOf de documentos completos."""
    return _obj({
        "tipo": {"type": "string", "enum": [tipo]},
        # raw_text se conserva: Jev (validation_service.validate) valida cada campo contra él.
        "raw_text": {"type": "string"},
        "fields": _obj({k: _campo(t) for k, t in campos.items()}),
    })


DOCUMENTOS_SCHEMA = _obj({"documentos": {"type": "array", "items": {"anyOf": [
    _documento("remision", _REMISION_TIPOS),
    _documento("liquidacion_planta", _LIQUIDACION_TIPOS),
    _documento("liquidacion_bascula", _BASCULA_TIPOS),
    _documento("otro", {}),
]}}})

# Planillas. El orden de las propiedades = orden en que el modelo las genera = orden físico
# de las columnas de la hoja. Las descripciones atan cada campo a SU columna: sin ellas, en
# strict el modelo corría valores de columna (LOTE -> rend, JOR vacío) y metía números o la
# palabra "contrato" en und. build_rows deriva Modalidad de rend (rend -> Contrato, si no Jornal).
_FILA_CAMPOS = {
    "nombre": (_S, "Columna NOMBRE."),
    "cedula": (_S, "Columna CEDULA, solo dígitos."),
    "labor": (_S, "Columna LABOR, tal como está escrita."),
    "cultivo": (_S, "Columna CULTIVO (ej. 'Limón', 'Plátano')."),
    "lote": (_S, "Columna LOTE (número/código del lote), justo después de las columnas "
                 "angostas D y P (que solo llevan 'X' o 'N/A' y se ignoran). No es una cantidad."),
    "jor": (_N, "Columna JOR: número escrito en la casilla JOR (jornales, normalmente 1). "
                "Se copia aunque la fila sea a contrato. null solo si la casilla JOR está vacía. "
                "No uses aquí el número de LOTE ni el de CANT CONTRATO."),
    "rend": (_N, "Cantidad a contrato: el NÚMERO de la columna CANT CONTRATO ('A x B = C' -> A; "
                 "'11 hectáreas x 85.000' -> 11). Solo si hay un número en CANT CONTRATO "
                 "(fila a Contrato). Fila de jornal (CANT CONTRATO vacía o con una palabra de "
                 "unidad como 'Día') -> null."),
    "und": (_S, "Unidad escrita en CANT CONTRATO o junto a la cantidad: palabra como 'Día', "
                "'Hectáreas', 'Metro', 'Horas'. Nunca un número ni 'contrato'/'jornal'. "
                "null si no hay palabra de unidad."),
    "vr_unit": (_I, "Columna VLR UNITARIO en pesos enteros ('85.000' -> 85000; 'A x B = C' -> B)."),
    "total_escrito": (_I, "Columna VALOR TOTAL en pesos enteros ('=935.000' -> 935000)."),
}
# raw_text se conserva: Jev valida los campos de la planilla contra él (planilla_service).
PLANILLA_SCHEMA = _obj({
    "raw_text": {"type": "string", "description": "Transcripción fiel de todo el texto de la hoja."},
    "finca": _campo(_S, "Casilla FINCA en MAYÚSCULAS."),
    "anio": _campo(_I, "Casilla AÑO."),
    "semana": _campo(_I, "Número de semana SOLO si está escrito un número en SEMANA."),
    "dia_semana": _campo(_S, "LUNES|MARTES|MIERCOLES|JUEVES|VIERNES|SABADO|DOMINGO."),
    "dia_mes": _campo(_I, "Día del mes: 'Viernes 23' -> 23."),
    "nota": _campo(_S, "Texto adicional escrito en la hoja (ej. 'Solo plátano')."),
    "filas": {"type": "array", "description": (
                  "Una fila por trabajador escrito; ignora filas vacías. Columnas de izquierda "
                  "a derecha: NOMBRE, CEDULA, LABOR, CULTIVO, D, P, LOTE, JOR, CANT CONTRATO, "
                  "VLR UNITARIO, VALOR TOTAL, ALIMENTACION/TIENDA. Cada número va al campo de "
                  "SU columna; no corras valores de una columna a otra."),
              "items": _obj({k: _campo(t, d) for k, (t, d) in _FILA_CAMPOS.items()})},
})


def _es_razonador(model: str) -> bool:
    """gpt-5 / o-series: modelos de razonamiento (aceptan reasoning_effort)."""
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _sin_temperature(model: str) -> bool:
    """gpt-5 / o-series no aceptan temperature distinto al default."""
    return _es_razonador(model)


def _usa_max_completion_tokens(model: str) -> bool:
    """gpt-5/o-series exigen max_completion_tokens; gpt-4o/4.1 lo aceptan. Solo los modelos
    antiguos (gpt-3.5, gpt-4, gpt-4-turbo...) se quedan con max_tokens."""
    return not (model.startswith(("gpt-3.5", "gpt-4-")) or model == "gpt-4")


def _retry_after(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    try:
        v = resp.headers.get("retry-after") if resp is not None else None
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _sleep(seconds: float) -> None:  # aislado para parchear en tests
    time.sleep(seconds)


class PlazoAgotado(Exception):
    """La llamada superó su plazo TOTAL (reloj de pared); se trata igual que un timeout."""


def _consumir_stream(stream, limite: float):
    """Lee el stream de chat.completions en ESTE hilo (el que tiene OPENAI_SEM) y arma una
    respuesta equivalente a la no-stream (choices[0].message.content/refusal, finish_reason,
    usage). Comprueba el plazo en cada chunk; al superarlo cierra el stream (cierra el
    socket) y lanza PlazoAgotado. Un socket mudo lo corta antes el read-timeout de httpx."""
    contenido: list[str] = []
    rechazo: list[str] = []
    finish = None
    usage = None
    try:
        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            for ch in getattr(chunk, "choices", None) or []:
                if getattr(ch, "index", 0) != 0:
                    continue
                d = getattr(ch, "delta", None)
                if d is not None:
                    if getattr(d, "content", None):
                        contenido.append(d.content)
                    if getattr(d, "refusal", None):
                        rechazo.append(d.refusal)
                if getattr(ch, "finish_reason", None):
                    finish = ch.finish_reason
            if time.monotonic() > limite:
                raise PlazoAgotado("plazo total agotado")
    except httpx.TimeoutException as e:
        raise PlazoAgotado(f"read timeout: {e}") from e
    except httpx.TransportError as e:
        raise openai.APIConnectionError(request=getattr(e, "request", None) or _REQ) from e
    finally:
        try:
            stream.close()
        except Exception:  # pragma: no cover
            pass
    msg = SimpleNamespace(content="".join(contenido), refusal="".join(rechazo) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=usage)


_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

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
    "raw_text": "<transcripción fiel del texto de ESTE documento, sin repetir líneas>",
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
        schema: dict | None = None,
        schema_name: str | None = None,
        max_tokens: int | None = None,
        filename: str | None = None,
        timeout: float | None = None,
        max_tokens_por_pagina: bool = False,
        tipo: str | None = None,
    ) -> dict:
        """Manda el documento (páginas como imágenes) al modelo de visión y devuelve el JSON.

        Con `schema` usa Structured Outputs strict; sin él, json_object (p. ej. clasificador).
        `max_tokens_por_pagina`: el tope es por página (x nº de páginas, hasta OCR_MAX_TOKENS_TOPE).
        `tipo` (planilla|documento|clasificador) solo etiqueta la telemetría."""
        model = model or settings.ocr_model
        client = get_openai_client()
        images = to_png_b64(content, max_pages=max_pages or settings.max_pdf_pages)
        tope = max(settings.ocr_max_tokens_tope, max_tokens or 0)
        if max_tokens and max_tokens_por_pagina and len(images) > 1:
            max_tokens = min(max_tokens * len(images), tope)

        parts: list[dict] = [{"type": "text", "text": user}]
        for b64 in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}", "detail": detail},
                }
            )
        if schema is not None:
            response_format = {"type": "json_schema", "json_schema": {
                "name": schema_name or "ocr", "strict": True, "schema": schema}}
        else:
            response_format = {"type": "json_object"}
        base: dict = {
            "model": model,
            "response_format": response_format,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }
        if _es_razonador(model) and settings.ocr_reasoning_effort:
            base["reasoning_effort"] = settings.ocr_reasoning_effort
        plazo = timeout or settings.openai_timeout_s
        # Plazo TOTAL de reloj de pared (lo impone _consumir_stream) + timeouts por fase.
        base["timeout"] = httpx_timeout(plazo)
        base["stream"] = True
        base["stream_options"] = {"include_usage": True}
        if tipo is None:
            tipo = {"planilla": "planilla", "documentos": "documento"}.get(
                schema_name or "", "clasificador" if schema is None else (schema_name or "ocr"))
        return self._llamar_con_reintentos(client, base, model, filename, max_tokens, tope, tipo,
                                           plazo=plazo)

    def _llamar_con_reintentos(self, client, base: dict, model: str, filename: str | None,
                               max_tokens: int | None = None, tope: int | None = None,
                               tipo: str = "ocr", plazo: float | None = None) -> dict:
        """Intento 1 a temperature=0. Truncado (finish_reason=length) -> reintento con el tope
        de tokens x2 (hasta `tope`; clave para gpt-5/o-series, que no aceptan temperature y
        si no repetirían la misma llamada) y, si el modelo acepta temperature, a
        OCR_RETRY_TEMPERATURE. Timeout/rechazo/JSON inválido -> reintento a
        OCR_RETRY_TEMPERATURE. 429/5xx/conexión -> misma temperatura, backoff exponencial +
        jitter (respeta Retry-After). El SDK va con max_retries=0 (openai_client).
        `plazo`: segundos TOTALES por intento; la llamada va en streaming y se corta al
        superarlo (PlazoAgotado -> misma ruta que un timeout). El semáforo se suelta en este
        hilo al salir del `with`, tras cerrar el stream."""
        plazo = plazo or settings.openai_timeout_s
        intentos = max(1, settings.ocr_max_attempts)
        tope = max(tope or 0, max_tokens or 0)
        key = "max_completion_tokens" if _usa_max_completion_tokens(model) else "max_tokens"
        temp_alta = False
        truncado = False
        ultimo = ""
        for intento in range(1, intentos + 1):
            kwargs = dict(base)
            if max_tokens:
                kwargs[key] = max_tokens
            if not _sin_temperature(model):
                kwargs["temperature"] = settings.ocr_retry_temperature if temp_alta else 0
            backoff: float | None = None  # None = sin espera; <0 = backoff exponencial
            resp, finish, t0 = None, None, time.monotonic()
            try:
                with OPENAI_SEM:
                    limite = time.monotonic() + plazo
                    stream = client.chat.completions.create(**kwargs)
                    resp = _consumir_stream(stream, limite)
            except PlazoAgotado as e:
                ultimo, temp_alta, truncado = f"timeout: plazo total {plazo:g}s ({e})", True, True
            except openai.APITimeoutError as e:  # antes que APIConnectionError (es subclase)
                ultimo, temp_alta, truncado = f"timeout: {e}", True, True
            except openai.RateLimitError as e:
                ra = _retry_after(e)
                ultimo, backoff, truncado = f"429: {e}", (-1 if ra is None else ra), False
            except openai.APIStatusError as e:
                if e.status_code < 500:
                    _telemetria(tipo, model, filename, None, intento, None, t0, f"http {e.status_code}")
                    raise OCRError(f"OpenAI respondió {e.status_code}: {e}", filename) from e
                ra = _retry_after(e)
                ultimo, backoff, truncado = f"{e.status_code}: {e}", (-1 if ra is None else ra), False
            except openai.APIConnectionError as e:
                ultimo, backoff, truncado = f"conexión: {e}", -1, False
            else:
                choice = resp.choices[0]
                msg = choice.message
                finish = choice.finish_reason
                if finish == "length":
                    ultimo, temp_alta, truncado = "salida truncada (finish_reason=length)", True, True
                    if max_tokens:
                        max_tokens = min(max_tokens * 2, tope)
                elif getattr(msg, "refusal", None):
                    ultimo, temp_alta, truncado = f"rechazo del modelo: {msg.refusal}", True, False
                else:
                    try:
                        data = json.loads(msg.content or "")
                        if not isinstance(data, dict):
                            raise ValueError("el JSON no es un objeto")
                        _telemetria(tipo, model, filename, resp, intento, finish, t0)
                        return data
                    except ValueError as e:  # JSONDecodeError es subclase de ValueError
                        ultimo, temp_alta, truncado = f"JSON inválido: {e}", True, True
            _telemetria(tipo, model, filename, resp, intento, finish, t0, ultimo)
            logger.warning("OCR %s intento %d/%d falló: %s", filename or "?", intento, intentos, ultimo)
            if intento < intentos and backoff is not None:
                espera = backoff if backoff >= 0 else (
                    settings.ocr_backoff_base_s * 2 ** (intento - 1) * (1 + random.random()))
                _sleep(min(espera, 60.0))
        cls = OCRTruncadoError if truncado else OCRError
        raise cls(f"OCR falló tras {intentos} intentos: {ultimo}", filename)

    def extract(self, content: bytes, filename: str | None = None) -> list[OCRResult]:
        """Proceso 2: devuelve un OCRResult por cada documento encontrado en el archivo.
        Tope de tokens por página (PDF de varias páginas -> tope x páginas, con techo)."""
        data = self.extract_json(
            content, SYSTEM_PROMPT, USER_PROMPT, schema=DOCUMENTOS_SCHEMA,
            schema_name="documentos", max_tokens=settings.ocr_max_tokens_documentos,
            max_tokens_por_pagina=True, timeout=settings.openai_timeout_documentos_s,
            filename=filename, tipo="documento",
        )
        return [
            OCRResult(
                tipo=d.get("tipo") or "otro",
                raw_text=d.get("raw_text") or "",
                fields=normalize_fields(d.get("fields") or {}),
            )
            for d in (data.get("documentos") or [])
        ]


def _telemetria(tipo, model, filename, resp, intento, finish, t0, error=None) -> None:
    registrar_uso(tipo=tipo, modelo=model, archivo=filename, resp=resp, intento=intento,
                  finish_reason=finish, segundos=time.monotonic() - t0, error=error)


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
