"""Render de documentos a imágenes PNG en base64 para los modelos de visión."""

import base64

import pymupdf  # PyMuPDF (antes "fitz")

PDF_MAGIC = b"%PDF"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def is_pdf(data: bytes) -> bool:
    return data[:4] == PDF_MAGIC


def to_png_b64(data: bytes, dpi: int = 200, max_pages: int | None = None) -> list[str]:
    """Convierte bytes de un documento (PDF o imagen) a una lista de PNG en base64.

    - PDF: una imagen por página (renderizada a `dpi`), hasta `max_pages` (las liquidaciones
      Gramaluz traen 9 págs, pero solo la 1–2 tienen datos; el resto son fotos).
    - Imagen: se devuelve tal cual (ya es base64-codificable).
    """
    if is_pdf(data):
        out: list[str] = []
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            for i, page in enumerate(doc):
                if max_pages is not None and i >= max_pages:
                    break
                pix = page.get_pixmap(dpi=dpi)
                out.append(_b64(pix.tobytes("png")))
        finally:
            doc.close()
        return out
    # imagen (jpeg/png/...): el endpoint de visión acepta data URL directamente
    return [_b64(data)]


def pdf_text(data: bytes, max_pages: int = 2) -> str:
    """Texto embebido de un PDF (vacío si es imagen o un PDF escaneado sin capa de texto).

    Sirve para clasificar sin gastar OCR: los reportes Gramaluz y las remisiones generadas
    por software sí traen texto; una foto escaneada no.
    """
    if not is_pdf(data):
        return ""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\n".join(page.get_text() for i, page in enumerate(doc) if i < max_pages)
    finally:
        doc.close()
