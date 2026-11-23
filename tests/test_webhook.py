"""Entrada por WhatsApp (YCloud): clasificación, firma y enrutamiento."""

import hashlib
import hmac
import json
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.api import webhooks
from app.core.config import settings
from app.domain.models import DocumentType
from app.infrastructure.files import UploadedDoc
from app.infrastructure.ycloud import verificar_firma
from app.services.classifier_service import (
    RUTA_DESCONOCIDA,
    RUTA_DOCUMENTOS,
    RUTA_PLANILLAS,
    ClassifierService,
    por_reglas,
)

SECRETO = "whsec_test"


def firmar(cuerpo: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    s = hmac.new(SECRETO.encode(), f"{ts}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return f"t={ts},s={s}"


def pdf_con_texto(texto: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), texto)
    return doc.tobytes()


# --- reglas deterministas ---------------------------------------------------

@pytest.mark.parametrize(
    "texto, tipo",
    [
        ("CONTROL DIARIO DE TRABAJO finca Ginebra", DocumentType.CONTROL_DIARIO),
        ("REMISIÓN MC 125", DocumentType.REMISION),
        ("GRAMALUZ - Reporte proceso", DocumentType.LIQUIDACION_PLANTA),
        ("tiquete de báscula", DocumentType.LIQUIDACION_BASCULA),
        ("remisión + liquidación del lote", DocumentType.REMISION),  # mismo proceso -> concluye
    ],
)
def test_reglas(texto, tipo):
    assert por_reglas(texto)[0] == tipo


def test_reglas_no_concluyen_con_procesos_mezclados_o_sin_texto():
    assert por_reglas("planilla remisión")[0] is None
    assert por_reglas("")[0] is None
    assert por_reglas("foto.jpeg")[0] is None


class FakeOCR:
    def __init__(self, data):
        self.data, self.calls = data, 0

    def extract_json(self, *a, **k):
        self.calls += 1
        return self.data


class FakeJev:
    def __init__(self, res):
        self.res = res

    def classify_text(self, text):
        return self.res


def test_caption_decide_sin_ocr():
    ocr = FakeOCR({})
    c = ClassifierService(ocr, FakeJev(None)).classify(UploadedDoc("x.jpeg", b"img"), "planilla de hoy")
    assert c.ruta == RUTA_PLANILLAS and c.metodo == "reglas" and ocr.calls == 0


def test_texto_del_pdf_decide_sin_ocr():
    ocr = FakeOCR({})
    doc = UploadedDoc("doc.pdf", pdf_con_texto("GRAMALUZ REPORTE PROCESO"))
    c = ClassifierService(ocr, FakeJev(None)).classify(doc)
    assert c.ruta == RUTA_DOCUMENTOS and c.metodo == "reglas" and ocr.calls == 0


def test_encabezado_ocr_y_luego_jev_y_luego_modelo():
    img = UploadedDoc("foto.jpeg", b"img")
    c = ClassifierService(FakeOCR({"encabezado": "Control Diario", "tipo": "otro"}), FakeJev(None)).classify(img)
    assert c.metodo == "encabezado_reglas" and c.ruta == RUTA_PLANILLAS

    c = ClassifierService(
        FakeOCR({"encabezado": "Finca X 23/10", "tipo": "otro"}),
        FakeJev((DocumentType.REMISION, 0.9)),
    ).classify(img)
    assert c.metodo == "jev" and c.ruta == RUTA_DOCUMENTOS

    c = ClassifierService(
        FakeOCR({"encabezado": "Finca X", "tipo": "liquidacion_bascula"}),
        FakeJev((DocumentType.REMISION, 0.3)),  # Jev sin confianza suficiente
    ).classify(img)
    assert c.metodo == "modelo" and c.tipo == DocumentType.LIQUIDACION_BASCULA

    c = ClassifierService(FakeOCR({"encabezado": "", "tipo": "otro"}), FakeJev(None)).classify(img)
    assert c.ruta == RUTA_DESCONOCIDA


# --- firma -----------------------------------------------------------------

def test_firma():
    cuerpo = b'{"a":1}'
    assert verificar_firma(firmar(cuerpo), cuerpo, SECRETO)
    assert not verificar_firma(firmar(cuerpo), b'{"a":2}', SECRETO)
    assert not verificar_firma(firmar(cuerpo, int(time.time()) - 3600), cuerpo, SECRETO)
    assert not verificar_firma(firmar(cuerpo), cuerpo, "")


# --- webhook ---------------------------------------------------------------

class FakeYCloud:
    def __init__(self):
        self.textos, self.docs = [], []

    def descargar(self, media):
        return b"bytes"

    def enviar_texto(self, d, t):
        self.textos.append((d, t))

    def enviar_documento(self, d, link, filename, caption=""):
        self.docs.append((d, link, filename, caption))


class FakePlanillas:
    def process(self, docs):
        from app.services.planilla_service import PlanillaResult

        return PlanillaResult(xlsx=b"xlsx", rows=3, yellow_cells=["Mano de obra!K4"], exceptions=[])


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setenv("DOWNLOAD_URL_SECRET", "dl-secret")
    monkeypatch.setattr(settings, "ycloud_enabled", True)
    monkeypatch.setattr(settings, "ycloud_whatsapp_from", "+573000000000")
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", "https://excel.techapoli.com")
    fake = FakeYCloud()
    from app.services.inbound_service import InboundService

    inbound = InboundService(ClassifierService(FakeOCR({}), FakeJev(None)), planillas=FakePlanillas(), batch_window_s=0)
    monkeypatch.setattr(webhooks, "get_ycloud", lambda: fake)
    monkeypatch.setattr(webhooks, "get_inbound", lambda: inbound)
    monkeypatch.setattr(webhooks, "vistos", webhooks._Vistos())
    from main import app

    return TestClient(app), fake


def evento(msg_id="wamid.1", tipo="image", to="+573000000000", caption="planilla"):
    m = {"id": msg_id, "from": "+573111111111", "to": to, "type": tipo}
    if tipo == "image":
        m["image"] = {"link": "https://ycloud/x", "mimeType": "image/jpeg", "caption": caption}
    else:
        m["text"] = {"body": "hola"}
    return json.dumps({"type": "whatsapp.inbound_message.received", "whatsappInboundMessage": m}).encode()


def post(c, cuerpo, firma=None):
    r = c.post("/webhooks/ycloud", content=cuerpo, headers={"YCloud-Signature": firma or firmar(cuerpo)})
    webhooks.get_inbound().lotes.esperar()  # ventana 0 -> el vaciado corre en un hilo propio
    return r


def test_webhook_rechaza_firma_invalida(client):
    c, _ = client
    assert post(c, evento(), "t=1,s=00").status_code == 403


def test_webhook_planilla_devuelve_excel_y_dedup(client):
    c, fake = client
    cuerpo = evento()
    assert post(c, cuerpo).json() == {"ok": True}
    assert len(fake.docs) == 1
    destino, link, filename, caption = fake.docs[0]
    assert destino == "+573111111111" and filename == "planillas_mano_de_obra.xlsx"
    assert link.startswith("https://excel.techapoli.com/descargas/") and "3 filas" in caption

    post(c, cuerpo)  # reintento de YCloud
    assert len(fake.docs) == 1

    ruta = link.split("excel.techapoli.com", 1)[1]
    r = c.get(ruta)
    assert r.status_code == 200 and r.content == b"xlsx"
    assert c.get(ruta.replace("sig=", "sig=0")).status_code == 404


def test_webhook_ignora_otro_numero_y_responde_ayuda_a_texto(client):
    c, fake = client
    post(c, evento("wamid.2", to="+579999999999"))
    assert fake.docs == [] and fake.textos == []
    post(c, evento("wamid.3", tipo="text"))
    assert len(fake.textos) == 1


def test_webhook_no_clasificado_pide_aclarar(client, monkeypatch):
    c, fake = client
    post(c, evento("wamid.4", caption=""))  # FakeOCR({}) -> sin encabezado -> desconocido
    assert fake.docs == [] and "No pude identificar" in fake.textos[0][1]


# --- lotes por remitente -----------------------------------------------------

class FakeClassifierPorNombre:
    """planilla*.jpg -> planillas, rem*.jpg -> documentos, falla*.jpg lanza, resto desconocido."""

    def classify(self, doc, caption=""):
        from app.services.classifier_service import Clasificacion

        if doc.filename.startswith("falla"):
            raise RuntimeError("OCR caído")
        if doc.filename.startswith("planilla"):
            return Clasificacion(DocumentType.CONTROL_DIARIO, RUTA_PLANILLAS, "reglas", 1.0, "")
        if doc.filename.startswith("rem"):
            return Clasificacion(DocumentType.REMISION, RUTA_DOCUMENTOS, "reglas", 1.0, "")
        return Clasificacion(DocumentType.OTRO, RUTA_DESCONOCIDA, "reglas", 0.0, "")


class FakePipeline:
    """Implementa analizar/armar como el servicio real: un PhotoResult por doc, nunca lanza."""

    def __init__(self):
        self.llamadas = []  # una entrada por llamada a analizar
        self.armados = []
        self.analizados = []  # un nombre por cada análisis individual

    def analizar(self, docs):
        from app.services.resultados import PhotoResult

        self.llamadas.append([d.filename for d in docs])
        out = []
        for d in docs:
            self.analizados.append(d.filename)
            if d.filename.startswith("rem_mala"):
                out.append(PhotoResult(ok=False, filename=d.filename, error="doc corrupto", exc_type="RuntimeError"))
            else:
                out.append(PhotoResult(ok=True, filename=d.filename, data=d.filename))
        return out

    def armar(self, results):
        from types import SimpleNamespace

        self.armados.append([r.filename for r in results])
        s = SimpleNamespace(documents=[SimpleNamespace(doc_type=DocumentType.REMISION)],
                            yellow_cells=["A1"], exceptions=[])
        return b"xlsx2", s


class CountingPlanillas(FakePlanillas):
    def __init__(self):
        self.llamadas = []

    def process(self, docs):
        self.llamadas.append([d.filename for d in docs])
        return super().process(docs)


@pytest.fixture
def lote(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", "https://excel.techapoli.com")
    monkeypatch.setenv("DOWNLOAD_URL_SECRET", "dl-secret")
    from app.services.inbound_service import InboundService, Lotes

    plan, pipe = CountingPlanillas(), FakePipeline()
    svc = InboundService(FakeClassifierPorNombre(), pipeline=pipe, planillas=plan)
    svc._lotes = Lotes(svc.procesar_lote, ventana=3600, maximo=1000, espera_max=7200)
    return svc, plan, pipe


def _encolar(svc, fake, nombres, destino="+573111111111"):
    for n in nombres:
        svc.encolar(fake, destino, {"link": n}, n)


def test_lote_un_envio_por_grupo(lote):
    svc, plan, pipe = lote
    fake = FakeYCloud()
    _encolar(svc, fake, ["planilla1.jpg", "planilla2.jpg", "rem1.jpg", "rem2.jpg", "rem3.jpg", "x.jpg", "y.jpg"])
    assert fake.docs == [] and svc.lotes.pendientes("+573111111111") == 7
    svc.lotes.vaciar_todo()
    assert plan.llamadas == [["planilla1.jpg", "planilla2.jpg"]]
    assert pipe.llamadas == [["rem1.jpg", "rem2.jpg", "rem3.jpg"]]
    assert len(fake.docs) == 2
    assert "2 planilla(s)" in fake.docs[0][3] and "3 documento(s)" in fake.docs[1][3]
    assert len(fake.textos) == 1 and "x.jpg, y.jpg" in fake.textos[0][1]


def test_lote_fallo_parcial_no_hunde_el_lote(lote):
    svc, plan, pipe = lote
    fake = FakeYCloud()
    _encolar(svc, fake, ["planilla1.jpg", "falla1.jpg", "rem1.jpg", "rem_mala.jpg", "rem2.jpg"])
    svc.lotes.vaciar_todo()
    assert len(fake.docs) == 2
    assert pipe.llamadas == [["rem1.jpg", "rem_mala.jpg", "rem2.jpg"]]  # un solo análisis
    assert pipe.armados == [["rem1.jpg", "rem2.jpg"]]  # Excel solo con los buenos
    assert len(fake.textos) == 1  # un solo aviso de error por lote
    assert "falla1.jpg" in fake.textos[0][1] and "rem_mala.jpg" in fake.textos[0][1]


def test_fallo_de_envio_no_dispara_mensaje_de_error(lote, caplog):
    svc, _, _ = lote

    class YCloudBloqueado(FakeYCloud):
        def enviar_documento(self, *a, **k):
            raise RuntimeError("131056 rate limit")

        def enviar_texto(self, d, t):
            raise AssertionError("no debe intentar otro mensaje")

    fake = YCloudBloqueado()
    _encolar(svc, fake, ["planilla1.jpg", "rem1.jpg", "x.jpg", "falla.jpg"])
    with caplog.at_level("INFO"):
        svc.lotes.vaciar_todo()
    assert "no se pudo entregar" in caplog.text
    assert "falló el procesamiento del lote" not in caplog.text


def test_lote_maximo_y_ventana_por_remitente(lote):
    svc, plan, _ = lote
    svc.lotes.maximo = 2
    fake = FakeYCloud()
    _encolar(svc, fake, ["planilla1.jpg"], "+571")
    _encolar(svc, fake, ["planilla2.jpg"], "+572")
    assert fake.docs == []
    _encolar(svc, fake, ["planilla3.jpg"], "+571")  # llega al máximo -> vacía ya (en otro hilo)
    svc.lotes.esperar()
    assert [d[0] for d in fake.docs] == ["+571"] and plan.llamadas == [["planilla1.jpg", "planilla3.jpg"]]
    svc.lotes.vaciar_todo()


def test_lote_timer_vence(monkeypatch, tmp_path):
    from app.services.inbound_service import Lotes

    vaciados = []
    lotes = Lotes(lambda y, d, items: vaciados.append((d, [i.filename for i in items])), ventana=0.05)
    from app.services.inbound_service import Pendiente

    lotes.agregar(None, "+57", Pendiente({}, "a"))
    lotes.agregar(None, "+57", Pendiente({}, "b"))
    for _ in range(100):
        if vaciados:
            break
        time.sleep(0.02)
    assert vaciados == [("+57", ["a", "b"])]


def test_lote_descarga_y_clasificacion_concurrentes(lote, monkeypatch):
    """Los ítems se clasifican en paralelo (Barrier) y se preservan orden y aislamiento de fallos."""
    import threading

    svc, plan, pipe = lote
    monkeypatch.setenv("INBOUND_PROCESS_WORKERS", "4")
    barrera = threading.Barrier(4, timeout=5)  # solo se rompe si 4 clasificaciones corren a la vez
    base = svc.classifier

    class ClasificadorConcurrente:
        def classify(self, doc, caption=""):
            barrera.wait()
            if doc.filename == "rem_boom.jpg":
                raise RuntimeError("OCR caído")
            return base.classify(doc, caption)

    svc.classifier = ClasificadorConcurrente()
    fake = FakeYCloud()
    _encolar(svc, fake, ["rem3.jpg", "rem_boom.jpg", "rem1.jpg", "rem2.jpg"])
    svc.lotes.vaciar_todo()
    assert not barrera.broken
    assert pipe.llamadas == [["rem3.jpg", "rem1.jpg", "rem2.jpg"]]  # orden de llegada
    assert len(fake.docs) == 1
    assert len(fake.textos) == 1 and "rem_boom.jpg" in fake.textos[0][1]


def test_lote_cada_doc_se_analiza_una_sola_vez(lote):
    svc, _, pipe = lote
    fake = FakeYCloud()
    _encolar(svc, fake, ["rem1.jpg", "rem_mala.jpg", "rem2.jpg"])
    svc.lotes.vaciar_todo()
    assert sorted(pipe.analizados) == ["rem1.jpg", "rem2.jpg", "rem_mala.jpg"]  # sin reintentos
    assert pipe.armados == [["rem1.jpg", "rem2.jpg"]]
    assert len(fake.docs) == 1 and len(fake.textos) == 1 and "rem_mala.jpg" in fake.textos[0][1]
