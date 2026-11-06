"""Proceso 1 — endpoint de planillas de trabajadores -> Excel 1 (carga al CRM)."""

import json
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.api.deps import get_planilla_service
from app.infrastructure.files import UploadedDoc
from app.services.planilla_service import PlanillaService

router = APIRouter(tags=["planillas"])

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/planillas/process")
async def process_planillas(
    files: Annotated[list[UploadFile], File(description="Fotos/PDF de planillas 'Control Diario de Trabajo'")],
    service: Annotated[PlanillaService, Depends(get_planilla_service)],
):
    """Lee las planillas y devuelve el Excel 1 (hoja 'Mano de obra' + 'Excepciones')."""
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Debes adjuntar al menos un archivo.")

    docs = [UploadedDoc(filename=f.filename or "sin_nombre", content=await f.read()) for f in files]
    result = await run_in_threadpool(service.process, docs)
    summary = {
        "rows": result.rows,
        "yellow_cells": result.yellow_cells,
        "exceptions": [e.model_dump() for e in result.exceptions],
    }
    return StreamingResponse(
        BytesIO(result.xlsx),
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": 'attachment; filename="planillas_mano_de_obra.xlsx"',
            "X-Process-Summary": json.dumps(summary, ensure_ascii=True, default=str),
        },
    )
