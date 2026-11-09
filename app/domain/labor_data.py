"""Diccionario finca -> labores de Valdivia, cargado UNA vez en memoria al importar.

Fuente: app/data/labores_por_finca.json (generado con scripts/build_labor_data.py desde el
Excel que entrega Valdivia). En runtime nunca se lee el Excel.
"""

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "labores_por_finca.json"

LABOR_DATA: dict = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else {"fincas": {}}

# finca oficial -> lista de labores oficiales
LABORES_POR_FINCA: dict[str, list[str]] = {f: list(d["labores"]) for f, d in LABOR_DATA["fincas"].items()}
FINCAS: list[str] = list(LABORES_POR_FINCA)
LABORES_GENERAL: list[str] = sorted({l for ls in LABORES_POR_FINCA.values() for l in ls})
