"""WhatsApp vía YCloud: verificación de firma del webhook, descarga de media y envío.

Adaptado de inmobiliaria (`app/infrastructure/messaging/ycloud_client.py`), en versión
síncrona: aquí todo corre en una tarea de fondo en threadpool (OpenAI + openpyxl también
son síncronos), así que no hace falta un cliente async.

Firma: cabecera `YCloud-Signature: t=<unix_ts>,s=<hmac_sha256_hex>` sobre `"<t>.<cuerpo>"`
con el secreto `whsec_` del endpoint (mismo formato que usa inmobiliaria en producción).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

YCLOUD_API_BASE_URL = "https://api.ycloud.com"
CABECERA_FIRMA = "YCloud-Signature"
VENTANA_REPLAY_SEGUNDOS = 300


def solo_digitos(s: str | None) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def verificar_firma(cabecera: str | None, cuerpo: bytes, secreto: str | None = None, ahora: float | None = None) -> bool:
    """True si la firma es válida y reciente. Fail-closed: sin secreto se rechaza."""
    secreto = (secreto if secreto is not None else os.environ.get("YCLOUD_WEBHOOK_SECRET", "")).strip()
    if not secreto or not cabecera:
        logger.warning("webhook YCloud sin secreto o sin cabecera de firma")
        return False
    ts: float | None = None
    firma: str | None = None
    for parte in cabecera.split(","):
        k, _, v = parte.strip().partition("=")
        if k == "t":
            try:
                ts = float(v)
            except ValueError:
                return False
        elif k == "s":
            firma = v.strip() or None
    if ts is None or firma is None:
        return False
    if abs((ahora or time.time()) - ts) > VENTANA_REPLAY_SEGUNDOS:
        logger.warning("webhook YCloud fuera de la ventana de replay")
        return False
    esperada = hmac.new(secreto.encode(), f"{int(ts)}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, firma)


def _destinatario(destino: str) -> dict[str, str]:
    """`to` para E.164; `recipient` para el BSUID (`CO.123...`) de usuarios con username."""
    return {"to": destino} if destino.lstrip("+").isdigit() else {"recipient": destino}


class YCloudClient:
    _CAMPOS_URL = ("link", "url", "fileUrl", "mediaUrl", "downloadUrl")
    _CAMPOS_ID = ("id", "mediaId", "media_id")

    def __init__(self, http: httpx.Client | None = None) -> None:
        if http is None:
            api_key = os.environ.get("YCLOUD_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("YCLOUD_API_KEY no está configurada")
            http = httpx.Client(
                base_url=YCLOUD_API_BASE_URL,
                timeout=settings.messaging_timeout_seconds,
                headers={"Accept": "application/json", "X-API-Key": api_key},
            )
        self._http = http

    def descargar(self, media: dict[str, Any]) -> bytes:
        url = next((media[c] for c in self._CAMPOS_URL if media.get(c)), None)
        media_id = next((media[c] for c in self._CAMPOS_ID if media.get(c)), None)
        if not url and not media_id:
            raise ValueError("media de YCloud sin URL ni id")
        r = self._http.get(str(url) if url else f"/v2/whatsapp/media/{media_id}")
        r.raise_for_status()
        return r.content

    def _enviar(self, cuerpo: dict[str, Any]) -> None:
        origen = settings.ycloud_whatsapp_from.strip()
        if not origen:
            raise RuntimeError("YCLOUD_WHATSAPP_FROM no está configurado")
        r = self._http.post("/v2/whatsapp/messages/sendDirectly", json={"from": origen, **cuerpo})
        if r.status_code >= 400:
            logger.warning("YCloud rechazó el envío %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()

    def enviar_texto(self, destino: str, texto: str) -> None:
        self._enviar({**_destinatario(destino), "type": "text", "text": {"body": texto[:4000]}})

    def enviar_documento(self, destino: str, link: str, filename: str, caption: str = "") -> None:
        doc = {"link": link, "filename": filename}
        if caption:
            doc["caption"] = caption[:1000]
        self._enviar({**_destinatario(destino), "type": "document", "document": doc})
