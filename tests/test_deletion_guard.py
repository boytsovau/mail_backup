import json
from datetime import timedelta

import pytest
from conftest import add_mailbox

from mail_backup import app_settings, deletion_guard, imapcheck, restic
from mail_backup.db import new_session
from mail_backup.imapcheck import count_messages as real_count_messages
from mail_backup.jobs import execute_run
from mail_backup.models import Run, RunKind, RunStatus, utcnow
from mail_backup.providers import Security
from mail_backup.web.dashboard import Health, backup_health


@pytest.fixture
def restic_on(fake_restic):
    restic.init_local()
    return fake_restic


def deletes(record) -> bool:
    return "--delete2" in json.loads(record.read_text())["args"]


def backup(session, box) -> Run:
    run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
    session.add(run)
    session.commit()
    run = execute_run(session, run.id)
    assert run.status == RunStatus.SUCCESS, run.error_message
    return run


def deleting_mailbox(session, retention_days=30):
    box = add_mailbox(session)
    box.mirror_deletions = True
    box.retention_days = retention_days
    session.commit()
    return box


def test_drop_threshold():
    assert not deletion_guard.dropped(None, 0, 10)  # точки отсчёта ещё нет
    assert not deletion_guard.dropped(100, 91, 10)
    assert deletion_guard.dropped(100, 90, 10)  # ровно 10% — уже проседание
    assert deletion_guard.dropped(100, 0, 10)
    assert not deletion_guard.dropped(100, 0, 0)  # 0 — защита выключена
    assert not deletion_guard.dropped(0, 0, 10)


def test_drop_stops_deletions_until_admin_allows(fake_imapsync, restic_on, host_messages):
    with new_session() as session:
        box = deleting_mailbox(session)
        updated = box.updated_at
        host_messages["count"] = 1000
        run = backup(session, box)
        assert deletes(fake_imapsync) and run.guard_messages == 1000 and run.deletions_note is None

        # Убыло 5% — обычная чистка ящика, удаление идёт
        host_messages["count"] = 950
        assert backup(session, box).deletions_note is None and deletes(fake_imapsync)

        # Убыло 10% от максимума — удаление остановлено
        host_messages["count"] = 900
        run = backup(session, box)
        assert not deletes(fake_imapsync)
        assert run.snapshot_id, "снимок после бэкапа делается и при остановленном удалении"
        assert "900 писем, а было до 1000 (−10%)" in run.deletions_note
        session.refresh(box)
        assert box.deletions_hold_at and "900" in box.deletions_hold_reason
        # Остановка — не правка ящика: расписание не сбивается
        assert box.updated_at == updated
        assert backup_health(box, run, run, utcnow(), snapshots=True) == Health.HOLD

        # Остановка держится, даже если письма «вернулись»: решает админ
        host_messages["count"], calls = 1000, host_messages["calls"]
        run = backup(session, box)
        assert not deletes(fake_imapsync) and "остановлено защитой" in run.deletions_note
        assert host_messages["calls"] == calls

        deletion_guard.resume(session, box)
        assert box.deletions_hold_at is None and box.deletions_confirmed_at
        # После разрешения прежний максимум не в счёт: 900 — новая точка отсчёта
        host_messages["count"] = 900
        assert backup(session, box).deletions_note is None and deletes(fake_imapsync)
        host_messages["count"] = 800
        assert backup(session, box).deletions_note and not deletes(fake_imapsync)


def test_baseline_only_within_retention(fake_imapsync, restic_on, host_messages):
    with new_session() as session:
        box = deleting_mailbox(session, retention_days=30)
        host_messages["count"] = 1000
        old = backup(session, box)
        # Тот бэкап был давно: его снимки уже ушли по сроку хранения
        old.started_at = utcnow() - timedelta(days=31)
        session.commit()
        host_messages["count"] = 500
        assert backup(session, box).deletions_note is None and deletes(fake_imapsync)

        # Бессрочное хранение — максимум за всё время
        box.retention_days = 0
        session.commit()
        host_messages["count"] = 400
        assert "было до 1000" in backup(session, box).deletions_note


def test_count_failure_skips_deletions_without_hold(fake_imapsync, restic_on, host_messages):
    with new_session() as session:
        box = deleting_mailbox(session)
        host_messages["error"] = "нет соединения с imap.yandex.ru:993"
        run = backup(session, box)
        assert not deletes(fake_imapsync)
        assert "Не удалось посчитать письма" in run.deletions_note and run.guard_messages is None
        session.refresh(box)
        assert box.deletions_hold_at is None


def test_guard_off_and_no_deletions(fake_imapsync, restic_on, host_messages):
    with new_session() as session:
        app_settings.save(session, app_settings.AppSettings(deletion_guard_percent=0))
        box = deleting_mailbox(session)
        assert backup(session, box).guard_messages is None and deletes(fake_imapsync)
        # Ящик без удалений хостер не опрашивает вовсе
        box.mirror_deletions = False
        session.commit()
        backup(session, box)
        assert host_messages["calls"] == 0


def test_validate_percent():
    assert app_settings.validate(2, "Europe/Moscow", 90, 25).deletion_guard_percent == 25
    with pytest.raises(ValueError, match="проседания"):
        app_settings.validate(2, "Europe/Moscow", 90, 95)


class FakeImap:
    """IMAP-сервер в памяти: ответы LIST и STATUS как у imaplib."""

    def __init__(self):
        self.commands = []

    def login(self, login, password):
        self.commands.append("LOGIN")

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            (b'(\\HasNoChildren) "/" {13}', b'&BCEEPwQwBDw-'),
            b"",
            b'(\\HasNoChildren \\Trash) "/" Trash',
            b'(\\HasNoChildren) "/" "Say \\"hi\\""',
        ]

    def status(self, name, items):
        self.commands.append(f"STATUS {name}")
        counts = {'"INBOX"': 10, '"&BCEEPwQwBDw-"': 5, '"Trash"': 2, '"Say \\"hi\\""': 1}
        return "OK", [f"{name} (MESSAGES {counts[name]})".encode()]

    def logout(self):
        self.commands.append("LOGOUT")


def test_count_messages_reads_only_status(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(imapcheck, "_connect", lambda *a: fake)
    args = ("h", 993, Security.SSL, "u", "p")
    assert real_count_messages(*args) == 18
    # Ни SELECT, ни EXAMINE, ни FETCH: только список папок и их счётчики
    assert all(c.split()[0] in ("LOGIN", "STATUS", "LOGOUT") for c in fake.commands)
    assert 'STATUS "[Gmail]"' not in fake.commands
    # Исключение папок — и по имени в mUTF-7, и по-русски
    assert real_count_messages(*args, exclude="^Спам$|^Trash$") == 11
    assert real_count_messages(*args, exclude="^&BCE") == 13
    # Непонятное Python выражение — считаем все папки, а не падаем
    assert real_count_messages(*args, exclude="(?<=a+)b") == 18


def test_decode_mutf7():
    assert imapcheck.decode_mutf7("&BCEEPwQwBDw-") == "Спам"
    assert imapcheck.decode_mutf7("A&-B") == "A&B"
    assert imapcheck.decode_mutf7("INBOX") == "INBOX"
