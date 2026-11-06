"""Agrega los routers de la API v1."""

from fastapi import APIRouter

from app.api.v1 import documents, planillas

api_router = APIRouter()
api_router.include_router(documents.router)
api_router.include_router(planillas.router)


@api_router.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
