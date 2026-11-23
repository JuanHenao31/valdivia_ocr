"""Persistencia en disco de la media entrante por WhatsApp hasta que su lote quede respondido.

Estructura (en el volumen de salidas, sobrevive a reinicios del contenedor):

    <pending_dir>/<destino_saneado>/<msg_id_saneado>.json  sidecar: filename, caption, destino, mime,
                                                            received_at, msg_id, media, intentos
    <pending_dir>/<destino_saneado>/<msg_id_saneado>.bin   bytes (se escriben en segundo plano)
    <pending_dir>/procesados.log                            msg_ids ya respondidos (uno por línea)
    <pending_dir>/_fallidos/<lote_key>/                     ítems que agotaron PENDING_MAX_INTENTOS
    <pending_dir>/_corruptos/<destino>/                     sidecars ilegibles (revisión manual)

Toda escritura es atómica (`.tmp` + fsync + `os.replace` + fsync del directorio en POSIX). El
sidecar se escribe ANTES de responder 200 al webhook (sin bytes); el `.bin` llega después desde
la BackgroundTask. Sidecar sin `.bin` => se descarga al vaciar. `.bin` sin sidecar => huérfano,
se borra al cargar. `_fallidos/` y `_corruptos/` nunca se recuperan.

Entrega al-menos-una-vez: un ítem solo se marca procesado cuando su lote terminó; si el proceso
muere a mitad del envío, al reiniciar el lote se repite y el remitente puede recibir un Excel
duplicado. Se prefiere duplicar a perder documentos.

Los archivos se borran por msg_id (no el directorio entero) para no pisar un lote nuevo del mismo
remitente que llegue mientras el anterior se procesa.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_PROCESADOS = "procesados.log"
DIR_FALLIDOS = "_fallidos"


def nuevo_lote_id(prefijo: str | None = None) -> str:
    """Id único y ordenable cronológicamente: `[prefijo_]YYYYmmdd_HHMMSS_<8 hex>`. El sufijo
    uuid4 evita choques dentro del mismo segundo. Solo es una etiqueta (logs, telemetría,
    carpetas de _fallidos): la recuperación y la deduplicación van por msg_id, no por él."""
    base = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    return f"{prefijo}_{base}" if prefijo else base
DIR_CORRUPTOS = "_corruptos"
RESERVADOS = frozenset({DIR_FALLIDOS, DIR_CORRUPTOS})  # nunca se recuperan


def sanear(valor: str) -> str:
    limpio = re.sub(r"[^\w.\-]", "_", valor or "").strip(".")
    return limpio or "_"


def fsync_dir(d: Path) -> None:
    """Persiste la entrada de directorio tras un rename (POSIX). En Windows no aplica."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logger.warning("no se pudo hacer fsync del directorio %s", d)


def escribir_atomico(ruta: Path, datos: bytes) -> None:
    """Escribe en `ruta.tmp`, fsync, `os.replace` y fsync del directorio. Si falla, borra el `.tmp`."""
    tmp = ruta.with_name(ruta.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(datos)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    fsync_dir(ruta.parent)


class Pendientes:
    def __init__(self, base: str | Path, max_procesados: int = 20000) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self._max = max_procesados
        self._lock = threading.Lock()
        self._procesados: OrderedDict[str, None] = OrderedDict()
        self._lineas_log = 0
        self._cargar_procesados()

    # --- log de procesados ---------------------------------------------------

    @property
    def _log(self) -> Path:
        return self.base / LOG_PROCESADOS

    def _cargar_procesados(self) -> None:
        try:
            lineas = self._log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for l in lineas[-self._max:]:
            if l.strip():
                self._procesados[l.strip()] = None
        self._lineas_log = len(lineas)

    def _marcar_procesados(self, msg_ids: list[str]) -> None:
        ids = [m for m in msg_ids if m]
        if not ids:
            return
        with self._lock:
            for m in ids:
                self._procesados[m] = None
                self._procesados.move_to_end(m)
            while len(self._procesados) > self._max:
                self._procesados.popitem(last=False)
            with open(self._log, "a", encoding="utf-8") as f:
                f.write("".join(m + "\n" for m in ids))
                f.flush()
                os.fsync(f.fileno())
            self._lineas_log += len(ids)
            if self._lineas_log > 2 * self._max:  # rotación: se reescribe con los últimos N
                try:
                    escribir_atomico(self._log, "".join(m + "\n" for m in self._procesados).encode("utf-8"))
                    self._lineas_log = len(self._procesados)
                except OSError:  # p. ej. PermissionError en Windows si otro proceso tiene el log abierto
                    logger.exception("no se pudo rotar %s; se reintenta en la próxima escritura", self._log)

    def procesado(self, msg_id: str) -> bool:
        with self._lock:
            return msg_id in self._procesados

    # --- media pendiente -----------------------------------------------------

    def _dir(self, destino: str) -> Path:
        return self.base / sanear(destino)

    def ruta_bin(self, destino: str, msg_id: str) -> Path:
        return self._dir(destino) / f"{sanear(msg_id)}.bin"

    def _ruta_meta(self, destino: str, msg_id: str) -> Path:
        return self._dir(destino) / f"{sanear(msg_id)}.json"

    def pendiente(self, msg_id: str) -> bool:
        clave = sanear(msg_id)
        return any(p for p in self.base.glob(f"*/{glob_escape(clave)}.json") if p.parent.name not in RESERVADOS)

    def conocido(self, msg_id: str) -> bool:
        """True si ya está en disco esperando su lote o si su lote ya se respondió."""
        return bool(msg_id) and (self.procesado(msg_id) or self.pendiente(msg_id))

    def tiene_meta(self, destino: str, msg_id: str) -> bool:
        return self._ruta_meta(destino, msg_id).is_file()

    def guardar_meta(self, destino: str, msg_id: str, meta: dict) -> None:
        """Sidecar atómico, sin bytes. El webhook lo llama antes de responder 200."""
        self._dir(destino).mkdir(parents=True, exist_ok=True)
        sidecar = {**meta, "msg_id": msg_id, "destino": destino}
        sidecar.setdefault("received_at", time.time())
        sidecar.setdefault("intentos", 0)
        escribir_atomico(self._ruta_meta(destino, msg_id), json.dumps(sidecar, ensure_ascii=False).encode("utf-8"))

    def guardar_bin(self, destino: str, msg_id: str, contenido: bytes) -> Path:
        self._dir(destino).mkdir(parents=True, exist_ok=True)
        ruta = self.ruta_bin(destino, msg_id)
        escribir_atomico(ruta, contenido)
        return ruta

    def guardar(self, destino: str, msg_id: str, contenido: bytes | None, meta: dict) -> None:
        if contenido is not None:
            self.guardar_bin(destino, msg_id, contenido)
        self.guardar_meta(destino, msg_id, meta)

    def incrementar_intentos(self, meta: dict) -> int:
        """Sube y persiste (atómico) el contador `intentos` del sidecar; devuelve el nuevo valor."""
        nuevo = {k: v for k, v in meta.items() if k != "ruta_bin"}
        nuevo["intentos"] = int(nuevo.get("intentos") or 0) + 1
        escribir_atomico(self._ruta_meta(nuevo["destino"], nuevo["msg_id"]),
                         json.dumps(nuevo, ensure_ascii=False).encode("utf-8"))
        meta["intentos"] = nuevo["intentos"]
        return nuevo["intentos"]

    def mover_a_fallidos(self, destino: str, msg_ids: list[str]) -> Path:
        """Aparta los archivos a `_fallidos/<lote_key>/` y marca los ids como procesados."""
        lote_key = nuevo_lote_id(sanear(destino))
        dst = self.base / DIR_FALLIDOS / lote_key
        dst.mkdir(parents=True, exist_ok=True)
        d = self._dir(destino)
        for m in msg_ids:
            for ext in (".bin", ".json"):  # el sidecar al final
                src = d / f"{sanear(m)}{ext}"
                if src.exists():
                    shutil.move(str(src), str(dst / src.name))
        self._marcar_procesados(msg_ids)
        self._rmdir_si_vacio(d)
        return dst

    def cargar(self) -> dict[str, list[dict]]:
        """{destino: [sidecar + 'ruta_bin' (str|None)]} ordenado por llegada. NO lee los bytes.

        Limpieza: borra `.tmp` y `.bin` huérfanos (sin sidecar), aparta sidecars ilegibles a
        `_corruptos/`, borra ítems ya procesados y directorios vacíos. Ignora `_fallidos`/`_corruptos`."""
        lotes: dict[str, list[dict]] = {}
        for d in sorted(p for p in self.base.iterdir() if p.is_dir() and p.name not in RESERVADOS):
            for tmp in d.glob("*.tmp"):
                tmp.unlink(missing_ok=True)
            for js in d.glob("*.json"):
                try:
                    meta = json.loads(js.read_text(encoding="utf-8"))
                    if not isinstance(meta, dict):
                        raise ValueError("el sidecar no es un objeto JSON")
                except (OSError, ValueError):
                    logger.exception("sidecar pendiente ilegible, se aparta a %s: %s", DIR_CORRUPTOS, js)
                    self._apartar_corrupto(d, js)
                    continue
                meta.setdefault("msg_id", js.stem)
                meta.setdefault("destino", d.name)
                if self.procesado(meta["msg_id"]):
                    self._borrar_item(d, js.stem)
                    continue
                binario = js.with_suffix(".bin")
                meta["ruta_bin"] = str(binario) if binario.is_file() else None
                lotes.setdefault(meta["destino"], []).append(meta)
            for b in d.glob("*.bin"):
                if not b.with_suffix(".json").exists():
                    logger.warning("media huérfana sin sidecar, se borra: %s", b)
                    b.unlink(missing_ok=True)
            self._rmdir_si_vacio(d)
        for items in lotes.values():
            items.sort(key=lambda m: m.get("received_at", 0))
        return lotes

    def _apartar_corrupto(self, d: Path, js: Path) -> None:
        dst = self.base / DIR_CORRUPTOS / d.name
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for src in (js, js.with_suffix(".bin")):
                if src.exists():
                    os.replace(src, dst / src.name)
        except OSError:
            logger.exception("no se pudo apartar el sidecar corrupto %s", js)

    def completar(self, destino: str, msg_ids: list[str]) -> None:
        """Lote respondido (o fallo definitivo registrado): log de procesados y luego borrado."""
        ids = [m for m in msg_ids if m]
        if not ids:
            return
        self._marcar_procesados(ids)
        d = self._dir(destino)
        for m in ids:
            self._borrar_item(d, sanear(m))
        self._rmdir_si_vacio(d)

    @staticmethod
    def _borrar_item(d: Path, clave: str) -> None:
        for ext in (".json", ".bin"):  # primero el sidecar: sin él, el .bin no se recupera
            (d / f"{clave}{ext}").unlink(missing_ok=True)

    @staticmethod
    def _rmdir_si_vacio(d: Path) -> None:
        try:
            d.rmdir()
        except OSError:
            pass


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)
