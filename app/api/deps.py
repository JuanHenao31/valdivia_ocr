"""Dependencias (DI) de la API."""

from functools import lru_cache

from app.services.pipeline_service import DocumentPipeline
from app.services.planilla_service import PlanillaService


@lru_cache
def get_pipeline() -> DocumentPipeline:
    return DocumentPipeline()


@lru_cache
def get_planilla_service() -> PlanillaService:
    return PlanillaService()
