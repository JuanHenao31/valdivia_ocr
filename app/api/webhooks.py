"""Punto de entrada por WhatsApp: `POST /webhooks/ycloud` y descarga firmada del Excel.

Flujo: verificar firma sobre el cuerpo CRUDO -> ignorar todo lo que no sea un inbound del
número propio -> dedup por message-id (YCloud reintenta) -> responder 200 al instante ->
en segundo plano: descargar, clasificar, procesar y devolver el Excel por WhatsApp.

Nunca se responde 4xx a un evento legítimo (YCloud reintentaría en bucle); solo 403 si la
firma no cuadra.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections import OrderedDict
from functools import lru_cache
from threading import Lock
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.infrastructure import storage
from app.infrastructure.ycloud import CABECERA_FIRMA, YCloudClient, solo_digitos, verificar_firma
from app.services.inbound_service import MSG_AYUDA, InboundService, metadatos

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_OK = {"ok": True}
_EVENTO_INBOUND = "whatsapp.inbound_message.received"
_TIPOS_MEDIA = ("image", "document")
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Vistos:
    """Dedup en memoria (un solo worker). Acotado para no crecer sin límite."""

    def __init__(self, maximo: int = 5000) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._max = maximo
        self._lock = Lock()

    def ya_visto(self, clave: str) -> bool:
        with self._lock:
            if clave in self._d:
                return True
            self._d[clave] = None
            if len(self._d) > self._max:
                self._d.popitem(last=False)
            return False


vistos = _Vistos()


@lru_cache
def get_ycloud() -> YCloudClient:
    return YCloudClient()


@lru_cache
def get_inbound() -> InboundService:
    return InboundService()


def _conocido_en_disco(msg_id: str) -> bool:
    try:
        return get_inbound().pendientes.conocido(msg_id)
    except Exception:
        logger.exception("no se pudo consultar el dedup en disco para id=%s", msg_id)
        return False


def _remitente(m: dict[str, Any]) -> str | None:
    for k in ("from", "fromPhoneNumber", "customerPhoneNumber", "waId", "wa_id", "fromUserId"):
        if m.get(k):
            return m[k]
    return None


def _nombre_archivo(tipo: str, bloque: dict[str, Any], msg_id: str) -> str:
    nombre = bloque.get("filename") or bloque.get("fileName")
    if nombre:
        return str(nombre)
    mime = str(bloque.get("mimeType") or bloque.get("mime_type") or "").split(";")[0]
    ext = mimetypes.guess_extension(mime) or (".pdf" if tipo == "document" else ".jpg")
    return f"whatsapp_{msg_id}{ext}"


@router.post("/webhooks/ycloud")
async def ycloud_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    crudo = await request.body()
    if not verificar_firma(request.headers.get(CABECERA_FIRMA), crudo):
        raise HTTPException(status_code=403, detail="firma de webhook inválida o ausente")
    if not settings.ycloud_enabled:
        return _OK

    try:
        evento = json.loads(crudo)
    except ValueError:
        return _OK
    if not isinstance(evento, dict) or evento.get("type") != _EVENTO_INBOUND:
        return _OK  # status updates, echoes, etc.
    m = evento.get("whatsappInboundMessage")
    if not isinstance(m, dict):
        return _OK

    # Guard multi-número: la cuenta de YCloud puede compartir el webhook entre números.
    negocio = solo_digitos(settings.ycloud_whatsapp_from)
    destino_msg = solo_digitos(m.get("to"))
    if negocio and destino_msg and negocio != destino_msg:
        return _OK

    remitente = _remitente(m)
    msg_id = str(m.get("id") or "")
    tipo = m.get("type")
    if not remitente:
        return _OK
    # Dedup: memoria (este proceso) + disco (media pendiente o lote ya respondido) para que
    # un reintento de YCloud tras un reinicio no se procese dos veces.
    if msg_id and (vistos.ya_visto(msg_id) or _conocido_en_disco(msg_id)):
        logger.info("mensaje duplicado de YCloud, se ignora id=%s", msg_id)
        return _OK

    ycloud = get_ycloud()
    if tipo in _TIPOS_MEDIA:
        bloque = m.get(tipo) if isinstance(m.get(tipo), dict) else {}
        caption = str(bloque.get("caption") or "")
        filename = _nombre_archivo(tipo, bloque, msg_id or "sin_id")
        logger.info("media entrante destino=%s tipo=%s archivo=%s", remitente, tipo, filename)
        # Se encola por remitente; el lote se procesa tras la ventana de silencio.
        mime = str(bloque.get("mimeType") or bloque.get("mime_type") or "")
        # Persistir ANTES del 200: el sidecar (sin bytes) sobrevive a un kill entre el 200 y la
        # BackgroundTask; al arrancar, la recuperación descarga la media desde `media`. Los bytes
        # NO se bajan aquí: la descarga puede tardar varios segundos (PDF grandes, CDN lento) y
        # superar el timeout del webhook de YCloud, que reintentaría y duplicaría el envío.
        if msg_id:
            try:
                await run_in_threadpool(get_inbound().pendientes.guardar_meta, remitente, msg_id,
                                        metadatos(bloque, filename, caption, mime))
            except Exception:
                logger.exception("no se pudo persistir el sidecar de %s (%s); sigue solo en memoria", filename, msg_id)
        background.add_task(get_inbound().encolar, ycloud, remitente, bloque, filename, caption,
                            msg_id, mime)
    elif tipo == "text":
        background.add_task(ycloud.enviar_texto, remitente, MSG_AYUDA)
    return _OK


@router.get("/descargas/{nombre}")
def descargar(nombre: str, exp: int, sig: str):
    ruta = storage.resolver(nombre, exp, sig)
    if ruta is None:
        raise HTTPException(status_code=404, detail="enlace inválido o vencido")
    return FileResponse(ruta, media_type=XLSX_MEDIA, filename=nombre.split("__", 1)[1])
