from conftest import add_mailbox

from mail_backup import storage_estimate as se
from mail_backup.db import new_session
from mail_backup.models import Run, RunKind, RunStatus


def test_formulas():
    gib = 1024**3
    # 10 ГиБ, 100 000 писем: зеркало = 10 ГиБ × 1.02 + 100 000 × 2 КиБ
    assert se.mirror_size(100_000, 10 * gib) == int(10 * gib * 1.02 + 100_000 * 2048)
    assert se.restic_size(10 * gib) == int(10 * gib * 0.8)
    expected = int((se.mirror_size(100_000, 10 * gib) + se.restic_size(10 * gib)) * 1.3)
    assert se.recommended_size(100_000, 10 * gib) == expected


def test_plan_uses_latest_successful_measurement():
    with new_session() as session:
        box = add_mailbox(session)
        session.add_all([
            Run(kind=RunKind.BACKUP, mailbox_id=box.id, status=RunStatus.SUCCESS, host1_messages=10, host1_bytes=1000),
            Run(kind=RunKind.ESTIMATE, mailbox_id=box.id, status=RunStatus.SUCCESS, host1_messages=20, host1_bytes=5000),
            # Неудачная оценка и пробный запуск без цифр не учитываются
            Run(kind=RunKind.ESTIMATE, mailbox_id=box.id, status=RunStatus.FAILED, host1_messages=1, host1_bytes=1),
            Run(kind=RunKind.BACKUP, mailbox_id=box.id, status=RunStatus.SUCCESS),
        ])
        session.commit()
        plan = se.plan(session)
        assert (plan.messages, plan.bytes) == (20, 5000)
        assert plan.unknown == []
        assert plan.recommended_bytes == se.recommended_size(20, 5000)


def test_plan_marks_unmeasured_mailboxes():
    with new_session() as session:
        add_mailbox(session)
        plan = se.plan(session)
        assert len(plan.unknown) == 1 and plan.bytes == 0
