"""
POC OCR -> Excel (TechApoli / liquidaciones de fruta).

Flujo:
  1. OCR de la remision de finca (PDF escaneado) con OpenAI vision.
  2. Mapea esos datos a las columnas A-H + Lote de la hoja "Plantilla Limon".
  3. OCR del "Control Diario de Trabajo" (imagen) -> se vuelca a output/control_diario.json
     (no se mapea al Excel todavia; falta definir ese proceso).
  4. Escribe una fila NUEVA en una copia del template, conservando formato/formulas.

NOTA: es una POC, un solo archivo, sin pretensiones de arquitectura. Luego se reestructura.
"""

import base64
import json
import os
import shutil
import sys
from datetime import date

import pymupdf  # PyMuPDF (antes se importaba como "fitz")
from dotenv import load_dotenv
from openai import OpenAI
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

# En redes con proxy/antivirus corporativo, OpenSSL rechaza el certificado inyectado.
# truststore hace que Python use el almacen de certificados de Windows (donde suele
# estar el CA corporativo), sin desactivar la verificacion TLS.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "docs")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

REMISION_PDF = os.path.join(DOCS_DIR, "CAMELIA LIMON RM-124.pdf")
CONTROL_IMG = os.path.join(DOCS_DIR, "WhatsApp Image 2026-09-29 at 15.39.05.jpeg")
TEMPLATE_XLSX = os.path.join(DOCS_DIR, "Plantillas Liquidaciones de Fruta.xlsx")

OUTPUT_XLSX = os.path.join(OUTPUT_DIR, "Plantillas Liquidaciones de Fruta - OUTPUT.xlsx")
CONTROL_JSON = os.path.join(OUTPUT_DIR, "control_diario.json")

SHEET_NAME = "Plantilla Limón"


# ---------------------------------------------------------------------------
# Utilidades de imagen / OpenAI
# ---------------------------------------------------------------------------
def pdf_to_png_b64(pdf_path, dpi=200):
    """Renderiza cada pagina del PDF a PNG y devuelve lista de base64."""
    out = []
    doc = pymupdf.open(pdf_path)
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            out.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
    finally:
        doc.close()
    return out


def image_to_b64(img_path):
    with open(img_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def ask_vision(client, model, system_prompt, user_text, images_b64):
    """Llama al modelo de vision y devuelve un dict (JSON parseado)."""
    content = [{"type": "text", "text": user_text}]
    for b64 in images_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
            }
        )
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    )
    return json.loads(resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# Extraccion
# ---------------------------------------------------------------------------
REMISION_SYSTEM = (
    "Eres un asistente de digitacion de documentos agricolas colombianos. "
    "Lees remisiones de finca escaneadas (texto impreso + manuscrito) y devuelves "
    "SIEMPRE un objeto JSON con exactamente estas claves. Para CADA campo devuelves un "
    "objeto {\"valor\": ..., \"confianza\": <entero 0-100>}, donde 0 = nada seguro y "
    "100 = totalmente seguro. Usa valor null y confianza 0 si el dato no aparece o es "
    "ilegible. Se honesto: si la escritura es dudosa, esta borrosa o podrias estar "
    "adivinando, baja la confianza (p.ej. 30-60) para que un humano lo revise. "
    "Guia: 0-40 baja, 41-69 media, 70-100 alta."
)

REMISION_PROMPT = """Esta es una REMISION de finca. Extrae estos campos y devuelve SOLO JSON.
Cada campo es un objeto {"valor": ..., "confianza": <entero 0-100>} (0=inseguro, 100=seguro):

{
  "remision_num":   {"valor": <int>,    "confianza": <0-100>},  // "CA 124"/"RM-124" -> 124
  "fecha":          {"valor": "<YYYY-MM-DD>", "confianza": <0-100>}, // "15/09/26" -> "2026-09-15"
  "cliente":        {"valor": "<str MAYUS>", "confianza": <0-100>},  // SEÑOR(ES). "RAMAFRUTS"
  "placa":          {"valor": "<str sin guiones/espacios MAYUS>", "confianza": <0-100>}, // "JVM-364"->"JVM364"
  "variedad":       {"valor": "<str MAYUS>", "confianza": <0-100>},  // "LIMON TAHITI"
  "kg_despachados": {"valor": <int>,    "confianza": <0-100>},  // CANTIDAD kg. "10 220" -> 10220
  "canastillas":    {"valor": <int>,    "confianza": <0-100>},  // "480 Canastillas..." -> 480
  "lote":           {"valor": <int>,    "confianza": <0-100>}   // OBSERVACIONES. "Lote 5" -> 5
}

Devuelve numeros como numeros (no strings). No incluyas texto fuera del JSON."""

CONTROL_SYSTEM = (
    "Eres un asistente de digitacion. Lees planillas manuscritas colombianas de "
    "'Control Diario de Trabajo' y devuelves SIEMPRE un objeto JSON. Usa null si falta un dato."
)

CONTROL_PROMPT = """Esta es una planilla manuscrita 'CONTROL DIARIO DE TRABAJO'. Devuelve SOLO JSON:

{
  "finca": "<str>",
  "anio": <int>,
  "semana": <int>,
  "dia": "<str>",
  "trabajadores": [
    {
      "nombre": "<str>",
      "cedula": "<str>",
      "labor": "<str>",
      "cultivo": "<str>",
      "lote": "<str>",
      "jornal": "<str>",
      "cant_contrato": "<str>",
      "vr_unitario": "<str>",
      "valor_total": "<str>"
    }
  ]
}

Incluye una fila por cada trabajador legible. No inventes datos."""


def extract_remision(client, model):
    imgs = pdf_to_png_b64(REMISION_PDF)
    return ask_vision(client, model, REMISION_SYSTEM, REMISION_PROMPT, imgs)


def extract_control(client, model):
    imgs = [image_to_b64(CONTROL_IMG)]
    return ask_vision(client, model, CONTROL_SYSTEM, CONTROL_PROMPT, imgs)


# ---------------------------------------------------------------------------
# Mapeo remision -> fila Excel
# ---------------------------------------------------------------------------
def _to_int(v):
    """Limpia '10 220', '$1.200', '480' -> int. Devuelve None si no se puede."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) if digits else None


def parse_fecha(v):
    if not v:
        return None
    try:
        y, m, d = (int(x) for x in str(v).split("-"))
        return date(y, m, d)
    except Exception:
        return None


# Umbrales de confianza (0-100): 0-40 baja, 41-69 media, 70-100 alta.
# Se pintan de amarillo (revision humana) las celdas por DEBAJO de este valor.
CONF_UMBRAL_REVISION = 70


def banda(conf):
    """Nombre de la banda segun el score 0-100."""
    if conf <= 40:
        return "baja"
    if conf <= 69:
        return "media"
    return "alta"


def _field(rem, key):
    """Devuelve (valor, confianza 0-100) tolerando campo plano o {valor,confianza}."""
    f = rem.get(key)
    if isinstance(f, dict):
        val = f.get("valor")
        try:
            conf = int(round(float(f.get("confianza"))))
        except (TypeError, ValueError):
            conf = 0  # sin confianza numerica -> tratar como baja
        conf = max(0, min(100, conf))
        return val, conf
    # fallback: campo plano sin confianza -> 100 si trae dato, 0 si viene null
    return f, (100 if f not in (None, "") else 0)


def build_row(rem):
    """Devuelve (row, review) donde row={columna: valor} y review=set de columnas inciertas."""
    row, review = {}, set()

    # Mapeo campo-remision -> (columna, transformacion)
    mapping = [
        ("remision_num", "C", _to_int),
        ("cliente", "D", lambda v: v),
        ("placa", "E", lambda v: v),
        ("variedad", "F", lambda v: v),
        ("kg_despachados", "G", _to_int),
        ("canastillas", "H", _to_int),
        ("lote", "V", _to_int),
    ]

    # Fecha (A) y Sem (B, derivada de la fecha)
    raw_fecha, conf_fecha = _field(rem, "fecha")
    fecha = parse_fecha(raw_fecha)
    # La finca NO usa semana ISO: para 2026-09-15 el ejemplo tiene Sem=37 (ISO daria 38).
    # %U (domingo como primer dia) reproduce ese 37.
    sem = int(fecha.strftime("%U")) if fecha else None
    row["A"], row["B"] = fecha, sem
    if conf_fecha < CONF_UMBRAL_REVISION or fecha is None:
        review.update({"A", "B"})  # si la fecha es dudosa, la semana derivada tambien

    for key, col, fn in mapping:
        raw, conf = _field(rem, key)
        val = fn(raw)
        row[col] = val
        if conf < CONF_UMBRAL_REVISION or val in (None, ""):
            review.add(col)

    return row, review


# ---------------------------------------------------------------------------
# Escritura Excel
# ---------------------------------------------------------------------------
YELLOW_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")


def write_excel(row, review=None):
    review = review or set()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    shutil.copyfile(TEMPLATE_XLSX, OUTPUT_XLSX)  # no tocamos el template original

    wb = load_workbook(OUTPUT_XLSX)
    ws = wb[SHEET_NAME]

    # Datos reales empiezan en la fila 3 (fila 2 = encabezados). Buscamos la 1a fila
    # libre a partir de la 3 mirando la columna A (Fecha).
    target = 3
    while ws[f"A{target}"].value not in (None, ""):
        target += 1

    for col, val in row.items():
        cell = ws[f"{col}{target}"]
        cell.value = val
        if col == "A" and val is not None:
            cell.number_format = "dd/mm/yyyy"
        if col in review:
            cell.fill = YELLOW_FILL  # baja confianza -> revisar manualmente

    wb.save(OUTPUT_XLSX)
    return target


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: falta OPENAI_API_KEY. Copia .env.example a .env y pon tu API key."
        )
    model = os.getenv("OCR_MODEL", "gpt-4o")
    client = OpenAI(api_key=api_key)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[1/3] OCR remision ({os.path.basename(REMISION_PDF)}) con modelo {model}...")
    rem = extract_remision(client, model)
    print("      Remision extraida:")
    print(json.dumps(rem, ensure_ascii=False, indent=2))

    print("[2/3] OCR Control Diario de Trabajo (imagen)...")
    control = extract_control(client, model)
    with open(CONTROL_JSON, "w", encoding="utf-8") as f:
        json.dump(control, f, ensure_ascii=False, indent=2)
    n = len(control.get("trabajadores") or [])
    print(f"      Guardado -> {CONTROL_JSON} ({n} trabajadores)")

    print("[3/3] Escribiendo fila en el Excel...")
    row, review = build_row(rem)
    fila = write_excel(row, review)
    print(f"      Fila {fila} escrita (A-H + Lote) -> {OUTPUT_XLSX}")
    if review:
        print(
            f"      ⚠ {len(review)} celda(s) con baja confianza pintadas de AMARILLO "
            f"para revision humana: {', '.join(sorted(review))}"
        )
    else:
        print("      Todas las celdas con confianza alta.")
    print("\nColumnas I-U (liquidacion de planta) quedan vacias: falta ese documento.")
    print("Listo.")


if __name__ == "__main__":
    main()
