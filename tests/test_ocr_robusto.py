"""OCR robusto: Structured Outputs strict, topes de tokens, reintentos y semáforo global."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.infrastructure.files import UploadedDoc
from app.services import ocr_service as mod
from app.services.ocr_service import (
    DOCUMENTOS_SCHEMA,
    PLANILLA_SCHEMA,
    OCRError,
    OCRService,
    OCRTruncadoError,
)
from app.services.planilla_service import USER_PROMPT as PLANILLA_PROMPT
from app.services.planilla_service import PlanillaService

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _chunk(content=None, finish=None, refusal=None, usage=None, choices=True):
    ch = [SimpleNamespace(index=0, delta=SimpleNamespace(content=content, refusal=refusal),
                          finish_reason=finish)] if choices else []
    return SimpleNamespace(choices=ch, usage=usage)


class FakeStream:
    """Imita openai.Stream: iterable de chunks con close()."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for c in self.chunks:
            if self.closed:
                return
            yield c

    def close(self):
        self.closed = True


def _resp(content: str, finish="stop", refusal=None, usage=None):
    """Respuesta en streaming: el contenido partido en varios deltas + chunk final de usage."""
    mitad = len(content) // 2
    chunks = [_chunk(content[:mitad]), _chunk(content[mitad:]), _chunk(finish=finish, refusal=refusal)]
    chunks.append(_chunk(usage=usage, choices=False))
    return FakeStream(chunks)


def _rate_limit(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return openai.RateLimitError("rate", response=httpx.Response(429, request=_REQ, headers=headers), body=None)


class FakeClient:
    """Devuelve (o lanza) en orden lo que se le pasa; guarda los kwargs de cada llamada."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class RecordingSem:
    def __init__(self):
        self.depth = 0
        self.entered = 0

    def __enter__(self):
        self.depth += 1
        self.entered += 1

    def __exit__(self, *a):
        self.depth -= 1


@pytest.fixture
def env(monkeypatch):
    sleeps: list[float] = []
    sem = RecordingSem()
    state = {}

    def install(outputs):
        client = FakeClient(outputs)
        orig = client._create

        def create(**kw):  # verifica que la llamada de red va dentro del semáforo
            assert sem.depth == 1
            return orig(**kw)

        client.chat.completions.create = create
        state["client"] = client
        return client

    monkeypatch.setattr(mod, "get_openai_client", lambda: state["client"])
    monkeypatch.setattr(mod, "to_png_b64", lambda content, max_pages=None: ["AAAA"])
    monkeypatch.setattr(mod, "_sleep", sleeps.append)
    monkeypatch.setattr(mod, "OPENAI_SEM", sem)
    monkeypatch.setattr(mod.settings, "ocr_max_attempts", 3)
    monkeypatch.setattr(mod.settings, "ocr_model", "gpt-4o")
    return SimpleNamespace(install=install, sleeps=sleeps, sem=sem)


DOC_OK = json.dumps({"documentos": [{
    "tipo": "remision", "raw_text": "CAMELIA LIMON MC 125",
    "fields": {"remision_num": {"valor": 125, "confianza": 95},
               "lotes": {"valor": [{"lote": 5, "canastillas": 169, "kg": 3201}], "confianza": 85}},
}]})


def test_truncado_reintenta_a_02_y_luego_ok(env):
    client = env.install([_resp('{"documentos": [{"raw_text": "aaaaaaa', finish="length"), _resp(DOC_OK)])
    docs = OCRService().extract(b"x", filename="remision.pdf")
    assert docs[0].raw_text == "CAMELIA LIMON MC 125"
    assert docs[0].fields["lotes"]["valor"][0]["kg"] == 3201
    assert [c["temperature"] for c in client.calls] == [0, 0.2]
    assert env.sleeps == []  # un truncado no espera
    assert env.sem.entered == 2 and env.sem.depth == 0


def test_json_invalido_tambien_reintenta_a_02(env):
    client = env.install([_resp('{"documentos": [{"raw_text": "sin cerrar'), _resp(DOC_OK)])
    OCRService().extract(b"x")
    assert [c["temperature"] for c in client.calls] == [0, 0.2]


def test_429_reintenta_a_0_con_backoff(env):
    client = env.install([_rate_limit(), _rate_limit(retry_after=7), _resp(DOC_OK)])
    OCRService().extract(b"x")
    assert [c["temperature"] for c in client.calls] == [0, 0, 0]
    assert len(env.sleeps) == 2
    assert 1.0 <= env.sleeps[0] <= 2.0  # base 1s * (1 + jitter)
    assert env.sleeps[1] == 7.0  # respeta Retry-After


def test_timeout_reintenta_a_02(env):
    client = env.install([openai.APITimeoutError(request=_REQ), _resp(DOC_OK)])
    OCRService().extract(b"x")
    assert [c["temperature"] for c in client.calls] == [0, 0.2]


def test_agotado_lanza_error_claro_con_archivo(env):
    env.install([_resp('{"a": "x', finish="length")] * 3)
    with pytest.raises(OCRTruncadoError, match="remision.pdf"):
        OCRService().extract(b"x", filename="remision.pdf")


def test_agotado_por_429_lanza_ocrerror(env):
    env.install([_rate_limit()] * 3)
    with pytest.raises(OCRError) as ei:
        OCRService().extract(b"x", filename="f.pdf")
    assert not isinstance(ei.value, OCRTruncadoError)


def test_4xx_no_reintenta(env):
    err = openai.BadRequestError("bad", response=httpx.Response(400, request=_REQ), body=None)
    client = env.install([err, _resp(DOC_OK)])
    with pytest.raises(OCRError):
        OCRService().extract(b"x")
    assert len(client.calls) == 1


def test_documentos_schema_strict_y_tope(env, monkeypatch):
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_documentos", 1234)
    client = env.install([_resp(DOC_OK)])
    OCRService().extract(b"x")
    rf = client.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] is DOCUMENTOS_SCHEMA
    assert client.calls[0]["max_completion_tokens"] == 1234


def _assert_strict(schema):
    """Reglas de strict mode: todo objeto con required = todas sus props y sin extras."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or (isinstance(schema.get("type"), list) and "object" in schema["type"]):
            assert schema["additionalProperties"] is False
            assert sorted(schema["required"]) == sorted(schema["properties"])
        for v in schema.values():
            _assert_strict(v)
    elif isinstance(schema, list):
        for v in schema:
            _assert_strict(v)


def test_esquemas_cumplen_strict():
    _assert_strict(DOCUMENTOS_SCHEMA)
    _assert_strict(PLANILLA_SCHEMA)
    assert "raw_text" in PLANILLA_SCHEMA["required"]


def test_modelos_sin_temperature_y_max_tokens_por_familia(env):
    client = env.install([_resp('{"a": "x', finish="length"), _resp(DOC_OK)])
    OCRService().extract_json(b"x", "s", "u", model="gpt-5-mini", max_tokens=50)
    assert all("temperature" not in c for c in client.calls)
    assert client.calls[0]["max_completion_tokens"] == 50
    client = env.install([_resp("{}")])
    OCRService().extract_json(b"x", "s", "u", model="gpt-4-turbo", max_tokens=50)
    assert client.calls[0]["max_tokens"] == 50


def test_clasificador_sin_schema_sigue_en_json_object(env):
    client = env.install([_resp('{"encabezado": "REMISION", "tipo": "remision"}')])
    data = OCRService().extract_json(b"x", "s", "Lee el encabezado", model="gpt-4o-mini")
    assert data["tipo"] == "remision"
    assert client.calls[0]["response_format"] == {"type": "json_object"}


# --- Planillas: raw_text se conserva y llega a Jev tras la doble lectura (reconcile real) ---

def v(x, c=95):
    return {"valor": x, "confianza": c}


def _fila(nombre, cedula, jor=1, vr=63000):
    return {"nombre": v(nombre), "cedula": v(cedula), "labor": v("Deshojando"), "cultivo": v(None),
            "lote": v("1"), "jor": v(jor), "rend": v(None), "und": v(None), "vr_unit": v(vr),
            "total_escrito": v(vr)}


def _planilla(raw, filas):
    return json.dumps({"raw_text": raw, "finca": v("GINEBRA"), "anio": v(2026), "semana": v(42),
                       "dia_semana": v("VIERNES"), "dia_mes": v(23), "nota": v(None), "filas": filas})


def test_planilla_raw_text_de_segunda_lectura_llega_a_jev(env, monkeypatch):
    monkeypatch.setattr(mod.settings, "planilla_double_read", True)
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_planillas", 4321)
    primera = _planilla("lectura 1: Libardo", [_fila("Libardo", "18494034")])
    segunda = _planilla("lectura 2: Libardo / Orlando",
                        [_fila("Libardo", "18494034"), _fila("Orlando", "7524180")])
    client = env.install([_resp(primera), _resp(segunda)])

    svc = PlanillaService.__new__(PlanillaService)
    svc.ocr = OCRService()
    vistos = []
    svc._jev_supports = lambda raw_text, flat: vistos.append(raw_text) or {}
    rows, reviews, exc = svc._process_doc(UploadedDoc(filename="p.jpg", content=b"x"), "gpt-4o")

    assert len(rows) == 2  # la base es la segunda lectura (recupera la fila de Orlando)
    assert vistos == ["lectura 2: Libardo / Orlando"]
    assert any(e.tipo == "Lecturas difieren" for e in exc)
    for c in client.calls:
        assert c["response_format"]["json_schema"]["schema"] is PLANILLA_SCHEMA
        assert c["response_format"]["json_schema"]["strict"] is True
        assert c["max_completion_tokens"] == 4321
    assert "CONTROL DIARIO DE TRABAJO" in PLANILLA_PROMPT


# --- gpt-5: reasoning_effort, topes que crecen al truncar, tope por página, telemetría ---

def test_gpt5_envia_reasoning_effort_y_duplica_tope_al_truncar(env, monkeypatch):
    monkeypatch.setattr(mod.settings, "ocr_reasoning_effort", "low")
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_tope", 12000)
    client = env.install([_resp('{"a": "x', finish="length")] * 2 + [_resp("{}")])
    OCRService().extract_json(b"x", "s", "u", model="gpt-5.4-mini", max_tokens=6000,
                              schema=PLANILLA_SCHEMA, schema_name="planilla")
    assert [c["max_completion_tokens"] for c in client.calls] == [6000, 12000, 12000]
    assert all(c["reasoning_effort"] == "low" for c in client.calls)
    assert all("temperature" not in c for c in client.calls)


def test_gpt4o_sin_reasoning_effort(env, monkeypatch):
    monkeypatch.setattr(mod.settings, "ocr_reasoning_effort", "low")
    client = env.install([_resp("{}")])
    OCRService().extract_json(b"x", "s", "u", model="gpt-4o")
    assert "reasoning_effort" not in client.calls[0]


def test_documentos_tope_por_pagina_y_timeout(env, monkeypatch):
    monkeypatch.setattr(mod, "to_png_b64", lambda content, max_pages=None: ["A", "B", "C"])
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_documentos", 3000)
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_tope", 8000)
    monkeypatch.setattr(mod.settings, "openai_timeout_documentos_s", 45.0)
    client = env.install([_resp('{"a": "x', finish="length"), _resp(DOC_OK)])
    OCRService().extract(b"x")
    assert [c["max_completion_tokens"] for c in client.calls] == [8000, 8000]  # 3x3000 -> techo
    t = client.calls[0]["timeout"]  # timeouts por fase estrictos; el plazo total es 45 s
    assert (t.connect, t.read, t.write, t.pool) == (10.0, 35.0, 15.0, 10.0)
    assert client.calls[0]["stream"] is True
    assert client.calls[0]["stream_options"] == {"include_usage": True}


def test_planilla_sin_schema_ya_no_se_detecta_por_prompt(env):
    client = env.install([_resp("{}")])
    OCRService().extract_json(b"x", "s", PLANILLA_PROMPT, model="gpt-4o")
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_telemetria_una_linea_json_por_llamada(env, caplog):
    from app.infrastructure.telemetria import lote

    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=20))
    ok = _resp(DOC_OK, usage=usage)
    env.install([_resp('{"a', finish="length"), ok])
    with caplog.at_level("INFO", logger="app.telemetria"), lote("L-1"):
        OCRService().extract(b"x", filename="r.pdf")
    eventos = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.telemetria"]
    assert [e["intento"] for e in eventos] == [1, 2]
    assert eventos[0]["finish_reason"] == "length" and eventos[0]["prompt_tokens"] is None
    e = eventos[1]
    assert e["evento"] == "openai_uso" and e["tipo"] == "documento" and e["lote_id"] == "L-1"
    assert (e["prompt_tokens"], e["completion_tokens"], e["reasoning_tokens"]) == (100, 50, 20)
    assert e["archivo"] == "r.pdf" and e["finish_reason"] == "stop"


def test_documentos_schema_ata_tipo_a_sus_campos():
    ramas = DOCUMENTOS_SCHEMA["properties"]["documentos"]["items"]["anyOf"]
    por_tipo = {r["properties"]["tipo"]["enum"][0]: r for r in ramas}
    assert set(por_tipo) == {"remision", "liquidacion_planta", "liquidacion_bascula", "otro"}
    assert all(len(r["properties"]["tipo"]["enum"]) == 1 for r in ramas)
    assert set(por_tipo["remision"]["properties"]["fields"]["properties"]) == set(mod.REMISION_FIELDS)
    assert set(por_tipo["liquidacion_planta"]["properties"]["fields"]["properties"]) == set(mod.LIQUIDACION_FIELDS)
    assert set(por_tipo["liquidacion_bascula"]["properties"]["fields"]["properties"]) == set(mod.BASCULA_FIELDS)
    assert por_tipo["otro"]["properties"]["fields"]["properties"] == {}
    liq = por_tipo["liquidacion_planta"]["properties"]["fields"]["properties"]
    assert "number" in liq["liq_kg_primera"]["properties"]["valor"]["type"]


def test_planilla_schema_filas_nulables_y_en_orden_de_columnas():
    fila = PLANILLA_SCHEMA["properties"]["filas"]["items"]["properties"]
    assert list(fila)[4:8] == ["lote", "jor", "rend", "und"]
    for k in ("cultivo", "lote", "jor", "rend", "und", "vr_unit", "total_escrito", "cedula"):
        assert "null" in fila[k]["properties"]["valor"]["type"]
        assert fila[k]["properties"]["valor"]["description"]


# --- Plazo TOTAL de reloj de pared (streaming) ---

class SlowStream(FakeStream):
    """Un chunk cada 0.2 s para siempre (httpx nunca vería inactividad)."""

    def __init__(self):
        super().__init__([])

    def __iter__(self):
        import time
        while not self.closed:
            time.sleep(0.2)
            yield _chunk("x")


def test_plazo_total_corta_stream_lento_y_libera_semaforo(env, monkeypatch):
    import threading
    import time

    real = threading.BoundedSemaphore(3)
    monkeypatch.setattr(mod, "OPENAI_SEM", real)
    monkeypatch.setattr(mod.settings, "ocr_max_attempts", 1)
    lento = SlowStream()
    client = FakeClient([lento])
    monkeypatch.setattr(mod, "get_openai_client", lambda: client)
    t0 = time.monotonic()
    with pytest.raises(OCRTruncadoError, match="timeout"):
        OCRService().extract_json(b"x", "s", "u", timeout=1.0)
    assert time.monotonic() - t0 < 1.0 + 0.5
    assert lento.closed
    tomados = [real.acquire(blocking=False) for _ in range(3)]
    assert all(tomados)
    for _ in tomados:
        real.release()


def test_stream_normal_arma_json_finish_y_usage(env, caplog):
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=3))
    s = _resp('{"tipo": "remision"}', usage=usage)
    env.install([s])
    with caplog.at_level("INFO", logger="app.telemetria"):
        data = OCRService().extract_json(b"x", "s", "u", tipo="clasificador")
    assert data == {"tipo": "remision"}
    assert s.closed
    e = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.telemetria"][-1]
    assert e["finish_reason"] == "stop" and "error" not in e
    assert (e["prompt_tokens"], e["completion_tokens"], e["reasoning_tokens"]) == (11, 7, 3)


def test_rechazo_en_stream_reintenta(env):
    client = env.install([_resp("", refusal="no puedo"), _resp("{}")])
    assert OCRService().extract_json(b"x", "s", "u") == {}
    assert [c["temperature"] for c in client.calls] == [0, 0.2]


def test_plazo_agotado_entra_en_la_ruta_de_reintento(env, monkeypatch, caplog):
    monkeypatch.setattr(mod.settings, "ocr_max_tokens_tope", 8000)
    lento = SlowStream()
    client = env.install([lento, _resp(DOC_OK)])
    with caplog.at_level("INFO", logger="app.telemetria"):
        docs = OCRService().extract_json(b"x", "s", "u", timeout=0.5, max_tokens=100,
                                         schema=DOCUMENTOS_SCHEMA, schema_name="documentos")
    assert docs["documentos"][0]["raw_text"] == "CAMELIA LIMON MC 125"
    assert lento.closed
    assert [c["temperature"] for c in client.calls] == [0, 0.2]  # misma política que un timeout
    assert env.sleeps == []
    e = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.telemetria"]
    assert e[0]["error"].startswith("timeout") and e[0]["intento"] == 1 and e[0]["segundos"] < 1.2
