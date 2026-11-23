"""Persistencia de lotes de WhatsApp: recuperación tras reinicio, dedup en disco, cierre de lote."""

import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api import webhooks
from app.core.config import settings
from app.infrastructure import pendientes as pend_mod
from app.infrastructure.pendientes import Pendientes
from app.services.inbound_service import InboundService, Lotes, Pendiente, resumen_lote
from tests.test_webhook import (
    SECRETO,
    CountingPlanillas,
    FakeClassifierPorNombre,
    FakePipeline,
    FakeYCloud,
    evento,
    firmar,
)

DEST = "+573111111111"


class YCloudSinRed(FakeYCloud):
    """Tras el reinicio la URL de media ya caducó: descargar() no debe llamarse."""

    def descargar(self, media):
        raise AssertionError("debe usar los bytes persistidos")


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "outputs_dir", str(tmp_path / "out"))
    monkeypatch.setattr(settings, "pending_dir", str(tmp_path / "pending"))
    monkeypatch.setattr(settings, "public_base_url", "https://excel.techapoli.com")
    monkeypatch.setenv("DOWNLOAD_URL_SECRET", "dl-secret")
    return tmp_path / "pending"


def _servicio(ventana=3600):
    plan, pipe = CountingPlanillas(), FakePipeline()
    svc = InboundService(FakeClassifierPorNombre(), pipeline=pipe, planillas=plan)
    svc._lotes = Lotes(svc.procesar_lote, ventana=ventana, maximo=1000, espera_max=7200)
    return svc, plan, pipe


def _esperar(cond, timeout=5.0):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_reinicio_recupera_un_lote_un_resumen_y_limpia(entorno):
    # 1) Antes de la caída: llegan 4 fotos y se persisten (el lote no alcanza a vaciarse).
    svc, _, _ = _servicio()
    fake = FakeYCloud()
    for i, n in enumerate(["planilla1.jpg", "planilla2.jpg", "rem1.jpg", "x.jpg"]):
        svc.encolar(fake, DEST, {"link": n}, n, msg_id=f"wamid.{i}", mime="image/jpeg")
    assert len(list(entorno.glob("*/*.bin"))) == 4 and len(list(entorno.glob("*/*.json"))) == 4
    meta = json.loads(next(entorno.glob("*/wamid.0.json")).read_text(encoding="utf-8"))
    assert meta["filename"] == "planilla1.jpg" and meta["destino"] == DEST and meta["mime"] == "image/jpeg"
    del svc  # "kill": el lote en memoria se pierde, sin vaciado al apagar

    # 2) Arranque nuevo: servicio y Lotes frescos, recuperación con ventana corta.
    svc2, plan, pipe = _servicio()
    fake2 = YCloudSinRed()
    assert svc2.recuperar(fake2, espera=0.05) == 4
    assert _esperar(lambda: not list(entorno.glob("*/*")))

    assert plan.llamadas == [["planilla1.jpg", "planilla2.jpg"]]  # exactamente un lote
    assert pipe.llamadas == [["rem1.jpg"]]
    assert len(fake2.docs) == 2 and len(fake2.textos) == 1
    resumenes = [c for *_, c in fake2.docs if "Procesadas" in c] + [t for _, t in fake2.textos if "Procesadas" in t]
    assert resumenes == [fake2.docs[0][3]]  # UN solo resumen, en el caption del primer envío
    assert "Procesadas 4 fotos: 2 planillas, 1 documento, 1 no identificada." in resumenes[0]
    assert [p.name for p in entorno.iterdir()] == ["procesados.log"]  # dir del remitente borrado

    # 3) Otro reinicio: nada que recuperar y los ids quedan como procesados.
    svc3, _, _ = _servicio()
    assert svc3.recuperar(fake2, espera=0.05) == 0
    assert svc3.pendientes.conocido("wamid.0")


def test_webhook_duplicado_tras_reinicio_se_ignora(entorno, monkeypatch):
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setattr(settings, "ycloud_enabled", True)
    monkeypatch.setattr(settings, "ycloud_whatsapp_from", "+573000000000")
    fake = FakeYCloud()
    monkeypatch.setattr(webhooks, "get_ycloud", lambda: fake)
    from main import app

    c = TestClient(app)

    def post(cuerpo):
        return c.post("/webhooks/ycloud", content=cuerpo, headers={"YCloud-Signature": firmar(cuerpo)})

    # Antes del reinicio: wamid.p queda pendiente (ventana larga), wamid.d ya respondido.
    svc, _, _ = _servicio()
    monkeypatch.setattr(webhooks, "get_inbound", lambda: svc)
    monkeypatch.setattr(webhooks, "vistos", webhooks._Vistos())
    post(evento("wamid.p"))
    svc.pendientes.completar(DEST, ["wamid.d"])
    assert svc.pendientes.pendiente("wamid.p")

    # Reinicio: memoria vacía, servicio nuevo. YCloud reintenta ambos -> se ignoran.
    svc2, plan, _ = _servicio()
    monkeypatch.setattr(webhooks, "get_inbound", lambda: svc2)
    monkeypatch.setattr(webhooks, "vistos", webhooks._Vistos())
    post(evento("wamid.p"))
    post(evento("wamid.d"))
    assert svc2.lotes.pendientes(DEST) == 0 and len(list(entorno.glob("*/*.json"))) == 1


def test_tope_de_tamano_vacia_en_otro_hilo():
    hilos, listo = [], threading.Event()

    def flush(y, d, items):
        hilos.append(threading.current_thread())
        listo.set()

    lotes = Lotes(flush, ventana=3600, maximo=2, espera_max=7200)
    lotes.agregar(None, "+57", Pendiente({}, "a"))
    lotes.agregar(None, "+57", Pendiente({}, "b"))  # tope -> no bloquea al llamador
    assert listo.wait(5)
    assert hilos and hilos[0] is not threading.current_thread() and hilos[0].daemon
    assert lotes.pendientes("+57") == 0


def test_timer_viejo_no_vacia_antes_de_tiempo():
    vaciados = []
    lotes = Lotes(lambda y, d, items: vaciados.append([i.filename for i in items]),
                  ventana=3600, maximo=1000, espera_max=7200)
    lotes.agregar(None, "+57", Pendiente({}, "a"))
    with lotes._lock:
        lote = lotes._lotes["+57"]
        gen_viejo = lote["gen"]
    lotes.agregar(None, "+57", Pendiente({}, "b"))
    # Simula el Timer de "a" que ya estaba disparando cuando "b" lo canceló.
    lotes._vencer("+57", lote, gen_viejo)
    assert vaciados == [] and lotes.pendientes("+57") == 2
    lotes.vaciar_todo()
    assert vaciados == [["a", "b"]]


def test_escritura_atomica_no_deja_parciales(tmp_path, monkeypatch):
    store = Pendientes(tmp_path)
    real = os.replace

    def boom(src, dst):
        raise OSError("disco lleno")

    monkeypatch.setattr(pend_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.guardar(DEST, "wamid.x", b"datos", {"filename": "a.jpg"})
    monkeypatch.setattr(pend_mod.os, "replace", real)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert not store.conocido("wamid.x") and store.cargar() == {}


def test_tmp_huerfano_y_procesado_se_limpian_al_cargar(tmp_path):
    store = Pendientes(tmp_path)
    store.guardar(DEST, "wamid.1", b"1", {"filename": "a.jpg"})
    store.guardar(DEST, "wamid.2", b"2", {"filename": "b.jpg"})
    d = next(p for p in tmp_path.iterdir() if p.is_dir())
    (d / "wamid.3.bin.tmp").write_bytes(b"parcial")
    store._marcar_procesados(["wamid.2"])  # caída entre log y borrado
    lotes = Pendientes(tmp_path).cargar()
    assert [m["msg_id"] for m in lotes[DEST]] == ["wamid.1"]
    assert "contenido" not in lotes[DEST][0]  # no se cargan bytes en RAM al recuperar
    assert open(lotes[DEST][0]["ruta_bin"], "rb").read() == b"1"
    assert sorted(p.name for p in d.iterdir()) == ["wamid.1.bin", "wamid.1.json"]


def test_log_procesados_rota(tmp_path):
    store = Pendientes(tmp_path, max_procesados=3)
    for i in range(10):
        store._marcar_procesados([f"m{i}"])
    lineas = (tmp_path / "procesados.log").read_text().split()
    assert len(lineas) <= 6 and lineas[-1] == "m9"
    nuevo = Pendientes(tmp_path, max_procesados=3)
    assert nuevo.procesado("m9") and not nuevo.procesado("m0")


def test_resumen_lote():
    assert resumen_lote(85, 69, 15, 1, 0) == "📦 Procesadas 85 fotos: 69 planillas, 15 documentos, 1 no identificada."
    assert resumen_lote(1, 0, 0, 0, 1) == "📦 Procesada 1 foto: 1 con error."


# --- robustez: veneno, persistencia antes del 200, huérfanos, memoria, apagado, rotación ---


class YCloudContador(FakeYCloud):
    def __init__(self):
        super().__init__()
        self.descargas = []

    def descargar(self, media):
        self.descargas.append(media.get("link"))
        return b"bytes-descargados"


def test_lote_veneno_se_aparta_tras_n_reinicios_y_avisa_una_vez(entorno):
    svc, _, _ = _servicio()
    svc.encolar(FakeYCloud(), DEST, {"link": "p.jpg"}, "planilla1.jpg", msg_id="wamid.v", mime="image/jpeg")
    # Cada "arranque" recupera y el proceso muere antes de vaciar (ventana larga, sin flush).
    for i in range(1, 4):
        s, _, _ = _servicio()
        assert s.recuperar(FakeYCloud(), espera=3600, max_intentos=3) == 1
        meta = json.loads(next(entorno.glob("*/wamid.v.json")).read_text(encoding="utf-8"))
        assert meta["intentos"] == i
    # 4º arranque: supera el máximo -> _fallidos, procesado, un aviso.
    s4, plan, _ = _servicio()
    fake = FakeYCloud()
    assert s4.recuperar(fake, espera=3600, max_intentos=3) == 0
    s4.lotes.esperar()
    assert len(fake.textos) == 1 and "planilla1.jpg" in fake.textos[0][1] and plan.llamadas == []
    fallidos = list((entorno / "_fallidos").glob("*/*"))
    assert sorted(p.name for p in fallidos) == ["wamid.v.bin", "wamid.v.json"]
    assert s4.pendientes.conocido("wamid.v")
    # 5º arranque: _fallidos se ignora; no hay segundo aviso.
    s5, _, _ = _servicio()
    fake5 = FakeYCloud()
    assert s5.recuperar(fake5, espera=0.01, max_intentos=3) == 0
    s5.lotes.esperar()
    assert fake5.textos == [] and len(list((entorno / "_fallidos").glob("*/*"))) == 2


def test_sidecar_existe_antes_del_200_y_la_recuperacion_descarga(entorno, monkeypatch):
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setattr(settings, "ycloud_enabled", True)
    monkeypatch.setattr(settings, "ycloud_whatsapp_from", "+573000000000")
    svc, _, _ = _servicio()
    monkeypatch.setattr(webhooks, "get_inbound", lambda: svc)
    monkeypatch.setattr(webhooks, "get_ycloud", lambda: FakeYCloud())
    monkeypatch.setattr(webhooks, "vistos", webhooks._Vistos())
    # La BackgroundTask nunca corre (kill justo después del 200).
    monkeypatch.setattr(svc, "encolar", lambda *a, **k: None)
    from main import app

    cuerpo = evento("wamid.s", caption="planilla")
    r = TestClient(app).post("/webhooks/ycloud", content=cuerpo, headers={"YCloud-Signature": firmar(cuerpo)})
    assert r.status_code == 200
    meta = json.loads(next(entorno.glob("*/wamid.s.json")).read_text(encoding="utf-8"))
    assert meta["destino"] == DEST and meta["media"]["link"] == "https://ycloud/x"
    assert meta["caption"] == "planilla" and meta["mime"] == "image/jpeg" and "received_at" in meta
    assert not list(entorno.glob("*/*.bin"))  # sin bytes antes del 200

    svc2, plan, _ = _servicio()
    fake = YCloudContador()
    assert svc2.recuperar(fake, espera=0.01) == 1
    assert _esperar(lambda: fake.textos)  # nombre sin pista -> "no identificado", pero se procesó
    svc2.lotes.esperar()
    assert fake.descargas == ["https://ycloud/x"]
    assert _esperar(lambda: not list(entorno.glob("*/*.json")))


def test_huerfanos_y_corruptos_se_limpian_al_cargar(tmp_path):
    store = Pendientes(tmp_path)
    store.guardar(DEST, "wamid.ok", b"1", {"filename": "a.jpg"})
    d = next(p for p in tmp_path.iterdir() if p.is_dir())
    (d / "wamid.huerf.bin").write_bytes(b"sin sidecar")
    (d / "wamid.ok.json.tmp").write_bytes(b"{")
    (d / "wamid.mal.json").write_text("{no es json", encoding="utf-8")
    (d / "wamid.mal.bin").write_bytes(b"x")
    vacio = tmp_path / "_573999"
    vacio.mkdir()
    lotes = store.cargar()
    assert [m["msg_id"] for m in lotes[DEST]] == ["wamid.ok"]
    assert sorted(p.name for p in d.iterdir()) == ["wamid.ok.bin", "wamid.ok.json"]
    assert sorted(p.name for p in (tmp_path / "_corruptos" / d.name).iterdir()) == ["wamid.mal.bin", "wamid.mal.json"]
    assert not vacio.exists()
    assert Pendientes(tmp_path).cargar().keys() == {DEST}  # _corruptos no se recupera


def test_bytes_perezosos_no_quedan_en_memoria(entorno):
    svc, plan, _ = _servicio()
    capturados = []
    svc.lotes.agregar = lambda y, d, it, ventana=None: capturados.append(it)
    svc.encolar(FakeYCloud(), DEST, {"link": "planilla1.jpg"}, "planilla1.jpg", msg_id="wamid.l")
    (it,) = capturados
    assert it.contenido is None and it.ruta and open(it.ruta, "rb").read() == b"bytes"
    # La recuperación tampoco carga bytes: solo la ruta.
    svc2, plan2, _ = _servicio()
    items = []
    svc2.lotes.agregar = lambda y, d, it, ventana=None: items.append(it)
    svc2.recuperar(YCloudSinRed(), espera=0.01)
    assert items[0].contenido is None and items[0].ruta.endswith("wamid.l.bin")
    # Y al procesar se leen del disco (YCloudSinRed falla si intenta descargar).
    svc2.procesar_lote(YCloudSinRed(), DEST, items)
    assert plan2.llamadas == [["planilla1.jpg"]]


def test_flush_por_timer_lo_espera_esperar_y_apagar():
    empezo, soltar, termino = threading.Event(), threading.Event(), []

    def flush(y, d, items):
        empezo.set()
        soltar.wait(5)
        time.sleep(0.2)
        termino.append(d)

    lotes = Lotes(flush, ventana=0.01, maximo=1000, espera_max=7200)
    lotes.agregar(None, "+57", Pendiente({}, "a"))
    assert empezo.wait(5)
    soltar.set()
    lotes.esperar(5)
    assert termino == ["+57"]

    # Apagado: espera a lo que está en curso y no arranca timers nuevos.
    empezo.clear()
    soltar.clear()
    lotes.agregar(None, "+58", Pendiente({}, "b"))
    assert empezo.wait(5)
    lotes.agregar(None, "+59", Pendiente({}, "c"))  # aún en ventana
    threading.Timer(0.1, soltar.set).start()
    assert lotes.apagar(5) is True
    assert termino == ["+57", "+58"]
    lotes.agregar(None, "+60", Pendiente({}, "d"))
    time.sleep(0.1)
    assert termino == ["+57", "+58"] and lotes.pendientes("+59") == 0 and lotes.pendientes("+60") == 0


def test_rotacion_tolera_permission_error(tmp_path, monkeypatch, caplog):
    store = Pendientes(tmp_path, max_procesados=2)

    def bloqueado(src, dst):
        raise PermissionError("archivo en uso")

    monkeypatch.setattr(pend_mod.os, "replace", bloqueado)
    for i in range(10):
        store._marcar_procesados([f"m{i}"])  # nunca lanza
    assert store.procesado("m9")
    assert "no se pudo rotar" in caplog.text
    monkeypatch.undo()
    (tmp_path / DEST).mkdir()
    store.completar(DEST, ["m10"])
    assert store.procesado("m10")


def test_lote_id_de_telemetria_llega_a_los_hilos_de_clasificacion(entorno, monkeypatch):
    from app.infrastructure import telemetria

    monkeypatch.setenv("INBOUND_PROCESS_WORKERS", "4")
    svc, _, _ = _servicio()
    vistos = []
    real = svc._clasificar

    def espia(doc, caption):
        vistos.append(telemetria.lote_actual.get())
        return real(doc, caption)

    svc._clasificar = espia
    items = [Pendiente({"link": n}, n) for n in ["planilla1.jpg", "rem1.jpg"]]
    svc.procesar_lote(FakeYCloud(), DEST, items)
    assert len(vistos) == 2 and len(set(vistos)) == 1 and vistos[0].startswith("_573111111111_")
    assert telemetria.lote_actual.get() is None


def test_lote_id_unico_y_ordenable_en_el_mismo_segundo(monkeypatch):
    import re
    import time as _time

    from app.infrastructure import pendientes as pmod

    real_strftime = _time.strftime  # time es el mismo módulo: capturar antes de parchear
    monkeypatch.setattr(pmod.time, "strftime", lambda fmt, *a: real_strftime(fmt, (2026, 11, 20, 11, 7, 37, 4, 324, 0)))
    ids = {pmod.nuevo_lote_id("573001234567") for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"573001234567_20261120_110737_[0-9a-f]{8}", i) for i in ids)
    assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", pmod.nuevo_lote_id())
    # orden cronológico por el prefijo de fecha
    monkeypatch.undo()
    assert pmod.nuevo_lote_id("x") > "x_20261120_110737_ffffffff"

