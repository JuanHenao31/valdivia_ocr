"""Proceso 2: mapeo remisión/liquidación -> plantilla, contrastado con la hoja EJEMPLO LIMON."""

from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from app.core.config import settings
from app.domain.models import ExtractedField, SourcedDoc
from app.services.excel_service import ExcelService, format_lotes, parse_fecha, semana, to_int


def _doc(filename="f", review=(), **values):
    return SourcedDoc(
        filename=filename,
        fields=[ExtractedField(name=k, value=v, ocr_confidence=90, needs_review=k in review)
                for k, v in values.items()],
    )


def rem_mc125(review=(), **over):
    base = dict(cultivo="LIMON", finca="CAMELIA", remision_prefijo="MC", remision_num=125,
                fecha="2026-10-14", cliente="RAMAFRUTS", placa="TRJ399", variedad="LIMON TAHITI",
                kg_despachados=5000, canastillas=264,
                lotes=[{"lote": 5, "canastillas": 169, "kg": 3201}, {"lote": 9, "canastillas": 95, "kg": 1799}])
    base.update(over)
    return _doc("Remision MC-125.jpeg", review, **base)


def liq_mc125(review=(), **over):
    base = dict(liq_remision_prefijo="MC", liq_remision_num=125, liq_placa="TRJ399", liq_fecha="2026-10-15",
                liq_kg_primera=2909, liq_kg_primera_parejo=363, liq_precio_exportacion=2019,
                liq_kg_nacional=721, liq_precio_nacional=1500, liq_kg_industrial=957, liq_precio_industrial=694)
    base.update(over)
    return _doc("Liquidacion MC125.pdf", review, **base)


def _ws(res, sheet=None):
    return load_workbook(BytesIO(res.xlsx))[sheet or settings.sheet_name]


def test_helpers():
    assert to_int("10 220") == 10220
    assert to_int("2.909,0") == 2909
    assert to_int("$ 2.019") == 2019
    assert to_int(None) is None
    # Semana %U (no ISO): 2026-10-14 -> 41 como en EJEMPLO LIMON; 2026-09-15 -> 37.
    assert semana(parse_fecha("2026-10-14")) == 41
    assert semana(parse_fecha("2026-09-15")) == 37


def test_format_lotes():
    assert format_lotes([{"lote": 5, "kg": 3201}, {"lote": 9, "kg": 1799}]) == ("5 - 3201 KG\n9 - 1799 KG", False)
    # Sin kg: se escribe tal cual con canastillas y se marca; NO se prorratea.
    assert format_lotes([{"lote": 10, "canastillas": 120, "kg": None}]) == ("10 - 120 CAN", True)
    assert format_lotes([]) == (None, True)


def test_mc125_matches_ejemplo_limon():
    res = ExcelService().fill([rem_mc125()], [liq_mc125()])
    ws = _ws(res)
    ex = load_workbook(settings.template_path)["EJEMPLO LIMON"]  # fila 2 = MC 125
    r = 3
    assert ws[f"A{r}"].value.date() == date(2026, 10, 14)
    for col in "BCDFGHJKMNPQ":
        assert ws[f"{col}{r}"].value == ex[f"{col}2"].value, col
    assert ws[f"Y{r}"].value == ex["Y2"].value  # "5 - 3201 KG\n9 - 1799 KG"
    # Derivadas: mismas fórmulas de la plantilla trasladadas a la fila 3.
    for col in ["I", "L", "O", "R", "S", "T", "U", "V", "W", "X", "Z", "AA", "AB"]:
        assert ws[f"{col}{r}"].value == ex[f"{col}2"].value.replace("2", "3"), col
    assert res.remision_pos == {0: (settings.sheet_name, 3)}
    assert res.liquidacion_pos == {0: (settings.sheet_name, 3)}
    assert res.yellow_cells == []
    assert res.exceptions == []


def test_match_by_remision_number_not_order():
    rems = [rem_mc125(remision_num=126, placa="ESU127"), rem_mc125()]
    res = ExcelService().fill(rems, [liq_mc125()])
    assert res.liquidacion_pos[0] == (settings.sheet_name, 4)  # MC 125 está en la 2ª fila
    assert {e.tipo for e in res.exceptions} == {"Remisión sin liquidación"}


def test_placa_mismatch_flagged():
    res = ExcelService().fill([rem_mc125(placa="TIJ399")], [liq_mc125()])
    assert f"{settings.sheet_name}!E3" in res.yellow_cells
    assert "Placa no coincide" in {e.tipo for e in res.exceptions}


def test_fallback_by_placa_when_no_number():
    liq = liq_mc125(liq_remision_prefijo=None, liq_remision_num=None)
    res = ExcelService().fill([rem_mc125()], [liq])
    assert res.liquidacion_pos[0] == (settings.sheet_name, 3)
    assert "Cruce por placa" in {e.tipo for e in res.exceptions}


def test_liquidacion_without_remision_new_row_and_exception():
    res = ExcelService().fill([rem_mc125()], [liq_mc125(liq_remision_num=999, liq_placa="XYZ999")])
    assert res.liquidacion_pos[0] == (settings.sheet_name, 4)
    tipos = {e.tipo for e in res.exceptions}
    assert {"Liquidación sin remisión", "Remisión sin liquidación"} <= tipos
    assert _ws(res)["C4"].value == 999


def test_duplicates():
    res = ExcelService().fill([rem_mc125(), rem_mc125()], [liq_mc125(), liq_mc125()])
    tipos = [e.tipo for e in res.exceptions]
    assert "Remisión duplicada" in tipos
    assert "Liquidación duplicada" in tipos


def test_kg_difference_flagged():
    res = ExcelService().fill([rem_mc125(kg_despachados=6000)], [liq_mc125()])
    assert "Diferencia kg" in {e.tipo for e in res.exceptions}


def test_review_flags():
    res = ExcelService().fill(
        [rem_mc125(review=("placa", "fecha"), lotes=[{"lote": 10, "canastillas": 120, "kg": None}])],
        [liq_mc125(review=("liq_precio_nacional",))],
    )
    y = set(res.yellow_cells)
    s = settings.sheet_name
    assert {f"{s}!A3", f"{s}!B3", f"{s}!E3", f"{s}!Y3", f"{s}!N3"} <= y
    assert f"{s}!D3" not in y and f"{s}!I3" not in y


def test_trazabilidad_sheet():
    res = ExcelService().fill([rem_mc125()], [liq_mc125()])
    wb = load_workbook(BytesIO(res.xlsx))
    assert [c.value for c in wb["Trazabilidad"][2]] == [
        settings.sheet_name, 3, "MC 125", "Remision MC-125.jpeg", "Liquidacion MC125.pdf", "OK"]
    assert "Excepciones" in wb.sheetnames


# --- Plátano ---


def rem_pa206(**over):
    base = dict(cultivo="PLATANO", finca="LA PALMA", remision_prefijo="PA", remision_num=206,
                fecha="2026-10-23", cliente="CUATRO CIELOS", placa="WOK917", variedad="HARTON",
                kg_despachados=7567, canastillas=375,
                lotes=[{"lote": 3, "canastillas": None, "kg": 299}, {"lote": 4, "canastillas": None, "kg": 1334}])
    base.update(over)
    return _doc("Remision PA-206.jpeg", **base)


def bas_pa206(**over):
    base = dict(bas_placa="WOK917", bas_cliente="CUATRO CIELOS",
                bas_entregas=[{"finca": "LA PALMA", "kg": 7567, "canastillas": 375}])
    base.update(over)
    return _doc("Liquidacion PA206.jpeg", **base)


def test_platano_row(monkeypatch):
    monkeypatch.setattr(settings, "platano_precio_kg", 1000)
    monkeypatch.setattr(settings, "platano_precios_cliente", "CUATRO CIELOS=1200")
    res = ExcelService().fill([rem_pa206()], [], [bas_pa206()])
    ws = _ws(res, settings.sheet_name_platano)
    assert [ws[f"{c}3"].value for c in "BCDEFGHIJK"] == [
        42, 206, "CUATRO CIELOS", "WOK917", "HARTON", 7567, 1200, "=G3*H3", 375, "3 - 299 KG\n4 - 1334 KG"]
    assert res.yellow_cells == []
    assert res.exceptions == []


def test_platano_without_price_or_bascula_flagged(monkeypatch):
    monkeypatch.setattr(settings, "platano_precio_kg", None)
    res = ExcelService().fill([rem_pa206()], [], [])
    s = settings.sheet_name_platano
    assert {f"{s}!G3", f"{s}!H3"} <= set(res.yellow_cells)
    assert "Remisión sin liquidación" in {e.tipo for e in res.exceptions}


def test_platano_split_bascula_two_fincas(monkeypatch):
    monkeypatch.setattr(settings, "platano_precio_kg", 1000)
    bz = rem_pa206(finca="BONANZA", remision_prefijo="RM", remision_num=15, placa="EST389", kg_despachados=2120)
    ca = rem_pa206(finca="CAMELIA", remision_prefijo="RM", remision_num=43, placa="EST389", kg_despachados=1130)
    bas = bas_pa206(bas_placa="EST389",
                    bas_entregas=[{"finca": "BONANZA", "kg": 2120}, {"finca": "CAMELIA", "kg": 1130}])
    res = ExcelService().fill([bz, ca], [], [bas])
    ws = _ws(res, settings.sheet_name_platano)
    assert (ws["G3"].value, ws["G4"].value) == (2120, 1130)
    assert res.exceptions == []
