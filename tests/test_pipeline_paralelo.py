"""Proceso 2 en paralelo: concurrencia real, salida idéntica a workers=1 y contrato de fallo."""

import random
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.domain.models import OCRResult
from app.services.pipeline_service import DocumentPipeline
from app.services.validation_service import ValidationResult


class Doc(SimpleNamespace):
    pass


class FakeOCR:
    def __init__(self, barrier=None, fail_on=None, jitter=False):
        self.barrier, self.fail_on, self.jitter = barrier, fail_on, jitter
        self.threads = set()

    def extract(self, content: bytes, filename=None):
        self.threads.add(threading.get_ident())
        if self.barrier:
            self.barrier.wait(timeout=5)  # solo pasa si N docs están en vuelo a la vez
        if self.jitter:
            time.sleep(random.uniform(0, 0.02))  # desordena finalizaciones
        key = content.decode()
        if key == self.fail_on:
            raise RuntimeError(f"OCR falló en {key}")
        i = int(key.split("-")[1])
        tipos = ["remision", "liquidacion_planta", "liquidacion_bascula", "otro"]
        n = 2 if i % 3 == 0 else 1  # algunos archivos traen 2 documentos
        return [
            OCRResult(
                tipo=tipos[(i + k) % 4],
                raw_text=key,
                fields={"rem_numero": {"valor": f"R{i}", "confianza": 90}},
            )
            for k in range(n)
        ]


class FakeJev:
    def validate(self, ocr):
        return ValidationResult(enabled=False)


class FakeExcel:
    def fill(self, rem, liq, bas):
        self.args = ([s.filename for s in rem], [s.filename for s in liq], [s.filename for s in bas])
        return SimpleNamespace(
            xlsx=repr(self.args).encode(),
            remision_pos={i: ("R", i + 2) for i in range(len(rem))},
            liquidacion_pos={i: ("L", i + 2) for i in range(len(liq))},
            bascula_pos={},
            yellow_cells=[],
            exceptions=[],
        )


def make_pipeline(ocr):
    p = DocumentPipeline.__new__(DocumentPipeline)
    p.ocr, p.jev, p.excel = ocr, FakeJev(), FakeExcel()
    return p


DOCS = [Doc(filename=f"f{i}.pdf", content=f"d-{i}".encode()) for i in range(12)]


def test_concurrencia_real(monkeypatch):
    monkeypatch.setattr(settings, "pipeline_workers", 4)
    ocr = FakeOCR(barrier=threading.Barrier(4))
    make_pipeline(ocr).process(DOCS[:8])  # con ejecución secuencial la barrera haría timeout
    assert len(ocr.threads) == 4


def test_salida_identica_a_secuencial(monkeypatch):
    monkeypatch.setattr(settings, "pipeline_workers", 1)
    seq_x, seq_r = make_pipeline(FakeOCR()).process(DOCS)
    monkeypatch.setattr(settings, "pipeline_workers", 8)
    par_x, par_r = make_pipeline(FakeOCR(jitter=True)).process(DOCS)
    assert par_x == seq_x
    assert par_r.model_dump() == seq_r.model_dump()
    assert seq_r.documents[1].filename == "f0.pdf (doc 2)"


def test_workers_1_corre_en_hilo_llamador(monkeypatch):
    monkeypatch.setattr(settings, "pipeline_workers", 1)
    ocr = FakeOCR()
    make_pipeline(ocr).process(DOCS)
    assert ocr.threads == {threading.get_ident()}


@pytest.mark.parametrize("workers", [1, 8])
def test_fallo_de_un_doc_propaga_la_excepcion(monkeypatch, workers):
    monkeypatch.setattr(settings, "pipeline_workers", workers)
    p = make_pipeline(FakeOCR(fail_on="d-5"))
    with pytest.raises(RuntimeError, match="d-5"):
        p.process(DOCS)
    assert not hasattr(p.excel, "args")  # no se arma Excel con un grupo fallido
