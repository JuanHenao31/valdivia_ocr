"""Orquesta los documentos que llegan por WhatsApp: clasifica -> enruta -> procesa -> responde.

- Proceso 1 (planillas)  -> PlanillaService  -> Excel 1 "Mano de obra"
- Proceso 2 (documentos) -> DocumentPipeline -> Excel 2 plantilla Valdivia
- No concluyente         -> se le pide al remitente que aclare (caption "planilla"/"remisión").

Lotes por remitente: cada media entrante se encola por `destino` y el lote se procesa tras una
ventana de silencio (INBOUND_BATCH_WINDOW_S, se reinicia con cada media), con tope de tamaño
(INBOUND_BATCH_MAX) y de espera (INBOUND_BATCH_MAX_WAIT_S). Así 120 fotos en 3 minutos producen
un Excel por ruta y pocos mensajes, en vez de 120 envíos que Meta bloquea (131056).

Errores de procesamiento -> un solo aviso por lote. Errores de envío -> solo log (el Excel queda
guardado en disco); no se intenta otro mensaje a ese número.

Entrega al-menos-una-vez: los msg_ids se marcan procesados solo cuando el lote termina. Si el
proceso muere a mitad de envío, el lote se repite al arrancar y el remitente puede recibir un
Excel duplicado. Un lote que tumba el proceso una y otra vez se aparta tras PENDING_MAX_INTENTOS
recuperaciones (pending/_fallidos) y se avisa una vez al remitente.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from app.infrastructure import storage
from app.infrastructure.files import UploadedDoc
from app.infrastructure.pendientes import Pendientes, nuevo_lote_id, sanear
from app.services.classifier_service import (
    RUTA_DOCUMENTOS,
    RUTA_PLANILLAS,
    Clasificacion,
    ClassifierService,
)

try:  # telemetría opcional (otro módulo); sin ella los logs siguen llevando el lote_id
    from app.infrastructure.telemetria import lote_actual as _lote_actual
except Exception:  # pragma: no cover - depende de si el módulo existe
    _lote_actual = None

logger = logging.getLogger(__name__)

MSG_FALLIDO_DEFINITIVO = (
    "No pude procesar {archivos} tras varios intentos. Por favor reenvíalos o envíalos por otro medio."
)
MSG_NO_CLASIFICADO = (
    "No pude identificar qué documento es {archivo}. Reenvíalo con un texto que diga "
    "*planilla* (control diario de trabajadores) o *remisión* / *liquidación*."
)
MSG_NO_CLASIFICADO_LOTE = (
    "No pude identificar qué documento es: {archivos}. Reenvíalos con un texto que diga "
    "*planilla* (control diario de trabajadores) o *remisión* / *liquidación*."
)
MSG_ERROR = "Tuve un problema procesando {archivo}. Ya quedó registrado; inténtalo de nuevo en unos minutos."
MSG_ERROR_LOTE = (
    "Tuve un problema procesando {n} archivo(s): {archivos}. Ya quedó registrado; "
    "reenvíalos en unos minutos."
)
MSG_AYUDA = (
    "Hola 👋 Envíame la foto o el PDF de una *planilla* de control diario, una *remisión* "
    "o una *liquidación* y te devuelvo el Excel listo."
)


@dataclass
class Resultado:
    clasificacion: Clasificacion | None
    nombre_excel: str | None = None
    resumen: str = ""
    detalles: dict = field(default_factory=dict)


@dataclass
class Pendiente:
    media: dict
    filename: str
    caption: str = ""
    msg_id: str = ""
    contenido: bytes | None = None  # solo si NO se pudo persistir (si no, se lee de `ruta`)
    ruta: str | None = None  # .bin persistido; los bytes se leen justo antes de clasificar


class InboundService:
    def __init__(self, classifier=None, pipeline=None, planillas=None,
                 lotes: "Lotes | None" = None, batch_window_s: float | None = None,
                 pendientes: Pendientes | None = None) -> None:
        self.classifier = classifier or ClassifierService()
        self._pipeline = pipeline
        self._planillas = planillas
        self._pendientes = pendientes
        self._lotes = lotes
        if lotes is None and batch_window_s is not None:
            self._lotes = Lotes(self.procesar_lote, ventana=batch_window_s)

    @property
    def pipeline(self):
        if self._pipeline is None:
            from app.api.deps import get_pipeline

            self._pipeline = get_pipeline()
        return self._pipeline

    @property
    def planillas(self):
        if self._planillas is None:
            from app.api.deps import get_planilla_service

            self._planillas = get_planilla_service()
        return self._planillas

    @property
    def lotes(self) -> "Lotes":
        if self._lotes is None:
            self._lotes = Lotes(self.procesar_lote)
        return self._lotes

    @property
    def pendientes(self) -> Pendientes:
        if self._pendientes is None:
            from app.core.config import settings

            self._pendientes = Pendientes(settings.pending_path, settings.processed_ids_max)
        return self._pendientes

    def procesar(self, doc: UploadedDoc, caption: str = "") -> Resultado:
        """Un documento suelto (sin responder). Se mantiene para uso directo/HTTP."""
        c = self._clasificar(doc, caption)
        if c.ruta in (RUTA_PLANILLAS, RUTA_DOCUMENTOS):
            res = self._ejecutar(c.ruta, [doc])
            res.clasificacion = c
            return res
        return Resultado(c, None, MSG_NO_CLASIFICADO.format(archivo=doc.filename))

    def _clasificar(self, doc: UploadedDoc, caption: str) -> Clasificacion:
        c = self.classifier.classify(doc, caption)
        logger.info(
            "clasificado %s -> %s (%s, %s, conf=%.2f, evidencia=%r)",
            doc.filename, c.ruta, c.tipo.value, c.metodo, c.confianza, c.evidencia,
        )
        return c

    # --- entrada por WhatsApp -----------------------------------------------

    def encolar(self, ycloud, destino: str, media: dict, filename: str, caption: str = "",
                msg_id: str = "", mime: str = "") -> None:
        """Descarga YA la media (las URLs caducan), persiste los bytes en disco (si hay msg_id) y
        la agrega al lote del remitente; se procesa al vencer la ventana. Nunca lanza.

        El sidecar normalmente ya lo escribió el webhook antes del 200; si no existe se escribe
        aquí. Una vez persistido, el ítem en memoria guarda solo la ruta, no los bytes."""
        contenido = None
        try:
            contenido = ycloud.descargar(media)
        except Exception:
            logger.exception("no se pudo descargar %s de %s al llegar; se reintenta al procesar", filename, destino)
        ruta = None
        if msg_id:
            try:
                if contenido is not None:
                    ruta = str(self.pendientes.guardar_bin(destino, msg_id, contenido))
                if not self.pendientes.tiene_meta(destino, msg_id):
                    self.pendientes.guardar_meta(destino, msg_id, metadatos(media, filename, caption, mime))
                if ruta is not None:
                    contenido = None  # ya está en disco: no retener los bytes en RAM
            except Exception:
                logger.exception("no se pudo persistir %s (%s) de %s; sigue solo en memoria", filename, msg_id, destino)
        try:
            self.lotes.agregar(ycloud, destino, Pendiente(media, filename, caption, msg_id, contenido, ruta))
        except Exception:
            logger.exception("no se pudo encolar %s de %s", filename, destino)

    def recuperar(self, ycloud, espera: float | None = None, max_intentos: int | None = None) -> int:
        """Arranque: reconstruye los lotes persistidos en disco y los programa para vaciarse en
        `espera` s (PENDING_RECOVERY_DELAY_S). No carga bytes en RAM (solo rutas).

        Cada recuperación sube `intentos` en el sidecar (atómico). Un ítem que supera
        `max_intentos` (PENDING_MAX_INTENTOS) se considera veneno: se aparta a `_fallidos/`, se
        marca procesado y se avisa UNA vez al remitente. Devuelve cuántos se reencolaron."""
        from app.core.config import settings

        espera = settings.pending_recovery_delay_s if espera is None else espera
        max_intentos = settings.pending_max_intentos if max_intentos is None else max_intentos
        total = 0
        for destino, metas in self.pendientes.cargar().items():
            veneno: list[dict] = []
            vivos: list[dict] = []
            for m in metas:
                try:
                    n = self.pendientes.incrementar_intentos(m)
                except Exception:
                    logger.exception("no se pudo actualizar intentos de %s", m.get("msg_id"))
                    n = int(m.get("intentos") or 0) + 1
                (veneno if n > max_intentos else vivos).append(m)
            if veneno:
                self._apartar_veneno(ycloud, destino, veneno)
            if vivos:
                logger.info("recuperando lote pendiente de %s: %d archivo(s)", destino, len(vivos))
            for m in vivos:
                it = Pendiente(m.get("media") or {}, m.get("filename") or f"whatsapp_{m.get('msg_id')}",
                               m.get("caption") or "", m.get("msg_id") or "", None, m.get("ruta_bin"))
                self.lotes.agregar(ycloud, destino, it, ventana=espera)
                total += 1
        return total

    def _apartar_veneno(self, ycloud, destino: str, metas: list[dict]) -> None:
        nombres = [m.get("filename") or str(m.get("msg_id")) for m in metas]
        try:
            dst = self.pendientes.mover_a_fallidos(destino, [m["msg_id"] for m in metas])
        except Exception:
            logger.exception("no se pudo apartar a _fallidos los archivos de %s: %s", destino, nombres)
            return
        logger.error("lote de %s descartado tras agotar intentos (%d archivo(s): %s); apartado en %s",
                     destino, len(metas), nombres, dst)
        # Un único aviso, por el envío normal (con ritmo por destino), fuera del hilo de arranque.
        texto = MSG_FALLIDO_DEFINITIVO.format(archivos=", ".join(nombres))
        self.lotes.tarea(lambda: self._enviar_texto(ycloud, destino, texto), nombre="aviso-fallidos")

    def procesar_y_responder(self, ycloud, destino: str, media: dict, filename: str, caption: str = "") -> None:
        """Compatibilidad: procesa un único archivo como un lote de uno. Nunca lanza."""
        self.procesar_lote(ycloud, destino, [Pendiente(media, filename, caption)])

    def procesar_lote(self, ycloud, destino: str, items: list[Pendiente]) -> None:
        """Descarga, clasifica, procesa por ruta y responde UN mensaje por ruta. Nunca lanza.

        Solo si el lote termina (Excel escrito y mensajes enviados, o fallo de envío registrado)
        se marcan sus msg_ids como procesados y se borra su media de disco. Si revienta de forma
        inesperada la media queda en disco y se reintenta en el próximo arranque."""
        lote_id = nuevo_lote_id(sanear(destino))
        token = _lote_actual.set(lote_id) if _lote_actual is not None else None
        try:
            self._procesar_lote(ycloud, destino, items, lote_id)
        except Exception:  # red de seguridad: nunca tumbar el hilo del timer
            logger.exception("falló el procesamiento del lote %s de %s (%d archivos)", lote_id, destino, len(items))
            return
        finally:
            if token is not None:
                _lote_actual.reset(token)
        ids = [it.msg_id for it in items if it.msg_id]
        if ids:
            try:
                self.pendientes.completar(destino, ids)
            except Exception:
                logger.exception("no se pudo limpiar la media pendiente de %s", destino)

    def _procesar_lote(self, ycloud, destino: str, items: list[Pendiente], lote_id: str = "-") -> None:
        logger.info("procesando lote %s de %s: %d archivo(s)", lote_id, destino, len(items))
        grupos: dict[str, list[UploadedDoc]] = {RUTA_PLANILLAS: [], RUTA_DOCUMENTOS: []}
        desconocidos: list[str] = []
        fallidos: list[str] = []
        # Descarga + clasificación (I/O: red, OCR, LLM) en paralelo; el orden se preserva.
        for it, (doc, c) in zip(items, _mapear(lambda it: self._descargar_clasificar(ycloud, destino, it), items)):
            if doc is None:
                fallidos.append(it.filename)
            elif c.ruta in grupos:
                grupos[c.ruta].append(doc)
            else:
                desconocidos.append(doc.filename)

        # Se procesan todas las rutas antes de enviar para que el primer mensaje lleve el resumen.
        resultados: list[Resultado] = []
        ok = {RUTA_PLANILLAS: 0, RUTA_DOCUMENTOS: 0}
        for ruta, docs in grupos.items():
            if not docs:
                continue
            res, fallos = self._procesar_grupo(ruta, docs, destino)
            fallidos.extend(fallos)
            if res is not None:
                ok[ruta] = len(docs) - len(fallos)
                resultados.append(res)
        resumen = resumen_lote(len(items), ok[RUTA_PLANILLAS], ok[RUTA_DOCUMENTOS],
                               len(desconocidos), len(fallidos))
        logger.info("lote %s de %s: %s", lote_id, destino, resumen)

        envio_bloqueado = False
        for res in resultados:
            if envio_bloqueado:
                logger.error("no se pudo entregar %s a %s; queda guardado en %s (envío bloqueado)",
                             _publico(res.nombre_excel), destino, res.nombre_excel)
                continue
            if resumen:  # el resumen viaja en el caption del primer envío (sin envío extra)
                res = Resultado(res.clasificacion, res.nombre_excel, f"{resumen}\n{res.resumen}", res.detalles)
                resumen = ""
            if not self._enviar_doc(ycloud, destino, res):
                envio_bloqueado = True

        if envio_bloqueado:
            if desconocidos or fallidos:
                logger.error("no se avisa a %s de no clasificados=%s fallidos=%s (envío bloqueado)",
                             destino, desconocidos, fallidos)
            return
        textos = []
        if desconocidos:
            textos.append(MSG_NO_CLASIFICADO.format(archivo=desconocidos[0]) if len(desconocidos) == 1
                          else MSG_NO_CLASIFICADO_LOTE.format(archivos=", ".join(desconocidos)))
        if fallidos:
            textos.append(MSG_ERROR.format(archivo=fallidos[0]) if len(fallidos) == 1
                          else MSG_ERROR_LOTE.format(n=len(fallidos), archivos=", ".join(fallidos)))
        for texto in textos:
            if resumen:
                texto, resumen = f"{resumen}\n{texto}", ""
            if not self._enviar_texto(ycloud, destino, texto):
                return

    def _descargar_clasificar(self, ycloud, destino: str, it: Pendiente):
        """(doc, clasificación) o (None, None) si falla; nunca lanza (aísla el fallo del ítem)."""
        try:
            contenido = it.contenido
            if contenido is None and it.ruta and os.path.isfile(it.ruta):
                with open(it.ruta, "rb") as f:  # lectura perezosa: solo mientras se procesa
                    contenido = f.read()
            if contenido is None:  # sidecar sin .bin (descarga pendiente o fallida al llegar)
                contenido = ycloud.descargar(it.media)
            doc = UploadedDoc(filename=it.filename, content=contenido)
            return doc, self._clasificar(doc, it.caption)
        except Exception:
            logger.exception("falló el procesamiento de %s de %s (descarga/clasificación)", it.filename, destino)
            return None, None

    def _procesar_grupo(self, ruta: str, docs: list[UploadedDoc], destino: str) -> tuple[Resultado | None, list[str]]:
        """Analiza cada doc del grupo UNA sola vez (en paralelo, dentro del servicio) y arma el
        Excel con los que salieron bien; los fallidos se reportan. Sin reintentos ni pools anidados."""
        servicio = self.planillas if ruta == RUTA_PLANILLAS else self.pipeline
        analizar = getattr(servicio, "analizar", None)
        if analizar is None:  # servicio sin analizar/armar (envoltorios/fakes): todo-o-nada, 1 llamada
            try:
                return self._resultado(ruta, len(docs), servicio.process(docs)), []
            except Exception:
                logger.exception("falló el procesamiento del grupo %s de %s (%d docs)", ruta, destino, len(docs))
                return None, [d.filename for d in docs]
        try:
            results = analizar(docs)
        except Exception:  # fallo inesperado del propio servicio (no de una foto)
            logger.exception("falló el análisis del grupo %s de %s (%d docs)", ruta, destino, len(docs))
            return None, [d.filename for d in docs]
        for r in results:
            if not r.ok:
                logger.error("falló %s de %s (%s: %s)", r.filename, destino, r.exc_type, r.error)
        fallos = [r.filename for r in results if not r.ok]
        buenos = [r for r in results if r.ok]
        if not buenos:
            return None, fallos
        try:
            return self._resultado(ruta, len(buenos), servicio.armar(buenos)), fallos
        except Exception:
            logger.exception("falló el armado del Excel %s de %s", ruta, destino)
            return None, [d.filename for d in docs]

    def _ejecutar(self, ruta: str, docs: list[UploadedDoc]) -> Resultado:
        """Procesa `docs` todo-o-nada (uso directo: `procesar`)."""
        servicio = self.planillas if ruta == RUTA_PLANILLAS else self.pipeline
        return self._resultado(ruta, len(docs), servicio.process(docs))

    @staticmethod
    def _resultado(ruta: str, n: int, salida) -> Resultado:
        """Guarda el Excel producido por process/armar y arma el resumen."""
        if ruta == RUTA_PLANILLAS:
            r = salida
            nombre = storage.guardar(r.xlsx, "planillas_mano_de_obra.xlsx")
            resumen = (
                f"✅ {n} planilla(s) procesada(s) ({r.rows} filas). "
                f"Celdas a revisar (amarillo): {len(r.yellow_cells)}. Excepciones: {len(r.exceptions)}."
            )
            return Resultado(None, nombre, resumen)
        xlsx, summary = salida
        nombre = storage.guardar(xlsx, "remisiones_liquidaciones.xlsx")
        tipos = ", ".join(sorted({d.doc_type.value for d in summary.documents})) or "documentos"
        resumen = (
            f"✅ {n} documento(s) procesado(s) ({tipos}). "
            f"Celdas a revisar (amarillo): {len(summary.yellow_cells)}. "
            f"Excepciones: {len(summary.exceptions)}."
        )
        return Resultado(None, nombre, resumen)

    @staticmethod
    def _enviar_doc(ycloud, destino: str, res: Resultado) -> bool:
        publico = _publico(res.nombre_excel)
        try:
            ycloud.enviar_documento(destino, storage.enlace(res.nombre_excel), publico, res.resumen)
            return True
        except Exception:
            logger.exception("no se pudo entregar %s a %s; queda guardado en %s", publico, destino, res.nombre_excel)
            return False

    @staticmethod
    def _enviar_texto(ycloud, destino: str, texto: str) -> bool:
        try:
            ycloud.enviar_texto(destino, texto)
            return True
        except Exception:
            logger.exception("no se pudo entregar el aviso a %s: %r", destino, texto[:80])
            return False


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def resumen_lote(total: int, planillas: int, documentos: int, desconocidos: int, fallidos: int) -> str:
    """'📦 Procesadas 84 fotos: 69 planillas, 15 documentos, 1 no identificada, 2 con error.'"""
    partes = []
    if planillas:
        partes.append(_plural(planillas, "planilla", "planillas"))
    if documentos:
        partes.append(_plural(documentos, "documento", "documentos"))
    if desconocidos:
        partes.append(_plural(desconocidos, "no identificada", "no identificadas"))
    if fallidos:
        partes.append(f"{fallidos} con error")
    verbo = "Procesada" if total == 1 else "Procesadas"
    return f"📦 {verbo} {_plural(total, 'foto', 'fotos')}: {', '.join(partes)}."


def metadatos(media: dict, filename: str, caption: str = "", mime: str = "") -> dict:
    """Sidecar de un ítem pendiente (lo comparten el webhook y `encolar`)."""
    return {"filename": filename, "caption": caption, "mime": mime,
            "received_at": time.time(), "media": media, "intentos": 0}


def _publico(nombre: str) -> str:
    return nombre.split("__", 1)[-1]


def _env_float(nombre: str, defecto: float) -> float:
    try:
        return float(os.getenv(nombre, defecto))
    except ValueError:
        return defecto


def _workers() -> int:
    return max(1, int(_env_float("INBOUND_PROCESS_WORKERS", 8)))


def _mapear(fn: Callable, items: list) -> list:
    """`fn` sobre cada ítem con un pool acotado (INBOUND_PROCESS_WORKERS); resultados en orden.
    `fn` debe aislar sus propias excepciones."""
    n = min(_workers(), len(items))
    if n <= 1:
        return [fn(x) for x in items]
    ctx = contextvars.copy_context()  # propaga el lote_id de telemetría a los hilos del pool
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="inbound") as ex:
        return list(ex.map(lambda x: ctx.copy().run(fn, x), items))


class Lotes:
    """Buffer por remitente con debounce. Thread-safe (las BackgroundTasks síncronas corren en
    el threadpool de Starlette y los vaciados en hilos de `threading.Timer`).

    - `ventana`: segundos de silencio tras la última media para vaciar el lote (0 = inmediato).
    - `maximo`: al llegar a N archivos se vacía ya.
    - `espera_max`: tope desde la primera media aunque sigan llegando.

    El cierre de un lote (`cerrado=True` + pop) ocurre siempre bajo `_lock`, tanto en el timer
    como en el tope de tamaño. Cada `agregar` sube `gen`; un timer viejo que dispare mientras otro
    `agregar` añadía (cancel() no detiene un Timer ya en marcha) ve otra generación y no vacía.
    Los vaciados inmediatos corren en un hilo daemon propio, nunca en el hilo de la petición.
    """

    def __init__(self, flush: Callable, ventana: float | None = None, maximo: int | None = None,
                 espera_max: float | None = None) -> None:
        self._flush = flush
        self.ventana = _env_float("INBOUND_BATCH_WINDOW_S", 45.0) if ventana is None else ventana
        self.maximo = int(_env_float("INBOUND_BATCH_MAX", 200)) if maximo is None else maximo
        self.espera_max = _env_float("INBOUND_BATCH_MAX_WAIT_S", 300.0) if espera_max is None else espera_max
        self._lock = threading.Lock()
        self._lotes: dict[str, dict] = {}
        self._hilos: set[threading.Thread] = set()
        self._apagado = False

    def agregar(self, ycloud, destino: str, item: Pendiente, ventana: float | None = None) -> None:
        """`ventana` opcional acorta la espera de este lote (recuperación al arrancar)."""
        with self._lock:
            if self._apagado:  # apagando: no se arrancan vaciados; lo persistido sigue en disco
                logger.warning("apagando: %s de %s queda en disco para la recuperación", item.filename, destino)
                return
            lote = self._lotes.get(destino)
            if lote is None:
                lote = {"items": [], "inicio": time.monotonic(), "timer": None, "gen": 0, "cerrado": False}
                self._lotes[destino] = lote
            lote["items"].append(item)
            lote["ycloud"] = ycloud
            lote["gen"] += 1
            if lote["timer"] is not None:
                lote["timer"].cancel()
                lote["timer"] = None
            restante = self.espera_max - (time.monotonic() - lote["inicio"])
            base = self.ventana if ventana is None else min(self.ventana, ventana)
            espera = min(base, max(restante, 0.0))
            if espera <= 0 or len(lote["items"]) >= self.maximo:
                self._cerrar(destino, lote)
                self._lanzar(destino, lote)
            else:
                t = threading.Timer(espera, self._vencer, args=(destino, lote, lote["gen"]))
                t.daemon = True
                lote["timer"] = t
                t.start()

    def _cerrar(self, destino: str, lote: dict) -> None:
        """Requiere `_lock`."""
        lote["cerrado"] = True
        if self._lotes.get(destino) is lote:
            self._lotes.pop(destino)

    def _lanzar(self, destino: str, lote: dict) -> None:
        """Requiere `_lock`. Vacía en un hilo daemon propio (no el de la petición)."""
        t = threading.Thread(target=self._correr, args=(destino, lote), daemon=True, name="lote-flush")
        self._hilos.add(t)
        t.start()

    def _correr(self, destino: str, lote: dict) -> None:
        try:
            self._flush(lote["ycloud"], destino, lote["items"])
        finally:
            with self._lock:
                self._hilos.discard(threading.current_thread())

    def tarea(self, fn: Callable[[], object], nombre: str = "lote-tarea") -> None:
        """Corre `fn` en un hilo daemon registrado (esperar()/apagar() lo esperan). Nunca lanza."""
        def correr():
            try:
                fn()
            except Exception:
                logger.exception("falló la tarea %s", nombre)
            finally:
                with self._lock:
                    self._hilos.discard(threading.current_thread())

        with self._lock:
            if self._apagado:
                return
            t = threading.Thread(target=correr, daemon=True, name=nombre)
            self._hilos.add(t)
            t.start()

    def _vencer(self, destino: str, lote: dict, gen: int) -> None:
        with self._lock:
            if self._apagado or lote["cerrado"] or lote["gen"] != gen or self._lotes.get(destino) is not lote:
                return  # apagando, timer viejo, o ya se vació por tamaño / vaciar_todo
            self._cerrar(destino, lote)
            # El hilo del Timer queda registrado como vaciado en curso: esperar()/apagar() lo unen.
            self._hilos.add(threading.current_thread())
        self._correr(destino, lote)

    def apagar(self, timeout: float = 8.0) -> bool:
        """Apagado ordenado: no acepta ni arranca vaciados nuevos (cancela timers; esos ítems
        siguen en disco y se recuperan al arrancar) y espera hasta `timeout` s a los que están en
        curso. Devuelve True si todos terminaron a tiempo."""
        with self._lock:
            self._apagado = True
            for lote in self._lotes.values():
                if lote["timer"] is not None:
                    lote["timer"].cancel()
            self._lotes.clear()
        self.esperar(timeout)
        with self._lock:
            quedan = len(self._hilos)
        if quedan:
            logger.warning("apagado: %d vaciado(s) siguen en curso; se reanudan desde disco al arrancar", quedan)
        return quedan == 0

    def pendientes(self, destino: str) -> int:
        with self._lock:
            lote = self._lotes.get(destino)
            return len(lote["items"]) if lote else 0

    def esperar(self, timeout: float = 10.0) -> None:
        """Espera a que terminen los vaciados en hilo propio (tests / diagnóstico)."""
        limite = time.monotonic() + timeout
        while True:
            with self._lock:
                hilos = list(self._hilos)
            if not hilos or time.monotonic() > limite:
                return
            for h in hilos:
                h.join(max(0.0, limite - time.monotonic()))

    def vaciar_todo(self) -> None:
        """Procesa ya, en este hilo, todos los lotes pendientes (tests / diagnóstico)."""
        with self._lock:
            pendientes = list(self._lotes.items())
            for destino, lote in pendientes:
                if lote["timer"] is not None:
                    lote["timer"].cancel()
                self._cerrar(destino, lote)
        for destino, lote in pendientes:
            self._flush(lote["ycloud"], destino, lote["items"])
