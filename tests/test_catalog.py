"""Diccionario finca/labor: corrección al nombre oficial y celdas amarillas."""

from app.services.catalog_service import DEFAULT_CATALOG, LaborCatalog, similarity
from app.services.planilla_service import PlanillaService
from tests.test_planilla import planilla, svc, v

CAT = LaborCatalog({
    "GINEBRA": ["DESHOJE", "RECOGER BASURA", "EMBOLSE", "APLICACIÓN GRANULADA"],
    "LIMONAR": ["PLATEO MANUAL"],
    "LIMONARES": ["PODA SOSTENIMIENTO"],
})


def with_cat(cat=CAT) -> PlanillaService:
    s = svc()
    s.catalog = cat
    return s


def fila(labor):
    return {"nombre": v("Ana"), "cedula": v("123"), "labor": v(labor), "jor": v(1),
            "vr_unit": v(63000), "total_escrito": v(63000)}


def test_real_data_loaded_in_memory():
    assert {"GINEBRA", "LA CAMELIA", "LIMONAR", "LIMONARES"} <= set(DEFAULT_CATALOG.fincas)
    assert DEFAULT_CATALOG.match_labor("LA CAMELIA", "aplicacion abono").oficial == "APLICACION DE ABONO"


def test_similarity_verbs_and_accents():
    assert similarity("Deshojando", "DESHOJE") >= 90
    assert similarity("aplicacion granulada", "APLICACIÓN GRANULADA") == 100
    assert similarity("Volar cometa", "DESHOJE") < 75
    # el texto idéntico gana a uno que solo comparte raíz
    assert CAT.match_finca("Limonares").oficial == "LIMONARES"
    assert CAT.match_finca("limonar").oficial == "LIMONAR"


def test_labor_corrected_to_official_without_yellow():
    rows, reviews, exc = with_cat().build_rows(planilla(), "p.jpeg")
    assert [r["labor"] for r in rows] == ["DESHOJE", "RECOGER BASURA", "EMBOLSE"]
    assert rows[0]["_labor_ocr"] == "Deshojando"
    assert "labor" not in reviews[0] and "finca" not in reviews[0]
    assert exc == []


def test_unknown_labor_kept_and_yellow():
    rows, reviews, exc = with_cat().build_rows(planilla(filas=[fila("Volar cometa")]), "p.jpeg")
    assert rows[0]["labor"] == "Volar cometa" and "labor" in reviews[0]
    assert exc[0].tipo == "Labor no existe en finca"


def test_doubtful_labor_official_but_yellow(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "labor_match_ok", 101)  # fuerza la franja media
    rows, reviews, exc = with_cat().build_rows(planilla(filas=[fila("Deshojando")]), "p.jpeg")
    assert rows[0]["labor"] == "DESHOJE" and "labor" in reviews[0]
    assert "OCR leyó: Deshojando" in rows[0]["observaciones"]
    assert exc[-1].tipo == "Labor dudosa"


def test_finca_fuzzy_and_unknown():
    data = planilla(filas=[fila("Plateo manual")])
    data["finca"] = v("Limonar")
    rows, reviews, _ = with_cat().build_rows(data, "p.jpeg")
    assert rows[0]["finca"] == "LIMONAR" and reviews[0] == set()

    data["finca"] = v("LA ESPERANZA")  # no está: finca amarilla, labor contra el general
    rows, reviews, exc = with_cat().build_rows(data, "p.jpeg")
    assert rows[0]["finca"] == "LA ESPERANZA" and "finca" in reviews[0]
    assert rows[0]["labor"] == "PLATEO MANUAL" and "labor" not in reviews[0]
    assert exc[0].tipo == "Finca no está en el diccionario"


def test_empty_catalog_disables_check():
    rows, reviews, exc = with_cat(LaborCatalog({})).build_rows(planilla(filas=[fila("Volar cometa")]), "p.jpeg")
    assert rows[0]["labor"] == "Volar cometa" and "labor" not in reviews[0] and exc == []
