"""Обслуживание restic: очистка по сроку хранения, выгрузка на серверы, проверка.

Задачи (ResticTask) ставит расписание из настроек «Снимки» или кнопка в панели,
выполняет воркер — по одной за раз, отдельно от запусков imapsync. Одновременную
работу с репозиторием разруливают блокировки самого restic (--retry-lock).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import app_settings, restic
from .config import get_settings
from .models import Mailbox, ResticRemote, ResticTask, ResticTaskKind, RunStatus, RunTrigger, utcnow
from .schedule import latest_slot, parse_schedule

log = logging.getLogger("mail_backup.restic")

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)

TASK_LABELS = {
    ResticTaskKind.PRUNE: "очистка",
    ResticTaskKind.COPY: "выгрузка",
    ResticTaskKind.CHECK: "проверка",
}


def enqueue_task(session: Session, kind: ResticTaskKind, remote: ResticRemote | None = None,
                 trigger: RunTrigger = RunTrigger.MANUAL) -> ResticTask:
    """Ставит задачу в очередь; такая же ещё не выполненная — возвращается она."""
    existing = session.scalars(
        select(ResticTask).where(
            ResticTask.kind == kind,
            ResticTask.remote_id.is_(None) if remote is None else ResticTask.remote_id == remote.id,
            ResticTask.status.in_(ACTIVE),
        )
    ).first()
    if existing:
        return existing
    task = ResticTask(kind=kind, remote_id=remote.id if remote else None, trigger=trigger)
    session.add(task)
    session.commit()
    return task


def enqueue_due(session: Session, prefs: app_settings.AppSettings, rprefs: app_settings.ResticSettings,
                now: datetime | None = None) -> list[ResticTask]:
    """По расписанию: очистка локального репозитория и выгрузка на каждый включённый сервер."""
    if not rprefs.enabled:
        return []
    now = now or utcnow()
    now_local = now.replace(tzinfo=timezone.utc).astimezone(prefs.tz)
    try:
        slot = latest_slot(parse_schedule(rprefs.schedule), now_local)
    except ValueError:
        log.warning("Неверное расписание обслуживания restic: %r", rprefs.schedule)
        return []
    if slot is None:
        return []
    slot_utc = slot.astimezone(timezone.utc).replace(tzinfo=None)
    already = session.scalars(
        select(ResticTask.id).where(ResticTask.trigger == RunTrigger.SCHEDULE, ResticTask.created_at >= slot_utc)
    ).first()
    if already is not None:
        return []
    tasks = [enqueue_task(session, ResticTaskKind.PRUNE, trigger=RunTrigger.SCHEDULE)]
    for remote in session.scalars(select(ResticRemote).where(ResticRemote.enabled.is_(True)).order_by(ResticRemote.id)):
        tasks.append(enqueue_task(session, ResticTaskKind.COPY, remote, trigger=RunTrigger.SCHEDULE))
    log.info("Обслуживание restic по расписанию (%s): задач %s", slot.strftime("%H:%M"), len(tasks))
    return tasks


def _forget_all(session: Session, repo: restic.Repo, log_path, on_start) -> None:
    """Срок хранения по каждому ящику (0 — бессрочно, такие не трогаем)."""
    for box in session.scalars(select(Mailbox).where(Mailbox.retention_days > 0).order_by(Mailbox.id)):
        restic.forget(repo, box.id, box.retention_days, log_path, on_start)


def execute_task(session: Session, task_id: int) -> ResticTask:
    task = session.get_one(ResticTask, task_id)
    if task.status != RunStatus.QUEUED:
        raise ValueError(f"Задача restic #{task.id} уже в статусе {task.status}")
    started = utcnow()
    task.status, task.started_at = RunStatus.RUNNING, started
    log_path = get_settings().logs_dir / f"{started:%Y-%m}" / f"restic_{task.id}.log"
    task.log_path = str(log_path)
    session.commit()

    def on_start(pid: int) -> None:
        task.pid = pid
        session.commit()

    try:
        if not restic.ready():
            raise restic.ResticError("restic не установлен или локальный репозиторий не создан")
        if task.kind == ResticTaskKind.COPY and task.remote is None:
            raise ValueError("Сервер для выгрузки удалён")
        local = restic.local_repo()
        match task.kind:
            case ResticTaskKind.PRUNE:
                _forget_all(session, local, log_path, on_start)
                restic.prune(local, log_path, on_start)
            case ResticTaskKind.COPY:
                remote = restic.remote_repo(task.remote)
                if restic.init_remote(remote, log_path):
                    log.info("Создан репозиторий на %s", task.remote.url)
                restic.copy_to(remote, log_path, on_start)
                # Удалённый репозиторий хранит столько же, сколько локальный
                _forget_all(session, remote, log_path, on_start)
                restic.prune(remote, log_path, on_start)
            case ResticTaskKind.CHECK:
                restic.check(restic.remote_repo(task.remote) if task.remote else local, log_path, on_start)
    except Exception as exc:
        session.rollback()
        session.refresh(task, ["cancel_requested"])
        task.status = RunStatus.CANCELLED if task.cancel_requested else RunStatus.FAILED
        if not task.cancel_requested:
            task.error_message = str(exc) if isinstance(exc, restic.ResticError | ValueError) else f"{type(exc).__name__}: {exc}"
    else:
        task.status = RunStatus.SUCCESS
    task.finished_at = utcnow()
    task.pid = None
    session.commit()
    return task


def last_tasks(session: Session, limit: int = 20) -> list[ResticTask]:
    return list(session.scalars(select(ResticTask).order_by(ResticTask.id.desc()).limit(limit)))


def last_copy(session: Session, remote_id: int) -> ResticTask | None:
    """Последняя завершённая выгрузка на сервер."""
    return session.scalars(
        select(ResticTask)
        .where(ResticTask.remote_id == remote_id, ResticTask.kind == ResticTaskKind.COPY,
               ResticTask.status.not_in(ACTIVE))
        .order_by(ResticTask.id.desc())
        .limit(1)
    ).first()
