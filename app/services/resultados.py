"""Resultado tipado por foto/documento para el patrón analizar -> armar.

`analizar(docs)` corre el trabajo caro (OCR + Jev) de cada documento en paralelo y devuelve un
`PhotoResult` por documento, en el orden original, capturando el fallo de cada uno (nunca lanza
por un fallo individual). `armar(results)` ensambla el Excel solo con los `ok`.
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from app.infrastructure.files import UploadedDoc

logger = logging.getLogger(__name__)


@dataclass
class PhotoResult:
    ok: bool
    filename: str
    data: Any = None  # payload de análisis propio de cada servicio
    error: str | None = None
    exc_type: str | None = None
    exception: BaseException | None = field(default=None, repr=False, compare=False)


def analizar_en_paralelo(fn: Callable[[UploadedDoc], Any], docs: list[UploadedDoc],
                         workers: int, prefijo: str) -> list[PhotoResult]:
    """Aplica `fn` a cada doc con un pool acotado; orden preservado; fallos capturados por doc."""

    def _uno(doc: UploadedDoc) -> PhotoResult:
        try:
            return PhotoResult(ok=True, filename=doc.filename, data=fn(doc))
        except Exception as e:  # aislar el fallo de ESTA foto
            logger.exception("falló el análisis de %s", doc.filename)
            return PhotoResult(ok=False, filename=doc.filename, error=str(e) or repr(e),
                               exc_type=type(e).__name__, exception=e)

    n = max(1, min(int(workers or 1), len(docs)))
    if n == 1:
        return [_uno(d) for d in docs]
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=prefijo) as pool:
        # Cada tarea corre en una copia del contexto del llamador (p. ej. lote_id de telemetría).
        futuros = [pool.submit(contextvars.copy_context().run, _uno, d) for d in docs]
        return [f.result() for f in futuros]


def primer_fallo(results: list[PhotoResult]) -> None:
    """Re-lanza la excepción original del primer resultado fallido (en orden), si lo hay."""
    for r in results:
        if not r.ok:
            raise r.exception if r.exception is not None else RuntimeError(r.error)
