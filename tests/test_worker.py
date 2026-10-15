import subprocess
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import add_mailbox

from mail_backup import app_settings
from mail_backup.db import new_session
from mail_backup.models import Run, RunKind, RunStatus, RunTrigger, utcnow
from mail_backup.schedule import latest_slot, next_slot, parse_schedule
from mail_backup.worker import Worker, enqueue_due_backups, worker_alive

MSK = ZoneInfo("Europe/Moscow")
PREFS = app_settings.AppSettings(max_parallel_jobs=2, timezone="Europe/Moscow")


def test_parse_schedule():
    assert parse_schedule("15:30, 3:00;03:00") == [dtime(3, 0), dtime(15, 30)]
    assert parse_schedule("") == [] and parse_schedule(None) == []
    with pytest.raises(ValueError):
        parse_schedule("25:00")


def test_latest_and_next_slot():
    times = [dtime(3, 0), dtime(15, 0)]
    now = datetime(2026, 10, 14, 10, 0, tzinfo=MSK)
    assert latest_slot(times, now) == datetime(2026, 10, 14, 3, 0, tzinfo=MSK)
    assert next_slot(times, now) == datetime(2026, 10, 14, 15, 0, tzinfo=MSK)
    # Ночью до первого слота — последний был вчера в 15:00
    early = datetime(2026, 10, 14, 1, 0, tzinfo=MSK)
    assert latest_slot(times, early) == datetime(2026, 10, 13, 15, 0, tzinfo=MSK)


def utc_from_msk(*args) -> datetime:
    return datetime(*args, tzinfo=MSK).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def test_schedule_enqueues_once_per_slot():
    with new_session() as session:
        box = add_mailbox(session)
        box.schedule = "03:00"
        box.updated_at = utc_from_msk(2026, 10, 13, 12, 0)
        session.commit()

        now = utc_from_msk(2026, 10, 14, 3, 1)
        created = enqueue_due_backups(session, PREFS, now)
        assert len(created) == 1 and created[0].trigger == RunTrigger.SCHEDULE
        created[0].created_at = now
        created[0].status = RunStatus.SUCCESS
        session.commit()
        # Тот же слот второй раз не запускается
        assert enqueue_due_backups(session, PREFS, now + timedelta(minutes=5)) == []
        # Следующие сутки — новый запуск
        assert len(enqueue_due_backups(session, PREFS, now + timedelta(days=1))) == 1


def test_new_schedule_does_not_fire_missed_slot():
    with new_session() as session:
        box = add_mailbox(session)
        box.schedule = "03:00"
        session.commit()
        # Ящик правили в 10:00, слот 03:00 уже прошёл — ждём следующего
        box.updated_at = utc_from_msk(2026, 10, 14, 10, 0)
        session.commit()
        assert enqueue_due_backups(session, PREFS, utc_from_msk(2026, 10, 14, 10, 1)) == []


def test_disabled_mailbox_not_scheduled():
    with new_session() as session:
        box = add_mailbox(session)
        box.schedule, box.enabled = "03:00", False
        box.updated_at = utc_from_msk(2026, 10, 1, 0, 0)
        session.commit()
        assert enqueue_due_backups(session, PREFS, utc_from_msk(2026, 10, 14, 4, 0)) == []


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def run_status(run_id: int) -> RunStatus:
    with new_session() as session:
        return session.get(Run, run_id).status


def test_worker_executes_queued_run(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run_id = run.id

    worker = Worker()
    worker.tick()
    assert wait_until(lambda: run_status(run_id) == RunStatus.SUCCESS)
    with new_session() as session:
        assert session.get(Run, run_id).messages_transferred == 3
        assert worker_alive(session)


def test_one_run_per_mailbox_at_a_time(fake_imapsync, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1")
    with new_session() as session:
        box = add_mailbox(session)
        # Два запуска одного ящика (например, ручной и пробный) — выполняются по очереди
        ids = []
        for dry in (False, True):
            run = Run(kind=RunKind.BACKUP, mailbox_id=box.id, dry_run=dry)
            session.add(run)
            session.commit()
            ids.append(run.id)

    worker = Worker()
    worker.tick()
    assert list(worker.active) == [ids[0]]
    assert wait_until(lambda: run_status(ids[0]) == RunStatus.SUCCESS)
    worker.tick()
    assert wait_until(lambda: run_status(ids[1]) == RunStatus.SUCCESS)


def test_cancel_running(fake_imapsync, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run_id = run.id

    worker = Worker()
    worker.tick()

    def has_pid():
        with new_session() as session:
            return session.get(Run, run_id).pid is not None

    assert wait_until(has_pid)
    with new_session() as session:
        session.get(Run, run_id).cancel_requested = True
        session.commit()
    worker.tick()
    assert wait_until(lambda: run_status(run_id) == RunStatus.CANCELLED)


def process_state(pid: int) -> str:
    """Первая буква состояния процесса из ps: T — заморожен, S/R — работает, пусто — нет."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.strip()[:1]


def test_pause_resume_and_stop_paused(fake_imapsync, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run_id = run.id

    worker = Worker()
    worker.tick()

    def get_run() -> Run:
        with new_session() as session:
            return session.get(Run, run_id)

    def request(**values):
        with new_session() as session:
            run = session.get(Run, run_id)
            for name, value in values.items():
                setattr(run, name, value)
            session.commit()

    assert wait_until(lambda: get_run().pid is not None)
    pid = get_run().pid

    request(pause_requested=True)
    worker.tick()
    assert get_run().paused_at is not None
    assert wait_until(lambda: process_state(pid) == "T")

    request(pause_requested=False)
    worker.tick()
    assert get_run().paused_at is None
    assert wait_until(lambda: process_state(pid) in ("S", "R"))

    # Остановка замороженного: SIGTERM дойдёт только вместе с SIGCONT
    request(pause_requested=True)
    worker.tick()
    assert wait_until(lambda: process_state(pid) == "T")
    request(cancel_requested=True)
    worker.tick()
    assert wait_until(lambda: run_status(run_id) == RunStatus.CANCELLED, timeout=10)


def test_cancel_queued_without_starting():
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id, cancel_requested=True)
        session.add(run)
        session.commit()
        run_id = run.id
    Worker().tick()
    assert run_status(run_id) == RunStatus.CANCELLED


def test_recover_marks_stale_running_failed():
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id, status=RunStatus.RUNNING, started_at=utcnow())
        session.add(run)
        session.commit()
        run_id = run.id
    Worker().recover()
    with new_session() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED and "перезапущен" in run.error_message
