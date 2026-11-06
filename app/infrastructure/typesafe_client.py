"""Fábrica del cliente TypeSafe / Jev (clasificación + validación)."""

from functools import lru_cache

from typesafe_sdk import TypeSafeClient

from app.core.config import settings
from app.core.ssl import enable_system_trust


@lru_cache
def get_typesafe_client() -> TypeSafeClient | None:
    """Devuelve el cliente de Jev, o None si no hay API key (fallback sin Jev)."""
    if not settings.typesafe_api_key:
        return None
    enable_system_trust()
    return TypeSafeClient(
        api_key=settings.typesafe_api_key,
        model=settings.typesafe_model,
        timeout=30.0,
    )
