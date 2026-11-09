"""Convierte el diccionario de Valdivia (Excel) en app/data/labores_por_finca.json.

En runtime NUNCA se lee el Excel: el servicio carga este JSON una vez como constante.
Re-ejecutar cuando Valdivia envíe un diccionario nuevo:

    python scripts/build_labor_data.py "business/Diccionario_labores_por_finca (1).xlsx"
"""

import json
import sys
from pathlib import Path

from openpyxl import load_workbook

DEFAULT_SRC = Path("business/Diccionario_labores_por_finca (1).xlsx")
OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "labores_por_finca.json"


def _s(v) -> str:
    s = "" if v is None else str(v).strip()
    return s if any(ch.isalnum() for ch in s) else ""  # "—" y similares = vacío


def _list(v) -> list[str]:
    return [x.strip() for x in _s(v).split(",") if x.strip()]


def build(src: Path) -> dict:
    wb = load_workbook(src, read_only=True, data_only=True)
    rows = wb["Base finca-labor"].iter_rows(values_only=True)
    header = [_s(h) for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    fincas: dict[str, dict] = {}
    for r in rows:
        finca, labor = _s(r[idx["Finca"]]).upper(), _s(r[idx["Labor"]]).upper()
        if not finca or not labor:
            continue
        cc = set()
        for col, i in idx.items():
            if col.startswith("Centros de costo"):
                cc.update(c for c in _list(r[i]) if c.lower() != "sin centro")
        f = fincas.setdefault(finca, {"labores": {}})
        f["labores"][labor] = {
            "clasificacion": _s(r[idx["Clasificación"]]),
            "categoria": _s(r[idx["Categoría"]]),
            "cultivos": _list(r[idx["Cultivos"]]),
            "codigos": _s(r[idx["Código(s) de labor por cultivo"]]),
            "unidades": _list(r[idx["Unidades de medida"]]),
            "centros_costo": sorted(cc),
        }
    return {"fuente": src.name, "fincas": dict(sorted(fincas.items()))}


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    data = build(src)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(f["labores"]) for f in data["fincas"].values())
    print(f"{OUT}: {len(data['fincas'])} fincas, {n} finca-labor")
