"""Telemetría de uso de OpenAI: una línea JSON por llamada en el logger "app.telemetria".

`lote_actual` es un ContextVar que el llamador (p. ej. inbound_service al procesar un lote)
fija con `with lote(lote_id): ...` o `lote_actual.set(lote_id)`; cada línea lo incluye.
Ojo: los ContextVar NO se heredan en hilos de ThreadPoolExecutor salvo que se use
contextvars.copy_context().run(...) al enviar la tarea.
"""

import contextvars
import json
import logging
from contextlib import contextmanager

logger = logging.getLogger("app.telemetria")

lote_actual: contextvars.ContextVar[str | None] = contextvars.ContextVar("lote_actual", default=None)


@contextmanager
def lote(lote_id: str | None):
    token = lote_actual.set(lote_id)
    try:
        yield
    finally:
        lote_actual.reset(token)


def registrar_uso(*, tipo: str, modelo: str, archivo: str | None, resp=None, intento: int,
                  finish_reason: str | None, segundos: float, error: str | None = None) -> dict:
    """Emite {"evento": "openai_uso", ...}. Nunca lanza (la telemetría no rompe el OCR)."""
    try:
        u = getattr(resp, "usage", None)
        det = getattr(u, "completion_tokens_details", None)
        evento = {
            "evento": "openai_uso",
            "tipo": tipo,
            "modelo": modelo,
            "archivo": archivo,
            "lote_id": lote_actual.get(),
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None),
            "reasoning_tokens": getattr(det, "reasoning_tokens", None),
            "intento": intento,
            "finish_reason": finish_reason,
            "segundos": round(segundos, 2),
        }
        if error:
            evento["error"] = error[:200]
        logger.info(json.dumps(evento, ensure_ascii=False))
        return evento
    except Exception:  # pragma: no cover
        return {}
