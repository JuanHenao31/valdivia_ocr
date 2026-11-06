"""Proceso 1: planilla Control Diario -> Excel 1 (Mano de obra)."""

from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from app.services.planilla_service import PlanillaService, fecha_from_semana


def v(x, c=95):
    return {"valor": x, "confianza": c}


def planilla(semana=42, filas=None):
    return {
        "finca": v("GINEBRA"), "anio": v(2026), "semana": v(semana), "dia_semana": v("Viernes"),
        "dia_mes": v(23), "nota": v("Solo plátano"),
        "filas": filas if filas is not None else [
            {"nombre": v("Libardo Albeiro Timana"), "cedula": v("18494034"), "labor": v("Deshojando"),
             "lote": v("1"), "rend": v(11), "und": v("Hectáreas"), "vr_unit": v(85000), "total_escrito": v(935000)},
            {"nombre": v("Orlando Villabon"), "cedula": v("7524180"), "labor": v("Recogiendo basura"),
             "jor": v(1), "vr_unit": v(63000), "total_escrito": v(63000)},
            {"nombre": v("Jhon Flores"), "cedula": v("17948598"), "labor": v("Embolsando"),
             "jor": v(1), "vr_unit": v("?")},
        ],
    }


def svc():
    return PlanillaService.__new__(PlanillaService)  # sin clientes externos


def test_fecha_from_semana_inverse_of_U():
    assert fecha_from_semana(2026, 42, "Viernes") == date(2026, 10, 23)
    assert fecha_from_semana(2026, 41, "miércoles") == date(2026, 10, 14)
    assert int(date(2026, 10, 23).strftime("%U")) == 42
    assert fecha_from_semana(2026, None, "Martes") is None  # sin semana no se adivina el mes


def test_build_rows():
    rows, reviews, exc = svc().build_rows(planilla(), "p.jpeg")
    assert rows[0]["fecha"] == date(2026, 10, 23)
    assert rows[0]["modalidad"] == "Contrato" and rows[0]["rend"] == 11 and rows[0]["vr_unit"] == 85000
    assert rows[1]["modalidad"] == "Jornal"
    assert reviews[0] == set() and reviews[1] == set()
    assert {"vr_unit", "total"} <= reviews[2]  # "?" en VLR UNITARIO
    assert exc == []


def test_missing_semana_flags_fecha():
    rows, reviews, exc = svc().build_rows(planilla(semana=None), "p.jpeg")
    assert rows[0]["fecha"] is None and "fecha" in reviews[0]
    assert exc[0].tipo == "Fecha incompleta"


def test_double_read_flags_differences():
    s = svc()
    a = s.build_rows(planilla(), "p.jpeg")
    filas_b = planilla()["filas"]
    filas_b[0] = dict(filas_b[0], rend=v(1.1))  # la 2ª lectura ve "1,1" en vez de 11
    b = s.build_rows(planilla(filas=filas_b), "p.jpeg")
    rows, reviews, exc = s.reconcile(a, b, "p.jpeg")
    assert "rend" in reviews[0]
    assert reviews[1] == set()  # fila igual en ambas lecturas
    assert exc == []


def test_double_read_uses_fuller_read_when_one_is_empty():
    s = svc()
    full = s.build_rows(planilla(), "p.jpeg")
    empty = s.build_rows(planilla(filas=[]), "p.jpeg")
    rows, _, exc = s.reconcile(empty, full, "p.jpeg")
    assert len(rows) == 3
    assert exc[-1].tipo == "Lectura única"


def test_total_mismatch_and_duplicate_excel():
    f = planilla()["filas"][1]
    bad = dict(f, total_escrito=v(70000))
    rows, reviews, exc = svc().build_rows(planilla(filas=[f, bad]), "p.jpeg")
    assert "total" in reviews[1] and exc[0].tipo == "Total no cuadra"
    res = svc().to_excel(rows, reviews, exc)
    ws = load_workbook(BytesIO(res.xlsx))["Mano de obra"]
    assert ws["L2"].value == "=H2*K2"
    assert ws["C2"].value == "Orlando Villabon"
    assert "Posible duplicado" in {e.tipo for e in res.exceptions}
