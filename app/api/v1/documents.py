"""Endpoint de carga de documentos -> Excel llenado."""

import json
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.api.deps import get_pipeline
from app.infrastructure.files import UploadedDoc
from app.services.pipeline_service import DocumentPipeline

router = APIRouter(tags=["documents"])

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/documents/process")
async def process_documents(
    files: Annotated[list[UploadFile], File(description="PDF y/o imágenes a procesar")],
    pipeline: Annotated[DocumentPipeline, Depends(get_pipeline)],
):
    """Recibe uno o varios documentos (PDF/imagen), extrae y valida la info, y devuelve
    la plantilla de liquidación con la remisión ya diligenciada (descarga .xlsx)."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes adjuntar al menos un archivo.",
        )

    docs = [UploadedDoc(filename=f.filename or "sin_nombre", content=await f.read()) for f in files]

    # El pipeline es síncrono (OpenAI + Jev + openpyxl) -> lo corremos en threadpool.
    xlsx_bytes, summary = await run_in_threadpool(pipeline.process, docs)

    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": 'attachment; filename="remisiones_liquidaciones.xlsx"',
            "X-Process-Summary": json.dumps(summary.model_dump(), ensure_ascii=True, default=str),
        },
    )
