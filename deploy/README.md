# Despliegue — OCR Excel (Valdivia)

Mismo VPS Hetzner y mismo patrón que *electricos*: stack propio con Docker Compose,
**sin base de datos**, detrás del **Caddy compartido** de inmobiliaria.

```
WhatsApp ─▶ YCloud ─POST─▶ https://excel.techapoli.com/webhooks/ycloud
                              │ Caddy (compartido) · red techapoli_web
                              ▼
                       ocr-excel-app:8010
                         1. verifica firma HMAC (YCloud-Signature)
                         2. dedup por message-id, responde 200
                         3. fondo: descarga media → clasifica → Proceso 1 o 2
                         4. responde por WhatsApp con el .xlsx (enlace firmado /descargas/…)
                              │
                              └─ volumen outputs (/data/outputs)
```

## Clasificación (¿a qué proceso va?)

De más barato a más caro, se detiene en el primer nivel concluyente
(`app/services/classifier_service.py`):

| Nivel | Fuente | Costo |
|---|---|---|
| 1. Reglas | caption del WhatsApp + nombre de archivo + texto embebido del PDF | 0 |
| 2. Encabezado + reglas | `CLASSIFIER_MODEL` (gpt-4o-mini, detail low, pág. 1) | muy bajo |
| 3. Jev | `Choice` sobre el encabezado, si `TYPESAFE_API_KEY` y confianza ≥ `JEV_SUPPORT_THRESHOLD` | bajo |
| 4. Modelo | tipo sugerido en el paso 2 | — |

Si nada concluye se le responde al remitente que reenvíe con *planilla* o *remisión*.
Truco operativo: si las fincas escriben "planilla" o "remisión" en el texto de la foto,
todo se decide en el nivel 1, sin costo.

## Una sola vez en el servidor

1. DNS: registro A `excel.techapoli.com` → IP del VPS.
2. Red compartida (ya existe si corren inmobiliaria/electricos): `docker network create techapoli_web`.
3. Caddy: el bloque `excel.techapoli.com` ya está en `inmobiliaria/deploy/Caddyfile`
   (se sincroniza con el deploy de inmobiliaria). Para aplicarlo sin esperar ese deploy,
   cópielo a mano y: `docker exec <caddy> caddy reload --config /etc/caddy/Caddyfile`.
4. Carpeta y `.env`:
   ```bash
   sudo mkdir -p /opt/ocr-excel && sudo chown $DEPLOY_USER /opt/ocr-excel
   # cree /opt/ocr-excel/.env a partir de deploy/.env.example (OPENAI_API_KEY, YCLOUD_*,
   # DOWNLOAD_URL_SECRET, PUBLIC_BASE_URL=https://excel.techapoli.com, YCLOUD_ENABLED=true)
   ```
5. YCloud (dashboard → Webhooks): endpoint `https://excel.techapoli.com/webhooks/ycloud`,
   evento `whatsapp.inbound_message.received`; copie el secreto `whsec_…` a `YCLOUD_WEBHOOK_SECRET`.

## CI/CD

Push a `main` → `.github/workflows/deploy.yml`: tests → build y push a
`ghcr.io/juanhenao31/ocr-excel` → copia `deploy/docker-compose.yml` a `/opt/ocr-excel` →
`docker compose pull && up -d`. Secretos del repo: los mismos de electricos/inmobiliaria
(`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `GHCR_USER`, `GHCR_TOKEN`).

Rollback: fije `IMAGE=ghcr.io/juanhenao31/ocr-excel:sha-xxxxxxx` en el `.env` y `up -d`.

## Diagnóstico

```bash
docker compose logs -f app   # cada documento deja "clasificado <archivo> -> <ruta> (<tipo>, <método>, conf=…)"
curl https://excel.techapoli.com/health
```
