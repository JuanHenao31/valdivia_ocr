"""analizar -> armar: tope global de concurrencia (OpenAI/Jev), fallo parcial sin reintentos y
equivalencia con el process() anterior."""

import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from app.core.config import settings
from app.domain.models import DocumentResult, DocumentType, ProcessResult, SourcedDoc
from app.infrastructure import limites
from app.infrastructure.files import UploadedDoc
from app.services import ocr_service as ocr_mod
from app.services import validation_service as val_mod
from app.services.ocr_service import OCRService
from app.services.pipeline_service import DocumentPipeline
from app.services.planilla_service import PlanillaService
from app.services.validation_service import JevValidationService
from tests.test_planilla_paralelo import FakeJev as PlanillaJev, FakeOCR as PlanillaOCR, _cells, _docs
from tests.test_pipeline_paralelo import DOCS, FakeExcel, FakeJev as PipeJev, FakeOCR as PipeOCR


class Pico:
    """Cuenta llamadas concurrentes a un proveedor falso (con latencia simulada)."""

    def __init__(self, delay):
        self.delay, self.lock, self.activos, self.max = delay, threading.Lock(), 0, 0

    def __call__(self):
        with self.lock:
            self.activos += 1
            self.max = max(self.max, self.activos)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.activos -= 1


class FakeOpenAI:
    def __init__(self, pico):
        self.pico = pico
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.pico()
        doc = {"tipo": "remision", "raw_text": "R1", "fields": {"rem_numero": {"valor": "R1", "confianza": 90}}}
        assert kw.get("stream") is True  # el OCR va en streaming (plazo total)
        delta = SimpleNamespace(content=json.dumps({"documentos": [doc]}), refusal=None)
        return iter([SimpleNamespace(choices=[SimpleNamespace(index=0, delta=delta, finish_reason="stop")],
                                     usage=None)])


class FakeTypeSafe:
    def __init__(self, pico):
        self.pico = pico

    def system_one(self, state, questions):
        self.pico()
        ans = {k: SimpleNamespace(choice="remision", confidence=0.9, noul=0.9) for k in questions}
        return SimpleNamespace(answers=ans)


def test_tope_global_concurrencia_20_lotes_160_fotos(monkeypatch):
    if "OPENAI_SEM" not in inspect.getsource(ocr_mod):
        pytest.skip("ocr_service.py aún no envuelve la llamada a OpenAI con OPENAI_SEM (otro agente)")
    p_ai, p_jev = Pico(0.01), Pico(0.01)
    monkeypatch.setattr(ocr_mod, "get_openai_client", lambda: FakeOpenAI(p_ai))
    monkeypatch.setattr(ocr_mod, "to_png_b64", lambda *a, **k: ["x"])
    monkeypatch.setattr(val_mod, "get_typesafe_client", lambda: FakeTypeSafe(p_jev))
    monkeypatch.setattr(settings, "pipeline_workers", 8)
    pipe = DocumentPipeline.__new__(DocumentPipeline)
    pipe.ocr, pipe.jev, pipe.excel = OCRService(), JevValidationService(), FakeExcel()

    lotes = [[UploadedDoc(filename=f"l{b}_{i}.jpg", content=b"img") for i in range(8)] for b in range(20)]
    with ThreadPoolExecutor(max_workers=20) as ex:  # 20 lotes simultáneos (un hilo por lote)
        salidas = list(ex.map(lambda docs: pipe.armar(pipe.analizar(docs)), lotes))

    assert all(len(s[1].documents) == 8 for s in salidas)
    assert 1 < p_ai.max <= limites.OPENAI_MAX_CONCURRENCY
    assert 1 < p_jev.max <= limites.JEV_MAX_CONCURRENCY


def test_tope_jev_planillas_20_lotes_160_fotos(monkeypatch):
    """Jev de planillas (PlanillaService._jev_supports -> validate) respeta JEV_SEM."""
    p_jev = Pico(0.01)
    monkeypatch.setattr(val_mod, "get_typesafe_client", lambda: FakeTypeSafe(p_jev))
    monkeypatch.setattr(settings, "planilla_double_read", False)
    svc = PlanillaService.__new__(PlanillaService)
    svc.ocr, svc.jev = PlanillaOCR(), JevValidationService()
    lotes = [_docs(8) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(lambda d: svc.armar(svc.analizar(d, workers=8)), lotes))
    assert 1 < p_jev.max <= limites.JEV_MAX_CONCURRENCY


class ContadorOCR(PlanillaOCR):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.llamadas: dict[int, int] = {}

    def extract_json(self, content, *a, **k):
        n = int(content.decode())
        with self.lock:
            self.llamadas[n] = self.llamadas.get(n, 0) + 1
        return super().extract_json(content, *a, **k)


def test_planilla_fallo_parcial(monkeypatch):
    monkeypatch.setattr(settings, "planilla_double_read", False)
    ocr = ContadorOCR(fail={3, 7})
    svc = PlanillaService.__new__(PlanillaService)
    svc.ocr, svc.jev = ocr, PlanillaJev()
    res = svc.analizar(_docs(10), workers=8)
    assert [r.filename for r in res] == [f"p{i}.jpg" for i in range(10)]
    assert [r.filename for r in res if not r.ok] == ["p3.jpg", "p7.jpg"]
    assert res[3].exc_type == "RuntimeError" and "doc 3" in res[3].error
    assert ocr.llamadas == {i: 1 for i in range(10)}  # cada foto analizada una sola vez
    out = svc.armar(res)
    assert out.rows == 16  # 8 docs x 2 filas
    ws = load_workbook(BytesIO(out.xlsx))["Mano de obra"]
    archivos = {c for r in ws.iter_rows(min_row=2, values_only=True) for c in r if isinstance(c, str) and c.endswith(".jpg")}
    assert archivos == {f"p{i}.jpg" for i in range(10)} - {"p3.jpg", "p7.jpg"}
    with pytest.raises(RuntimeError, match="doc 3"):  # process (REST) sigue siendo todo-o-nada
        svc.process(_docs(10), workers=8)


def test_planilla_equivalencia_con_process_anterior(monkeypatch):
    monkeypatch.setattr(settings, "planilla_double_read", True)
    svc = PlanillaService.__new__(PlanillaService)
    svc.ocr, svc.jev = PlanillaOCR(), PlanillaJev()
    docs = _docs(12)
    # process() anterior, secuencial: _process_doc en orden + to_excel
    rows, revs, exc = [], [], []
    for d in docs:
        r, v, e = svc._process_doc(d, settings.planillas_model)
        rows += r
        revs += v
        exc += e
    viejo = svc.to_excel(rows, revs, exc)
    nuevo = svc.armar(svc.analizar(docs, workers=8))
    assert _cells(nuevo.xlsx) == _cells(viejo.xlsx)
    assert (nuevo.rows, nuevo.yellow_cells, nuevo.exceptions) == (viejo.rows, viejo.yellow_cells, viejo.exceptions)


def _pipe(ocr):
    p = DocumentPipeline.__new__(DocumentPipeline)
    p.ocr, p.jev, p.excel = ocr, PipeJev(), FakeExcel()
    return p


def test_pipeline_fallo_parcial(monkeypatch):
    monkeypatch.setattr(settings, "pipeline_workers", 8)
    llamadas: dict[str, int] = {}
    lock = threading.Lock()

    class OCR(PipeOCR):
        def extract(self, content, filename=None):
            k = content.decode()
            with lock:
                llamadas[k] = llamadas.get(k, 0) + 1
            if k in ("d-2", "d-5"):
                raise RuntimeError(f"falla {k}")
            return super().extract(content)

    p = _pipe(OCR())
    docs = DOCS[:10]
    res = p.analizar(docs)
    assert [r.filename for r in res if not r.ok] == ["f2.pdf", "f5.pdf"]
    assert llamadas == {f"d-{i}": 1 for i in range(10)}  # cada foto analizada una sola vez
    _, summary = p.armar(res)
    assert {d.filename.split(" ")[0] for d in summary.documents} == {d.filename for d in docs} - {"f2.pdf", "f5.pdf"}
    with pytest.raises(RuntimeError, match="falla d-2"):  # process (REST) sigue siendo todo-o-nada
        p.process(docs)


def test_pipeline_equivalencia_con_process_anterior(monkeypatch):
    monkeypatch.setattr(settings, "pipeline_workers", 8)
    # process() anterior, secuencial (copia del ensamblado original)
    p = _pipe(PipeOCR())
    result = ProcessResult(jev_enabled=settings.jev_enabled)
    buckets = {t: [] for t in p._FIELDS_BY_TYPE}
    for doc in DOCS:
        for k, (t, conf, fields) in enumerate(p._analyze(doc)):
            name = doc.filename if k == 0 else f"{doc.filename} (doc {k + 1})"
            dr = DocumentResult(filename=name, doc_type=t, doc_type_confidence=conf, fields=fields)
            result.documents.append(dr)
            if t in buckets:
                buckets[t].append((SourcedDoc(filename=name, fields=fields), dr))
    rem, liq, bas = (buckets[DocumentType.REMISION], buckets[DocumentType.LIQUIDACION_PLANTA],
                     buckets[DocumentType.LIQUIDACION_BASCULA])
    fill = p.excel.fill([s for s, _ in rem], [s for s, _ in liq], [s for s, _ in bas])
    for items, pos in ((rem, fill.remision_pos), (liq, fill.liquidacion_pos), (bas, fill.bascula_pos)):
        for i, (_, dr) in enumerate(items):
            if i in pos:
                dr.written_sheet, dr.written_row = pos[i]
    result.yellow_cells, result.exceptions = fill.yellow_cells, fill.exceptions

    p2 = _pipe(PipeOCR(jitter=True))
    xlsx, nuevo = p2.armar(p2.analizar(DOCS))
    assert xlsx == fill.xlsx
    assert nuevo.model_dump() == result.model_dump()
