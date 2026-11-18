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
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.infrastructure import storage
from app.infrastructure.files import UploadedDoc
from app.services.classifier_service import (
    RUTA_DOCUMENTOS,
    RUTA_PLANILLAS,
    Clasificacion,
    ClassifierService,
)

logger = logging.getLogger(__name__)

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


class InboundService:
    def __init__(self, classifier=None, pipeline=None, planillas=None,
                 lotes: "Lotes | None" = None, batch_window_s: float | None = None) -> None:
        self.classifier = classifier or ClassifierService()
        self._pipeline = pipeline
        self._planillas = planillas
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

    def encolar(self, ycloud, destino: str, media: dict, filename: str, caption: str = "") -> None:
        """Agrega la media al lote del remitente; se procesa al vencer la ventana. Nunca lanza."""
        try:
            self.lotes.agregar(ycloud, destino, Pendiente(media, filename, caption))
        except Exception:
            logger.exception("no se pudo encolar %s de %s", filename, destino)

    def procesar_y_responder(self, ycloud, destino: str, media: dict, filename: str, caption: str = "") -> None:
        """Compatibilidad: procesa un único archivo como un lote de uno. Nunca lanza."""
        self.procesar_lote(ycloud, destino, [Pendiente(media, filename, caption)])

    def procesar_lote(self, ycloud, destino: str, items: list[Pendiente]) -> None:
        """Descarga, clasifica, procesa por ruta y responde UN mensaje por ruta. Nunca lanza."""
        try:
            self._procesar_lote(ycloud, destino, items)
        except Exception:  # red de seguridad: nunca tumbar el hilo del timer
            logger.exception("falló el procesamiento del lote de %s (%d archivos)", destino, len(items))

    def _procesar_lote(self, ycloud, destino: str, items: list[Pendiente]) -> None:
        logger.info("procesando lote de %s: %d archivo(s)", destino, len(items))
        grupos: dict[str, list[UploadedDoc]] = {RUTA_PLANILLAS: [], RUTA_DOCUMENTOS: []}
        desconocidos: list[str] = []
        fallidos: list[str] = []
        for it in items:
            try:
                doc = UploadedDoc(filename=it.filename, content=ycloud.descargar(it.media))
                c = self._clasificar(doc, it.caption)
            except Exception:
                logger.exception("falló el procesamiento de %s de %s (descarga/clasificación)", it.filename, destino)
                fallidos.append(it.filename)
                continue
            if c.ruta in grupos:
                grupos[c.ruta].append(doc)
            else:
                desconocidos.append(doc.filename)

        envio_bloqueado = False
        for ruta, docs in grupos.items():
            if not docs:
                continue
            res, fallos = self._procesar_grupo(ruta, docs, destino)
            fallidos.extend(fallos)
            if res is None:
                continue
            if envio_bloqueado:
                logger.error("no se pudo entregar %s a %s; queda guardado en %s (envío bloqueado)",
                             _publico(res.nombre_excel), destino, res.nombre_excel)
                continue
            if not self._enviar_doc(ycloud, destino, res):
                envio_bloqueado = True

        if envio_bloqueado:
            if desconocidos or fallidos:
                logger.error("no se avisa a %s de no clasificados=%s fallidos=%s (envío bloqueado)",
                             destino, desconocidos, fallidos)
            return
        if desconocidos:
            texto = (MSG_NO_CLASIFICADO.format(archivo=desconocidos[0]) if len(desconocidos) == 1
                     else MSG_NO_CLASIFICADO_LOTE.format(archivos=", ".join(desconocidos)))
            if not self._enviar_texto(ycloud, destino, texto):
                return
        if fallidos:
            texto = (MSG_ERROR.format(archivo=fallidos[0]) if len(fallidos) == 1
                     else MSG_ERROR_LOTE.format(n=len(fallidos), archivos=", ".join(fallidos)))
            self._enviar_texto(ycloud, destino, texto)

    def _procesar_grupo(self, ruta: str, docs: list[UploadedDoc], destino: str) -> tuple[Resultado | None, list[str]]:
        """Procesa el grupo en una sola llamada; si falla entero, aísla los docs malos uno a uno."""
        try:
            return self._ejecutar(ruta, docs), []
        except Exception:
            logger.exception("falló el procesamiento del grupo %s de %s (%d docs); se reintenta uno a uno",
                             ruta, destino, len(docs))
        if len(docs) == 1:
            return None, [docs[0].filename]
        buenos, fallos = [], []
        for d in docs:
            try:
                self._ejecutar(ruta, [d])
                buenos.append(d)
            except Exception:
                logger.exception("falló el procesamiento de %s de %s", d.filename, destino)
                fallos.append(d.filename)
        if not buenos:
            return None, fallos
        try:
            res = self._ejecutar(ruta, buenos)
        except Exception:
            logger.exception("falló el procesamiento del grupo %s de %s tras excluir fallidos", ruta, destino)
            return None, [d.filename for d in docs]
        return res, fallos

    def _ejecutar(self, ruta: str, docs: list[UploadedDoc]) -> Resultado:
        n = len(docs)
        if ruta == RUTA_PLANILLAS:
            r = self.planillas.process(docs)
            nombre = storage.guardar(r.xlsx, "planillas_mano_de_obra.xlsx")
            resumen = (
                f"✅ {n} planilla(s) procesada(s) ({r.rows} filas). "
                f"Celdas a revisar (amarillo): {len(r.yellow_cells)}. Excepciones: {len(r.exceptions)}."
            )
            return Resultado(None, nombre, resumen)
        xlsx, summary = self.pipeline.process(docs)
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


def _publico(nombre: str) -> str:
    return nombre.split("__", 1)[-1]


def _env_float(nombre: str, defecto: float) -> float:
    try:
        return float(os.getenv(nombre, defecto))
    except ValueError:
        return defecto


class Lotes:
    """Buffer por remitente con debounce. Thread-safe (las BackgroundTasks síncronas corren en
    el threadpool de Starlette y los vaciados en hilos de `threading.Timer`).

    - `ventana`: segundos de silencio tras la última media para vaciar el lote (0 = inmediato).
    - `maximo`: al llegar a N archivos se vacía ya.
    - `espera_max`: tope desde la primera media aunque sigan llegando.
    """

    def __init__(self, flush: Callable, ventana: float | None = None, maximo: int | None = None,
                 espera_max: float | None = None) -> None:
        self._flush = flush
        self.ventana = _env_float("INBOUND_BATCH_WINDOW_S", 45.0) if ventana is None else ventana
        self.maximo = int(_env_float("INBOUND_BATCH_MAX", 50)) if maximo is None else maximo
        self.espera_max = _env_float("INBOUND_BATCH_MAX_WAIT_S", 300.0) if espera_max is None else espera_max
        self._lock = threading.Lock()
        self._lotes: dict[str, dict] = {}

    def agregar(self, ycloud, destino: str, item: Pendiente) -> None:
        listo = None
        with self._lock:
            lote = self._lotes.get(destino)
            if lote is None:
                lote = {"items": [], "inicio": time.monotonic(), "timer": None}
                self._lotes[destino] = lote
            lote["items"].append(item)
            lote["ycloud"] = ycloud
            if lote["timer"] is not None:
                lote["timer"].cancel()
                lote["timer"] = None
            restante = self.espera_max - (time.monotonic() - lote["inicio"])
            espera = min(self.ventana, max(restante, 0.0))
            if espera <= 0 or len(lote["items"]) >= self.maximo:
                listo = self._lotes.pop(destino)
            else:
                t = threading.Timer(espera, self._vencer, args=(destino, lote))
                t.daemon = True
                lote["timer"] = t
                t.start()
        if listo is not None:
            self._flush(listo["ycloud"], destino, listo["items"])

    def _vencer(self, destino: str, lote: dict) -> None:
        with self._lock:
            if self._lotes.get(destino) is not lote:
                return  # ya se vació por tamaño o por vaciar_todo
            self._lotes.pop(destino)
        self._flush(lote["ycloud"], destino, lote["items"])

    def pendientes(self, destino: str) -> int:
        with self._lock:
            lote = self._lotes.get(destino)
            return len(lote["items"]) if lote else 0

    def vaciar_todo(self) -> None:
        """Procesa ya todos los lotes pendientes (apagado / tests)."""
        with self._lock:
            pendientes = list(self._lotes.items())
            self._lotes.clear()
            for _, lote in pendientes:
                if lote["timer"] is not None:
                    lote["timer"].cancel()
        for destino, lote in pendientes:
            self._flush(lote["ycloud"], destino, lote["items"])
