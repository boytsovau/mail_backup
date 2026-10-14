import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import add_mailbox

from mail_backup import crypto, stats
from mail_backup.db import new_session
from mail_backup.models import Mailbox, Run, RunKind, RunStatus
from mail_backup.web.dashboard import Health, backup_health

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 14)


def backup(box, finished, status=RunStatus.SUCCESS, **kw) -> Run:
    return Run(kind=kw.pop("kind", RunKind.BACKUP), mailbox_id=box.id, status=status, finished_at=finished, **kw)


def other_box(session, n: int) -> Mailbox:
    box = Mailbox(name=f"Ящик {n}", host="imap.example.com", login=f"u{n}@example.com",
                  password_enc=crypto.encrypt("x"), local_login=f"u{n}", local_password_enc=crypto.encrypt("x"))
    session.add(box)
    session.commit()
    return box


def test_runs_grouped_by_admin_day_without_dry_and_transfers():
    with new_session() as session:
        box = add_mailbox(session)
        session.add_all([
            # 13.10 22:30 UTC = 14.10 01:30 по Москве
            backup(box, datetime(2026, 10, 13, 22, 30), bytes_transferred=1000, duration_seconds=120, errors_count=2,
                   status=RunStatus.WARNING),
            backup(box, datetime(2026, 10, 13, 10, 0), bytes_transferred=500, duration_seconds=60),
            backup(box, datetime(2026, 10, 14, 9, 0), status=RunStatus.FAILED),
            # Пробный прогон, восстановление и запуск до начала периода в объём не идут
            backup(box, datetime(2026, 10, 14, 10, 0), dry_run=True, bytes_transferred=10**9),
            backup(box, datetime(2026, 10, 14, 11, 0), kind=RunKind.RESTORE, bytes_transferred=10**9),
            backup(box, datetime(2026, 10, 1, 10, 0), bytes_transferred=10**9),
        ])
        session.commit()
        data = stats.collect(session, MSK, days=7, today=TODAY)

    assert data.days[0] == date(2026, 10, 8) and data.days[-1] == TODAY
    volume = data.charts["volume"]["datasets"][0]["data"]
    assert volume[-2:] == [500, 1000]
    assert data.charts["duration"]["datasets"][0]["data"][-2:] == [1.0, 2.0]
    assert data.charts["errors"]["datasets"][0]["data"][-1] == 2
    outcomes = {ds["color"]: ds["data"][-1] for ds in data.charts["outcomes"]["datasets"]}
    assert outcomes == {"ok": 0, "warn": 1, "error": 1, "muted": 0}

    t = data.totals[0]
    assert (t.backups, t.ok, t.warning, t.failed, t.bytes, t.errors) == (3, 1, 1, 1, 1500, 2)
    assert t.avg_duration == 60
    assert json.dumps(data.to_json())  # в шаблон уходит сериализуемым


def test_size_carried_forward_from_last_measurement():
    with new_session() as session:
        box = add_mailbox(session)
        session.add_all([
            # Замер до периода — от него начинается линия
            backup(box, datetime(2026, 9, 1, 10, 0), kind=RunKind.ESTIMATE, host1_bytes=100, host1_messages=1),
            # Пробный бэкап тоже считает размер ящика
            backup(box, datetime(2026, 10, 12, 10, 0), dry_run=True, host1_bytes=300, host1_messages=3),
            # Неудачный запуск размер не меняет
            backup(box, datetime(2026, 10, 13, 10, 0), status=RunStatus.FAILED, host1_bytes=1),
        ])
        session.commit()
        data = stats.collect(session, MSK, days=4, today=TODAY)

    assert data.charts["size"]["datasets"][0]["data"] == [100, 300, 300, 300]
    assert data.totals[0].size == 300 and data.totals[0].size_messages == 3
    assert not data.has_data("volume") and data.has_data("size")


def test_colors_follow_mailbox_and_fold_into_other():
    with new_session() as session:
        boxes = [other_box(session, n) for n in range(10)]
        slots = stats.color_slots(boxes)
        assert [slots[b.id] for b in boxes] == ["1", "2", "3", "4", "5", "6", "7"] + ["other"] * 3
        data = stats.collect(session, MSK, days=7, today=TODAY)
        assert [s.color for s in data.series][-1] == "other" and data.series[-1].label == "Другие (3)"
        assert len(data.series) == stats.MAX_SERIES

        # Фильтр по одному ящику: цвет и имя — его собственные
        one = stats.collect(session, MSK, days=7, mailbox_id=boxes[2].id, today=TODAY)
        assert [(s.label, s.color) for s in one.series] == [("Ящик 2", "3")]
        tail = stats.collect(session, MSK, days=7, mailbox_id=boxes[9].id, today=TODAY)
        assert [(s.label, s.color) for s in tail.series] == [("Ящик 9", "other")]


def test_backup_health():
    now = datetime(2026, 10, 14, 12, 0)
    box = Mailbox(enabled=True, schedule="03:00", created_at=now - timedelta(days=10))
    ok = Run(status=RunStatus.SUCCESS, finished_at=now - timedelta(hours=9))
    assert backup_health(box, ok, ok, now) == Health.OK

    warn = Run(status=RunStatus.WARNING, finished_at=now - timedelta(hours=9))
    assert backup_health(box, warn, warn, now) == Health.WARNING

    failed = Run(status=RunStatus.FAILED, finished_at=now - timedelta(hours=1))
    assert backup_health(box, failed, ok, now) == Health.FAILED

    old = Run(status=RunStatus.SUCCESS, finished_at=now - timedelta(days=2))
    assert backup_health(box, old, old, now) == Health.STALE
    assert backup_health(box, None, None, now) == Health.STALE

    # Без расписания «просрочен» не бывает; новый ящик ещё не успел
    assert backup_health(Mailbox(enabled=True, schedule=None, created_at=box.created_at), old, old, now) == Health.OK
    fresh = Mailbox(enabled=True, schedule="03:00", created_at=now - timedelta(hours=2))
    assert backup_health(fresh, None, None, now) == Health.NEVER
    assert backup_health(Mailbox(enabled=False, schedule="03:00", created_at=box.created_at), failed, ok, now) == Health.OFF
