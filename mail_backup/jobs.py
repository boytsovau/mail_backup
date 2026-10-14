"""Выполнение запуска (Run): связка БД и обёртки imapsync."""

import os
import time
from datetime import datetime

from sqlalchemy.orm import Session

from . import crypto, dovecot
from .config import get_settings
from .imapsync.command import Endpoint, Job
from .imapsync.parser import Progress, status_from_exit
from .imapsync.runner import run_imapsync
from .models import Mailbox, Run, RunKind, RunStatus, utcnow
from .providers import Security, get_provider

# Не чаще раза в N секунд пишем прогресс в БД, чтобы не нагружать SQLite
PROGRESS_SAVE_INTERVAL = 2.0


class StorageNotMountedError(RuntimeError):
    """PROD: диск с бэкапами не смонтирован."""


def check_backup_storage() -> None:
    """В PROD бэкапы пишутся только на отдельный диск.

    Если диск не смонтирован (сбой, перезагрузка без fstab), каталог
    под точкой монтирования лежит на системном диске — писать туда нельзя.
    """
    settings = get_settings()
    if settings.deploy_mode == "prod" and not os.path.ismount(settings.backup_root):
        raise StorageNotMountedError(f"Диск с бэкапами не смонтирован: {settings.backup_root}")


def mailbox_endpoint(box: Mailbox) -> Endpoint:
    return Endpoint(box.host, box.port, Security(box.security), box.login, crypto.decrypt(box.password_enc))


def local_endpoint(box: Mailbox) -> Endpoint:
    """Учётка ящика в локальном Dovecot."""
    settings = get_settings()
    return Endpoint(
        settings.local_imap_host,
        settings.local_imap_port,
        Security.PLAIN,
        box.local_login,
        crypto.decrypt(box.local_password_enc),
    )


def run_target_endpoint(run: Run) -> Endpoint:
    if not (run.target_host and run.target_login and run.target_password_enc):
        raise ValueError("Для восстановления и переноса нужен ящик-получатель")
    return Endpoint(
        run.target_host,
        run.target_port or 993,
        Security(run.target_security or Security.SSL),
        run.target_login,
        crypto.decrypt(run.target_password_enc),
    )


def build_job(run: Run, *, just_login: bool = False) -> Job:
    box = run.mailbox
    match run.kind:
        case RunKind.BACKUP:
            source, target = mailbox_endpoint(box), local_endpoint(box)
            extra = list(get_provider(box.provider).extra_args_source)
        case RunKind.RESTORE:
            source, target = local_endpoint(box), run_target_endpoint(run)
            extra = []
        case RunKind.MIGRATE:
            source, target = mailbox_endpoint(box), run_target_endpoint(run)
            extra = list(get_provider(box.provider).extra_args_source)
        case _:
            raise ValueError(f"Неизвестный тип запуска: {run.kind}")
    return Job(
        kind=RunKind(run.kind),
        source=source,
        target=target,
        dry_run=run.dry_run,
        just_login=just_login,
        exclude_folders=box.exclude_folders,
        extra_args=extra,
    )


def execute_run(session: Session, run_id: int, *, just_login: bool = False) -> Run:
    """Выполняет запуск синхронно и сохраняет результат в БД."""
    settings = get_settings()
    run = session.get_one(Run, run_id)
    if run.status != RunStatus.QUEUED:
        raise ValueError(f"Запуск #{run.id} уже в статусе {run.status}")

    started = utcnow()
    run.status = RunStatus.RUNNING
    run.started_at = started
    log_path = settings.logs_dir / f"{started:%Y-%m}" / f"run_{run.id}.log"
    run.log_path = str(log_path)
    session.commit()

    last_save = 0.0

    def on_start(pid: int) -> None:
        run.pid = pid
        session.commit()

    def on_progress(p: Progress) -> None:
        nonlocal last_save
        now = time.monotonic()
        if now - last_save < PROGRESS_SAVE_INTERVAL:
            return
        last_save = now
        run.progress_done, run.progress_total, run.eta_seconds = p.done, p.msgs_total, p.eta_seconds
        session.commit()

    try:
        if run.kind in (RunKind.BACKUP, RunKind.RESTORE):
            check_backup_storage()
            # Учётка ящика в Dovecot должна существовать до подключения к нему
            dovecot.sync_users(session)
        job = build_job(run, just_login=just_login)
        result = run_imapsync(
            job,
            imapsync_bin=settings.imapsync_bin,
            work_dir=settings.tmp_dir / f"run_{run.id}_{datetime.now():%Y%m%d%H%M%S}",
            log_path=log_path,
            on_start=on_start,
            on_progress=on_progress,
        )
    except Exception as exc:
        session.rollback()
        run.status = RunStatus.FAILED
        run.error_message = f"{type(exc).__name__}: {exc}"
    else:
        run.exit_code = result.exit_code
        run.status = status_from_exit(result.exit_code)
        for name, value in result.stats.as_dict().items():
            setattr(run, name, value)
        if result.last_progress:
            run.progress_done = result.last_progress.done
            run.progress_total = result.last_progress.msgs_total
        run.eta_seconds = None
    run.finished_at = utcnow()
    run.pid = None
    session.commit()
    return run

