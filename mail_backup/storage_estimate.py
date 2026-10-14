"""Оценка места под бэкапы по размерам ящиков на хостере.

Размеры берутся из последнего удачного запуска, где imapsync посчитал источник:
оценки (--justfoldersizes) или бэкапа. Коэффициенты — осторожные средние;
точные цифры появятся после первого бэкапа и снимков restic.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Mailbox, Run, RunKind, RunStatus

# Блок файловой системы: в Maildir каждое письмо — отдельный файл, в среднем
# теряется половина блока на письмо
FS_BLOCK = 4096
# Индексы и служебные файлы Dovecot относительно объёма писем
DOVECOT_OVERHEAD = 0.02
# Сжатый репозиторий restic относительно объёма писем: текст сжимается хорошо,
# вложения (PDF, JPG, архивы) — почти нет; берём осторожное значение
RESTIC_RATIO = 0.8
# Запас на рост ящиков и на удалённые письма, которые живут в снимках весь срок хранения
GROWTH_RESERVE = 1.3


@dataclass
class MailboxSize:
    box: Mailbox
    run: Run | None  # откуда взяты цифры
    messages: int = 0
    bytes: int = 0
    measured_at: datetime | None = None
    folders: list[dict] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.run is not None


@dataclass
class StoragePlan:
    rows: list[MailboxSize]
    messages: int
    bytes: int
    mirror_bytes: int  # зеркало Maildir в Dovecot
    restic_bytes: int  # первый снимок restic (столько же — на каждом удалённом сервере)
    recommended_bytes: int  # рекомендуемый размер диска под бэкапы

    @property
    def unknown(self) -> list[MailboxSize]:
        return [r for r in self.rows if not r.known]


def mirror_size(messages: int, size: int) -> int:
    return int(size * (1 + DOVECOT_OVERHEAD) + messages * FS_BLOCK / 2)


def restic_size(size: int) -> int:
    return int(size * RESTIC_RATIO)


def recommended_size(messages: int, size: int) -> int:
    return int((mirror_size(messages, size) + restic_size(size)) * GROWTH_RESERVE)


def latest_measurement(session: Session, mailbox_id: int) -> Run | None:
    """Последний удачный запуск с посчитанным источником (оценка или бэкап)."""
    return session.scalars(
        select(Run)
        .where(
            Run.mailbox_id == mailbox_id,
            Run.kind.in_([RunKind.ESTIMATE, RunKind.BACKUP]),
            Run.status.in_([RunStatus.SUCCESS, RunStatus.WARNING]),
            Run.host1_bytes.is_not(None),
        )
        .order_by(Run.id.desc())
        .limit(1)
    ).first()


def folders_of(run: Run | None) -> list[dict]:
    if run is None or not run.folder_stats:
        return []
    return sorted(json.loads(run.folder_stats), key=lambda f: f["size"], reverse=True)


def plan(session: Session) -> StoragePlan:
    rows = []
    for box in session.scalars(select(Mailbox).order_by(Mailbox.name)):
        run = latest_measurement(session, box.id)
        if run is None:
            rows.append(MailboxSize(box, None))
            continue
        rows.append(MailboxSize(
            box, run,
            messages=run.host1_messages or 0,
            bytes=run.host1_bytes or 0,
            measured_at=run.finished_at,
            folders=folders_of(run),
        ))
    messages = sum(r.messages for r in rows)
    size = sum(r.bytes for r in rows)
    return StoragePlan(
        rows=rows,
        messages=messages,
        bytes=size,
        mirror_bytes=mirror_size(messages, size),
        restic_bytes=restic_size(size),
        recommended_bytes=recommended_size(messages, size),
    )
