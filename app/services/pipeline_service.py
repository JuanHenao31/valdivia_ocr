"""Proceso 2 — orquestación: OCR -> validación Jev -> cruce y llenado del Excel 2."""

from app.core.config import settings
from app.domain.models import (
    DocumentResult,
    DocumentType,
    ExtractedField,
    OCRResult,
    ProcessResult,
    SourcedDoc,
)
from app.infrastructure.files import UploadedDoc
from app.services.excel_service import ExcelService
from app.services.ocr_service import (
    BASCULA_FIELDS,
    LIQUIDACION_FIELDS,
    REMISION_FIELDS,
    OCRService,
)
from app.services.validation_service import JevValidationService, ValidationResult

# Campos que no se marcan como "sin valor" (pueden faltar legítimamente).
OPTIONAL_FIELDS = {"bas_placa", "bas_cliente", "liq_fecha"}


def clamp_conf(raw) -> int:
    try:
        return max(0, min(100, int(round(float(raw)))))
    except (TypeError, ValueError):
        return 0


def build_fields(
    ocr_fields: dict, validation: ValidationResult, field_names: list[str]
) -> list[ExtractedField]:
    """Une valor + confianza OCR + soporte Jev y decide si el campo requiere revisión."""
    fields: list[ExtractedField] = []
    for name in field_names:
        f = ocr_fields.get(name) or {}
        value = f.get("valor")
        ocr_conf = clamp_conf(f.get("confianza"))
        jev_support = validation.supports.get(name)

        reasons = []
        if value in (None, "", []) and name not in OPTIONAL_FIELDS:
            reasons.append("sin valor")
        if value not in (None, "", []) and ocr_conf < settings.ocr_conf_threshold:
            reasons.append(f"OCR {ocr_conf}<{settings.ocr_conf_threshold}")
        if jev_support is not None and jev_support < settings.jev_support_threshold:
            reasons.append(f"Jev {jev_support:.2f}<{settings.jev_support_threshold}")

        fields.append(
            ExtractedField(
                name=name,
                value=value,
                ocr_confidence=ocr_conf,
                jev_support=jev_support,
                needs_review=bool(reasons),
                reason="; ".join(reasons) or None,
            )
        )
    return fields


class DocumentPipeline:
    _FIELDS_BY_TYPE = {
        DocumentType.REMISION: REMISION_FIELDS,
        DocumentType.LIQUIDACION_PLANTA: LIQUIDACION_FIELDS,
        DocumentType.LIQUIDACION_BASCULA: BASCULA_FIELDS,
    }

    def __init__(self) -> None:
        self.ocr = OCRService()
        self.jev = JevValidationService()
        self.excel = ExcelService()

    @staticmethod
    def _doc_type(ocr: OCRResult, validation: ValidationResult) -> tuple[DocumentType, float]:
        """El OCR clasifica (ve la imagen); Jev confirma. Si discrepan, confianza 0."""
        try:
            ocr_type = DocumentType(ocr.tipo)
        except ValueError:
            ocr_type = DocumentType.OTRO
        if not validation.enabled:
            return ocr_type, 0.0
        if validation.doc_type == ocr_type:
            return ocr_type, validation.doc_type_confidence
        return ocr_type, 0.0

    def process(self, docs: list[UploadedDoc]) -> tuple[bytes, ProcessResult]:
        result = ProcessResult(jev_enabled=settings.jev_enabled)
        buckets: dict[DocumentType, list[tuple[SourcedDoc, DocumentResult]]] = {
            t: [] for t in self._FIELDS_BY_TYPE
        }

        for doc in docs:
            for k, ocr in enumerate(self.ocr.extract(doc.content)):
                validation = self.jev.validate(ocr)
                doc_type, conf = self._doc_type(ocr, validation)
                names = self._FIELDS_BY_TYPE.get(doc_type, [])
                fields = build_fields(ocr.fields, validation, names)
                name = doc.filename if k == 0 else f"{doc.filename} (doc {k + 1})"
                dr = DocumentResult(filename=name, doc_type=doc_type, doc_type_confidence=conf, fields=fields)
                result.documents.append(dr)
                if doc_type in buckets:
                    buckets[doc_type].append((SourcedDoc(filename=name, fields=fields), dr))

        rem = buckets[DocumentType.REMISION]
        liq = buckets[DocumentType.LIQUIDACION_PLANTA]
        bas = buckets[DocumentType.LIQUIDACION_BASCULA]
        fill = self.excel.fill([s for s, _ in rem], [s for s, _ in liq], [s for s, _ in bas])

        for items, pos in ((rem, fill.remision_pos), (liq, fill.liquidacion_pos), (bas, fill.bascula_pos)):
            for i, (_, dr) in enumerate(items):
                if i in pos:
                    dr.written_sheet, dr.written_row = pos[i]

        result.yellow_cells = fill.yellow_cells
        result.exceptions = fill.exceptions
        return fill.xlsx, result
