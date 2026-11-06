"""Capa de validación con Jev (TypeSafe): clasifica el documento y valida cada campo
extraído contra el texto OCR, devolviendo confianza calibrada.

Jev opera sobre TEXTO (no imágenes): valida consistencia campo <-> raw_text y
plausibilidad. No corrige una mala lectura del OCR, pero da confianza calibrada y
clasifica el tipo de documento para enrutar.
"""

from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul

from app.domain.models import DocumentType, OCRResult
from app.infrastructure.typesafe_client import get_typesafe_client

# Etiquetas de clasificación -> enum de dominio
_DOC_CRITERIA = {
    DocumentType.REMISION.value: "Remisión de finca (REMISION): remitente, placa, cantidad en kg, canastillas.",
    DocumentType.LIQUIDACION_PLANTA.value: "Liquidación/reporte de planta de limón: kg exportación/nacional/industria, precios por kg.",
    DocumentType.LIQUIDACION_BASCULA.value: "Tiquete de báscula y/o mensaje de WhatsApp con kg de plátano entregados a un cliente.",
    DocumentType.CONTROL_DIARIO.value: "Control Diario de Trabajo: lista de trabajadores, cédulas, labores, jornales.",
    DocumentType.OTRO.value: "Ninguno de los anteriores.",
}


@dataclass
class ValidationResult:
    doc_type: DocumentType = DocumentType.OTRO
    doc_type_confidence: float = 0.0
    # P(sí) calibrada de que cada campo esté respaldado por el texto del documento
    supports: dict[str, float] = field(default_factory=dict)
    enabled: bool = False


class JevValidationService:
    def classify_text(self, text: str) -> tuple[DocumentType, float] | None:
        """Clasifica un texto (encabezado transcrito, caption) con Jev. None si Jev no está."""
        client = get_typesafe_client()
        if client is None or not text.strip():
            return None
        resp = client.system_one(
            state={"raw_text": text},
            questions={
                "doc_type": Choice(
                    instructions="¿Qué tipo de documento es `raw_text`? Clasifícalo en una categoría.",
                    criteria=_DOC_CRITERIA,
                )
            },
        )
        ans = resp.answers["doc_type"]
        try:
            return DocumentType(ans.choice), float(ans.confidence)
        except ValueError:
            return DocumentType.OTRO, float(ans.confidence)

    def validate(self, ocr: OCRResult) -> ValidationResult:
        client = get_typesafe_client()
        if client is None:
            return ValidationResult(enabled=False)

        # Estado de texto que ve Jev: texto crudo + los valores extraídos.
        values = {name: (f or {}).get("valor") for name, f in ocr.fields.items()}
        state = {"raw_text": ocr.raw_text, "fields": values}

        questions: dict = {
            "doc_type": Choice(
                instructions=(
                    "¿Qué tipo de documento es `raw_text`? Clasifícalo en una categoría."
                ),
                criteria=_DOC_CRITERIA,
            )
        }
        # Una Noul por cada campo con valor no vacío.
        checkable = [n for n, v in values.items() if v not in (None, "")]
        for name in checkable:
            questions[f"supp_{name}"] = Noul(
                instructions=(
                    f"El valor extraído en `fields.{name}` está clara e inequívocamente "
                    f"respaldado por el texto `raw_text` (misma información, sin ambigüedad)."
                ),
            )

        resp = client.system_one(state=state, questions=questions)
        answers = resp.answers

        dt_answer = answers["doc_type"]
        try:
            doc_type = DocumentType(dt_answer.choice)
        except ValueError:
            doc_type = DocumentType.OTRO

        supports = {
            name: float(answers[f"supp_{name}"].noul) for name in checkable
        }
        return ValidationResult(
            doc_type=doc_type,
            doc_type_confidence=float(dt_answer.confidence),
            supports=supports,
            enabled=True,
        )
