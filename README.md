# Kiosco #2 — SaaS Directory Submitter

Servicio event-driven para rellenar y enviar un producto a cinco directorios
públicos de IA. Usa FastAPI, una cola persistente SQLite, Playwright/Chromium,
2Captcha y Telegram. No crea cuentas, no realiza pagos y no intenta evadir
login, paywalls ni challenges administrados sin un `sitekey` reutilizable.

## Directorios incluidos

1. Come AI — `https://www.iatool.online/submit-tool/`
2. ListAI.cc — `https://listai.cc/submit`
3. DayToDay.ai — `https://daytoday.ai/submit`
4. AI Tools Directory — `https://aitoolsdirectory.site/submit.html`

The Next AI is intentionally excluded until its current submission flow is
verified again. Directories that require a login, payment, CAPTCHA, email
verification, or reciprocal link are handled manually and are never run by the
automated worker.

## Vista previa sin interacción externa

`POST /preflight` acepta el mismo JSON que `POST /jobs` y requiere `X-API-Key`.
No abre el navegador, no crea un trabajo, no envía Telegram y no contacta ningún
directorio. Devuelve los destinos y enlaces que usaría un envío para poder
revisarlos antes de lanzar incluso una simulación.

`POST /audit` realiza el control completo contra los formularios vigentes: exige
que existan los campos, que el texto entre en sus límites, que la categoría tenga
una opción compatible y que esté disponible el botón de envío. No rellena texto,
no pulsa Submit, no crea un trabajo ni envía Telegram. `POST /jobs` ejecuta esa
misma auditoría y rechaza el lote completo si algún destino no es compatible.

Los selectores fueron comprobados el 25-09-2026. Los directorios son servicios
externos y pueden cambiar el DOM o sus condiciones sin aviso. El worker usa
IDs/nombres de campo, guarda una captura por intento y aísla cada fallo.

## Comportamiento seguro

### Pausa operativa

`SERVICE_PAUSED=true` es una parada reversible y estricta: no recupera jobs de
SQLite, no inicia el worker, no abre Playwright y no contacta 2Captcha. Las
rutas `/jobs`, `/submit`, `/preflight`, `/audit` y la consulta de trabajos
devuelven HTTP 503; `/` informa que el servicio está en pausa y `/health`
confirma el estado. La base de datos y `/app/data` nunca se modifican. Para
reactivar, definir explícitamente `SERVICE_PAUSED=false` y desplegar de nuevo.

- `DRY_RUN=true` por defecto: abre y rellena los cinco formularios, toma
  capturas y genera el reporte, pero no pulsa el botón final.
- Un formulario puede reintentarse antes de pulsar Submit. Después del clic no
  se reintenta, para evitar duplicados.
- Los directorios se procesan secuencialmente.
- Un fallo de DOM, red o timeout no interrumpe los directorios restantes.
- reCAPTCHA v2 y Turnstile independientes usan la API v2 de 2Captcha.
- Si 2Captcha no responde dentro de 60 segundos, el resultado es `Omitido`.
- La cola SQLite conserva jobs pendientes y resultados parciales después de un
  reinicio. Ejecutar un solo proceso Uvicorn para no duplicar workers.
- Los reintentos HTTP con el mismo encabezado `Idempotency-Key` devuelven el
  mismo `job_id`; así un webhook de pago o un doble clic no genera otro lote.
- Si un directorio falla antes del clic final, cada intento queda guardado en
  SQLite junto con el error, URL observada y captura disponible.
- Esta primera ruta es sólo para `product_type: "ai_tool"`. Una empresa de
  servicios o un SaaS general se rechaza antes de abrir formularios de IA.
- Si una categoría requerida no tiene coincidencia, la auditoría rechaza el lote
  antes de abrir una simulación o un envío; el sistema no elige una categoría
  aproximada para poder continuar.

Utiliza este servicio únicamente para productos que representes y en
directorios cuyas condiciones permitan el envío automatizado.

## Ejecución local con Docker

1. Copia `.env.example` a `.env` y configura al menos:

   ```text
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHANNEL_ID=1577307373
   TWOCAPTCHA_API_KEY=...
   WEBHOOK_SECRET=un_valor_largo_y_aleatorio
   DRY_RUN=true
   ```

2. Construye e inicia todo con un comando:

   ```bash
   docker compose up -d --build
   ```

3. Comprueba el servicio:

   ```bash
   curl http://localhost:8000/health
   ```

   La respuesta confirma por separado FastAPI, SQLite y la instalación de
   Playwright/Chromium. Si alguno no está disponible, responde HTTP 503.

4. Envía el payload de prueba:

   ```bash
   curl -X POST http://localhost:8000/submit \
     -H "Content-Type: application/json" \
     -H "X-API-Key: un_valor_largo_y_aleatorio" \
     -H "Idempotency-Key: payment-12345" \
     --data-binary @payload.test.json
   ```

La API devuelve HTTP 202 con un `job_id`. Consulta el progreso con:

```bash
curl -H "X-API-Key: un_valor_largo_y_aleatorio" \
  http://localhost:8000/jobs/JOB_ID
```

Después de revisar las capturas de la simulación, cambia `DRY_RUN=false` y
reinicia el contenedor para habilitar envíos reales.

El payload debe declarar el tipo de producto. La ruta actual sólo acepta:

```json
{ "product_type": "ai_tool" }
```

Los valores `saas` y `service` quedan reservados para las rutas de directorios
compatibles que se incorporarán después de su validación.

## CLI sin servidor

Con Python 3.12 instalado:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install --with-deps chromium
python main.py run payload.test.json
```

## Railway — despliegue directo

1. Sube esta carpeta como raíz de un repositorio GitHub.
2. En Railway elige **New Project → Deploy from GitHub Repo** y selecciona el
   repositorio. `railway.json` y `Dockerfile` se detectan automáticamente.
3. Copia las variables de `.env.example` en **Variables**. Mantén
   `DRY_RUN=true` durante la primera ejecución.
4. Añade un volumen montado en `/app/data`; conserva la cola SQLite y las
   capturas entre despliegues.
5. Pulsa **Generate Domain**. Usa `https://TU-DOMINIO/submit` como webhook.

No se incluye un botón Railway con URL inventada: el botón real solo puede
apuntar a un repositorio GitHub público. Una vez publicado el repositorio, el
paso 2 realiza el despliegue desde la interfaz sin configuración de build.

## VPS

Instala Docker y Docker Compose, copia la carpeta, crea `.env` y ejecuta:

```bash
docker compose up -d --build
```

Para exponerlo públicamente, coloca Caddy, Traefik o Nginx delante del puerto
8000 y activa HTTPS. El contenedor se reinicia automáticamente salvo que se
detenga manualmente.

## Respuesta y reporte

Estados de directorio:

- `Enviado`: POST/navegación aceptada sin cola editorial detectada.
- `Pendiente de aprobación`: el directorio confirmó la recepción y revisará el alta.
- `Error`: DOM, validación, red o respuesta HTTP fallida.
- `Omitido`: CAPTCHA no resuelto dentro de la kill rule o challenge no soportado.
- `Simulado`: formulario preparado bajo `DRY_RUN`.

Telegram recibe un CSV UTF-8 con URLs de formulario/confirmación, estado,
detalle, HTTP observado, intento y ruta de la captura. `Éxitos` cuenta solo
`Enviado`; el resumen conserva por separado pendientes, simulados, omitidos y
errores.

El mensaje final separa enviados, pendientes, simulados, omitidos y errores.
El detalle completo del lote puede consultarse con `GET /jobs/{job_id}`: incluye
`attempts`, el registro de fallos transitorios y el resultado final por
directorio.
