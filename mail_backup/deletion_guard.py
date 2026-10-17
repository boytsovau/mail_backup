"""Защита зеркала от проседания ящика на хостере.

Бэкап с удалениями (--delete2) повторяет ящик: если на хостере удалят всю почту
(взлом, ошибка, сбой хостера), зеркало опустеет вслед за ним, а через срок хранения
restic забудет и последние снимки с письмами. Поэтому перед таким бэкапом письма на
хостере считаются (LIST + STATUS, только чтение) и сравниваются с максимумом, который
защита видела за срок хранения снимков. Писем стало меньше на порог из «Настроек»
(app.deletion_guard_percent) или больше — бэкап идёт без удалений, а ящик помечается
остановленным, пока админ не разрешит удаление на странице ящика.

Сравнивается только счёт самой защиты, а не «Host1 Nb messages» imapsync: исключение
папок у них проверяется по-разному, и смешанный счёт давал бы ложные тревоги.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import crypto, imapcheck
from .models import Mailbox, Run, RunKind, utcnow
from .providers import Security

log = logging.getLogger("mail_backup.deletion_guard")


def baseline(session: Session, box: Mailbox, run_id: int, now: datetime) -> int | None:
    """Больше всего писем, сколько защита насчитала у ящика за срок хранения снимков.

    Срок хранения, а не прошлый бэкап: так ловится и проседание по чуть-чуть за много
    бэкапов. Счёт до разрешения админом не берётся — он уже согласился с убылью.
    """
    query = select(func.max(Run.guard_messages)).where(
        Run.mailbox_id == box.id,
        Run.kind == RunKind.BACKUP,
        Run.id != run_id,
        Run.guard_messages.is_not(None),
    )
    if box.retention_days:
        query = query.where(Run.started_at >= now - timedelta(days=box.retention_days))
    if box.deletions_confirmed_at:
        query = query.where(Run.started_at > box.deletions_confirmed_at)
    return session.scalar(query)


def dropped(base: int | None, count: int, percent: int) -> bool:
    """Писем меньше максимума на percent процентов или больше."""
    return bool(percent and base and count * 100 <= base * (100 - percent))


def _set_hold(session: Session, box_id: int, **values) -> None:
    """Меняет поля защиты, не трогая updated_at: это не правка ящика админом,
    и расписание (слоты раньше updated_at не срабатывают) от неё не зависит."""
    session.execute(update(Mailbox).where(Mailbox.id == box_id).values(updated_at=Mailbox.updated_at, **values))
    session.commit()


def allow_deletions(session: Session, run: Run, percent: int) -> bool:
    """Можно ли этому бэкапу удалять из зеркала. Если нет — причина в run.deletions_note."""
    box = run.mailbox
    if box.deletions_hold_at:
        run.deletions_note = ("Удаление из бэкапа остановлено защитой от проседания ящика: "
                              f"{box.deletions_hold_reason}. Разрешить можно на странице ящика")
        session.commit()
        return False
    if not percent:
        return True
    try:
        count = imapcheck.count_messages(box.host, box.port, Security(box.security), box.login,
                                         crypto.decrypt(box.password_enc), box.exclude_folders)
    except imapcheck.ImapError as exc:
        run.deletions_note = f"Не удалось посчитать письма на хостере ({exc}) — в этот раз из бэкапа ничего не удалялось"
        session.commit()
        return False
    now = utcnow()
    base = baseline(session, box, run.id, now)
    run.guard_messages = count
    session.commit()
    if not dropped(base, count, percent):
        return True
    reason = f"на хостере {count} писем, а было до {base} (−{round((base - count) * 100 / base)}%)"
    log.warning("Ящик #%s: %s — удаление из зеркала остановлено", box.id, reason)
    _set_hold(session, box.id, deletions_hold_at=now, deletions_hold_reason=reason)
    session.refresh(box)
    run.deletions_note = f"Удаление из бэкапа остановлено: {reason}. Разрешить можно на странице ящика"
    session.commit()
    return False


def resume(session: Session, box: Mailbox) -> None:
    """Админ разрешил удаление: нынешний счёт писем — новая точка отсчёта."""
    _set_hold(session, box.id, deletions_hold_at=None, deletions_hold_reason=None,
              deletions_confirmed_at=utcnow())
    session.refresh(box)
