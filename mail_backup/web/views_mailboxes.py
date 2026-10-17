"""Почтовые ящики: список, добавление, правка, удаление, проверка, запуск бэкапа."""

import re
from dataclasses import asdict, dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import app_settings, crypto, deletion_guard, dovecot
from ..config import get_settings
from ..db import get_session
from ..imapcheck import check_login
from ..jobs import active_run, enqueue_run, reset_cache
from ..models import Mailbox, RunKind, RunTrigger
from ..providers import PROVIDERS, Security, get_provider
from ..schedule import parse_schedule
from .common import csrf_protect, flash, render, require_admin, templates
from .dashboard import mailbox_overview

router = APIRouter(prefix="/mailboxes", dependencies=[Depends(require_admin)])


@dataclass
class MailboxForm:
    """Значения формы ящика (строки — как пришли из браузера)."""

    name: str = ""
    provider: str = "yandex"
    host: str = ""
    port: str = "993"
    security: str = Security.SSL.value
    login: str = ""
    password: str = ""
    enabled: bool = True
    schedule: str = "03:00"
    retention_days: str = "30"
    exclude_folders: str = ""
    mirror_deletions: bool = False
    note: str = ""

    @classmethod
    async def from_request(cls, request: Request) -> "MailboxForm":
        form = await request.form()
        checkboxes = ("enabled", "mirror_deletions")
        values = {k: str(form.get(k, "")).strip() for k in cls.__dataclass_fields__ if k not in checkboxes}
        # Пароль не обрезаем: пробелы могут быть его частью
        values["password"] = str(form.get("password", ""))
        return cls(**values, **{k: form.get(k) == "on" for k in checkboxes})

    @classmethod
    def from_mailbox(cls, box: Mailbox) -> "MailboxForm":
        return cls(
            name=box.name,
            provider=box.provider,
            host=box.host,
            port=str(box.port),
            security=str(box.security),
            login=box.login,
            enabled=box.enabled,
            schedule=box.schedule or "",
            retention_days=str(box.retention_days),
            exclude_folders=box.exclude_folders or "",
            mirror_deletions=box.mirror_deletions,
            note=box.note or "",
        )

    def validate(self, *, creating: bool) -> dict[str, str]:
        """Возвращает ошибки по полям; пустой словарь — всё верно."""
        errors = {}
        if self.provider not in PROVIDERS:
            errors["provider"] = "Выберите хостера"
        if not self.host:
            errors["host"] = "Укажите IMAP-сервер"
        if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
            errors["port"] = "Порт — число от 1 до 65535"
        if self.security not in {s.value for s in Security}:
            errors["security"] = "Выберите тип шифрования"
        if creating:
            if not self.login:
                errors["login"] = "Укажите логин (адрес ящика)"
            else:
                try:
                    dovecot.validate_local_login(self.login.lower())
                except ValueError:
                    errors["login"] = "Недопустимые символы в логине"
            if not self.password:
                errors["password"] = "Укажите пароль (пароль приложения)"
        try:
            parse_schedule(self.schedule)
        except ValueError as exc:
            errors["schedule"] = str(exc)
        if not self.retention_days.isdigit():
            errors["retention_days"] = "Число дней, 0 — бессрочно"
        if self.exclude_folders:
            try:
                re.compile(self.exclude_folders)
            except re.error as exc:
                errors["exclude_folders"] = f"Неверное регулярное выражение: {exc}"
        return errors

    def apply(self, box: Mailbox) -> None:
        box.name = self.name or self.login or box.login
        box.provider = self.provider
        box.host = self.host
        box.port = int(self.port)
        box.security = Security(self.security)
        box.enabled = self.enabled
        box.schedule = ", ".join(t.strftime("%H:%M") for t in parse_schedule(self.schedule)) or None
        box.retention_days = int(self.retention_days)
        box.exclude_folders = self.exclude_folders or None
        box.mirror_deletions = self.mirror_deletions
        box.note = self.note or None
        if self.password:
            box.password_enc = crypto.encrypt(self.password)


def _presets() -> list[dict]:
    return [asdict(p) | {"security": p.security.value} for p in PROVIDERS.values()]


def _view_access(session: Session, box: Mailbox | None, password: str | None) -> dict | None:
    """Данные для подключения почтового клиента к бэкапу ящика (только чтение)."""
    if box is None:
        return None
    prefs = app_settings.load(session)
    return {
        "enabled": bool(box.view_password_enc),
        "host": prefs.view_imap_host,
        "port": get_settings().local_imap_port,
        "login": box.view_login,
        "password": password,
        "nets": app_settings.parse_nets(prefs.view_allowed_nets),
    }


def _form_page(request: Request, session: Session, form: MailboxForm, *, box: Mailbox | None = None,
               errors: dict | None = None, status_code: int = 200, view_password: str | None = None):
    return render(request, session, "mailboxes/form.html", {
        "form": form,
        "box": box,
        "errors": errors or {},
        "presets": _presets(),
        "provider_note": get_provider(form.provider).note,
        "view": _view_access(session, box, view_password),
    }, status_code)


def _get_box(session: Session, mailbox_id: int) -> Mailbox:
    box = session.get(Mailbox, mailbox_id)
    if box is None:
        raise HTTPException(404, "Ящик не найден")
    return box


@router.get("")
def mailbox_list(request: Request, session: Session = Depends(get_session)):
    return render(request, session, "mailboxes/list.html", {"rows": mailbox_overview(session)})


@router.get("/new")
def mailbox_new(request: Request, session: Session = Depends(get_session)):
    preset = get_provider("yandex")
    form = MailboxForm(host=preset.host, port=str(preset.port), security=preset.security.value)
    return _form_page(request, session, form)


@router.post("/new", dependencies=[Depends(csrf_protect)])
async def mailbox_create(request: Request, session: Session = Depends(get_session)):
    form = await MailboxForm.from_request(request)
    if errors := form.validate(creating=True):
        return _form_page(request, session, form, errors=errors, status_code=400)
    local_login, local_password_enc = dovecot.new_local_credentials(session, form.login)
    box = Mailbox(login=form.login, local_login=local_login, local_password_enc=local_password_enc)
    form.apply(box)
    session.add(box)
    session.commit()
    dovecot.sync_users(session)
    flash(request, f"Ящик «{box.name}» добавлен. Чтобы узнать, сколько места нужно под бэкап, "
                   "нажмите «Оценить» — письма при этом не копируются.")
    return RedirectResponse("/mailboxes", 303)


@router.get("/{mailbox_id}/edit")
def mailbox_edit(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    return _form_page(request, session, MailboxForm.from_mailbox(box), box=box)


@router.post("/{mailbox_id}/edit", dependencies=[Depends(csrf_protect)])
async def mailbox_update(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    form = await MailboxForm.from_request(request)
    # Логин не меняется: другой логин — это другой ящик, его бэкап нельзя
    # смешивать с этим (в зеркале оказались бы письма двух ящиков)
    form.login = box.login
    if errors := form.validate(creating=False):
        return _form_page(request, session, form, box=box, errors=errors, status_code=400)
    form.apply(box)
    session.commit()
    flash(request, f"Ящик «{box.name}» сохранён")
    return RedirectResponse("/mailboxes", 303)


@router.post("/{mailbox_id}/delete", dependencies=[Depends(csrf_protect)])
def mailbox_delete(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    if active_run(session, box.id):
        flash(request, "Сначала дождитесь окончания или отмените текущий запуск", "error")
        return RedirectResponse(f"/mailboxes/{box.id}/edit", 303)
    maildir = get_settings().maildir_root / box.local_login
    name = box.name
    reset_cache(box)
    session.delete(box)
    session.commit()
    dovecot.sync_users(session)
    flash(request, f"Ящик «{name}» удалён. Его бэкап остался на диске: {maildir}")
    return RedirectResponse("/mailboxes", 303)


@router.post("/{mailbox_id}/reset-cache", dependencies=[Depends(csrf_protect)])
def mailbox_reset_cache(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    """Полная сверка: следующий бэкап сверит с зеркалом все письма, а не только новые."""
    box = _get_box(session, mailbox_id)
    if active_run(session, box.id):
        # imapsync пишет в кэш по ходу бэкапа — удалять из-под него нельзя
        flash(request, "Сначала дождитесь окончания или отмените текущий запуск", "error")
    elif reset_cache(box):
        flash(request, "Кэш сверки сброшен: следующий бэкап сверит с бэкапом все письма ящика — это дольше обычного")
    else:
        flash(request, "Кэша сверки ещё нет: следующий бэкап и так сверит все письма")
    return RedirectResponse(f"/mailboxes/{box.id}/edit", 303)


@router.post("/{mailbox_id}/resume-deletions", dependencies=[Depends(csrf_protect)])
def mailbox_resume_deletions(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    """Админ проверил ящик после срабатывания защиты от проседания и разрешил удаление."""
    box = _get_box(session, mailbox_id)
    if box.deletions_hold_at:
        deletion_guard.resume(session, box)
        flash(request, "Удаление разрешено: следующий бэкап удалит из бэкапа письма, которых нет на хостере. "
                       "Нынешнее число писем — новая точка отсчёта для защиты")
    return RedirectResponse(f"/mailboxes/{box.id}/edit", 303)


@router.post("/{mailbox_id}/view", dependencies=[Depends(csrf_protect)])
async def mailbox_view_access(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    """Учётка просмотра бэкапа почтовым клиентом: включить, показать пароль, выключить."""
    box = _get_box(session, mailbox_id)
    action = (await request.form()).get("action")
    if action == "enable" and not box.view_password_enc:
        box.view_password_enc = dovecot.new_view_password()
        session.commit()
        dovecot.sync_users(session)
        flash(request, "Просмотр включён: почтовый клиент видит бэкап, но ничего не может в нём изменить")
    elif action == "disable" and box.view_password_enc:
        box.view_password_enc = None
        session.commit()
        dovecot.sync_users(session)
        flash(request, "Просмотр выключен, учётка удалена")
    elif action == "password" and box.view_password_enc:
        # Пароль — только по явному запросу, не в обычной выдаче страницы
        return _form_page(request, session, MailboxForm.from_mailbox(box), box=box,
                          view_password=crypto.decrypt(box.view_password_enc))
    return RedirectResponse(f"/mailboxes/{box.id}/edit", 303)


@router.post("/check", dependencies=[Depends(csrf_protect)], response_class=HTMLResponse)
async def mailbox_check(request: Request, session: Session = Depends(get_session)):
    """Проверка входа с данными из формы (до сохранения)."""
    form = await MailboxForm.from_request(request)
    mailbox_id = (await request.form()).get("mailbox_id")
    login, password = form.login, form.password
    if mailbox_id and str(mailbox_id).isdigit():
        box = _get_box(session, int(mailbox_id))
        login = box.login
        # Пустое поле пароля при правке — проверяем сохранённый
        password = password or crypto.decrypt(box.password_enc)
    if not (form.host and form.port.isdigit() and login and password):
        result = {"ok": False, "message": "Заполните сервер, порт, логин и пароль"}
    else:
        res = await run_in_threadpool(check_login, form.host, int(form.port), Security(form.security), login, password)
        result = {"ok": res.ok, "message": res.message}
    return templates.TemplateResponse(request, "mailboxes/_check.html", result)


@router.post("/{mailbox_id}/backup", dependencies=[Depends(csrf_protect)])
async def mailbox_backup(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    box = _get_box(session, mailbox_id)
    dry_run = (await request.form()).get("dry_run") == "1"
    existing = active_run(session, box.id)
    run = enqueue_run(session, box, RunKind.BACKUP, RunTrigger.MANUAL, dry_run=dry_run)
    if existing:
        flash(request, "Этот ящик уже в работе — открыт текущий запуск", "warn")
    else:
        flash(request, f"Бэкап «{box.name}» поставлен в очередь" + (" (пробный прогон)" if dry_run else ""))
    return RedirectResponse(f"/runs/{run.id}", 303)


@router.post("/{mailbox_id}/estimate", dependencies=[Depends(csrf_protect)])
def mailbox_estimate(request: Request, mailbox_id: int, session: Session = Depends(get_session)):
    """Оценка объёма: только подсчёт писем и размеров, ничего не копируется."""
    box = _get_box(session, mailbox_id)
    existing = active_run(session, box.id)
    run = enqueue_run(session, box, RunKind.ESTIMATE, RunTrigger.MANUAL)
    if existing:
        flash(request, "Этот ящик уже в работе — открыт текущий запуск", "warn")
    else:
        flash(request, f"Оценка объёма «{box.name}» поставлена в очередь")
    return RedirectResponse(f"/runs/{run.id}", 303)

