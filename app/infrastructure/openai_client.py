"""Fábrica del cliente OpenAI (visión / OCR)."""

from functools import lru_cache

from openai import OpenAI

from app.core.config import settings
from app.core.ssl import enable_system_trust


@lru_cache
def get_openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError(
            "Falta OPENAI_API_KEY. Copia .env.example a .env y pon tu API key."
        )
    enable_system_trust()
    return OpenAI(api_key=settings.openai_api_key)
