"""Entrypoint FastAPI del servicio OCR -> Excel de liquidaciones."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from dotenv import load_dotenv

# Los secretos del canal (YCLOUD_*, DOWNLOAD_URL_SECRET) se leen de os.environ; en local
# vienen del .env. En Docker ya llegan por env_file y esto no los pisa.
load_dotenv()

from app.api.v1.router import api_router  # noqa: E402
from app.api.webhooks import router as webhooks_router  # noqa: E402
from app.core.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ocr_excel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fix SSL en redes con proxy/antivirus corporativo: usar el almacén de Windows.
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # pragma: no cover
        logger.warning("truststore no disponible; se usa la verificación TLS por defecto")
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # pragma: no cover
    logger.exception("Error no controlado: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


app.include_router(api_router, prefix="/api/v1")
app.include_router(webhooks_router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
