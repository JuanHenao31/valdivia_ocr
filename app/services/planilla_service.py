"""Proceso 1 — planillas "Control Diario de Trabajo" -> Excel 1 (carga al CRM, "Mano de obra").

Excel generado de cero (no hay plantilla): una fila por trabajador-labor con las columnas que
pide el formulario del CRM (Fecha labor, Personal, Lote, Labor, Modalidad, Jor, Rend, Und,
Vr Unit, Total, Observaciones) + Finca, Cédula y Archivo origen (trazabilidad).

Reglas:
- Fecha labor = Año + Semana (%U, domingo primer día) + día de la semana. Si no hay semana,
  NO se adivina el mes: queda vacía y amarilla.
- Modalidad = "Contrato" si hay cantidad a contrato ("11 hectáreas x 85.000"), si no "Jornal".
- Total = fórmula (Rend x Vr Unit en contrato, Jor x Vr Unit en jornal). Si difiere del total
  escrito en la planilla, se marca amarillo.
- "?" o ilegible -> vacío + amarillo. Duplicados (cédula + fecha + labor) -> señalados.
- Finca y Labor se contrastan con el diccionario de Valdivia (app/data/labores_por_finca.json):
  coincidencia alta -> nombre oficial; media -> oficial en amarillo; baja -> lo leído en amarillo.
"""

import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from app.core.config import settings
from app.domain.models import Exception_, OCRResult
from app.infrastructure.files import UploadedDoc
from app.services.catalog_service import DEFAULT_CATALOG, LaborCatalog
from app.services.excel_service import YELLOW_FILL, to_int
from app.services.ocr_service import OCRService
from app.services.validation_service import JevValidationService

COLUMNS = [
    ("fecha", "Fecha labor"),
    ("finca", "Finca"),
    ("personal", "Personal"),
    ("cedula", "Cédula"),
    ("lote", "Lote"),
    ("labor", "Labor"),
    ("modalidad", "Modalidad"),
    ("jor", "Jor"),
    ("rend", "Rend"),
    ("und", "Und"),
    ("vr_unit", "Vr Unit"),
    ("total", "Total"),
    ("observaciones", "Observaciones"),
    ("archivo", "Archivo origen"),
]
COL = {key: chr(ord("A") + i) for i, (key, _) in enumerate(COLUMNS)}

SYSTEM_PROMPT = (
    "Eres un asistente de digitación de planillas manuscritas de trabajadores de fincas "
    "colombianas (empresa Valdivia). Devuelves SIEMPRE JSON. NUNCA inventes valores: si algo "
    "no aparece, es '?' o no es legible, usa null y confianza baja. Confianza 0-100 honesta."
)

USER_PROMPT = """La imagen es una planilla "CONTROL DIARIO DE TRABAJO". Devuelve SOLO:

{
  "raw_text": "<transcripción fiel de todo el texto>",
  "finca":   {"valor": "<str MAYUS>", "confianza": <0-100>},
  "anio":    {"valor": <int>, "confianza": <0-100>},
  "semana":  {"valor": <int|null>, "confianza": <0-100>},   // SOLO si está escrito un número de semana
  "dia_semana": {"valor": "LUNES|MARTES|MIERCOLES|JUEVES|VIERNES|SABADO|DOMINGO|null", "confianza": <0-100>},
  "dia_mes": {"valor": <int|null>, "confianza": <0-100>},   // "Viernes 23" -> 23
  "nota":    {"valor": "<texto adicional escrito en la hoja, ej. 'Solo plátano'>", "confianza": <0-100>},
  "filas": [
    {
      "nombre":   {"valor": "<str>", "confianza": <0-100>},
      "cedula":   {"valor": "<solo dígitos>", "confianza": <0-100>},
      "labor":    {"valor": "<str>", "confianza": <0-100>},
      "cultivo":  {"valor": "<str|null>", "confianza": <0-100>},
      "lote":     {"valor": "<str|null>", "confianza": <0-100>},
      "jor":      {"valor": <number|null>, "confianza": <0-100>},  // columna JOR (jornales)
      "rend":     {"valor": <number|null>, "confianza": <0-100>},  // cantidad a contrato: "11x85000" -> 11
      "und":      {"valor": "<str|null>", "confianza": <0-100>},   // unidad del contrato: "Hectáreas"
      "vr_unit":  {"valor": <int|null>, "confianza": <0-100>},     // "11x85000" -> 85000; jornal "63.000" -> 63000
      "total_escrito": {"valor": <int|null>, "confianza": <0-100>} // total escrito: "=935.000" -> 935000
    }
  ]
}

Notas: a veces escriben el día en la casilla equivocada (ej. "SEMANA: Martes 20"): eso es
dia_semana=MARTES, dia_mes=20 y semana=null. Las columnas CANT CONTRATO / VLR UNITARIO suelen tener la expresión "A x B = C":
rend=A, vr_unit=B, total_escrito=C. Si solo hay un valor (jornal, ej. "63.000") es vr_unit
y total_escrito. Si VLR UNITARIO tiene "?" -> null. Una fila por trabajador escrito; ignora
filas vacías. Números como números."""

DIAS = ["DOMINGO", "LUNES", "MARTES", "MIERCOLES", "JUEVES", "VIERNES", "SABADO"]  # orden %U


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def fecha_from_semana(anio, semana, dia_semana) -> date | None:
    """Año + semana %U (domingo primer día) + día -> fecha. Inversa de strftime('%U')."""
    anio, semana = to_int(anio), to_int(semana)
    if not anio or semana is None or not dia_semana:
        return None
    dia = _strip_accents(str(dia_semana)).upper().strip()
    if dia not in DIAS:
        return None
    jan1 = date(anio, 1, 1)
    first_sunday = jan1 + timedelta(days=(6 - jan1.weekday()) % 7)  # semana 1 arranca aquí
    sunday = first_sunday + timedelta(weeks=semana - 1)
    return sunday + timedelta(days=DIAS.index(dia))


def _val(d: dict | None):
    if not isinstance(d, dict):
        return None
    v = d.get("valor")
    return None if v in ("", "?") else v


def _conf(d: dict | None) -> int:
    try:
        return int((d or {}).get("confianza") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class PlanillaResult:
    xlsx: bytes
    rows: int
    yellow_cells: list[str]
    exceptions: list[Exception_]


class PlanillaService:
    catalog: LaborCatalog = DEFAULT_CATALOG

    def __init__(self) -> None:
        self.ocr = OCRService()
        self.jev = JevValidationService()

    def _jev_supports(self, raw_text: str, flat: dict) -> dict[str, float]:
        """Valida con Jev cada campo (aplanado 'f{i}_{campo}') contra el raw_text."""
        ocr = OCRResult(tipo="control_diario", raw_text=raw_text,
                        fields={k: {"valor": v} for k, v in flat.items()})
        v = self.jev.validate(ocr)
        return v.supports if v.enabled else {}

    def build_rows(self, data: dict, filename: str) -> tuple[list[dict], list[set], list[Exception_]]:
        """JSON del OCR -> filas para el Excel 1 + columnas a revisar por fila (puro, testeable)."""
        exc: list[Exception_] = []
        thr = settings.ocr_conf_threshold
        finca = _val(data.get("finca"))
        anio, sem = _val(data.get("anio")), _val(data.get("semana"))
        dia_sem, dia_mes = _val(data.get("dia_semana")), to_int(_val(data.get("dia_mes")))

        fecha = fecha_from_semana(anio, sem, dia_sem)
        fecha_review = fecha is None or min(_conf(data.get("anio")), _conf(data.get("semana"))) < thr
        if fecha is None:
            exc.append(Exception_(tipo="Fecha incompleta",
                                  detalle=f"Año={anio} Semana={sem} Día={dia_sem} {dia_mes or ''}: no se puede fijar la fecha sin adivinar",
                                  archivo=filename))
        elif dia_mes and fecha.day != dia_mes:
            fecha_review = True
            exc.append(Exception_(tipo="Fecha inconsistente",
                                  detalle=f"Semana {sem} + {dia_sem} = {fecha:%d/%m/%Y}, pero la planilla dice día {dia_mes}",
                                  archivo=filename))

        finca, finca_oficial, finca_review = self._check_finca(finca, filename, exc)

        nota = _val(data.get("nota"))
        rows, reviews = [], []
        for f in data.get("filas") or []:
            review: set[str] = set()
            get = lambda k: _val(f.get(k))  # noqa: E731
            low = lambda k: _conf(f.get(k)) < thr  # noqa: E731

            rend, jor, vr_unit = get("rend"), get("jor"), to_int(get("vr_unit"))
            total_escrito = to_int(get("total_escrito"))
            modalidad = "Contrato" if rend not in (None, 0) else "Jornal"
            cantidad = rend if modalidad == "Contrato" else jor

            row = {
                "fecha": fecha,
                "finca": finca,
                "personal": get("nombre"),
                "cedula": "".join(ch for ch in str(get("cedula") or "") if ch.isdigit()) or None,
                "lote": get("lote"),
                "labor": get("labor"),
                "modalidad": modalidad,
                "jor": jor,
                "rend": rend,
                "und": get("und"),
                "vr_unit": vr_unit,
                "total": None,  # fórmula al escribir
                "observaciones": " | ".join(x for x in (nota, get("cultivo")) if x) or None,
                "archivo": filename,
                "_total_escrito": total_escrito,
                "_cantidad": cantidad,
            }
            for key, src in (("personal", "nombre"), ("cedula", "cedula"), ("labor", "labor"),
                             ("jor", "jor"), ("vr_unit", "vr_unit")):
                if row[key] in (None, "") or low(src):
                    review.add(key)
            if modalidad == "Contrato":
                for key in ("rend", "und"):
                    if row[key] in (None, "") or low(key):
                        review.add(key)
                review.discard("jor")  # en contrato la cantidad está en Rend, no en Jor
            if fecha_review:
                review.add("fecha")
            try:
                calc = float(cantidad) * vr_unit if cantidad is not None and vr_unit is not None else None
            except (TypeError, ValueError):
                calc = None
            if calc is None or (total_escrito is not None and abs(calc - total_escrito) > 1):
                review.add("total")
                if calc is not None:
                    exc.append(Exception_(tipo="Total no cuadra",
                                          detalle=f"{row['personal']}: {cantidad} x {vr_unit} = {calc:,.0f} vs escrito {total_escrito:,}",
                                          archivo=filename))
            if finca_review:
                review.add("finca")
            self._check_labor(row, finca_oficial, review, exc, filename)
            rows.append(row)
            reviews.append(review)
        return rows, reviews, exc

    def _check_finca(self, finca, filename: str, exc: list) -> tuple:
        """-> (finca a escribir, finca oficial o None, ¿amarilla?)."""
        if not finca:
            return finca, None, True
        if not self.catalog.enabled:
            return finca, None, False
        m = self.catalog.match_finca(finca)
        if m.score >= settings.labor_match_ok:
            return m.oficial, m.oficial, False
        if m.score >= settings.labor_match_review:
            exc.append(Exception_(tipo="Finca dudosa",
                                  detalle=f"OCR leyó '{finca}', se asumió '{m.oficial}' ({m.score}%)",
                                  archivo=filename))
            return m.oficial, m.oficial, True
        exc.append(Exception_(tipo="Finca no está en el diccionario",
                              detalle=f"'{finca}' (más parecida: {m.oficial} {m.score}%); labores validadas contra el listado general",
                              archivo=filename))
        return finca, None, True

    def _check_labor(self, row: dict, finca_oficial, review: set, exc: list, filename: str) -> None:
        leida = row["labor"]
        row["_labor_ocr"] = leida
        if not leida or not self.catalog.enabled:
            return
        m = self.catalog.match_labor(finca_oficial, leida)
        donde = f"en {finca_oficial}" if finca_oficial else "en el diccionario general"
        if m.score >= settings.labor_match_ok:
            row["labor"] = m.oficial
            return
        review.add("labor")
        if m.score >= settings.labor_match_review:
            row["labor"] = m.oficial
            row["observaciones"] = " | ".join(x for x in (row["observaciones"], f"OCR leyó: {leida}") if x)
            exc.append(Exception_(tipo="Labor dudosa",
                                  detalle=f"{row['personal']}: OCR leyó '{leida}', se asumió '{m.oficial}' ({m.score}%) {donde}",
                                  archivo=filename))
        else:
            exc.append(Exception_(tipo="Labor no existe en finca",
                                  detalle=f"{row['personal']}: '{leida}' no está {donde} (más parecida: {m.oficial} {m.score}%)",
                                  archivo=filename))

    @staticmethod
    def _same(a, b) -> bool:
        if a in (None, "") and b in (None, ""):
            return True
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            norm = lambda x: _strip_accents(str(x)).upper().replace(" ", "")  # noqa: E731
            return norm(a) == norm(b)

    def reconcile(self, a: tuple, b: tuple, filename: str):
        """Doble lectura: base = la lectura con más filas; cada fila se empareja con la otra
        lectura por cédula (o posición) y los campos que difieren se marcan en amarillo."""
        (rows_a, rev_a, exc_a), (rows_b, _, _) = a, b
        if len(rows_b) > len(rows_a):
            (rows_a, rev_a, exc_a), (rows_b, _, _) = b, a
        exc = list(exc_a)
        if not rows_b:
            exc.append(Exception_(tipo="Lectura única",
                                  detalle="La segunda lectura no devolvió filas; no se pudo contrastar",
                                  archivo=filename))
            return rows_a, rev_a, exc
        if len(rows_a) != len(rows_b):
            exc.append(Exception_(tipo="Lecturas difieren",
                                  detalle=f"Una lectura encontró {len(rows_a)} filas y la otra {len(rows_b)}",
                                  archivo=filename))
        by_ced = {r["cedula"]: r for r in rows_b if r["cedula"]}
        for i, (row, review) in enumerate(zip(rows_a, rev_a)):
            other = by_ced.get(row["cedula"]) or (rows_b[i] if i < len(rows_b) else None)
            if other is None:
                review.update({"personal", "cedula"})
                continue
            for key in ("fecha", "cedula", "lote", "jor", "rend", "und", "vr_unit", "modalidad"):
                if not self._same(row[key], other[key]):
                    review.add("total" if key == "modalidad" else key)
        return rows_a, rev_a, exc

    def process(self, docs: list[UploadedDoc]) -> PlanillaResult:
        all_rows, all_reviews, exceptions = [], [], []
        model = settings.planillas_model
        for doc in docs:
            data = self.ocr.extract_json(doc.content, SYSTEM_PROMPT, USER_PROMPT, model=model)
            rows, reviews, exc = self.build_rows(data, doc.filename)
            if settings.planilla_double_read:
                data2 = self.ocr.extract_json(doc.content, SYSTEM_PROMPT, USER_PROMPT, model=model)
                second = self.build_rows(data2, doc.filename)
                if len(second[0]) > len(rows):
                    data = data2  # raw_text de la lectura usada como base (para Jev)
                rows, reviews, exc = self.reconcile((rows, reviews, exc), second, doc.filename)
            # Jev: soporte por campo de cada fila.
            # Labor: se valida lo que leyó el OCR, no el nombre oficial del diccionario.
            flat = {f"f{i}_{k}": r.get("_labor_ocr") if k == "labor" else r[k] for i, r in enumerate(rows)
                    for k in ("personal", "cedula", "labor", "vr_unit", "rend") if r[k] not in (None, "")}
            if flat:
                for key, sup in self._jev_supports(data.get("raw_text") or "", flat).items():
                    if sup < settings.jev_support_threshold:
                        i, k = key[1:].split("_", 1)
                        reviews[int(i)].add(k)
            all_rows += rows
            all_reviews += reviews
            exceptions += exc
        return self.to_excel(all_rows, all_reviews, exceptions)

    @staticmethod
    def to_excel(rows: list[dict], reviews: list[set], exceptions: list[Exception_]) -> PlanillaResult:
        wb = Workbook()
        ws = wb.active
        ws.title = "Mano de obra"
        ws.append([title for _, title in COLUMNS])
        for c in ws[1]:
            c.font = Font(bold=True)

        yellow: list[str] = []
        seen: dict[tuple, int] = {}
        for idx, (row, review) in enumerate(zip(rows, reviews)):
            r = idx + 2
            for key, _ in COLUMNS:
                cell = ws[f"{COL[key]}{r}"]
                cell.value = row.get(key)
            q = COL["rend"] if row["modalidad"] == "Contrato" else COL["jor"]
            ws[f"{COL['total']}{r}"] = f"={q}{r}*{COL['vr_unit']}{r}"
            ws[f"{COL['fecha']}{r}"].number_format = "dd/mm/yyyy"
            for key in ("vr_unit", "total"):
                ws[f"{COL[key]}{r}"].number_format = '"$"#,##0'
            dup_key = (row["cedula"], row["fecha"], (row["labor"] or "").upper())
            if row["cedula"] and dup_key in seen:
                review.add("cedula")
                exceptions.append(Exception_(tipo="Posible duplicado",
                                             detalle=f"{row['personal']} ({row['cedula']}) misma fecha y labor que la fila {seen[dup_key]}",
                                             archivo=row["archivo"], hoja=ws.title, fila=r))
            else:
                seen[dup_key] = r
            for key in review:
                ws[f"{COL[key]}{r}"].fill = YELLOW_FILL
                yellow.append(f"{COL[key]}{r}")

        for col, w in zip("ABCDEFGHIJKLMN", (12, 12, 30, 14, 8, 22, 11, 6, 8, 12, 12, 14, 30, 40)):
            ws.column_dimensions[col].width = w

        ws2 = wb.create_sheet("Excepciones")
        ws2.append(["Tipo", "Detalle", "Archivo", "Hoja", "Fila"])
        for c in ws2[1]:
            c.font = Font(bold=True)
        for e in exceptions:
            ws2.append([e.tipo, e.detalle, e.archivo, e.hoja, e.fila])

        buf = BytesIO()
        wb.save(buf)
        return PlanillaResult(xlsx=buf.getvalue(), rows=len(rows), yellow_cells=yellow, exceptions=exceptions)
