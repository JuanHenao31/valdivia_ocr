"""Guarda los Excel generados por el webhook y firma enlaces de descarga con vencimiento.

WhatsApp necesita una URL pública para mandar el .xlsx como documento; el enlace va
firmado con HMAC (`DOWNLOAD_URL_SECRET`) para que no se puedan adivinar ni reutilizar
después de `DOWNLOAD_TTL_MINUTES`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import uuid
from pathlib import Path

from app.core.config import settings

_NOMBRE_SEGURO = re.compile(r"^[0-9a-f]{32}__[\w.\-]+\.xlsx$")


def _secreto() -> bytes:
    s = os.environ.get("DOWNLOAD_URL_SECRET", "").strip()
    if not s:
        raise RuntimeError("DOWNLOAD_URL_SECRET no está configurado")
    return s.encode()


def _firma(nombre: str, exp: int) -> str:
    return hmac.new(_secreto(), f"{nombre}.{exp}".encode(), hashlib.sha256).hexdigest()


def outputs_dir() -> Path:
    p = Path(settings.outputs_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def guardar(xlsx: bytes, filename: str) -> str:
    """Guarda el archivo y devuelve su nombre interno (`<uuid>__<filename>`)."""
    limpio = re.sub(r"[^\w.\-]", "_", filename)
    nombre = f"{uuid.uuid4().hex}__{limpio}"
    (outputs_dir() / nombre).write_bytes(xlsx)
    return nombre


def enlace(nombre: str) -> str:
    exp = int(time.time()) + settings.download_ttl_minutes * 60
    base = settings.public_base_url.rstrip("/")
    return f"{base}/descargas/{nombre}?exp={exp}&sig={_firma(nombre, exp)}"


def resolver(nombre: str, exp: int, sig: str) -> Path | None:
    """Ruta del archivo si la firma es válida y no venció; None en otro caso."""
    if not _NOMBRE_SEGURO.match(nombre) or exp < time.time():
        return None
    if not hmac.compare_digest(_firma(nombre, exp), sig):
        return None
    p = outputs_dir() / nombre
    return p if p.is_file() else None
