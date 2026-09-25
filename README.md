# Kiosco #2 — SaaS Directory Submitter

Servicio event-driven para rellenar y enviar un producto a cinco directorios
públicos de IA. Usa FastAPI, una cola persistente SQLite, Playwright/Chromium,
2Captcha y Telegram. No crea cuentas, no realiza pagos y no intenta evadir
login, paywalls ni challenges administrados sin un `sitekey` reutilizable.

## Directorios incluidos

1. Come AI — `https://www.iatool.online/submit-tool/`
2. The Next AI — `https://www.thenextai.com/submit-ai-tool/`
3. ListAI.cc — `https://listai.cc/submit`
4. DayToDay.ai — `https://daytoday.ai/submit`
5. AI Tools Directory — `https://aitoolsdirectory.site/submit.html`

Los selectores fueron comprobados el 25-09-2026. Los directorios son servicios
externos y pueden cambiar el DOM o sus condiciones sin aviso. El worker usa
IDs/nombres de campo, guarda una captura por intento y aísla cada fallo.

## Comportamiento seguro

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

Utiliza este servicio únicamente para productos que representes y en
directorios cuyas condiciones permitan el envío automatizado.

## Ejecución local con Docker

1. Copia `.env.example` a `.env` y configura al menos:

   ```text
   TELEGRAM_BOT_TOKEN=...
   TWOCAPTCHA_API_KEY=...
   WEBHOOK_API_KEY=un_valor_largo_y_aleatorio
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

4. Envía el payload de prueba:

   ```bash
   curl -X POST http://localhost:8000/submit \
     -H "Content-Type: application/json" \
     -H "X-API-Key: un_valor_largo_y_aleatorio" \
     --data-binary @payload.test.json
   ```

La API devuelve HTTP 202 con un `job_id`. Consulta el progreso con:

```bash
curl -H "X-API-Key: un_valor_largo_y_aleatorio" \
  http://localhost:8000/jobs/JOB_ID
```

Después de revisar las capturas de la simulación, cambia `DRY_RUN=false` y
reinicia el contenedor para habilitar envíos reales.

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
detalle, HTTP observado, intento y ruta de la captura. Como pidió el contrato,
`Éxitos` cuenta solo `Enviado`; pendientes, errores, omitidos y simulados se
agrupan en `Fallidos/Pendientes`.
