"""Utilidades de lectura de archivos subidos."""

from dataclasses import dataclass


@dataclass
class UploadedDoc:
    filename: str
    content: bytes
