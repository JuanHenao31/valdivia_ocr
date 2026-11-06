"""Excel 2 (Proceso 2): remisiones + liquidaciones -> plantilla de Valdivia.

- Limón -> hoja "Plantilla Limón" (A–AB). Remisión: A–H + Y (lotes). Liquidación de planta:
  J,K,M,N,P,Q (crudas). Las columnas derivadas (I,L,O,R,S,T,U,V,W,X,Z,AA,AB) se escriben
  con las MISMAS fórmulas de la fila de referencia de la plantilla, trasladadas a la fila.
- Plátano -> hoja "Plantilla Normal" (A–K). Kilos de la báscula/WhatsApp; precio configurable.
- Cruce remisión <-> liquidación por (prefijo, nº remisión); fallback por placa (limón) o
  finca/placa (plátano). Nada se une por suposición: lo no conciliado va a "Excepciones".
- Trazabilidad: hoja "Trazabilidad" con el archivo de origen de cada fila.
"""

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from openpyxl import load_workbook
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Font, PatternFill

from app.core.config import settings
from app.domain.models import Exception_, SourcedDoc

YELLOW_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
TEMPLATE_REF_ROW = 3  # fila de la plantilla con formatos/fórmulas de referencia
FIRST_DATA_ROW = 3  # fila 2 = encabezados

# Limón: campo de remisión -> columna (A/B fecha-semana y Y lotes se arman aparte).
LIMON_REM_COLUMNS = {
    "remision_num": "C",
    "cliente": "D",
    "placa": "E",
    "variedad": "F",
    "kg_despachados": "G",
    "canastillas": "H",
}
# Limón: campo crudo de liquidación -> columna (J = primera + parejo, aparte).
LIMON_LIQ_COLUMNS = {
    "liq_precio_exportacion": "K",
    "liq_kg_nacional": "M",
    "liq_precio_nacional": "N",
    "liq_kg_industrial": "P",
    "liq_precio_industrial": "Q",
}
LIMON_LOTE_COL = "Y"
NUMERIC_FIELDS = {
    "remision_num",
    "kg_despachados",
    "canastillas",
    *LIMON_LIQ_COLUMNS,
    "liq_kg_primera",
    "liq_kg_primera_parejo",
}


# ----------------------------------------------------------------------------- helpers


def norm_placa(v):
    """'JVM-364'/'jvm 364' -> 'JVM364'. None si queda vacío."""
    if v is None:
        return None
    cleaned = "".join(ch for ch in str(v) if ch.isalnum()).upper()
    return cleaned or None


def norm_text(v):
    return " ".join(str(v).upper().split()) if v not in (None, "") else None


def to_int(v):
    """'10 220', '$1.200', 480 -> int. '2.909,0' -> 2909. None si no se puede."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = str(v)
    if "," in s:  # decimales estilo colombiano: "2.909,0"
        s = s.split(",")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


def parse_fecha(v):
    if not v:
        return None
    try:
        y, m, d = (int(x) for x in str(v).split("-"))
        return date(y, m, d)
    except Exception:
        return None


def semana(fecha: date) -> int:
    """La finca NO usa semana ISO: %U (domingo primer día). 2026-10-14 -> 41."""
    return int(fecha.strftime("%U"))


def rem_key(prefijo, num):
    p = norm_placa(prefijo)  # mismo saneamiento: "MC " -> "MC"
    n = to_int(num)
    return (p, n) if p and n is not None else None


def format_lotes(lotes) -> tuple[str | None, bool]:
    """[{lote,canastillas,kg}] -> ('5 - 3201 KG\\n9 - 1799 KG', needs_review).

    Si un lote no trae kg se escribe tal cual con sus canastillas ('10 - 120 CAN') y se
    marca para revisión: NO se prorratean kg (regla de integridad).
    """
    if not isinstance(lotes, list) or not lotes:
        return None, True
    lines, review = [], False
    for item in lotes:
        if not isinstance(item, dict):
            review = True
            continue
        lote = to_int(item.get("lote"))
        kg = to_int(item.get("kg"))
        can = to_int(item.get("canastillas"))
        if lote is None:
            review = True
            continue
        if kg is not None:
            lines.append(f"{lote} - {kg} KG")
        elif can is not None:
            lines.append(f"{lote} - {can} CAN")
            review = True
        else:
            lines.append(f"{lote}")
            review = True
    return ("\n".join(lines) or None), review


@dataclass
class FillResult:
    xlsx: bytes
    yellow_cells: list[str]
    exceptions: list[Exception_]
    # índice del documento (en su lista de entrada) -> (hoja, fila)
    remision_pos: dict[int, tuple[str, int]] = field(default_factory=dict)
    liquidacion_pos: dict[int, tuple[str, int]] = field(default_factory=dict)
    bascula_pos: dict[int, tuple[str, int]] = field(default_factory=dict)


@dataclass
class _Trace:
    hoja: str
    fila: int
    remision: str = ""
    archivo_remision: str = ""
    archivo_liquidacion: str = ""
    estado: str = "OK"


# ----------------------------------------------------------------------------- servicio


class ExcelService:
    def __init__(self) -> None:
        self.template_path = settings.template_path
        self.sheet_limon = settings.sheet_name
        self.sheet_platano = settings.sheet_name_platano

    # ---- construcción de filas (puras, testeables) ----

    def _review(self, doc: SourcedDoc, name: str, value) -> bool:
        f = doc.get(name)
        return bool((f and f.needs_review) or value in (None, ""))

    def _fecha_sem(self, doc: SourcedDoc, row: dict, review: set) -> None:
        fecha = parse_fecha(doc.value("fecha"))
        row["A"] = fecha
        row["B"] = semana(fecha) if fecha else None
        if self._review(doc, "fecha", fecha):
            review.update({"A", "B"})

    def _row_from_remision(self, doc: SourcedDoc):
        """Remisión de limón -> (valores por columna A–H + Y, columnas a revisar)."""
        row: dict[str, object] = {}
        review: set[str] = set()
        self._fecha_sem(doc, row, review)
        for name, col in LIMON_REM_COLUMNS.items():
            raw = doc.value(name)
            value = to_int(raw) if name in NUMERIC_FIELDS else raw
            if name == "placa":
                value = norm_placa(raw)
            row[col] = value
            if self._review(doc, name, value):
                review.add(col)
        lotes, lotes_review = format_lotes(doc.value("lotes"))
        row[LIMON_LOTE_COL] = lotes
        if lotes_review or self._review(doc, "lotes", lotes):
            review.add(LIMON_LOTE_COL)
        return row, review

    def _row_from_liquidacion(self, doc: SourcedDoc):
        """Liquidación de planta -> (crudas J,K,M,N,P,Q, columnas a revisar)."""
        row: dict[str, object] = {}
        review: set[str] = set()
        # J = PRIMERA + PRIMERA PAREJO como "=a+b" (igual que EJEMPLO LIMON: "=2909+363").
        primera = to_int(doc.value("liq_kg_primera"))
        parejo = to_int(doc.value("liq_kg_primera_parejo"))
        if primera is not None and parejo is not None:
            row["J"] = f"={primera}+{parejo}"
        else:
            row["J"] = primera if primera is not None else parejo
        f_par = doc.get("liq_kg_primera_parejo")
        if self._review(doc, "liq_kg_primera", primera) or (f_par and f_par.needs_review):
            review.add("J")
        for name, col in LIMON_LIQ_COLUMNS.items():
            value = to_int(doc.value(name))
            row[col] = value
            if self._review(doc, name, value):
                review.add(col)
        return row, review

    @staticmethod
    def liq_kg_total(doc: SourcedDoc) -> int | None:
        parts = [
            to_int(doc.value(n))
            for n in ("liq_kg_primera", "liq_kg_primera_parejo", "liq_kg_nacional", "liq_kg_industrial")
        ]
        return sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None

    # ---- escritura ----

    @staticmethod
    def _template_formulas(ws) -> dict[str, str]:
        """Columnas con fórmula en la fila de referencia de la plantilla."""
        out = {}
        for cell in ws[TEMPLATE_REF_ROW]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                out[cell.column_letter] = cell.value
        return out

    @staticmethod
    def _template_formats(ws) -> dict[str, str]:
        return {c.column_letter: c.number_format for c in ws[TEMPLATE_REF_ROW]}

    def _write(self, ws, r: int, row: dict, review: set, yellow: list[str], formats: dict) -> None:
        for col, val in row.items():
            cell = ws[f"{col}{r}"]
            cell.value = val
            if col == "A" and isinstance(val, date):
                cell.number_format = "dd/mm/yyyy"
            elif col in formats:
                cell.number_format = formats[col]
            if isinstance(val, str) and "\n" in val:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            if col in review:
                cell.fill = YELLOW_FILL
                yellow.append(f"{ws.title}!{col}{r}")

    def _write_formulas(self, ws, r: int, formulas: dict[str, str], formats: dict) -> None:
        for col, f in formulas.items():
            cell = ws[f"{col}{r}"]
            cell.value = Translator(f, origin=f"{col}{TEMPLATE_REF_ROW}").translate_formula(f"{col}{r}")
            cell.number_format = formats.get(col, cell.number_format)

    def fill(
        self,
        remisiones: list[SourcedDoc],
        liquidaciones: list[SourcedDoc] | None = None,
        basculas: list[SourcedDoc] | None = None,
    ) -> FillResult:
        liquidaciones = liquidaciones or []
        basculas = basculas or []

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            shutil.copyfile(self.template_path, tmp.name)
            tmp_path = tmp.name
        wb = load_workbook(tmp_path)

        res = FillResult(xlsx=b"", yellow_cells=[], exceptions=[])
        traces: list[_Trace] = []

        limon_idx = [i for i, d in enumerate(remisiones) if norm_text(d.value("cultivo")) != "PLATANO"]
        platano_idx = [i for i, d in enumerate(remisiones) if norm_text(d.value("cultivo")) == "PLATANO"]

        self._fill_limon(wb[self.sheet_limon], remisiones, limon_idx, liquidaciones, res, traces)
        self._fill_platano(wb[self.sheet_platano], remisiones, platano_idx, basculas, res, traces)
        self._write_control_sheets(wb, traces, res.exceptions)

        buf = BytesIO()
        wb.save(buf)
        res.xlsx = buf.getvalue()
        return res

    # ---- limón ----

    def _fill_limon(self, ws, remisiones, idxs, liquidaciones, res: FillResult, traces):
        formulas = self._template_formulas(ws)
        formats = self._template_formats(ws)
        exc = res.exceptions
        sheet = ws.title

        target = FIRST_DATA_ROW
        while ws[f"A{target}"].value not in (None, "") or ws[f"C{target}"].value not in (None, ""):
            target += 1

        by_key: dict[tuple, int] = {}  # (prefijo, num) -> fila
        row_trace: dict[int, _Trace] = {}
        row_placa: dict[int, str | None] = {}
        row_kg: dict[int, int | None] = {}
        liquidated: set[int] = set()

        for i in idxs:
            doc = remisiones[i]
            row, review = self._row_from_remision(doc)
            key = rem_key(doc.value("remision_prefijo"), doc.value("remision_num"))
            label = f"{key[0]} {key[1]}" if key else "?"
            if key and key in by_key:
                review.add("C")
                exc.append(Exception_(tipo="Remisión duplicada", detalle=f"{label} ya está en la fila {by_key[key]}",
                                      archivo=doc.filename, hoja=sheet, fila=target))
            elif key:
                by_key[key] = target
            else:
                exc.append(Exception_(tipo="Remisión sin número", detalle="No se pudo leer prefijo/número de remisión",
                                      archivo=doc.filename, hoja=sheet, fila=target))
            self._write(ws, target, row, review, res.yellow_cells, formats)
            self._write_formulas(ws, target, formulas, formats)
            res.remision_pos[i] = (sheet, target)
            t = _Trace(hoja=sheet, fila=target, remision=label, archivo_remision=doc.filename)
            traces.append(t)
            row_trace[target] = t
            row_placa[target] = row.get("E")
            row_kg[target] = row.get("G")
            target += 1

        for j, liq in enumerate(liquidaciones):
            key = rem_key(liq.value("liq_remision_prefijo"), liq.value("liq_remision_num"))
            label = f"{key[0]} {key[1]}" if key else "?"
            placa = norm_placa(liq.value("liq_placa"))
            r = by_key.get(key) if key else None
            matched_by = "remisión"
            if r is None and placa:
                # Fallback por placa solo si es UNA única remisión sin liquidar con esa placa.
                cands = [x for x, p in row_placa.items() if p == placa and x not in liquidated]
                if len(cands) == 1:
                    r, matched_by = cands[0], "placa"
            if r is not None and r in liquidated:
                exc.append(Exception_(tipo="Liquidación duplicada", detalle=f"{label}: la fila {r} ya tiene liquidación",
                                      archivo=liq.filename, hoja=sheet, fila=r))
                continue

            row, review = self._row_from_liquidacion(liq)
            if r is None:
                r = target
                target += 1
                exc.append(Exception_(tipo="Liquidación sin remisión",
                                      detalle=f"{label} (placa {placa or '?'}) no cruza con ninguna remisión cargada",
                                      archivo=liq.filename, hoja=sheet, fila=r))
                row["C"] = key[1] if key else None
                row["E"] = placa
                fecha = parse_fecha(liq.value("liq_fecha"))
                row["A"], row["B"] = fecha, (semana(fecha) if fecha else None)
                review.update({"A", "B", "C", "D", "E", "F", "G", "H", LIMON_LOTE_COL})
                t = _Trace(hoja=sheet, fila=r, remision=label, archivo_liquidacion=liq.filename,
                           estado="Liquidación sin remisión")
                traces.append(t)
                self._write_formulas(ws, r, formulas, formats)
            else:
                t = row_trace[r]
                t.archivo_liquidacion = liq.filename
                if matched_by == "placa":
                    t.estado = "Cruzada por placa (verificar nº remisión)"
                    exc.append(Exception_(tipo="Cruce por placa", detalle=f"Liquidación {label} unida por placa {placa}",
                                          archivo=liq.filename, hoja=sheet, fila=r))
                if placa and row_placa.get(r) and placa != row_placa[r]:
                    ws[f"E{r}"].fill = YELLOW_FILL
                    res.yellow_cells.append(f"{sheet}!E{r}")
                    t.estado = "Placa no coincide"
                    exc.append(Exception_(tipo="Placa no coincide",
                                          detalle=f"Remisión {row_placa[r]} vs liquidación {placa}",
                                          archivo=liq.filename, hoja=sheet, fila=r))
                kg_rem, kg_liq = row_kg.get(r), self.liq_kg_total(liq)
                if kg_rem and kg_liq is not None and abs(kg_rem - kg_liq) / kg_rem > settings.kg_diff_tolerance:
                    exc.append(Exception_(tipo="Diferencia kg",
                                          detalle=f"Despachados {kg_rem} vs recibidos {kg_liq} ({(kg_rem - kg_liq) / kg_rem:.1%})",
                                          archivo=liq.filename, hoja=sheet, fila=r))
            self._write(ws, r, row, review, res.yellow_cells, formats)
            liquidated.add(r)
            res.liquidacion_pos[j] = (sheet, r)

        for r, t in row_trace.items():
            if r not in liquidated:
                t.estado = "Sin liquidación"
                exc.append(Exception_(tipo="Remisión sin liquidación", detalle=f"{t.remision} aún sin liquidación",
                                      archivo=t.archivo_remision, hoja=sheet, fila=r))

    # ---- plátano ----

    def _fill_platano(self, ws, remisiones, idxs, basculas, res: FillResult, traces):
        exc = res.exceptions
        sheet = ws.title
        formats = self._template_formats(ws)

        # Entregas de báscula: (doc_idx, finca, kg, canastillas, placa, cliente)
        entregas = []
        for b, doc in enumerate(basculas):
            items = doc.value("bas_entregas")
            if not isinstance(items, list) or not items:
                exc.append(Exception_(tipo="Báscula ilegible", detalle="No se leyeron entregas (finca/kg)",
                                      archivo=doc.filename, hoja=sheet))
                continue
            for it in items:
                if isinstance(it, dict):
                    entregas.append({"b": b, "finca": norm_text(it.get("finca")), "kg": to_int(it.get("kg")),
                                     "placa": norm_placa(doc.value("bas_placa")), "used": False})

        def find_entrega(finca, placa):
            cands = [e for e in entregas if not e["used"] and e["finca"] and finca
                     and (e["finca"] in finca or finca in e["finca"])]
            if placa:
                with_placa = [e for e in cands if e["placa"] == placa]
                if with_placa:
                    cands = with_placa
            return cands[0] if len(cands) == 1 or (cands and placa) else None

        target = FIRST_DATA_ROW
        while ws[f"A{target}"].value not in (None, "") or ws[f"C{target}"].value not in (None, ""):
            target += 1
        seen: dict[tuple, int] = {}

        for i in idxs:
            doc = remisiones[i]
            row: dict[str, object] = {}
            review: set[str] = set()
            self._fecha_sem(doc, row, review)
            key = rem_key(doc.value("remision_prefijo"), doc.value("remision_num"))
            label = f"{key[0]} {key[1]}" if key else "?"
            if key in seen:
                review.add("C")
                exc.append(Exception_(tipo="Remisión duplicada", detalle=f"{label} ya está en la fila {seen[key]}",
                                      archivo=doc.filename, hoja=sheet, fila=target))
            elif key:
                seen[key] = target
            for name, col in (("remision_num", "C"), ("cliente", "D"), ("placa", "E"), ("variedad", "F"),
                              ("canastillas", "J")):
                raw = doc.value(name)
                v = to_int(raw) if name in NUMERIC_FIELDS else (norm_placa(raw) if name == "placa" else raw)
                row[col] = v
                if self._review(doc, name, v):
                    review.add(col)

            t = _Trace(hoja=sheet, fila=target, remision=label, archivo_remision=doc.filename)
            kg_rem = to_int(doc.value("kg_despachados"))
            e = find_entrega(norm_text(doc.value("finca")), row.get("E"))
            if e:
                e["used"] = True
                row["G"] = e["kg"]
                t.archivo_liquidacion = basculas[e["b"]].filename
                res.bascula_pos.setdefault(e["b"], (sheet, target))
                f_ent = basculas[e["b"]].get("bas_entregas")
                if e["kg"] is None or (f_ent and f_ent.needs_review):
                    review.add("G")
                if kg_rem and e["kg"] is not None and kg_rem != e["kg"]:
                    review.add("G")
                    t.estado = "Diferencia kg"
                    exc.append(Exception_(tipo="Diferencia kg", detalle=f"Remisión {kg_rem} vs báscula {e['kg']}",
                                          archivo=t.archivo_liquidacion, hoja=sheet, fila=target))
            else:
                row["G"] = kg_rem
                review.add("G")
                t.estado = "Sin liquidación"
                exc.append(Exception_(tipo="Remisión sin liquidación",
                                      detalle=f"{label}: kilos tomados de la remisión, sin báscula que lo confirme",
                                      archivo=doc.filename, hoja=sheet, fila=target))

            precio = settings.precio_platano(row.get("D"))
            row["H"] = precio
            if precio is None:
                review.add("H")
            row["I"] = f"=G{target}*H{target}"
            lotes, lotes_review = format_lotes(doc.value("lotes"))
            row["K"] = lotes
            if lotes_review:
                review.add("K")

            self._write(ws, target, row, review, res.yellow_cells, formats)
            res.remision_pos[i] = (sheet, target)
            traces.append(t)
            target += 1

        for e in entregas:
            if not e["used"]:
                exc.append(Exception_(tipo="Liquidación sin remisión",
                                      detalle=f"Báscula {e['finca']} {e['kg']} kg sin remisión de plátano que cruce",
                                      archivo=basculas[e["b"]].filename, hoja=sheet))

    # ---- control ----

    @staticmethod
    def _write_control_sheets(wb, traces: list[_Trace], exceptions: list[Exception_]) -> None:
        bold = Font(bold=True)
        ws = wb.create_sheet("Trazabilidad")
        ws.append(["Hoja", "Fila", "Remisión", "Archivo remisión", "Archivo liquidación", "Estado"])
        for t in traces:
            ws.append([t.hoja, t.fila, t.remision, t.archivo_remision, t.archivo_liquidacion, t.estado])
        ws2 = wb.create_sheet("Excepciones")
        ws2.append(["Tipo", "Detalle", "Archivo", "Hoja", "Fila"])
        for e in exceptions:
            ws2.append([e.tipo, e.detalle, e.archivo, e.hoja, e.fila])
        for s in (ws, ws2):
            for c in s[1]:
                c.font = bold
            for col, w in zip("ABCDEF", (24, 8, 40, 45, 45, 30)):
                s.column_dimensions[col].width = w
