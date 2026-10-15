"""Главная страница, настройки и сведения о системе."""

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, replace
from enum import StrEnum
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import app_settings, auth, dovecot, restic, stats, storage_estimate
from ..config import get_settings
from ..db import get_session
from ..jobs import active_run, enqueue_run, last_backup, snapshots_wanted
from ..models import Mailbox, Run, RunKind, RunStatus, RunTrigger, utcnow
from ..schedule import next_slot, parse_schedule
from ..worker import worker_alive
from .common import csrf_protect, flash, render, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
# Бэкап по расписанию — минимум раз в сутки; удачный старше этого срока — «просрочен»
STALE_AFTER = timedelta(hours=26)
# График на обзоре, дней
OVERVIEW_DAYS = 14


class Health(StrEnum):
    """Состояние бэкапа ящика для обзора."""

    OK = "ok"
    WARNING = "warning"  # последний бэкап прошёл с ошибками по письмам
    FAILED = "failed"  # последний настоящий бэкап — сбой или отмена
    STALE = "stale"  # по расписанию, но удачного бэкапа давно не было
    SNAPSHOT = "snapshot"  # снимки включены, но после последнего бэкапа снимка нет
    NEVER = "never"  # бэкапа ещё не было, и это пока нормально
    OFF = "off"  # ящик выключен


HEALTH_LABELS = {
    Health.OK: "в порядке",
    Health.WARNING: "с ошибками",
    Health.FAILED: "сбой",
    Health.STALE: "просрочен",
    Health.SNAPSHOT: "нет снимка",
    Health.NEVER: "ещё не было",
    Health.OFF: "выключен",
}
templates.env.globals["HEALTH_LABELS"] = HEALTH_LABELS


@dataclass
class MailboxRow:
    box: Mailbox
    last_run: Run | None
    last_ok: Run | None
    active: Run | None
    next_at: datetime | None
    health: Health = Health.NEVER


def backup_health(box: Mailbox, last_real: Run | None, last_ok: Run | None, now: datetime,
                  snapshots: bool = False) -> Health:
    """Состояние по последнему настоящему бэкапу и последнему удачному.

    snapshots — снимки restic включены: тогда бэкап без снимка — проблема
    (удалённое на хостере не сохранится в истории).
    """
    if not box.enabled:
        return Health.OFF
    if last_real is not None and last_real.status not in stats.DONE_OK:
        return Health.FAILED
    # Без расписания бэкап только ручной — «просрочен» не бывает
    scheduled = bool(box.schedule)
    if last_ok is None:
        return Health.STALE if scheduled and box.created_at < now - STALE_AFTER else Health.NEVER
    if scheduled and last_ok.finished_at < now - STALE_AFTER:
        return Health.STALE
    if snapshots and (last_ok.snapshot_error or not last_ok.snapshot_id):
        return Health.SNAPSHOT
    return Health.WARNING if last_ok.status == RunStatus.WARNING else Health.OK


def mailbox_overview(session: Session) -> list[MailboxRow]:
    prefs = app_settings.load(session)
    now_local = datetime.now(prefs.tz)
    now = utcnow()
    snapshots = snapshots_wanted(session)
    rows = []
    for box in session.scalars(select(Mailbox).order_by(Mailbox.name)):
        backups = select(Run).where(Run.mailbox_id == box.id, Run.kind == RunKind.BACKUP).order_by(Run.id.desc())
        last_run = session.scalars(backups.where(Run.status.not_in(ACTIVE)).limit(1)).first()
        last_ok = last_backup(session, box.id)
        last_real = session.scalars(
            backups.where(Run.status.in_(stats.DONE), Run.dry_run.is_(False)).limit(1)
        ).first()
        active = session.scalars(
            select(Run).where(Run.mailbox_id == box.id, Run.status.in_(ACTIVE)).limit(1)
        ).first()
        try:
            nxt = next_slot(parse_schedule(box.schedule), now_local) if box.enabled else None
        except ValueError:
            nxt = None
        # next_slot отдаёт время в поясе админки; в шаблон — как наивный UTC, как всё остальное
        next_at = nxt.astimezone(timezone.utc).replace(tzinfo=None) if nxt else None
        rows.append(MailboxRow(box, last_run, last_ok, active, next_at,
                               backup_health(box, last_real, last_ok, now, snapshots)))
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
    rows = mailbox_overview(session)
    health = {h: [r for r in rows if r.health == h] for h in Health}
    return render(request, session, "dashboard.html", {
        "rows": rows,
        "health": health,
        "active_runs": active_runs,
        "disk": disk_usage(),
        "worker_ok": worker_alive(session),
        # Снимки включены, но restic не готов — бэкапы идут без истории
        "restic_broken": app_settings.load_restic(session).enabled and not restic.ready(),
        "chart": stats.collect(session, app_settings.load(session).tz, OVERVIEW_DAYS),
    })


@router.get("/storage")
def storage_page(request: Request, session: Session = Depends(get_session)):
    """Сколько места нужно под бэкапы — по размерам ящиков на хостере."""
    active = {run.mailbox_id: run for run in session.scalars(select(Run).where(Run.status.in_(ACTIVE)))}
    return render(request, session, "storage.html", {
        "plan": storage_estimate.plan(session),
        "active": active,
        "disk": disk_usage(),
        "backup_mounted": os.path.ismount(get_settings().backup_root),
        "coef": storage_estimate,
    })


@router.post("/storage/estimate-all", dependencies=[Depends(csrf_protect)])
def storage_estimate_all(request: Request, session: Session = Depends(get_session)):
    queued = 0
    for box in session.scalars(select(Mailbox)):
        if active_run(session, box.id) is None:
            enqueue_run(session, box, RunKind.ESTIMATE, RunTrigger.MANUAL)
            queued += 1
    flash(request, f"Оценка поставлена в очередь для ящиков: {queued}. Страница обновится сама.")
    return RedirectResponse("/storage", 303)


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    return render(request, session, "settings.html", {
        "admin_login": auth.admin_login(session),
        "view_imap_host": get_settings().view_imap_host,
        # Админ обычно заходит в панель через тот же VPN — подсказка, какую сеть разрешить
        "client_ip": request.client.host if request.client else None,
    })


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
        # Сети просмотра сохраняются своей формой — здесь оставляем как были
        current = app_settings.load(session)
        app_settings.save(session, replace(prefs, view_allowed_nets=current.view_allowed_nets))
        flash(request, "Настройки сохранены")
    return RedirectResponse("/settings", 303)


@router.post("/settings/view", dependencies=[Depends(csrf_protect)])
def view_settings_save(request: Request, view_allowed_nets: str = Form(""),
                       session: Session = Depends(get_session)):
    """Сети, из которых почтовый клиент может смотреть бэкапы (учётки только для чтения)."""
    try:
        nets = app_settings.parse_nets(view_allowed_nets)
    except ValueError as exc:
        flash(request, str(exc), "error")
    else:
        app_settings.save(session, replace(app_settings.load(session), view_allowed_nets=", ".join(nets)))
        dovecot.sync_users(session)
        flash(request, "Сети для просмотра почты сохранены")
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
        "MB_VIEW_IMAP_HOST": settings.view_imap_host or "— (только 127.0.0.1)",
        "MB_IMAPSYNC_BIN": settings.imapsync_bin,
        "MB_RESTIC_BIN": settings.restic_bin,
    }
    return render(request, session, "system.html", {
        "env": env,
        "disk": disk_usage(),
        "backup_mounted": os.path.ismount(settings.backup_root),
        "imapsync_version": _version([settings.imapsync_bin, "--version"]),
        "dovecot_version": _version(["dovecot", "--version"]),
        "restic_version": restic.version(),
        "restic_ready": restic.ready(),
        "dovecot_ok": _port_open(settings.local_imap_host, settings.local_imap_port),
        "worker_ok": worker_alive(session),
    })
