"""Главная страница, настройки и сведения о системе."""

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import app_settings, auth
from ..config import get_settings
from ..db import get_session
from ..models import Mailbox, Run, RunKind, RunStatus
from ..worker import next_slot, parse_schedule, worker_alive
from .common import csrf_protect, flash, render, require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
# Удачным бэкапом считается и завершённый с ошибками по отдельным письмам
DONE_OK = (RunStatus.SUCCESS, RunStatus.WARNING)


@dataclass
class MailboxRow:
    box: Mailbox
    last_run: Run | None
    last_ok: Run | None
    active: Run | None
    next_at: datetime | None


def mailbox_overview(session: Session) -> list[MailboxRow]:
    prefs = app_settings.load(session)
    now_local = datetime.now(prefs.tz)
    rows = []
    for box in session.scalars(select(Mailbox).order_by(Mailbox.name)):
        backups = select(Run).where(Run.mailbox_id == box.id, Run.kind == RunKind.BACKUP).order_by(Run.id.desc())
        last_run = session.scalars(backups.where(Run.status.not_in(ACTIVE)).limit(1)).first()
        last_ok = session.scalars(backups.where(Run.status.in_(DONE_OK), Run.dry_run.is_(False)).limit(1)).first()
        active = session.scalars(
            select(Run).where(Run.mailbox_id == box.id, Run.status.in_(ACTIVE)).limit(1)
        ).first()
        try:
            nxt = next_slot(parse_schedule(box.schedule), now_local) if box.enabled else None
        except ValueError:
            nxt = None
        # next_slot отдаёт время в поясе админки; в шаблон — как наивный UTC, как всё остальное
        next_at = nxt.astimezone(timezone.utc).replace(tzinfo=None) if nxt else None
        rows.append(MailboxRow(box, last_run, last_ok, active, next_at))
    return rows


def disk_usage() -> dict | None:
    root = get_settings().backup_root
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "percent": round(usage.used * 100 / usage.total) if usage.total else 0}


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    active_runs = list(session.scalars(select(Run).where(Run.status.in_(ACTIVE)).order_by(Run.id)))
    return render(request, session, "dashboard.html", {
        "rows": mailbox_overview(session),
        "active_runs": active_runs,
        "disk": disk_usage(),
        "worker_ok": worker_alive(session),
    })


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    return render(request, session, "settings.html", {"admin_login": auth.admin_login(session)})


@router.post("/settings", dependencies=[Depends(csrf_protect)])
def settings_save(
    request: Request,
    max_parallel_jobs: int = Form(...),
    timezone_name: str = Form(..., alias="timezone"),
    log_retention_days: int = Form(...),
    session: Session = Depends(get_session),
):
    try:
        prefs = app_settings.validate(max_parallel_jobs, timezone_name.strip(), log_retention_days)
    except ValueError as exc:
        flash(request, str(exc), "error")
    else:
        app_settings.save(session, prefs)
        flash(request, "Настройки сохранены")
    return RedirectResponse("/settings", 303)


@router.post("/settings/account", dependencies=[Depends(csrf_protect)])
def account_save(
    request: Request,
    login: str = Form(""),
    current_password: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    session: Session = Depends(get_session),
):
    current_login = auth.admin_login(session) or ""
    if not auth.check_admin(session, current_login, current_password):
        flash(request, "Текущий пароль указан неверно", "error")
    elif password != password2:
        flash(request, "Новые пароли не совпадают", "error")
    else:
        try:
            auth.set_admin(session, login or current_login, password)
        except ValueError as exc:
            flash(request, str(exc), "error")
        else:
            # Смена пароля завершает все остальные сессии; текущую обновляем
            request.session["admin"] = auth.session_version(session)
            flash(request, "Логин и пароль обновлены")
    return RedirectResponse("/settings", 303)


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"недоступен ({exc})"
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "?"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


@router.get("/system")
def system_page(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    env = {
        "MB_DEPLOY_MODE": settings.deploy_mode,
        "MB_DATA_DIR": settings.data_dir,
        "MB_BACKUP_ROOT": settings.backup_root,
        "MB_SECRETS_DIR": settings.secrets_dir,
        "MB_LOCAL_IMAP_HOST": settings.local_imap_host,
        "MB_LOCAL_IMAP_PORT": settings.local_imap_port,
        "MB_IMAPSYNC_BIN": settings.imapsync_bin,
    }
    return render(request, session, "system.html", {
        "env": env,
        "disk": disk_usage(),
        "backup_mounted": os.path.ismount(settings.backup_root),
        "imapsync_version": _version([settings.imapsync_bin, "--version"]),
        "dovecot_version": _version(["dovecot", "--version"]),
        "dovecot_ok": _port_open(settings.local_imap_host, settings.local_imap_port),
        "worker_ok": worker_alive(session),
    })
