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
    # Lotes de WhatsApp que quedaron a medias (reinicio/caída): se reconstruyen desde disco y se
    # vacían a los pocos segundos. No se vacía al apagar: lo pendiente sigue en disco.
    if settings.ycloud_enabled:
        try:
            from app.api.webhooks import get_inbound, get_ycloud

            n = get_inbound().recuperar(get_ycloud())
            if n:
                logger.info("recuperados %d archivo(s) pendientes de WhatsApp", n)
        except Exception:
            logger.exception("falló la recuperación de lotes pendientes de WhatsApp")
    yield
    # Apagado: no se arrancan vaciados nuevos y se espera (acotado, por debajo de los 10 s de
    # Docker) a los que están en curso. Lo no terminado sigue en disco y se recupera al arrancar
    # (entrega al-menos-una-vez: un lote cortado a mitad de envío puede repetirse).
    if settings.ycloud_enabled:
        try:
            from app.api.webhooks import get_inbound

            get_inbound().lotes.apagar(settings.shutdown_timeout_s)
        except Exception:
            logger.exception("falló el apagado ordenado de los lotes de WhatsApp")


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
