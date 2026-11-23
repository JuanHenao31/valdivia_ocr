"""PlanillaService.process en paralelo: concurrencia real, mismo resultado que workers=1 y
mismo contrato de fallo (un doc que falla hace fallar la llamada)."""

import threading
import time
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.core.config import settings
from types import SimpleNamespace as ValidationResult
from app.infrastructure.files import UploadedDoc
from app.services.planilla_service import PlanillaService


def _data(n: int) -> dict:
    v = lambda x: {"valor": x, "confianza": 95}  # noqa: E731
    return {
        "finca": v("ZZZ"), "anio": v(2026), "semana": v(10), "dia_semana": v("LUNES"),
        "dia_mes": v(None), "nota": v(None), "raw_text": f"doc{n}",
        "filas": [{"nombre": v(f"PERSONA {n}-{j}"), "cedula": v(str(1000 + n * 10 + j)),
                   "lote": v(j), "labor": v("X"), "jor": v(1), "rend": v(None), "und": v(None),
                   "vr_unit": v(50000 + n), "total_escrito": v(50000 + n)} for j in range(2)],
    }


class FakeOCR:
    def __init__(self, delay=0.0, barrier=None, fail=()):
        self.delay, self.barrier, self.fail = delay, barrier, set(fail)
        self.lock = threading.Lock()
        self.active = self.max_active = 0

    def extract_json(self, content, *a, **k):
        n = int(content.decode())
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier:
                self.barrier.wait(timeout=5)
            # docs con índice bajo tardan más: fuerza que terminen fuera de orden
            time.sleep(self.delay * (1 + (10 - n % 10) / 10))
            if n in self.fail:
                raise RuntimeError(f"OCR falló doc {n}")
            return _data(n)
        finally:
            with self.lock:
                self.active -= 1


class FakeJev:
    def validate(self, ocr):
        time.sleep(0.005)
        # baja confianza para la cédula de la fila 0 de docs pares -> amarillo determinista
        par = ocr.raw_text.endswith(("0", "2", "4", "6", "8"))
        return ValidationResult(enabled=True, supports={"f0_cedula": 0.1 if par else 0.99})


def _svc(ocr):
    s = PlanillaService.__new__(PlanillaService)
    s.ocr, s.jev = ocr, FakeJev()
    return s


def _docs(n):
    return [UploadedDoc(filename=f"p{i}.jpg", content=str(i).encode()) for i in range(n)]


def _cells(xlsx):
    wb = load_workbook(BytesIO(xlsx))
    return {ws.title: [[(c.value, c.fill.fgColor.rgb) for c in r] for r in ws.iter_rows()] for ws in wb}


@pytest.fixture(autouse=True)
def _double(monkeypatch):
    monkeypatch.setattr(settings, "planilla_double_read", True)


def test_concurrencia_real():
    ocr = FakeOCR(barrier=threading.Barrier(4))  # solo pasa si 4 corren a la vez
    _svc(ocr).process(_docs(4), workers=4)
    assert ocr.max_active >= 4


def test_mismo_resultado_que_secuencial():
    seq = _svc(FakeOCR(delay=0.01)).process(_docs(12), workers=1)
    par = _svc(FakeOCR(delay=0.01)).process(_docs(12), workers=8)
    assert par.rows == seq.rows == 24
    assert par.yellow_cells == seq.yellow_cells
    assert [e.model_dump() if hasattr(e, "model_dump") else vars(e) for e in par.exceptions] == \
           [e.model_dump() if hasattr(e, "model_dump") else vars(e) for e in seq.exceptions]
    assert _cells(par.xlsx) == _cells(seq.xlsx)
    nombres = [r[2][0] for r in _cells(par.xlsx)["Mano de obra"][1:]]
    assert nombres == [f"PERSONA {i}-{j}" for i in range(12) for j in range(2)]


def test_workers_1_es_secuencial():
    ocr = FakeOCR(delay=0.005)
    _svc(ocr).process(_docs(5), workers=1)
    assert ocr.max_active == 1


def test_default_desde_settings(monkeypatch):
    monkeypatch.setattr(settings, "planilla_workers", 1)
    ocr = FakeOCR()
    _svc(ocr).process(_docs(3))
    assert ocr.max_active == 1


@pytest.mark.parametrize("workers", [1, 8])
def test_fallo_de_un_doc_falla_la_llamada(workers):
    with pytest.raises(RuntimeError, match="doc 3"):
        _svc(FakeOCR(delay=0.005, fail={3, 7})).process(_docs(10), workers=workers)
