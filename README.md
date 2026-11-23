# valdivia_ocr

OCR -> Excel para Valdivia. Despliegue: ver deploy/README.md

## Probar un lote en local (sin WhatsApp)

Corre el mismo flujo de lote del webhook (`InboundService.procesar_lote`) con fotos locales y un
YCloud falso: la descarga lee el archivo del disco y los envíos solo se registran. OCR, Jev y LLM
son **reales** (consumen saldo). Si aquí funciona y en producción no, el problema está en Meta/YCloud.

```bash
python -m scripts.probar_lote_local "test_data/WhatsApp Unknown 2026-11-20 at 11.07.37" --limit 10
python -m scripts.probar_lote_local test_data --solo-clasificar   # solo clasificación, sin Excel
```

Opciones: `--limit N`, `--workers N` (sobrescribe `INBOUND_PROCESS_WORKERS`), `--solo-clasificar`,
`--out DIR` (por defecto `output/prueba_local/<timestamp>/`). En la salida quedan los Excel generados,
`reporte.md` y `reporte.json`: clasificación por archivo (ruta, tipo, método, confianza, segundos),
fallos con su traceback, los mensajes que se habrían enviado, tiempos por fase y el total de envíos.
El harness usa el mismo camino que producción (`analizar` por foto → `armar` con las buenas): el
reporte trae el resultado de cada foto (ok / tipo de error), el tiempo de `armar`, la línea
`📦 Procesadas N fotos: …` tal cual se enviaría, métricas de la planilla y, si el logger
`app.telemetria` emite líneas JSON con `usage`, los tokens prompt/completion/reasoning por tipo y
total. El costo solo se estima si defines `OPENAI_PRECIOS_JSON` (USD por 1M tokens,
`{"<modelo>": {"in": X, "out": Y}}`); no hay precios por defecto.

`--repeticiones N` corre el mismo lote N veces (`run_1/ … run_N/`) y escribe `comparacion.md/json`
con, por corrida: filas, Modalidad (Jornal/Contrato), Jor/Rend vacíos, valores de Und y cuántos
"Total no cuadra" hay en la hoja Excepciones. Sirve para verificar regresiones (cuesta N veces).
`test_data/` está en `.gitignore`: son datos reales del cliente y nunca se versionan.

## Entrega de lotes por WhatsApp: al-menos-una-vez

Cada media entrante deja un sidecar en `pending/` **antes** de responder 200 al webhook (los bytes
se descargan después, en segundo plano). Un lote solo se marca procesado cuando termina; si el
proceso muere a mitad, al arrancar se recupera y se repite. Consecuencia: si el contenedor muere
durante el envío, el remitente puede recibir **un Excel duplicado** (se prefiere a perder fotos).
Un lote que tumba el proceso en cada arranque se aparta tras `PENDING_MAX_INTENTOS` (3) a
`pending/_fallidos/` y se avisa una vez al remitente; sidecars ilegibles van a `pending/_corruptos/`.
Al apagar se esperan los lotes en curso hasta `SHUTDOWN_TIMEOUT_S` (8 s); el resto queda en disco.
