"""Fábrica del cliente OpenAI (visión / OCR)."""

from functools import lru_cache

import httpx

from openai import OpenAI

from app.core.config import settings
from app.core.ssl import enable_system_trust


def httpx_timeout(plazo_s: float) -> httpx.Timeout:
    """Timeouts por fase estrictos: una conexión muda muere pronto aunque no llegue ningún
    chunk. El plazo TOTAL (reloj de pared) lo impone ocr_service al leer el stream."""
    return httpx.Timeout(connect=10.0, read=min(35.0, plazo_s), write=15.0, pool=10.0)


@lru_cache
def get_openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError(
            "Falta OPENAI_API_KEY. Copia .env.example a .env y pon tu API key."
        )
    enable_system_trust()
    # max_retries=0: los reintentos los gestiona ocr_service (con cambio de temperatura y
    # backoff propio); así los intentos no se multiplican con los del SDK.
    return OpenAI(
        api_key=settings.openai_api_key,
        timeout=httpx_timeout(settings.openai_timeout_s),
        max_retries=0,
    )
