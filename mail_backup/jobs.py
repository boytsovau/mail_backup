"""Выполнение запуска (Run): связка БД и обёртки imapsync."""

import ipaddress
import json
import logging
import os
import secrets
import shutil
import time
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import app_settings, crypto, dovecot, restic
from .config import get_settings
from .imapsync.command import Endpoint, Job
from .imapsync.parser import Progress, status_from_exit
from .imapsync.runner import run_imapsync
from .models import Mailbox, Run, RunKind, RunPhase, RunStatus, RunTrigger, utcnow
from .providers import Security, get_provider

log = logging.getLogger("mail_backup.jobs")

# Не чаще раза в N секунд пишем прогресс в БД, чтобы не нагружать SQLite
PROGRESS_SAVE_INTERVAL = 2.0
# Сколько хранить пароль получателя после пробного прогона (для «Запустить по-настоящему»)
TARGET_PASSWORD_TTL = timedelta(hours=24)
# Удачным считается и запуск с ошибками по отдельным письмам
DONE_OK = (RunStatus.SUCCESS, RunStatus.WARNING)
# Запас места при распаковке снимка: Dovecot допишет индексы
UNPACK_SPACE_RESERVE = 1.1


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


def is_local_host(host: str) -> bool:
    """Адрес указывает на этот сервер (там наш Dovecot с бэкапами)."""
    host = host.strip().lower().strip("[]")
    if host in ("localhost", get_settings().local_imap_host.lower()) or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def snapshot_endpoint(run: Run) -> Endpoint:
    """Временная учётка Dovecot, куда распакован снимок для восстановления."""
    if not run.snapshot_password_enc:
        raise ValueError("Снимок не распакован — нет временной учётки")
    settings = get_settings()
    return Endpoint(settings.local_imap_host, settings.local_imap_port, Security.PLAIN,
                    run.temp_login, crypto.decrypt(run.snapshot_password_enc))


def run_target_endpoint(run: Run) -> Endpoint:
    if not (run.target_host and run.target_login):
        raise ValueError("Для восстановления и переноса нужен ящик-получатель")
    if not run.target_password_enc:
        raise ValueError("Пароль получателя уже удалён из БД — запустите мастер восстановления заново")
    # Запись в локальный Dovecot испортила бы бэкапы: чужие письма смешались бы с зеркалом ящика
    if is_local_host(run.target_host):
        raise ValueError("Получатель не может быть локальным хранилищем бэкапов")
    return Endpoint(
        run.target_host,
        run.target_port or 993,
        Security(run.target_security or Security.SSL),
        run.target_login,
        crypto.decrypt(run.target_password_enc),
    )


def build_job(run: Run, *, just_login: bool = False, mirror_deletions: bool = False) -> Job:
    box = run.mailbox
    match run.kind:
        case RunKind.BACKUP | RunKind.ESTIMATE:
            # Оценке тоже нужен host2 — imapsync всегда подключается к обеим сторонам
            source, target = mailbox_endpoint(box), local_endpoint(box)
            extra = list(get_provider(box.provider).extra_args_source)
        case RunKind.RESTORE:
            # Из зеркала (последний бэкап) или из распакованного снимка на дату
            source = snapshot_endpoint(run) if run.source_snapshot else local_endpoint(box)
            target, extra = run_target_endpoint(run), []
        case RunKind.MIGRATE:
            source, target = mailbox_endpoint(box), run_target_endpoint(run)
            extra = list(get_provider(box.provider).extra_args_source)
        case _:
            raise ValueError(f"Неизвестный тип запуска: {run.kind}")
    transfer = run.kind in (RunKind.RESTORE, RunKind.MIGRATE)
    cache = mailbox_cache_dir(box) if run.kind == RunKind.BACKUP else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Job(
        kind=RunKind(run.kind),
        source=source,
        target=target,
        dry_run=run.dry_run,
        just_login=just_login,
        exclude_folders=box.exclude_folders,
        subfolder2=run.target_subfolder if transfer else None,
        mirror_deletions=mirror_deletions,
        cache_dir=cache,
        extra_args=extra,
    )


def mailbox_cache_dir(box: Mailbox):
    """Кэш сверки imapsync ящика. Внутри imapsync раскладывает его по хостам, логинам
    и папкам — смена сервера ящика сама даёт новый, пустой кэш."""
    return get_settings().imapsync_cache_dir / str(box.id)


def reset_cache(box: Mailbox) -> bool:
    """Удаляет кэш сверки: следующий бэкап сверит все письма по заголовкам.

    Нужно, если хостер перенумеровал письма (сменил UIDVALIDITY) — imapsync этого
    не замечает и по старому кэшу мог бы пропустить новые письма. Возвращает,
    был ли кэш.
    """
    path = mailbox_cache_dir(box)
    existed = path.exists()
    shutil.rmtree(path, ignore_errors=True)
    return existed


# --- Снимки restic ----------------------------------------------------------------------

def snapshots_wanted(session: Session) -> bool:
    """Снимки включены в панели и restic готов (установлен, репозиторий создан)."""
    return app_settings.load_restic(session).enabled and restic.ready()


def mailbox_maildir(box: Mailbox):
    return get_settings().maildir_root / box.local_login


def _mirror_in_snapshot(session: Session, run: Run) -> bool:
    """Нынешнее содержимое зеркала уже есть в снимке.

    Зеркало меняют только бэкапы, так что достаточно, чтобы снимок был у последнего
    завершённого настоящего бэкапа. Нет — например, снимки были выключены или бэкап
    оборвался: письма, накопленные с тех пор, без снимка пропали бы при удалении.
    """
    if not mailbox_maildir(run.mailbox).is_dir():
        return True
    last = session.scalars(
        select(Run)
        .where(
            Run.mailbox_id == run.mailbox_id,
            Run.kind == RunKind.BACKUP,
            Run.dry_run.is_(False),
            Run.id != run.id,
            Run.status.not_in((RunStatus.QUEUED, RunStatus.RUNNING)),
        )
        .order_by(Run.id.desc())
        .limit(1)
    ).first()
    return bool(last and last.snapshot_id)


def _take_snapshot(session: Session, run: Run, log_path, on_start) -> restic.BackupSummary:
    """Снимок зеркала ящика (и срок хранения), запуск при этом в фазе «снимок»."""
    box = run.mailbox
    run.phase = RunPhase.SNAPSHOT
    session.commit()
    try:
        summary = restic.backup(mailbox_maildir(box), box.id, log_path, on_start)
        if box.retention_days:
            restic.forget(restic.local_repo(), box.id, box.retention_days, log_path, on_start)
        return summary
    finally:
        run.phase = None
        run.pid = None
        session.commit()


def _unpack_snapshot(session: Session, run: Run, log_path, on_start) -> None:
    """Распаковывает снимок во временную учётку Dovecot для восстановления."""
    settings = get_settings()
    found = restic.snapshots(restic.local_repo(), snapshot_id=run.source_snapshot)
    if not found or found[0].mailbox_id != run.mailbox_id:
        raise ValueError(f"Снимок {run.source_snapshot[:8]} не найден (удалён по сроку хранения?)")
    snap = found[0]
    target = settings.maildir_root / run.temp_login
    settings.maildir_root.mkdir(parents=True, exist_ok=True)
    if snap.size and shutil.disk_usage(settings.maildir_root).free < snap.size * UNPACK_SPACE_RESERVE:
        raise ValueError(f"Не хватает места на диске бэкапов для распаковки снимка ({snap.size} байт)")
    shutil.rmtree(target, ignore_errors=True)
    run.phase = RunPhase.UNPACK
    run.snapshot_password_enc = crypto.encrypt(secrets.token_urlsafe(24))
    session.commit()
    restic.restore(snap.id, snap.paths[0], target, log_path, on_start)
    run.phase = None
    run.pid = None
    session.commit()
    dovecot.sync_users(session)


def cleanup_unpacked(session: Session, run: Run) -> None:
    """Удаляет распакованный снимок и временную учётку."""
    shutil.rmtree(get_settings().maildir_root / run.temp_login, ignore_errors=True)
    if run.snapshot_password_enc:
        run.snapshot_password_enc = None
        session.commit()
        dovecot.sync_users(session)


def last_backup(session: Session, mailbox_id: int) -> Run | None:
    """Последний удачный настоящий (не пробный) бэкап ящика."""
    return session.scalars(
        select(Run)
        .where(
            Run.mailbox_id == mailbox_id,
            Run.kind == RunKind.BACKUP,
            Run.status.in_(DONE_OK),
            Run.dry_run.is_(False),
        )
        .order_by(Run.id.desc())
        .limit(1)
    ).first()


def active_run(session: Session, mailbox_id: int) -> Run | None:
    """Запуск ящика в очереди или в работе (одновременно — не больше одного)."""
    return session.scalars(
        select(Run)
        .where(Run.mailbox_id == mailbox_id, Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
        .limit(1)
    ).first()


def enqueue_run(
    session: Session, box: Mailbox, kind: RunKind, trigger: RunTrigger, *, dry_run: bool = False, **target
) -> Run:
    """Ставит запуск в очередь; если ящик уже в работе — возвращает текущий запуск.

    target — поля получателя для восстановления и переноса (target_host, target_login, ...).
    """
    if existing := active_run(session, box.id):
        return existing
    run = Run(kind=kind, mailbox_id=box.id, trigger=trigger, dry_run=dry_run, **target)
    session.add(run)
    session.commit()
    return run


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
    # Живые поля (PID, прогресс), ещё не записанные в БД
    pending: dict[str, int | None] = {}

    def save_live(**values: int | None) -> None:
        """Пишет живые поля запуска. БД занята — не беда: запишем в следующий раз.

        Иначе «database is locked» при обновлении процентов обрывал бы многочасовой бэкап.
        """
        pending.update(values)
        for name, value in pending.items():
            setattr(run, name, value)
        try:
            session.commit()
        except OperationalError as exc:
            session.rollback()
            log.warning("Запуск #%s: живые поля не записаны, БД занята: %s", run.id, exc.orig)
        else:
            pending.clear()

    def on_start(pid: int) -> None:
        save_live(pid=pid)

    def on_progress(p: Progress) -> None:
        nonlocal last_save
        now = time.monotonic()
        if now - last_save < PROGRESS_SAVE_INTERVAL:
            return
        last_save = now
        # Скопированное — в те же поля, что и итог: итоговая статистика их перезапишет,
        # а при сбое останется то, что успело скопироваться
        copied = ({"messages_planned": p.planned} if run.dry_run else
                  {"messages_transferred": p.copied, "bytes_transferred": p.copied_bytes})
        save_live(progress_done=p.done, progress_total=p.msgs_total, eta_seconds=p.eta_seconds, **copied)

    snapshot = (run.kind == RunKind.BACKUP and not run.dry_run and not just_login
                and snapshots_wanted(session))
    # Удаления в зеркале — только по настройке ящика и только когда удалённое
    # останется в снимках restic; иначе оно пропало бы из бэкапа насовсем
    deletions = snapshot and run.mailbox.mirror_deletions
    try:
        if run.kind in (RunKind.BACKUP, RunKind.RESTORE, RunKind.ESTIMATE):
            check_backup_storage()
            # Учётка ящика в Dovecot должна существовать до подключения к нему
            dovecot.sync_users(session)
        if deletions and not _mirror_in_snapshot(session, run):
            # Снимок до imapsync: в зеркале есть письма, которых нет ни в одном снимке,
            # а --delete2 удалит те из них, что удалены на хостере
            try:
                _take_snapshot(session, run, log_path, on_start)
            except restic.ResticError as exc:
                session.refresh(run, ["cancel_requested"])
                if run.cancel_requested:
                    raise
                deletions = False
                run.snapshot_error = (f"Снимок перед бэкапом не создан ({exc}); "
                                      "удалённое на хостере в этот раз из бэкапа не удалялось")
                session.commit()
        if run.source_snapshot:
            _unpack_snapshot(session, run, log_path, on_start)
        job = build_job(run, just_login=just_login, mirror_deletions=deletions)
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
        # Отмена во время снимка или распаковки тоже приходит сюда — ошибкой restic
        session.refresh(run, ["cancel_requested"])
        run.status = RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED
        run.error_message = None if run.cancel_requested else f"{type(exc).__name__}: {exc}"
        run.phase = None
    else:
        # Флаг отмены ставит веб-панель из другого процесса — перечитываем из БД
        session.refresh(run, ["cancel_requested"])
        run.exit_code = result.exit_code
        run.status = status_from_exit(result.exit_code, cancelled=run.cancel_requested)
        for name, value in result.stats.as_dict().items():
            # Нет итога (imapsync оборвался) — остаются живые счётчики скопированного
            if value is not None:
                setattr(run, name, value)
        if result.stats.folders:
            run.folder_stats = json.dumps(result.stats.folders_as_dicts(), ensure_ascii=False)
        if result.last_progress:
            run.progress_done = result.last_progress.done
            run.progress_total = result.last_progress.msgs_total
            if result.stats.messages_transferred is None and not run.dry_run:
                run.messages_transferred = result.last_progress.copied
                run.bytes_transferred = result.last_progress.copied_bytes
        run.eta_seconds = None
        run.pid = None
        if snapshot and run.status in DONE_OK:
            try:
                summary = _take_snapshot(session, run, log_path, on_start)
            except restic.ResticError as exc:
                session.refresh(run, ["cancel_requested"])
                run.snapshot_error = "Снимок отменён" if run.cancel_requested else str(exc)
            else:
                run.snapshot_id, run.snapshot_added = summary.snapshot_id, summary.added
                run.snapshot_error = ("Снимок создан, но часть файлов прочитать не удалось — см. лог"
                                      if summary.incomplete else None)
    finally:
        if run.source_snapshot:
            cleanup_unpacked(session, run)
    if run.dry_run and run.status in DONE_OK and run.messages_planned is None:
        run.messages_planned = 0
    # Чужой пароль не храним дольше нужного; после пробного он ещё нужен для повтора
    if not run.dry_run:
        run.target_password_enc = None
    run.finished_at = utcnow()
    run.pid = None
    session.commit()
    return run


def forget_target_passwords(session: Session, now: datetime | None = None) -> int:
    """Стирает пароли получателей у завершённых запусков старше TARGET_PASSWORD_TTL."""
    border = (now or utcnow()) - TARGET_PASSWORD_TTL
    result = session.execute(
        update(Run)
        .where(
            Run.target_password_enc.is_not(None),
            Run.status.not_in([RunStatus.QUEUED, RunStatus.RUNNING]),
            Run.finished_at < border,
        )
        .values(target_password_enc=None)
    )
    session.commit()
    return result.rowcount

