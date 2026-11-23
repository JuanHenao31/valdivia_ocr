"""Corre el flujo de lote de WhatsApp en local, SIN WhatsApp (YCloud/Meta falsos).

Sirve para separar fallos: si aquí funciona y en producción no, el problema es Meta/YCloud;
si aquí falla, es nuestra lógica. OCR/Jev/LLM son los reales (cuestan dinero).

    python -m scripts.probar_lote_local [carpeta] [--limit N] [--workers N]
                                        [--solo-clasificar] [--out DIR] [--repeticiones N]

Salida (default output/prueba_local/<timestamp>/): los Excel generados, reporte.md y reporte.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

EXTENSIONES = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
DESTINO = "+570000000000"


# --- dobles ------------------------------------------------------------------

class FakeYCloud:
    """Reemplaza al cliente YCloud: descarga = leer archivo local; envíos = se registran."""

    def __init__(self) -> None:
        self.envios: list[dict] = []
        self._lock = threading.Lock()

    def descargar(self, media: dict) -> bytes:
        return Path(media["path"]).read_bytes()

    def _registrar(self, d: dict) -> None:
        with self._lock:
            self.envios.append({"ts": datetime.now().isoformat(timespec="seconds"), **d})

    def enviar_documento(self, destino: str, url: str, nombre: str, caption: str = "") -> None:
        interno = url.split("/descargas/", 1)[-1].split("?", 1)[0]
        self._registrar({"tipo": "documento", "destino": destino, "nombre": nombre,
                         "caption": caption, "excel_interno": interno})

    def enviar_texto(self, destino: str, texto: str) -> None:
        self._registrar({"tipo": "texto", "destino": destino, "texto": texto})


class _ClasificadorMedido:
    def __init__(self, real, solo: bool) -> None:
        self.real, self.solo = real, solo
        self.filas: dict[str, dict] = {}
        self._lock = threading.Lock()

    def classify(self, doc, caption: str = ""):
        t0 = time.perf_counter()
        try:
            c = self.real.classify(doc, caption)
        except Exception as e:
            with self._lock:
                self.filas[doc.filename] = {"archivo": doc.filename, "error": repr(e),
                                            "traceback": traceback.format_exc(),
                                            "seg": round(time.perf_counter() - t0, 2)}
            raise
        fila = {"archivo": doc.filename, "ruta": c.ruta, "tipo": getattr(c.tipo, "value", str(c.tipo)),
                "metodo": c.metodo, "confianza": round(float(c.confianza), 3),
                "evidencia": str(getattr(c, "evidencia", ""))[:200],
                "seg": round(time.perf_counter() - t0, 2)}
        with self._lock:
            self.filas[doc.filename] = fila
        if self.solo:  # ruta inexistente: no se procesa ni se envía Excel
            from app.services.classifier_service import RUTA_DESCONOCIDA
            import dataclasses
            c = dataclasses.replace(c, ruta=RUTA_DESCONOCIDA + "_solo_clasificar")
        return c


class _ServicioMedido:
    """Envuelve PlanillaService/DocumentPipeline midiendo `analizar`, `armar` y `process`.

    Expone `analizar`/`armar` SOLO si el servicio real los tiene: InboundService decide con
    `getattr(servicio, "analizar", None)` si usa el camino por foto (producción) o el todo-o-nada.
    Cualquier otro atributo se delega con __getattr__ para no ocultar métodos futuros.
    """

    _MEDIDOS = ("analizar", "armar", "process")

    def __init__(self, nombre: str, real) -> None:
        self.nombre, self.real = nombre, real
        self.llamadas: list[dict] = []
        self.fotos: list[dict] = []
        self._lock = threading.Lock()

    def __getattr__(self, nombre: str):
        # solo se llama si el atributo no existe en la instancia/clase
        real = self.__dict__.get("real")
        if real is None:
            raise AttributeError(nombre)
        attr = getattr(real, nombre)  # AttributeError si el real no lo tiene (igual que en prod)
        if nombre in self._MEDIDOS and callable(attr):
            return lambda *a, **k: self._medir(nombre, attr, *a, **k)
        return attr

    def _medir(self, metodo: str, fn, docs, *a, **k):
        t0 = time.perf_counter()
        info = {"servicio": self.nombre, "metodo": metodo, "docs": len(docs)}
        try:
            res = fn(docs, *a, **k)
            if metodo == "analizar":
                filas = [{"servicio": self.nombre, "archivo": r.filename, "ok": bool(r.ok),
                          "exc_type": r.exc_type, "error": r.error} for r in res]
                info["fallidos"] = sum(1 for f in filas if not f["ok"])
                with self._lock:
                    self.fotos.extend(filas)
            return res
        except Exception as e:
            info["error"] = repr(e)
            raise
        finally:
            info["seg"] = round(time.perf_counter() - t0, 2)
            with self._lock:
                self.llamadas.append(info)


class _CapturaTelemetria(logging.Handler):
    """Junta las líneas JSON del logger "app.telemetria" (uso de OpenAI). Tolera su ausencia."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.eventos: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            d = json.loads(record.getMessage())
        except Exception:
            return
        if isinstance(d, dict):
            self.eventos.append(d)


def _tokens(ev: dict) -> tuple[int, int, int]:
    u = ev.get("usage") if isinstance(ev.get("usage"), dict) else ev

    def num(*claves):
        for c in claves:
            v = u.get(c)
            if isinstance(v, (int, float)):
                return int(v)
        return 0
    det = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    razon = num("reasoning_tokens") or int((det if isinstance(det, dict) else {}).get("reasoning_tokens") or 0)
    return num("prompt_tokens", "input_tokens"), num("completion_tokens", "output_tokens"), razon


def _precios() -> dict:
    """Precios USD por 1M tokens desde OPENAI_PRECIOS_JSON, p.ej. {"<modelo>": {"in": 1.0, "out": 4.0}}.
    TODO: el proyecto no configura precios; sin esa variable no se estima costo (no inventar)."""
    try:
        d = json.loads(os.getenv("OPENAI_PRECIOS_JSON", "") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def agregar_uso(eventos: list[dict]) -> dict:
    """Tokens prompt/completion/reasoning por tipo y total (+ costo si hay precios)."""
    precios = _precios()
    por_tipo: dict = {}
    total = {"llamadas": 0, "prompt": 0, "completion": 0, "reasoning": 0}
    costo, sin_precio = 0.0, set()
    for ev in eventos:
        p, c, r = _tokens(ev)
        if not (p or c or r):
            continue
        tipo = str(ev.get("tipo") or ev.get("evento") or ev.get("etapa") or "?")
        t = por_tipo.setdefault(tipo, {"llamadas": 0, "prompt": 0, "completion": 0, "reasoning": 0})
        for acc in (t, total):
            acc["llamadas"] += 1
            acc["prompt"] += p
            acc["completion"] += c
            acc["reasoning"] += r
        modelo = str(ev.get("model") or ev.get("modelo") or "")
        pr = precios.get(modelo)
        if isinstance(pr, dict):
            costo += (p * float(pr.get("in", 0)) + c * float(pr.get("out", 0))) / 1e6
        else:
            sin_precio.add(modelo or "?")
    return {"eventos": len(eventos), "por_tipo": por_tipo, "total": total,
            "costo_usd": round(costo, 4) if precios else None,
            "modelos_sin_precio": sorted(sin_precio) if precios else [],
            "nota_costo": None if precios else "TODO: definir OPENAI_PRECIOS_JSON para estimar costo"}


def metricas_planillas(xlsx: Path) -> dict:
    """Métricas para comparar corridas: hoja 'Mano de obra' + tipo 'Total no cuadra' en 'Excepciones'."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(xlsx, read_only=True)
    except Exception as e:  # Excel ilegible: se reporta, no tumba el harness
        return {"error": repr(e)}
    try:
        ws = wb["Mano de obra"] if "Mano de obra" in wb.sheetnames else wb.worksheets[0]
        filas = ws.iter_rows(values_only=True)
        cab = [str(c or "").strip() for c in next(filas, ())]
        ix = {n: cab.index(n) for n in ("Modalidad", "Jor", "Rend", "Und") if n in cab}
        m = {"filas": 0, "modalidad": {}, "jor_vacio": 0, "rend_vacio": 0, "und": {}, "total_no_cuadra": 0}

        def val(f, n):
            return f[ix[n]] if n in ix and ix[n] < len(f) else None
        for f in filas:
            if not any(v not in (None, "") for v in f):
                continue
            m["filas"] += 1
            mod = str(val(f, "Modalidad") or "(vacío)")
            m["modalidad"][mod] = m["modalidad"].get(mod, 0) + 1
            m["jor_vacio"] += val(f, "Jor") in (None, "")
            m["rend_vacio"] += val(f, "Rend") in (None, "")
            und = val(f, "Und")
            if und not in (None, ""):
                m["und"][str(und)] = m["und"].get(str(und), 0) + 1
        if "Excepciones" in wb.sheetnames:
            m["total_no_cuadra"] = sum(1 for f in wb["Excepciones"].iter_rows(min_row=2, values_only=True)
                                       if f and str(f[0] or "").strip() == "Total no cuadra")
        return m
    finally:
        wb.close()


class _CapturaErrores(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.registros: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        exc = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
        self.registros.append({"mensaje": record.getMessage(), "excepcion": exc})


# --- harness -----------------------------------------------------------------

def listar_archivos(carpeta: Path, limit: int | None) -> list[Path]:
    fs = sorted(p for p in carpeta.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONES)
    return fs[:limit] if limit else fs


def ejecutar(archivos: list[Path], out: Path, *, classifier=None, pipeline=None, planillas=None,
             solo_clasificar: bool = False, destino: str = DESTINO) -> dict:
    """Corre procesar_lote con un FakeYCloud y escribe el reporte en `out`. Devuelve el reporte."""
    os.environ.setdefault("DOWNLOAD_URL_SECRET", "prueba-local")
    from app.infrastructure import storage
    from app.services.inbound_service import InboundService, Pendiente

    if classifier is None:
        from app.services.classifier_service import ClassifierService
        classifier = ClassifierService()
    if pipeline is None:
        from app.api.deps import get_pipeline
        pipeline = get_pipeline()
    if planillas is None:
        from app.api.deps import get_planilla_service
        planillas = get_planilla_service()

    clasif = _ClasificadorMedido(classifier, solo_clasificar)
    pipe = _ServicioMedido("documentos", pipeline)
    plan = _ServicioMedido("planillas", planillas)
    svc = InboundService(classifier=clasif, pipeline=pipe, planillas=plan)
    yc = FakeYCloud()
    captura = _CapturaErrores()
    log_inbound = logging.getLogger("app.services.inbound_service")
    log_inbound.addHandler(captura)
    tele = _CapturaTelemetria()
    log_tele = logging.getLogger("app.telemetria")
    nivel_tele = log_tele.level
    log_tele.addHandler(tele)
    if log_tele.getEffectiveLevel() > logging.INFO:
        log_tele.setLevel(logging.INFO)

    items = [Pendiente({"path": str(p), "id": f"local-{i}"}, p.name, "") for i, p in enumerate(archivos)]
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        svc.procesar_lote(yc, destino, items)
    finally:
        log_inbound.removeHandler(captura)
        log_tele.removeHandler(tele)
        log_tele.setLevel(nivel_tele)
    total = round(time.perf_counter() - t0, 2)

    excels = []
    for e in yc.envios:
        if e["tipo"] == "documento":
            src = storage.outputs_dir() / e["excel_interno"]
            if src.is_file():
                shutil.copy2(src, out / e["nombre"])
                e["excel_copiado"] = str(out / e["nombre"])
                excels.append(str(out / e["nombre"]))

    clasificacion = [clasif.filas.get(p.name, {"archivo": p.name, "error": "no clasificado (falló la descarga)"})
                     for p in archivos]
    seg_clasif = [f["seg"] for f in clasificacion if "seg" in f]
    resumen_lote = ""
    if yc.envios:  # el resumen viaja como 1.a línea del primer envío (caption o texto)
        primero = yc.envios[0].get("caption") or yc.envios[0].get("texto") or ""
        if primero.startswith("\U0001f4e6"):
            resumen_lote = primero.split("\n", 1)[0]
    planilla_xlsx = next((x for x in excels if Path(x).name.startswith("planillas")), None)
    reporte = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "destino": destino,
        "archivos": len(archivos),
        "solo_clasificar": solo_clasificar,
        "workers": os.getenv("INBOUND_PROCESS_WORKERS", "8 (default)"),
        "tiempos": {
            "total_seg": total,
            "clasificacion_suma_seg": round(sum(seg_clasif), 2),
            "clasificacion_max_seg": max(seg_clasif, default=0),
            "procesamiento": pipe.llamadas + plan.llamadas,
        },
        "resumen_lote": resumen_lote,
        "analisis_por_foto": pipe.fotos + plan.fotos,
        "uso_openai": agregar_uso(tele.eventos),
        "metricas_planillas": metricas_planillas(Path(planilla_xlsx)) if planilla_xlsx else None,
        "resumen_rutas": _contar(f.get("ruta", "error") for f in clasificacion),
        "clasificacion": clasificacion,
        "fallos": captura.registros,
        "envios": yc.envios,
        "n_envios": len(yc.envios),
        "excels": excels,
    }
    (out / "reporte.json").write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "reporte.md").write_text(_markdown(reporte), encoding="utf-8")
    return reporte


def _contar(it) -> dict:
    d: dict = {}
    for x in it:
        d[x] = d.get(x, 0) + 1
    return d


def _markdown(r: dict) -> str:
    t = r["tiempos"]
    L = [f"# Prueba de lote local ({r['fecha']})", "",
         f"- Archivos: **{r['archivos']}** · destino falso `{r['destino']}` · workers {r['workers']}"
         + (" · **solo clasificar**" if r["solo_clasificar"] else ""),
         f"- Tiempo total: **{t['total_seg']} s** (clasificación: suma {t['clasificacion_suma_seg']} s, "
         f"máx {t['clasificacion_max_seg']} s)",
         f"- Mensajes de WhatsApp que se habrían enviado: **{r['n_envios']}**",
         f"- Rutas: {r['resumen_rutas']}",
         f"- Resumen del lote (tal cual se envía): {r['resumen_lote'] or '(ninguno)'}", "",
         "## Procesamiento", "", "| Servicio | Método | Docs | Fallidos | Seg | Error |",
         "|---|---|---|---|---|---|"]
    L += [f"| {p['servicio']} | {p.get('metodo', '')} | {p['docs']} | {p.get('fallidos', '')} | {p['seg']} "
          f"| {p.get('error', '')} |" for p in t["procesamiento"]]
    L += ["", "## Análisis por foto", "", "| Servicio | Archivo | OK | Error |", "|---|---|---|---|"]
    for f in r["analisis_por_foto"]:
        err = "" if f["ok"] else f"{f['exc_type']}: {f['error']}"
        L.append(f"| {f['servicio']} | {f['archivo']} | {'sí' if f['ok'] else 'NO'} | {err} |")
    u = r["uso_openai"]
    L += ["", f"## Uso OpenAI ({u['eventos']} eventos de app.telemetria)", ""]
    if u["total"]["llamadas"]:
        L += ["| Tipo | Llamadas | Prompt | Completion | Reasoning |", "|---|---|---|---|---|"]
        for k, v in sorted(u["por_tipo"].items()) + [("**TOTAL**", u["total"])]:
            L.append(f"| {k} | {v['llamadas']} | {v['prompt']} | {v['completion']} | {v['reasoning']} |")
        L += ["", f"Costo aprox.: {u['costo_usd']} USD" if u["costo_usd"] is not None else u["nota_costo"]]
    else:
        L.append("(sin datos de uso: app.telemetria no emitió líneas JSON con usage)")
    if r.get("metricas_planillas"):
        L += ["", "## Métricas de planillas", "", "```",
              json.dumps(r["metricas_planillas"], ensure_ascii=False), "```"]
    L += ["", "## Clasificación", "", "| Archivo | Ruta | Tipo | Método | Conf | Seg |", "|---|---|---|---|---|---|"]
    for f in r["clasificacion"]:
        if "error" in f:
            L.append(f"| {f['archivo']} | ERROR | {f['error']} | | | {f.get('seg', '')} |")
        else:
            L.append(f"| {f['archivo']} | {f['ruta']} | {f['tipo']} | {f['metodo']} | {f['confianza']} | {f['seg']} |")
    L += ["", "## Mensajes que se habrían enviado", ""]
    for e in r["envios"]:
        if e["tipo"] == "documento":
            L.append(f"- `{e['ts']}` 📎 **{e['nombre']}** — {e['caption']}")
        else:
            L.append(f"- `{e['ts']}` 💬 {e['texto']}")
    L += ["", f"## Fallos ({len(r['fallos'])})", ""]
    for f in r["fallos"]:
        L += [f"### {f['mensaje']}", "", "```", f["excepcion"].strip() or "(sin traceback)", "```", ""]
    return "\n".join(L) + "\n"


def ejecutar_repeticiones(archivos: list[Path], out: Path, n: int, **kw) -> dict:
    """Corre el mismo lote `n` veces (out/run_1..n) y escribe comparacion.md/json."""
    runs = []
    for i in range(1, n + 1):
        r = ejecutar(archivos, out / f"run_{i}", **kw)
        runs.append({"run": i, "total_seg": r["tiempos"]["total_seg"], "resumen_lote": r["resumen_lote"],
                     "fallos": len(r["fallos"]), "metricas": r["metricas_planillas"]})
    out.mkdir(parents=True, exist_ok=True)
    comp = {"repeticiones": n, "archivos": len(archivos), "runs": runs}
    (out / "comparacion.json").write_text(json.dumps(comp, ensure_ascii=False, indent=2), encoding="utf-8")
    L = [f"# Comparación de {n} corridas ({len(archivos)} archivos)", "",
         "| Run | Seg | Filas | Jornal | Contrato | Otras modalidades | Jor vacío | Rend vacío | Und distintas "
         "| Total no cuadra | Fallos |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in runs:
        m = x["metricas"]
        if not m or "error" in m:
            L.append(f"| {x['run']} | {x['total_seg']} | (sin planilla) | | | | | | | | {x['fallos']} |")
            continue
        mod = m["modalidad"]
        otras = {k: v for k, v in mod.items() if k not in ("Jornal", "Contrato")}
        und = ", ".join(f"{k}:{v}" for k, v in sorted(m["und"].items()))
        L.append(f"| {x['run']} | {x['total_seg']} | {m['filas']} | {mod.get('Jornal', 0)} | "
                 f"{mod.get('Contrato', 0)} | {otras or ''} | {m['jor_vacio']} | {m['rend_vacio']} | "
                 f"{len(m['und'])} ({und}) | {m['total_no_cuadra']} | {x['fallos']} |")
    L += ["", "## Resumen del lote por corrida", ""] + [f"- run {x['run']}: {x['resumen_lote']}" for x in runs]
    (out / "comparacion.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return comp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prueba el flujo de lote de WhatsApp sin WhatsApp.")
    ap.add_argument("carpeta", nargs="?", default="test_data")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None, help="sobrescribe INBOUND_PROCESS_WORKERS")
    ap.add_argument("--solo-clasificar", action="store_true", help="solo descarga+clasifica, sin Excel")
    ap.add_argument("--out", default=None)
    ap.add_argument("--repeticiones", type=int, default=1,
                    help="corre el mismo lote N veces y compara las planillas (comparacion.md)")
    a = ap.parse_args(argv)

    os.chdir(RAIZ)
    from dotenv import load_dotenv
    load_dotenv()
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.workers:
        os.environ["INBOUND_PROCESS_WORKERS"] = str(a.workers)

    archivos = listar_archivos(Path(a.carpeta), a.limit)
    if not archivos:
        print(f"No hay imágenes/PDF en {a.carpeta}", file=sys.stderr)
        return 1
    out = Path(a.out or f"output/prueba_local/{datetime.now():%Y%m%d_%H%M%S}")
    if a.repeticiones > 1:
        ejecutar_repeticiones(archivos, out, a.repeticiones, solo_clasificar=a.solo_clasificar)
        print(f"\n{a.repeticiones} corridas · Comparación: {out / 'comparacion.md'}")
        return 0
    r = ejecutar(archivos, out, solo_clasificar=a.solo_clasificar)
    print(f"\n{r['archivos']} archivos en {r['tiempos']['total_seg']} s · rutas {r['resumen_rutas']} · "
          f"{r['n_envios']} envío(s) · {len(r['fallos'])} fallo(s)\nReporte: {out / 'reporte.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
