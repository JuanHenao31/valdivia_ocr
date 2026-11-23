"""El harness de lote local corre sin red con clasificador/servicios falsos."""

import json
from types import SimpleNamespace

from app.core.config import settings
from app.domain.models import DocumentType
from app.services.classifier_service import RUTA_DESCONOCIDA, RUTA_DOCUMENTOS, RUTA_PLANILLAS, Clasificacion
from scripts.probar_lote_local import ejecutar, listar_archivos

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeClassifier:
    def classify(self, doc, caption=""):
        if "planilla" in doc.filename:
            return Clasificacion(next(iter(DocumentType)), RUTA_PLANILLAS, "reglas", 0.9)
        if "remision" in doc.filename:
            return Clasificacion(next(iter(DocumentType)), RUTA_DOCUMENTOS, "jev", 0.8)
        if "roto" in doc.filename:
            raise RuntimeError("OCR caído")
        return Clasificacion(next(iter(DocumentType)), RUTA_DESCONOCIDA, "ninguno", 0.0)


class FakePlanillas:
    def process(self, docs):
        return SimpleNamespace(xlsx=b"xlsx-planillas", rows=3, yellow_cells=[], exceptions=[])


class FakePipeline:
    def process(self, docs):
        return b"xlsx-docs", SimpleNamespace(documents=[], yellow_cells=[1], exceptions=[])


def test_harness_local(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path / "webhook"))
    datos = tmp_path / "datos"
    datos.mkdir()
    for n in ("planilla1.png", "remision1.jpeg", "otra.jpg", "roto.png", "ignorar.zip"):
        (datos / n).write_bytes(PNG)
    archivos = listar_archivos(datos, None)
    assert len(archivos) == 4

    out = tmp_path / "out"
    r = ejecutar(archivos, out, classifier=FakeClassifier(), pipeline=FakePipeline(), planillas=FakePlanillas())

    assert r["resumen_rutas"] == {RUTA_DESCONOCIDA: 1, RUTA_PLANILLAS: 1, RUTA_DOCUMENTOS: 1, "error": 1}
    tipos = [e["tipo"] for e in r["envios"]]
    assert tipos.count("documento") == 2 and tipos.count("texto") == 2
    assert r["n_envios"] == 4
    assert (out / "planillas_mano_de_obra.xlsx").read_bytes() == b"xlsx-planillas"
    assert (out / "remisiones_liquidaciones.xlsx").read_bytes() == b"xlsx-docs"
    assert any("OCR caído" in f["excepcion"] for f in r["fallos"])
    assert json.loads((out / "reporte.json").read_text(encoding="utf-8"))["n_envios"] == 4
    assert "Mensajes que se habrían enviado" in (out / "reporte.md").read_text(encoding="utf-8")


def test_solo_clasificar_no_genera_excel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path / "webhook"))
    (tmp_path / "planilla.png").write_bytes(PNG)
    r = ejecutar(listar_archivos(tmp_path, 1), tmp_path / "out", classifier=FakeClassifier(),
                 pipeline=FakePipeline(), planillas=FakePlanillas(), solo_clasificar=True)
    assert r["clasificacion"][0]["ruta"] == RUTA_PLANILLAS
    assert r["excels"] == [] and r["tiempos"]["procesamiento"] == []


# --- camino de producción: analizar -> armar (el envoltorio NO debe ocultarlo) ---------------

def _xlsx_planilla(modalidades, total_no_cuadra=0) -> bytes:
    import io

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mano de obra"
    ws.append(["Fecha labor", "Personal", "Modalidad", "Jor", "Rend", "Und", "Total"])
    for i, m in enumerate(modalidades):
        ws.append([None, f"p{i}", m, 1 if m == "Jornal" else None, None if m == "Jornal" else 5,
                   "Jor" if m == "Jornal" else "Dig", 100])
    ex = wb.create_sheet("Excepciones")
    ex.append(["Tipo", "Detalle", "Archivo", "Hoja", "Fila"])
    for _ in range(total_no_cuadra):
        ex.append(["Total no cuadra", "x", "a.png", None, None])
    ex.append(["Labor dudosa", "x", "a.png", None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakePlanillasPorFoto:
    """process() revienta: si el harness lo usa, no está probando el camino de producción."""

    def __init__(self, salidas=None):
        self.salidas = list(salidas or [])
        self.armados = []

    def process(self, docs):
        raise AssertionError("el harness usó process(): no expone analizar/armar")

    def analizar(self, docs):
        from app.services.resultados import PhotoResult
        return [PhotoResult(ok=False, filename=d.filename, error="ilegible", exc_type="ValueError")
                if "mala" in d.filename else PhotoResult(ok=True, filename=d.filename, data=d.filename)
                for d in docs]

    def armar(self, results):
        self.armados.append([r.filename for r in results])
        xlsx = self.salidas.pop(0) if self.salidas else _xlsx_planilla(["Jornal"])
        return SimpleNamespace(xlsx=xlsx, rows=1, yellow_cells=[], exceptions=[])

    def extra(self):  # método futuro: debe atravesar el envoltorio vía __getattr__
        return "ok"


def test_harness_usa_analizar_armar(tmp_path, monkeypatch):
    import logging

    from scripts.probar_lote_local import _ServicioMedido
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path / "webhook"))
    for n in ("planilla_ok.png", "planilla_mala.png", "planilla_ok2.png"):
        (tmp_path / n).write_bytes(PNG)
    fake = FakePlanillasPorFoto()
    tele = logging.getLogger("app.telemetria")

    class Clasif(FakeClassifier):
        def classify(self, doc, caption=""):
            tele.info(json.dumps({"tipo": "planilla", "model": "m",
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                                            "completion_tokens_details": {"reasoning_tokens": 2}}}))
            tele.info("línea no JSON")
            return super().classify(doc, caption)

    r = ejecutar(listar_archivos(tmp_path, None), tmp_path / "out", classifier=Clasif(),
                 pipeline=FakePipeline(), planillas=fake)

    assert fake.armados == [["planilla_ok.png", "planilla_ok2.png"]]
    assert [(p["metodo"], p["docs"]) for p in r["tiempos"]["procesamiento"]] == [("analizar", 3), ("armar", 2)]
    malas = [f for f in r["analisis_por_foto"] if not f["ok"]]
    assert malas == [{"servicio": "planillas", "archivo": "planilla_mala.png", "ok": False,
                      "exc_type": "ValueError", "error": "ilegible"}]
    assert r["resumen_lote"] == "📦 Procesadas 3 fotos: 2 planillas, 1 con error."
    assert r["envios"][0]["caption"].startswith(r["resumen_lote"])
    u = r["uso_openai"]
    assert u["total"] == {"llamadas": 3, "prompt": 30, "completion": 12, "reasoning": 6}
    assert u["por_tipo"]["planilla"]["llamadas"] == 3 and u["costo_usd"] is None and u["nota_costo"]
    md = (tmp_path / "out" / "reporte.md").read_text(encoding="utf-8")
    assert "Análisis por foto" in md and "ValueError: ilegible" in md and r["resumen_lote"] in md
    # el envoltorio no oculta métodos y no inventa analizar donde no existe
    w = _ServicioMedido("x", fake)
    assert w.extra() == "ok"
    assert getattr(_ServicioMedido("y", FakePipeline()), "analizar", None) is None


def test_uso_openai_con_precios(monkeypatch):
    from scripts.probar_lote_local import agregar_uso
    monkeypatch.setenv("OPENAI_PRECIOS_JSON", json.dumps({"m": {"in": 1.0, "out": 2.0}}))
    u = agregar_uso([{"tipo": "a", "model": "m", "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}}])
    assert u["costo_usd"] == 2.0 and u["total"]["prompt"] == 1_000_000
    assert agregar_uso([])["total"]["llamadas"] == 0


def test_repeticiones_compara_planillas(tmp_path, monkeypatch):
    from scripts.probar_lote_local import ejecutar_repeticiones, main
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path / "webhook"))
    (tmp_path / "datos").mkdir()
    (tmp_path / "datos" / "planilla1.png").write_bytes(PNG)
    fake = FakePlanillasPorFoto([_xlsx_planilla(["Jornal", "Contrato", "Contrato"], 2),
                                 _xlsx_planilla(["Jornal", "Jornal", "Jornal"], 0)])
    out = tmp_path / "out"
    comp = ejecutar_repeticiones(listar_archivos(tmp_path / "datos", None), out, 2,
                                 classifier=FakeClassifier(), pipeline=FakePipeline(), planillas=fake)
    m1, m2 = (x["metricas"] for x in comp["runs"])
    assert m1["modalidad"] == {"Jornal": 1, "Contrato": 2} and m1["total_no_cuadra"] == 2
    assert m1["jor_vacio"] == 2 and m1["rend_vacio"] == 1 and m1["und"] == {"Jor": 1, "Dig": 2}
    assert m2["modalidad"] == {"Jornal": 3} and m2["total_no_cuadra"] == 0 and m2["jor_vacio"] == 0
    assert (out / "run_1" / "reporte.md").is_file() and (out / "run_2" / "planillas_mano_de_obra.xlsx").is_file()
    md = (out / "comparacion.md").read_text(encoding="utf-8")
    assert "| 1 |" in md and "Total no cuadra" in md
    assert json.loads((out / "comparacion.json").read_text(encoding="utf-8"))["repeticiones"] == 2

    # CLI: --repeticiones llega a ejecutar_repeticiones (sin red: se intercepta)
    import scripts.probar_lote_local as h
    llamado = {}
    monkeypatch.setattr(h, "ejecutar_repeticiones", lambda a, o, n, **kw: llamado.update(n=n, k=len(a)))
    monkeypatch.setattr(h.os, "chdir", lambda *_: None)
    assert main([str(tmp_path / "datos"), "--repeticiones", "3", "--out", str(tmp_path / "cli")]) == 0
    assert llamado == {"n": 3, "k": 1}
