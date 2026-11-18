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
import random
import threading
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

YCLOUD_API_BASE_URL = "https://api.ycloud.com"
CABECERA_FIRMA = "YCloud-Signature"
VENTANA_REPLAY_SEGUNDOS = 300
CODIGO_RATE_LIMIT_PAR = "131056"  # (Business Account, Consumer Account) pair rate limit hit

__all__ = [
    "YCloudClient",
    "YCloudSendError",
    "YCloudRateLimitError",
    "verificar_firma",
    "solo_digitos",
]


def _env_float(nombre: str, defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, "").strip() or defecto)
    except ValueError:
        return defecto


def _env_int(nombre: str, defecto: int) -> int:
    try:
        return int(os.environ.get(nombre, "").strip() or defecto)
    except ValueError:
        return defecto


class YCloudSendError(httpx.HTTPStatusError):
    """Fallo al enviar un mensaje por YCloud (distinto de un fallo de procesamiento)."""


class YCloudRateLimitError(YCloudSendError):
    """Meta 131056: se agotaron los reintentos por rate limit del par negocio-cliente."""


def _es_rate_limit_par(r: httpx.Response) -> bool:
    return r.status_code == 400 and CODIGO_RATE_LIMIT_PAR in (r.text or "")


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
        self._min_intervalo = _env_float("YCLOUD_MIN_INTERVAL_S", 1.5)
        self._max_intentos = max(1, _env_int("YCLOUD_RATE_LIMIT_MAX_ATTEMPTS", 6))
        self._backoff_base = _env_float("YCLOUD_RATE_LIMIT_BACKOFF_BASE_S", 2.0)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._ultimo_envio: dict[str, float] = {}

    def _lock_de(self, destino: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(destino, threading.Lock())

    def _esperar_turno(self, destino: str) -> None:
        ultimo = self._ultimo_envio.get(destino)
        if ultimo is not None:
            falta = self._min_intervalo - (time.monotonic() - ultimo)
            if falta > 0:
                time.sleep(falta)

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
        destino = str(cuerpo.get("to") or cuerpo.get("recipient") or "")
        with self._lock_de(destino):
            for intento in range(1, self._max_intentos + 1):
                self._esperar_turno(destino)
                r = self._http.post("/v2/whatsapp/messages/sendDirectly", json={"from": origen, **cuerpo})
                self._ultimo_envio[destino] = time.monotonic()
                if r.status_code < 400:
                    return
                if not _es_rate_limit_par(r):
                    logger.warning("YCloud rechazó el envío %s: %s", r.status_code, r.text[:500])
                    try:
                        r.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        raise YCloudSendError(str(e), request=e.request, response=e.response) from e
                    return
                if intento == self._max_intentos:
                    break
                espera = self._backoff_base * (2 ** (intento - 1)) + random.uniform(0, 1)
                logger.warning(
                    "YCloud rate limit 131056 para %s (intento %d/%d); reintento en %.1fs",
                    destino, intento, self._max_intentos, espera,
                )
                time.sleep(espera)
            logger.error("YCloud rate limit 131056 para %s: reintentos agotados", destino)
            raise YCloudRateLimitError(
                f"rate limit 131056 tras {self._max_intentos} intentos", request=r.request, response=r
            )

    def enviar_texto(self, destino: str, texto: str) -> None:
        self._enviar({**_destinatario(destino), "type": "text", "text": {"body": texto[:4000]}})

    def enviar_documento(self, destino: str, link: str, filename: str, caption: str = "") -> None:
        doc = {"link": link, "filename": filename}
        if caption:
            doc["caption"] = caption[:1000]
        self._enviar({**_destinatario(destino), "type": "document", "document": doc})
