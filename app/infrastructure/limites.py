"""Límites globales de concurrencia hacia proveedores externos.

Singletons a nivel de módulo: todos los hilos de todos los lotes comparten el mismo cupo.
Úsese `with OPENAI_SEM:` alrededor de cada llamada a OpenAI y `with JEV_SEM:` alrededor de
cada llamada a Jev (TypeSafe). Nunca se crean semáforos por lote o por función.
"""

from __future__ import annotations

import os
import threading


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


OPENAI_MAX_CONCURRENCY = _int_env("OPENAI_MAX_CONCURRENCY", 12)
JEV_MAX_CONCURRENCY = _int_env("JEV_MAX_CONCURRENCY", 8)

OPENAI_SEM = threading.BoundedSemaphore(OPENAI_MAX_CONCURRENCY)
JEV_SEM = threading.BoundedSemaphore(JEV_MAX_CONCURRENCY)

__all__ = ["OPENAI_SEM", "JEV_SEM", "OPENAI_MAX_CONCURRENCY", "JEV_MAX_CONCURRENCY"]
