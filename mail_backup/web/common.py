"""Общее для веб-панели: шаблоны, вход, CSRF, сообщения, форматирование."""

import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import app_settings, auth
from ..db import get_session
from ..models import RunKind, RunStatus, RunTrigger

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


# --- Вход ------------------------------------------------------------------------------

class LoginRequired(Exception):
    """Нужен вход — обработчик в app.py перенаправит на страницу входа."""

    def __init__(self, url: str) -> None:
        self.url = url


def require_admin(request: Request, session: Session = Depends(get_session)) -> None:
    if not auth.admin_exists(session):
        raise LoginRequired("/setup")
    # В сессии хранится «версия» пароля: после смены пароля старые сессии недействительны
    version = auth.session_version(session)
    if not version or request.session.get("admin") != version:
        raise LoginRequired("/login?next=" + quote(request.url.path))


# --- CSRF -------------------------------------------------------------------------------

def safe_next(value: str | None, default: str = "/") -> str:
    """Куда вернуться после действия — только локальный путь этой панели.

    Защита от перенаправления на чужой сайт: «//host» и «/\\host» браузер
    считает адресом другого сервера.
    """
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def csrf_protect(request: Request) -> None:
    """Проверка токена для всех изменяющих запросов (формы и htmx)."""
    expected = request.session.get("csrf")
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        form = await request.form()
        sent = form.get("csrf_token")
    if not expected or not isinstance(sent, str) or not secrets.compare_digest(sent, expected):
        raise HTTPException(403, "Сессия устарела, обновите страницу")


# --- Сообщения после действий ---------------------------------------------------------

def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def render(request: Request, session: Session, name: str, context: dict | None = None, status_code: int = 200):
    """Отрисовка страницы: подмешивает сообщения и форматирование дат по часовому поясу."""
    prefs = app_settings.load(session)
    ctx = {"prefs": prefs, "dt": lambda value: fmt_dt(value, prefs), **(context or {})}
    # Фрагменты htmx передают flashes=[] — сообщения дождутся следующей полной страницы
    if "flashes" not in ctx:
        ctx["flashes"] = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# --- Форматирование ----------------------------------------------------------------

def fmt_dt(value: datetime | None, prefs: app_settings.AppSettings) -> str:
    if value is None:
        return "—"
    # В БД — наивный UTC (см. models.utcnow)
    return value.replace(tzinfo=timezone.utc).astimezone(prefs.tz).strftime("%d.%m.%Y %H:%M")


def fmt_size(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def fmt_num(value: int | None) -> str:
    """12345 → «12 345» (неразрывный пробел между разрядами)."""
    if value is None:
        return "—"
    return f"{value:,}".replace(",", "\u00a0")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


STATUS_LABELS = {
    RunStatus.QUEUED: "в очереди",
    RunStatus.RUNNING: "выполняется",
    RunStatus.SUCCESS: "успешно",
    RunStatus.WARNING: "с ошибками",
    RunStatus.FAILED: "сбой",
    RunStatus.CANCELLED: "отменён",
}
KIND_LABELS = {
    RunKind.BACKUP: "бэкап",
    RunKind.RESTORE: "восстановление",
    RunKind.MIGRATE: "перенос",
    RunKind.ESTIMATE: "оценка объёма",
}
TRIGGER_LABELS = {RunTrigger.MANUAL: "вручную", RunTrigger.SCHEDULE: "по расписанию"}

templates.env.globals.update(
    csrf_token=csrf_token,
    STATUS_LABELS=STATUS_LABELS,
    KIND_LABELS=KIND_LABELS,
    TRIGGER_LABELS=TRIGGER_LABELS,
)
templates.env.filters.update(size=fmt_size, duration=fmt_duration, num=fmt_num)
