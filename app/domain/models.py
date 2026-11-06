"""Modelos de dominio del pipeline OCR -> validación -> Excel."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    REMISION = "remision"
    LIQUIDACION_PLANTA = "liquidacion_planta"  # limón: informe Gramaluz
    LIQUIDACION_BASCULA = "liquidacion_bascula"  # plátano: tiquetes báscula + WhatsApp
    CONTROL_DIARIO = "control_diario"
    OTRO = "otro"


class ExtractedField(BaseModel):
    """Un campo extraído con sus dos señales de confianza."""

    name: str
    value: object | None = None
    ocr_confidence: int = 0  # 0-100, auto-reportada por el modelo de visión
    jev_support: float | None = None  # 0-1, confianza calibrada de Jev (None si no corrió)
    needs_review: bool = False
    reason: str | None = None  # por qué quedó marcado para revisión


class OCRResult(BaseModel):
    """Salida del OCR para UN documento: tipo sugerido, texto crudo y campos."""

    tipo: str = "otro"
    raw_text: str = ""
    fields: dict[str, dict] = Field(default_factory=dict)  # {name: {valor, confianza}}


class SourcedDoc(BaseModel):
    """Campos validados de un documento + archivo de origen (trazabilidad)."""

    filename: str
    fields: list[ExtractedField]

    def get(self, name: str) -> ExtractedField | None:
        return next((f for f in self.fields if f.name == name), None)

    def value(self, name: str):
        f = self.get(name)
        return f.value if f else None


class DocumentResult(BaseModel):
    """Resultado por documento procesado."""

    filename: str
    doc_type: DocumentType = DocumentType.OTRO
    doc_type_confidence: float = 0.0
    fields: list[ExtractedField] = Field(default_factory=list)
    written_sheet: str | None = None
    written_row: int | None = None  # fila del Excel donde se escribió (si aplica)


class Exception_(BaseModel):
    """Registro sin correspondencia / duplicado / con diferencias (hoja 'Control')."""

    tipo: str
    detalle: str
    archivo: str = ""
    hoja: str = ""
    fila: int | None = None


class ProcessResult(BaseModel):
    """Resumen global de un request de procesamiento."""

    documents: list[DocumentResult] = Field(default_factory=list)
    yellow_cells: list[str] = Field(default_factory=list)  # ej. ["Plantilla Limón!E4"]
    exceptions: list[Exception_] = Field(default_factory=list)
    jev_enabled: bool = False
