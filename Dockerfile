# OCR Excel (Valdivia) — imagen de producción. La construye GitHub Actions y se publica en GHCR.
# Un solo worker de uvicorn: el dedup de mensajes de YCloud es en memoria (por proceso).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUTS_DIR=/data/outputs

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Incluye docs/ (la plantilla Excel de Valdivia es parte del runtime).
COPY . .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/outputs \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
