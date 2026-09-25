#!/usr/bin/env python3
"""Kiosco #2: submission de un producto a directorios publicos de IA.

Modos de uso:
  python main.py serve
  python main.py run payload.test.json

El servicio usa una cola SQLite de un solo worker. Esto evita lanzar varios
navegadores a la vez y permite recuperar jobs pendientes despues de un reinicio.
DRY_RUN=true por defecto: rellena y captura formularios, pero no hace clic en
el boton final hasta que el operador lo habilite expresamente.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator
from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str
    twocaptcha_api_key: str
    webhook_api_key: str
    dry_run: bool
    headless: bool
    database_path: Path
    artifact_dir: Path
    captcha_timeout_seconds: int
    navigation_timeout_ms: int
    post_submit_wait_seconds: int
    directory_max_attempts: int


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
        twocaptcha_api_key=os.getenv("TWOCAPTCHA_API_KEY", "").strip(),
        webhook_api_key=os.getenv(
            "WEBHOOK_SECRET", os.getenv("WEBHOOK_API_KEY", "")
        ).strip(),
        dry_run=env_bool("DRY_RUN", True),
        headless=env_bool("HEADLESS", True),
        database_path=resolve_path(os.getenv("DATABASE_PATH", "data/jobs.sqlite3")),
        artifact_dir=resolve_path(os.getenv("ARTIFACT_DIR", "data/artifacts")),
        captcha_timeout_seconds=max(1, int(os.getenv("CAPTCHA_TIMEOUT_SECONDS", "60"))),
        navigation_timeout_ms=max(5_000, int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000"))),
        post_submit_wait_seconds=max(1, int(os.getenv("POST_SUBMIT_WAIT_SECONDS", "8"))),
        directory_max_attempts=max(1, min(3, int(os.getenv("DIRECTORY_MAX_ATTEMPTS", "2")))),
    )


settings = load_settings()
settings.database_path.parent.mkdir(parents=True, exist_ok=True)
settings.artifact_dir.mkdir(parents=True, exist_ok=True)


class SubmissionPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    product_name: str = Field(min_length=2, max_length=120)
    website_url: HttpUrl
    tagline: str = Field(min_length=5, max_length=160)
    description: str = Field(min_length=20, max_length=5_000)
    category: str = Field(min_length=2, max_length=200)
    contact_email: EmailStr
    telegram_chat_id: str = Field(default="", validate_default=True)

    # Opcionales: mejoran algunos formularios sin cambiar el contrato minimo.
    pricing_model: str = Field(default="Freemium", max_length=80)
    submitter_name: str = Field(default="", max_length=120)
    logo_url: Optional[HttpUrl] = None
    tags: List[str] = Field(default_factory=list, max_length=10)

    @field_validator("telegram_chat_id")
    @classmethod
    def validate_chat_id(cls, value: str) -> str:
        value = str(value or settings.telegram_channel_id).strip()
        if not re.fullmatch(r"-?\d+", value):
            raise ValueError(
                "telegram_chat_id debe ser numerico o TELEGRAM_CHANNEL_ID debe estar configurado"
            )
        return value


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str
    dry_run: bool


DirectoryStatus = Literal[
    "Enviado",
    "Pendiente de aprobación",
    "Error",
    "Omitido",
    "Simulado",
]


@dataclass
class DirectoryResult:
    directory: str
    form_url: str
    status: DirectoryStatus
    detail: str
    attempt: int
    confirmation_url: str = ""
    http_status: Optional[int] = None
    screenshot_path: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass(frozen=True)
class TextField:
    selector: str
    source: str
    required: bool = True


@dataclass(frozen=True)
class SelectField:
    selector: str
    source: str
    required: bool = True
    preferred_fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectorySpec:
    name: str
    url: str
    text_fields: tuple[TextField, ...]
    select_fields: tuple[SelectField, ...]
    submit_selector: str
    pre_click_selectors: tuple[str, ...] = ()
    form_selector: str = "form"
    manual_review: bool = True


# Formularios comprobados el 25-09-2026. Se usan IDs/names estables y no
# selectores nth-child. Si un DOM cambia, el directorio falla de forma aislada.
DIRECTORIES: tuple[DirectorySpec, ...] = (
    DirectorySpec(
        name="Come AI",
        url="https://www.iatool.online/submit-tool/",
        text_fields=(
            TextField("#email", "contact_email"),
            TextField("#tool-name", "product_name"),
            TextField("#tool-url", "website_url"),
            TextField("#category", "category", required=False),
            TextField("#description", "description"),
        ),
        select_fields=(),
        submit_selector='button[type="submit"]',
        form_selector="form.submit-form",
    ),
    DirectorySpec(
        name="The Next AI",
        url="https://www.thenextai.com/submit-ai-tool/",
        text_fields=(
            TextField("#f-name", "product_name"),
            TextField("#f-url", "website_url"),
            TextField("#f-short", "tagline"),
            TextField("#f-desc", "description"),
            TextField("#f-logo", "logo_url", required=False),
            TextField("#f-email", "contact_email"),
            TextField("#f-tags", "tags", required=False),
        ),
        select_fields=(
            SelectField("#f-cat", "category"),
            SelectField("#f-pricing", "pricing_model", preferred_fallbacks=("Freemium", "Other")),
        ),
        pre_click_selectors=("#toggleFree",),
        submit_selector="#submitBtn",
        form_selector="#submitBtn",
    ),
    DirectorySpec(
        name="ListAI.cc",
        url="https://listai.cc/submit",
        text_fields=(
            TextField("#toolName", "product_name"),
            TextField("#url", "website_url"),
            TextField("#description", "description"),
            TextField("#name", "submitter_name", required=False),
            TextField("#email", "contact_email"),
        ),
        select_fields=(SelectField("#category", "category"),),
        submit_selector='form button[type="submit"]',
    ),
    DirectorySpec(
        name="DayToDay.ai",
        url="https://daytoday.ai/submit",
        text_fields=(
            TextField('input[name="email"]', "contact_email"),
            TextField('input[name="tool-name"]', "product_name"),
            TextField('input[name="tool-url"]', "website_url"),
            TextField('input[name="short-description"]', "tagline"),
            TextField('textarea[name="full-description"]', "description"),
        ),
        select_fields=(SelectField('select[name="category"]', "category"),),
        submit_selector='form[name="tool-submission"] button[type="submit"]',
        form_selector='form[name="tool-submission"]',
    ),
    DirectorySpec(
        name="AI Tools Directory",
        url="https://aitoolsdirectory.site/submit.html",
        text_fields=(
            TextField("#toolname", "product_name"),
            TextField("#toolurl", "website_url"),
            TextField("#description", "description"),
            TextField("#email", "contact_email"),
        ),
        select_fields=(
            SelectField("#category", "category"),
            SelectField("#pricing", "pricing_model", required=False, preferred_fallbacks=("Freemium", "Other")),
        ),
        submit_selector='form button[type="submit"]',
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "directory"


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(self, payload: Dict[str, Any]) -> str:
        job_id = uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, "queued", json.dumps(payload, ensure_ascii=False), None, None, now, now),
            )
        return job_id

    def update(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: List[Any] = [utc_now()]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if result is not None:
            assignments.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            assignments.append("error = ?")
            values.append(error[:2_000])
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["id"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def recoverable_ids(self) -> List[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        return [row["id"] for row in rows]


store = JobStore(settings.database_path)
job_queue: asyncio.Queue[str] = asyncio.Queue()


class CaptchaTimeout(RuntimeError):
    pass


class CaptchaUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptchaChallenge:
    kind: Literal["recaptcha_v2", "turnstile"]
    site_key: str


class TwoCaptchaClient:
    CREATE_TASK_URL = "https://api.2captcha.com/createTask"
    GET_RESULT_URL = "https://api.2captcha.com/getTaskResult"

    def __init__(self, api_key: str, timeout_seconds: int) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def solve(self, challenge: CaptchaChallenge, page_url: str) -> str:
        if not self.api_key:
            raise CaptchaUnavailable("TWOCAPTCHA_API_KEY no esta configurada")

        task_type = (
            "RecaptchaV2TaskProxyless"
            if challenge.kind == "recaptcha_v2"
            else "TurnstileTaskProxyless"
        )
        request_body = {
            "clientKey": self.api_key,
            "task": {
                "type": task_type,
                "websiteURL": page_url,
                "websiteKey": challenge.site_key,
            },
        }
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds

        async with httpx.AsyncClient(timeout=15.0) as client:
            create_response = await client.post(self.CREATE_TASK_URL, json=request_body)
            create_response.raise_for_status()
            created = create_response.json()
            if created.get("errorId"):
                raise RuntimeError(created.get("errorDescription") or created.get("errorCode"))
            task_id = created.get("taskId")
            if not task_id:
                raise RuntimeError("2Captcha no devolvio taskId")

            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                await asyncio.sleep(min(5.0, max(0.1, remaining)))
                response = await client.post(
                    self.GET_RESULT_URL,
                    json={"clientKey": self.api_key, "taskId": task_id},
                )
                response.raise_for_status()
                result = response.json()
                if result.get("errorId"):
                    raise RuntimeError(result.get("errorDescription") or result.get("errorCode"))
                if result.get("status") == "ready":
                    solution = result.get("solution") or {}
                    token = solution.get("gRecaptchaResponse") or solution.get("token")
                    if not token:
                        raise RuntimeError("2Captcha devolvio una solucion sin token")
                    return str(token)

        raise CaptchaTimeout(f"2Captcha no respondio en {self.timeout_seconds} segundos")


def payload_value(payload: Dict[str, Any], source: str) -> str:
    value = payload.get(source)
    if source == "tags":
        tags = value or [part.strip() for part in re.split(r"[/,|]", payload["category"]) if part.strip()]
        return ", ".join(str(item) for item in tags[:10])
    return "" if value is None else str(value)


async def fill_text(page: Page, field_spec: TextField, payload: Dict[str, Any]) -> None:
    locator = page.locator(field_spec.selector).first
    if await locator.count() == 0:
        if field_spec.required:
            raise RuntimeError(f"Campo requerido no encontrado: {field_spec.selector}")
        return
    value = payload_value(payload, field_spec.source)
    if not value:
        if field_spec.required:
            raise RuntimeError(f"Valor requerido vacio: {field_spec.source}")
        return
    max_length = await locator.get_attribute("maxlength")
    if max_length and max_length.isdigit():
        value = value[: int(max_length)]
    await locator.fill(value)


def token_set(value: str) -> set[str]:
    ignored = {"and", "the", "for", "de", "y", "ia", "ai", "tool", "tools"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 1 and token not in ignored
    }


async def select_best_option(page: Page, field_spec: SelectField, payload: Dict[str, Any]) -> None:
    locator = page.locator(field_spec.selector).first
    if await locator.count() == 0:
        if field_spec.required:
            raise RuntimeError(f"Selector requerido no encontrado: {field_spec.selector}")
        return
    options = await locator.locator("option").evaluate_all(
        """elements => elements.map(option => ({
            value: option.value,
            label: (option.textContent || '').trim(),
            disabled: option.disabled
        }))"""
    )
    candidates = [option for option in options if option["value"] and not option["disabled"]]
    if not candidates:
        if field_spec.required:
            raise RuntimeError(f"El select no tiene opciones utilizables: {field_spec.selector}")
        return

    requested = payload_value(payload, field_spec.source)
    requested_tokens = token_set(requested)

    def score(option: Dict[str, Any]) -> tuple[int, int]:
        label = str(option["label"])
        overlap = len(requested_tokens.intersection(token_set(label)))
        contains = int(label.lower() in requested.lower() or requested.lower() in label.lower())
        return overlap, contains

    best = max(candidates, key=score)
    if score(best) == (0, 0):
        for preferred in (*field_spec.preferred_fallbacks, "Other", "General"):
            match = next(
                (option for option in candidates if preferred.lower() in option["label"].lower()),
                None,
            )
            if match:
                best = match
                break
    await locator.select_option(value=best["value"])


async def dismiss_common_overlays(page: Page) -> None:
    pattern = re.compile(r"^(accept|accept all|allow all|i agree|got it|aceptar|aceptar todo)$", re.I)
    with suppress(PlaywrightError):
        buttons = page.get_by_role("button", name=pattern)
        for index in range(min(await buttons.count(), 3)):
            button = buttons.nth(index)
            if await button.is_visible():
                await button.click(timeout=2_000)
                return


async def solve_simple_math_captcha(page: Page) -> None:
    locator = page.locator("#captchaInput").first
    if await locator.count() == 0 or not await locator.is_visible():
        return
    parent_text = await locator.locator("xpath=..").inner_text()
    body_text = await page.locator("body").inner_text()
    match = re.search(r"(\d+)\s*([+\-*x×])\s*(\d+)", parent_text) or re.search(
        r"(\d+)\s*([+\-*x×])\s*(\d+)", body_text
    )
    if not match:
        raise CaptchaUnavailable("No se pudo interpretar el control matematico")
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "+":
        answer = left + right
    elif operator == "-":
        answer = left - right
    else:
        answer = left * right
    await locator.fill(str(answer))


async def detect_captcha(page: Page) -> Optional[CaptchaChallenge]:
    recaptcha = page.locator(".g-recaptcha[data-sitekey]").first
    if await recaptcha.count():
        key = await recaptcha.get_attribute("data-sitekey")
        if key:
            return CaptchaChallenge("recaptcha_v2", key)

    turnstile = page.locator(".cf-turnstile[data-sitekey], [data-turnstile-sitekey]").first
    if await turnstile.count():
        key = await turnstile.get_attribute("data-sitekey") or await turnstile.get_attribute(
            "data-turnstile-sitekey"
        )
        if key:
            return CaptchaChallenge("turnstile", key)

    iframe_sources = await page.locator("iframe").evaluate_all(
        "elements => elements.map(frame => frame.src || '')"
    )
    for source in iframe_sources:
        parsed = urlparse(source)
        query = parse_qs(parsed.query)
        if "recaptcha" in source:
            key = (query.get("k") or query.get("sitekey") or [""])[0]
            if key:
                return CaptchaChallenge("recaptcha_v2", key)
        if "turnstile" in source:
            key = (query.get("k") or query.get("sitekey") or [""])[0]
            if key:
                return CaptchaChallenge("turnstile", key)

    body_text = (await page.locator("body").inner_text()).lower()
    has_captcha_response = await page.locator(
        '[name="g-recaptcha-response"], [name="cf-turnstile-response"]'
    ).count()
    if has_captcha_response or (
        "just a moment" in body_text and "verify you are human" in body_text
    ):
        raise CaptchaUnavailable(
            "Se detecto un CAPTCHA/challenge sin sitekey reutilizable; se omite para no forzar el sitio"
        )

    return None


async def inject_captcha_token(page: Page, challenge: CaptchaChallenge, token: str) -> None:
    injected = await page.evaluate(
        """
        ({kind, token}) => {
          const names = kind === 'recaptcha_v2'
            ? ['g-recaptcha-response']
            : ['cf-turnstile-response', 'g-recaptcha-response'];
          let updated = 0;
          for (const name of names) {
            let elements = Array.from(document.querySelectorAll(`[name="${name}"]`));
            if (!elements.length && name === names[0]) {
              const input = document.createElement('textarea');
              input.name = name;
              input.style.display = 'none';
              (document.querySelector('form') || document.body).appendChild(input);
              elements = [input];
            }
            for (const element of elements) {
              element.value = token;
              element.textContent = token;
              element.dispatchEvent(new Event('input', {bubbles: true}));
              element.dispatchEvent(new Event('change', {bubbles: true}));
              updated += 1;
            }
          }
          const widget = document.querySelector(
            kind === 'recaptcha_v2' ? '.g-recaptcha[data-callback]' : '.cf-turnstile[data-callback]'
          );
          const callbackName = widget && widget.getAttribute('data-callback');
          if (callbackName) {
            const callback = callbackName.split('.').reduce((value, key) => value && value[key], window);
            if (typeof callback === 'function') callback(token);
          }
          return updated;
        }
        """,
        {"kind": challenge.kind, "token": token},
    )
    if not injected:
        raise RuntimeError("No fue posible inyectar el token CAPTCHA")


async def visible_success(page: Page) -> bool:
    pattern = re.compile(
        r"(tool submitted|submission received|successfully submitted|thanks for submitting|"
        r"thank you for submitting|submitted successfully|formulario enviado)",
        re.I,
    )
    matches = page.get_by_text(pattern)
    for index in range(min(await matches.count(), 8)):
        with suppress(PlaywrightError):
            if await matches.nth(index).is_visible():
                return True
    return False


async def take_screenshot(page: Page, path: Path) -> str:
    try:
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
    except PlaywrightError:
        return ""


async def classify_submission(
    page: Page,
    spec: DirectorySpec,
    initial_url: str,
    post_responses: List[Dict[str, Any]],
) -> tuple[DirectoryStatus, str, Optional[int]]:
    last_status = post_responses[-1]["status"] if post_responses else None
    if await visible_success(page):
        status: DirectoryStatus = "Pendiente de aprobación" if spec.manual_review else "Enviado"
        return status, "El sitio mostro una confirmacion visible", last_status
    if last_status is not None and last_status >= 400:
        return "Error", f"El formulario respondio HTTP {last_status}", last_status
    if last_status is not None and 200 <= last_status < 400:
        status = "Pendiente de aprobación" if spec.manual_review else "Enviado"
        return status, f"El formulario acepto el POST con HTTP {last_status}", last_status
    if page.url != initial_url:
        status = "Pendiente de aprobación" if spec.manual_review else "Enviado"
        return status, "El sitio navego a una pagina posterior al envio", last_status
    invalid_count = await page.locator(":invalid").count()
    if invalid_count:
        return "Error", f"El navegador detecto {invalid_count} campos invalidos", last_status
    return "Error", "No se detecto POST, navegacion ni confirmacion visible", last_status


async def submit_to_directory(
    browser: Browser,
    spec: DirectorySpec,
    payload: Dict[str, Any],
    job_artifact_dir: Path,
) -> DirectoryResult:
    solver = TwoCaptchaClient(settings.twocaptcha_api_key, settings.captcha_timeout_seconds)
    last_error = ""

    for attempt in range(1, settings.directory_max_attempts + 1):
        started_at = utc_now()
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(settings.navigation_timeout_ms)
        post_responses: List[Dict[str, Any]] = []
        submitted = False
        screenshot_path = job_artifact_dir / f"{slugify(spec.name)}-attempt-{attempt}.png"

        def record_response(response: Any) -> None:
            with suppress(Exception):
                if response.request.method.upper() in {"POST", "PUT", "PATCH"}:
                    post_responses.append({"url": response.url, "status": response.status})

        page.on("response", record_response)

        try:
            await page.goto(spec.url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
            initial_url = page.url
            await dismiss_common_overlays(page)

            for selector in spec.pre_click_selectors:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click()

            for text_field in spec.text_fields:
                await fill_text(page, text_field, payload)
            for select_field in spec.select_fields:
                await select_best_option(page, select_field, payload)

            await solve_simple_math_captcha(page)
            challenge = await detect_captcha(page)
            if challenge:
                token = await solver.solve(challenge, page.url)
                await inject_captcha_token(page, challenge, token)

            submit_button = page.locator(spec.submit_selector).first
            if await submit_button.count() == 0 or not await submit_button.is_visible():
                raise RuntimeError(f"Boton submit no encontrado: {spec.submit_selector}")

            if settings.dry_run:
                screenshot = await take_screenshot(page, screenshot_path)
                return DirectoryResult(
                    directory=spec.name,
                    form_url=spec.url,
                    status="Simulado",
                    detail="Formulario rellenado; DRY_RUN impidio el envio final",
                    attempt=attempt,
                    confirmation_url=page.url,
                    screenshot_path=screenshot,
                    started_at=started_at,
                    finished_at=utc_now(),
                )

            await submit_button.click(timeout=settings.navigation_timeout_ms)
            submitted = True
            with suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=settings.post_submit_wait_seconds * 1000)
            await asyncio.sleep(2)

            status, detail, http_status = await classify_submission(
                page, spec, initial_url, post_responses
            )
            screenshot = await take_screenshot(page, screenshot_path)
            return DirectoryResult(
                directory=spec.name,
                form_url=spec.url,
                status=status,
                detail=detail,
                attempt=attempt,
                confirmation_url=page.url,
                http_status=http_status,
                screenshot_path=screenshot,
                started_at=started_at,
                finished_at=utc_now(),
            )
        except CaptchaTimeout as exc:
            screenshot = await take_screenshot(page, screenshot_path)
            return DirectoryResult(
                directory=spec.name,
                form_url=spec.url,
                status="Omitido",
                detail=str(exc),
                attempt=attempt,
                confirmation_url=page.url,
                screenshot_path=screenshot,
                started_at=started_at,
                finished_at=utc_now(),
            )
        except CaptchaUnavailable as exc:
            screenshot = await take_screenshot(page, screenshot_path)
            return DirectoryResult(
                directory=spec.name,
                form_url=spec.url,
                status="Omitido",
                detail=str(exc),
                attempt=attempt,
                confirmation_url=page.url,
                screenshot_path=screenshot,
                started_at=started_at,
                finished_at=utc_now(),
            )
        except Exception as exc:  # noqa: BLE001 - aislamiento por directorio
            last_error = f"{type(exc).__name__}: {exc}"
            screenshot = await take_screenshot(page, screenshot_path)
            # Kill rule: nunca reintentar despues de hacer clic; podria duplicar el alta.
            if submitted or attempt >= settings.directory_max_attempts:
                return DirectoryResult(
                    directory=spec.name,
                    form_url=spec.url,
                    status="Error",
                    detail=last_error,
                    attempt=attempt,
                    confirmation_url=page.url,
                    screenshot_path=screenshot,
                    started_at=started_at,
                    finished_at=utc_now(),
                )
            await asyncio.sleep(attempt * 2)
        finally:
            with suppress(PlaywrightError):
                await context.close()

    return DirectoryResult(
        directory=spec.name,
        form_url=spec.url,
        status="Error",
        detail=last_error or "Fallo no clasificado",
        attempt=settings.directory_max_attempts,
        started_at=utc_now(),
        finished_at=utc_now(),
    )


def report_dict(
    job_id: str,
    payload: Dict[str, Any],
    results: List[DirectoryResult],
    *,
    telegram_sent: Optional[bool] = None,
    telegram_error: str = "",
) -> Dict[str, Any]:
    processed = len(results)
    successes = sum(result.status == "Enviado" for result in results)
    failed_or_pending = processed - successes
    return {
        "job_id": job_id,
        "product_name": payload["product_name"],
        "dry_run": settings.dry_run,
        "directories_processed": processed,
        "successes": successes,
        "failed_or_pending": failed_or_pending,
        "directories": [asdict(result) for result in results],
        "telegram_sent": telegram_sent,
        "telegram_error": telegram_error,
        "updated_at": utc_now(),
    }


def make_csv_report(results: List[DirectoryResult]) -> bytes:
    stream = io.StringIO(newline="")
    fieldnames = [
        "directory",
        "status",
        "detail",
        "attempt",
        "form_url",
        "confirmation_url",
        "http_status",
        "screenshot_path",
        "started_at",
        "finished_at",
    ]
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for result in results:
        writer.writerow(asdict(result))
    return stream.getvalue().encode("utf-8-sig")


async def send_telegram_report(
    payload: Dict[str, Any], results: List[DirectoryResult], job_id: str
) -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no esta configurado")
    processed = len(results)
    successes = sum(result.status == "Enviado" for result in results)
    failed_or_pending = processed - successes
    summary = (
        f"✅ Proceso de Envío Completado para {payload['product_name']}\n"
        f"- Directorios procesados: {processed}\n"
        f"- Éxitos: {successes}\n"
        f"- Fallidos/Pendientes: {failed_or_pending}"
    )
    filename = f"directory-submissions-{slugify(payload['product_name'])}-{job_id[:8]}.csv"
    endpoint = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            endpoint,
            data={"chat_id": payload["telegram_chat_id"], "caption": summary},
            files={"document": (filename, make_csv_report(results), "text/csv; charset=utf-8")},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram rechazo el documento"))


async def process_submission(
    payload: Dict[str, Any],
    job_id: str,
    existing_report: Optional[Dict[str, Any]] = None,
    progress: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    job_artifact_dir = settings.artifact_dir / job_id
    job_artifact_dir.mkdir(parents=True, exist_ok=True)
    existing_directories = (existing_report or {}).get("directories") or []
    results = [DirectoryResult(**item) for item in existing_directories]
    finished_names = {result.directory for result in results}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=settings.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            for spec in DIRECTORIES:
                if spec.name in finished_names:
                    continue
                result = await submit_to_directory(browser, spec, payload, job_artifact_dir)
                results.append(result)
                partial = report_dict(job_id, payload, results)
                if progress:
                    await progress(partial)
        finally:
            await browser.close()

    telegram_sent = False
    telegram_error = ""
    try:
        await send_telegram_report(payload, results, job_id)
        telegram_sent = True
    except (httpx.HTTPError, RuntimeError) as exc:
        telegram_error = f"{type(exc).__name__}: {exc}"

    return report_dict(
        job_id,
        payload,
        results,
        telegram_sent=telegram_sent,
        telegram_error=telegram_error,
    )


async def persist_progress(job_id: str, report: Dict[str, Any]) -> None:
    store.update(job_id, status="running", result=report)


async def job_worker() -> None:
    while True:
        job_id = await job_queue.get()
        try:
            job = store.get(job_id)
            if not job:
                continue
            store.update(job_id, status="running", error="")
            result = await process_submission(
                job["payload"],
                job_id,
                existing_report=job.get("result"),
                progress=lambda report: persist_progress(job_id, report),
            )
            store.update(job_id, status="completed", result=result, error="")
        except asyncio.CancelledError:
            store.update(job_id, status="queued")
            raise
        except Exception as exc:  # noqa: BLE001 - frontera del worker
            store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            job_queue.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI):
    for job_id in store.recoverable_ids():
        store.update(job_id, status="queued")
        await job_queue.put(job_id)
    worker = asyncio.create_task(job_worker(), name="directory-submitter-worker")
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(
    title="Kiosco #2 - SaaS Directory Submitter",
    version="1.0.0",
    lifespan=lifespan,
)


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if settings.webhook_api_key and (
        x_api_key is None or not secrets.compare_digest(x_api_key, settings.webhook_api_key)
    ):
        raise HTTPException(status_code=401, detail="X-API-Key invalida")


@app.get("/health")
async def health(response: Response) -> Dict[str, Any]:
    sqlite_ok = False
    playwright_ok = False

    try:
        with sqlite3.connect(settings.database_path, timeout=5) as connection:
            sqlite_ok = connection.execute("SELECT 1").fetchone() == (1,)
    except sqlite3.Error:
        sqlite_ok = False

    try:
        async with async_playwright() as playwright:
            playwright_ok = Path(playwright.chromium.executable_path).is_file()
    except PlaywrightError:
        playwright_ok = False

    ready = sqlite_ok and playwright_ok
    if not ready:
        response.status_code = 503

    return {
        "status": "ok" if ready else "degraded",
        "queue_size": job_queue.qsize(),
        "dry_run": settings.dry_run,
        "directories": len(DIRECTORIES),
        "checks": {
            "fastapi": True,
            "sqlite": sqlite_ok,
            "playwright_chromium": playwright_ok,
        },
    }


@app.get("/jobs")
async def jobs_status() -> Dict[str, Any]:
    """Public, non-sensitive status endpoint used by deployment smoke tests."""
    return {
        "status": "ok",
        "queue_size": job_queue.qsize(),
        "dry_run": settings.dry_run,
    }


@app.post(
    "/jobs",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@app.post(
    "/submit",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
async def create_job(payload: SubmissionPayload) -> JobAccepted:
    job_id = store.create(payload.model_dump(mode="json"))
    await job_queue.put(job_id)
    return JobAccepted(
        job_id=job_id,
        status="queued",
        status_url=f"/jobs/{job_id}",
        dry_run=settings.dry_run,
    )


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str) -> Dict[str, Any]:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiosco #2 - SaaS Directory Submitter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="Inicia la API FastAPI")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    run_parser = subparsers.add_parser("run", help="Procesa un payload JSON y termina")
    run_parser.add_argument("payload_file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "serve":
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    try:
        raw_payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
        payload = SubmissionPayload.model_validate(raw_payload).model_dump(mode="json")
        job_id = uuid4().hex
        result = asyncio.run(process_submission(payload, job_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("telegram_sent") else 2
    except Exception as exc:  # noqa: BLE001 - CLI
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
