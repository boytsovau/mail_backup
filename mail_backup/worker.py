"""Воркер: очередь запусков, расписание, отмена, очистка логов, обслуживание restic.

Работает отдельным systemd-сервисом (mail-backup worker). Каждый запуск
imapsync выполняется в своём потоке со своей сессией БД; задачи restic
(очистка, выгрузка, проверка) — по одной, в отдельном потоке.
"""

import logging
import os
import shutil
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import app_settings, restic_tasks
from .config import get_settings
from .db import new_session
from .jobs import active_run, cleanup_unpacked, enqueue_run, execute_run, forget_target_passwords
from .models import Mailbox, ResticTask, Run, RunKind, RunStatus, RunTrigger, Setting, utcnow
from .schedule import latest_slot, parse_schedule

log = logging.getLogger("mail_backup.worker")

POLL_INTERVAL = 2.0
# Как часто воркер отмечается в БД; панель считает его «живым» в пределах HEARTBEAT_STALE
HEARTBEAT_KEY = "worker_heartbeat"
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_STALE = 60.0
CLEANUP_INTERVAL = 3600.0
# Сколько ждать завершения imapsync при остановке воркера
SHUTDOWN_TIMEOUT = 30.0


# --- Расписание -------------------------------------------------------------------------

def enqueue_due_backups(session: Session, prefs: app_settings.AppSettings, now: datetime | None = None) -> list[Run]:
    """Ставит в очередь бэкапы, чьё время по расписанию наступило."""
    now = now or utcnow()
    now_local = now.replace(tzinfo=timezone.utc).astimezone(prefs.tz)
    created = []
    boxes = session.scalars(select(Mailbox).where(Mailbox.enabled.is_(True), Mailbox.schedule.is_not(None)))
    for box in boxes:
        try:
            slot = latest_slot(parse_schedule(box.schedule), now_local)
        except ValueError:
            log.warning("Ящик #%s: неверное расписание %r", box.id, box.schedule)
            continue
        if slot is None:
            continue
        slot_utc = slot.astimezone(timezone.utc).replace(tzinfo=None)
        # Слоты до последней правки ящика пропускаем: новое расписание
        # не должно сразу запускать «пропущенный» утренний бэкап
        if slot_utc < box.updated_at:
            continue
        already = session.scalars(
            select(Run.id).where(
                Run.mailbox_id == box.id,
                Run.kind == RunKind.BACKUP,
                Run.trigger == RunTrigger.SCHEDULE,
                Run.created_at >= slot_utc,
            )
        ).first()
        if already is None and active_run(session, box.id) is None:
            created.append(enqueue_run(session, box, RunKind.BACKUP, RunTrigger.SCHEDULE))
            log.info("Ящик #%s: бэкап по расписанию (%s)", box.id, slot.strftime("%H:%M"))
    return created


# --- Очистка логов ----------------------------------------------------------------------

def cleanup_logs(retention_days: int) -> int:
    """Удаляет логи запусков старше срока. Статистика в БД остаётся."""
    logs_dir = get_settings().logs_dir
    if not logs_dir.exists():
        return 0
    border = time.time() - retention_days * 86400
    removed = 0
    # run_<id>.log — запуски imapsync, restic_<id>.log — задачи restic
    for path in logs_dir.glob("*/*.log"):
        if path.stat().st_mtime < border:
            path.unlink()
            removed += 1
    for month_dir in logs_dir.iterdir():
        if month_dir.is_dir() and not any(month_dir.iterdir()):
            month_dir.rmdir()
    return removed


# --- Воркер --------------------------------------------------------------------------------

def _kill(pid: int) -> None:
    """Завершает imapsync вместе с дочерними процессами (своя группа процессов)."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


@dataclass
class ActiveRun:
    thread: threading.Thread
    mailbox_id: int


class Worker:
    def __init__(self) -> None:
        # run_id → выполняющийся в этом воркере запуск
        self.active: dict[int, ActiveRun] = {}
        self.killed: set[int] = set()
        self.stop_event = threading.Event()
        self.last_cleanup = 0.0
        self.last_heartbeat = 0.0
        # Задача restic, выполняющаяся сейчас: (id, поток)
        self.task: tuple[int, threading.Thread] | None = None
        self.task_killed = False

    def recover(self) -> None:
        """После перезапуска воркера «зависшие» запуски и задачи помечаются сбоем."""
        with new_session() as session:
            for item in [*session.scalars(select(Run).where(Run.status == RunStatus.RUNNING)),
                         *session.scalars(select(ResticTask).where(ResticTask.status == RunStatus.RUNNING))]:
                item.status = RunStatus.FAILED
                item.error_message = "Прервано: воркер был перезапущен"
                item.finished_at = utcnow()
                item.pid = None
                if isinstance(item, Run):
                    item.phase = None
            session.commit()
            # Распакованные для восстановления снимки, оставшиеся от прерванных запусков
            for run in session.scalars(select(Run).where(Run.snapshot_password_enc.is_not(None),
                                                         Run.status != RunStatus.QUEUED)):
                cleanup_unpacked(session, run)
            queued = {run.temp_login for run in session.scalars(select(Run).where(Run.status == RunStatus.QUEUED))}
        maildir_root = get_settings().maildir_root
        if maildir_root.is_dir():
            for path in maildir_root.glob("_restore_*"):
                if path.name not in queued:
                    shutil.rmtree(path, ignore_errors=True)

    def tick(self) -> None:
        # Завершившиеся потоки больше не занимают слоты
        for run_id, active in list(self.active.items()):
            if not active.thread.is_alive():
                del self.active[run_id]
                self.killed.discard(run_id)

        with new_session() as session:
            if time.monotonic() - self.last_heartbeat > HEARTBEAT_INTERVAL:
                self.last_heartbeat = time.monotonic()
                session.merge(Setting(key=HEARTBEAT_KEY, value=utcnow().isoformat()))
                session.commit()
            prefs = app_settings.load(session)
            self.handle_cancels(session)
            enqueue_due_backups(session, prefs)
            restic_tasks.enqueue_due(session, prefs, app_settings.load_restic(session))
            self.start_queued(session, prefs)
            self.start_restic_task(session)

        if time.monotonic() - self.last_cleanup > CLEANUP_INTERVAL:
            self.last_cleanup = time.monotonic()
            removed = cleanup_logs(prefs.log_retention_days)
            if removed:
                log.info("Удалено старых логов: %s", removed)
            with new_session() as session:
                forget_target_passwords(session)

    def handle_cancels(self, session: Session) -> None:
        runs = session.scalars(
            select(Run).where(
                Run.cancel_requested.is_(True),
                Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
        )
        for run in runs:
            if run.status == RunStatus.QUEUED and run.id not in self.active:
                run.status = RunStatus.CANCELLED
                run.finished_at = utcnow()
            elif run.pid and run.id not in self.killed:
                log.info("Запуск #%s: отмена, завершаю процесс (pid %s)", run.id, run.pid)
                _kill(run.pid)
                self.killed.add(run.id)
        tasks = session.scalars(
            select(ResticTask).where(ResticTask.cancel_requested.is_(True),
                                     ResticTask.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
        )
        running_id = self.task[0] if self.task else None
        for task in tasks:
            if task.status == RunStatus.QUEUED and task.id != running_id:
                task.status = RunStatus.CANCELLED
                task.finished_at = utcnow()
            elif task.pid and task.id == running_id and not self.task_killed:
                log.info("Задача restic #%s: отмена (pid %s)", task.id, task.pid)
                _kill(task.pid)
                self.task_killed = True
        session.commit()

    def start_restic_task(self, session: Session) -> None:
        """Задачи restic — строго по одной: выгрузка и очистка долгие и тяжёлые."""
        if self.task and self.task[1].is_alive():
            return
        self.task = None
        task = session.scalars(
            select(ResticTask)
            .where(ResticTask.status == RunStatus.QUEUED, ResticTask.cancel_requested.is_(False))
            .order_by(ResticTask.id)
            .limit(1)
        ).first()
        if task is None:
            return
        thread = threading.Thread(target=self._execute_task, args=(task.id,), name=f"restic-{task.id}", daemon=True)
        self.task, self.task_killed = (task.id, thread), False
        log.info("Задача restic #%s: старт (%s)", task.id, task.kind)
        thread.start()

    def _execute_task(self, task_id: int) -> None:
        try:
            with new_session() as session:
                task = restic_tasks.execute_task(session, task_id)
                log.info("Задача restic #%s: %s %s", task_id, task.status, task.error_message or "")
        except Exception:
            log.exception("Задача restic #%s: необработанная ошибка", task_id)

    def start_queued(self, session: Session, prefs: app_settings.AppSettings) -> None:
        free = prefs.max_parallel_jobs - len(self.active)
        if free <= 0:
            return
        # Ящик занят, если его запуск уже выполняется (в т.ч. CLI-командой backup)
        busy = set(session.scalars(select(Run.mailbox_id).where(Run.status == RunStatus.RUNNING)))
        busy |= {a.mailbox_id for a in self.active.values()}
        queued = session.scalars(
            select(Run)
            .where(Run.status == RunStatus.QUEUED, Run.cancel_requested.is_(False))
            .order_by(Run.id)
        )
        for run in queued:
            if free <= 0:
                break
            if run.id in self.active or run.mailbox_id in busy:
                continue
            thread = threading.Thread(target=self._execute, args=(run.id,), name=f"run-{run.id}", daemon=True)
            self.active[run.id] = ActiveRun(thread, run.mailbox_id)
            busy.add(run.mailbox_id)
            free -= 1
            log.info("Запуск #%s: старт (%s, ящик #%s)", run.id, run.kind, run.mailbox_id)
            thread.start()

    def _execute(self, run_id: int) -> None:
        try:
            with new_session() as session:
                run = execute_run(session, run_id)
                log.info("Запуск #%s: %s (код %s)", run_id, run.status, run.exit_code)
        except Exception:
            log.exception("Запуск #%s: необработанная ошибка", run_id)

    def shutdown(self) -> None:
        threads = [a.thread for a in self.active.values()]
        if self.task and self.task[1].is_alive():
            threads.append(self.task[1])
        if not threads:
            return
        log.info("Остановка: завершаю запусков и задач: %s", len(threads))
        with new_session() as session:
            for run in session.scalars(select(Run).where(Run.id.in_(list(self.active)))):
                if run.pid:
                    _kill(run.pid)
            if self.task and (task := session.get(ResticTask, self.task[0])) and task.pid:
                _kill(task.pid)
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def run_forever(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())
        self.recover()
        log.info("Воркер запущен")
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # Сбой одного цикла (например, БД занята) не должен останавливать воркер
                log.exception("Ошибка в цикле воркера")
            self.stop_event.wait(POLL_INTERVAL)
        self.shutdown()
        log.info("Воркер остановлен")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    Worker().run_forever()


def worker_alive(session: Session) -> bool:
    row = session.get(Setting, HEARTBEAT_KEY)
    if row is None:
        return False
    return (utcnow() - datetime.fromisoformat(row.value)).total_seconds() < HEARTBEAT_STALE
