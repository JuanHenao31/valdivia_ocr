import httpx
import pytest

from app.infrastructure import ycloud
from app.infrastructure.ycloud import YCloudClient, YCloudRateLimitError, YCloudSendError

RL = '{"error":{"whatsappApiError":{"code":131056,"message":"(Business Account, Consumer Account) pair rate limit hit"}}}'


class Reloj:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def reloj(monkeypatch):
    r = Reloj()
    monkeypatch.setattr(ycloud.time, "monotonic", r.monotonic)
    monkeypatch.setattr(ycloud.time, "sleep", r.sleep)
    monkeypatch.setattr(ycloud.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(ycloud.settings, "ycloud_whatsapp_from", "+570000", raising=False)
    return r


def cliente(respuestas, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    llamadas = []
    it = iter(respuestas)

    def handler(req):
        llamadas.append(req)
        code, body = next(it)
        return httpx.Response(code, text=body)

    http = httpx.Client(base_url="https://api.ycloud.com", transport=httpx.MockTransport(handler))
    return YCloudClient(http=http), llamadas


def test_reintenta_131056_y_luego_ok(reloj, monkeypatch):
    c, llamadas = cliente([(400, RL), (400, RL), (200, "{}")], monkeypatch)
    c.enviar_texto("+573001", "hola")
    assert len(llamadas) == 3
    assert reloj.sleeps == [2.0, 4.0]


def test_agota_reintentos(reloj, monkeypatch):
    c, llamadas = cliente([(400, RL)] * 3, monkeypatch, YCLOUD_RATE_LIMIT_MAX_ATTEMPTS="3")
    with pytest.raises(YCloudRateLimitError) as e:
        c.enviar_texto("+573001", "hola")
    assert isinstance(e.value, YCloudSendError) and isinstance(e.value, httpx.HTTPStatusError)
    assert len(llamadas) == 3
    assert reloj.sleeps == [2.0, 4.0]


def test_otro_400_no_reintenta(reloj, monkeypatch):
    c, llamadas = cliente([(400, '{"error":"bad"}')], monkeypatch)
    with pytest.raises(YCloudSendError) as e:
        c.enviar_documento("+573001", "https://x/y.xlsx", "y.xlsx")
    assert not isinstance(e.value, YCloudRateLimitError)
    assert len(llamadas) == 1 and reloj.sleeps == []


def test_pacing_mismo_destino(reloj, monkeypatch):
    c, llamadas = cliente([(200, "{}")] * 3, monkeypatch, YCLOUD_MIN_INTERVAL_S="1.5")
    c.enviar_texto("+573001", "a")
    reloj.t += 0.5
    c.enviar_texto("+573001", "b")
    c.enviar_texto("+573002", "c")  # otro destino: sin espera
    assert reloj.sleeps == [pytest.approx(1.0)]
    assert len(llamadas) == 3
