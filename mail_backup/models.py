"""Модели БД."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .providers import Security


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo — все даты в БД хранятся так.

    SQLite не хранит часовой пояс, поэтому единый «наивный UTC» исключает
    ошибки сравнения aware/naive. В часовой пояс админки переводим при выводе.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RunKind(StrEnum):
    BACKUP = "backup"  # хостер → локальный Dovecot
    RESTORE = "restore"  # локальный Dovecot → любой ящик
    MIGRATE = "migrate"  # хостер → другой хостер напрямую


class RunTrigger(StrEnum):
    MANUAL = "manual"  # кнопка в админке или CLI
    SCHEDULE = "schedule"  # расписание ящика


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    WARNING = "warning"  # imapsync завершился, но с ошибками по отдельным письмам
    FAILED = "failed"
    CANCELLED = "cancelled"


class Mailbox(Base):
    """Почтовый ящик на хостере, который мы бэкапим."""

    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(50), default="custom")

    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(default=993)
    security: Mapped[Security] = mapped_column(String(20), default=Security.SSL)
    login: Mapped[str] = mapped_column(String(255))
    password_enc: Mapped[str] = mapped_column(Text)

    # Учётка этого ящика в локальном Dovecot (хранилище бэкапа)
    local_login: Mapped[str] = mapped_column(String(255), unique=True)
    local_password_enc: Mapped[str] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(default=True)
    # Время ежедневного бэкапа «ЧЧ:ММ[,ЧЧ:ММ...]» в часовом поясе админки,
    # пусто — только ручной запуск
    schedule: Mapped[str | None] = mapped_column(String(200))
    # Срок хранения в днях, 0 — хранить бессрочно
    retention_days: Mapped[int] = mapped_column(default=0)
    # Регулярное выражение для папок, которые не нужно копировать
    exclude_folders: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    runs: Mapped[list["Run"]] = relationship(back_populates="mailbox")


class Run(Base):
    """Один запуск imapsync: бэкап, восстановление или перенос."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[RunKind] = mapped_column(String(20))
    status: Mapped[RunStatus] = mapped_column(String(20), default=RunStatus.QUEUED, index=True)
    trigger: Mapped[RunTrigger] = mapped_column(String(20), default=RunTrigger.MANUAL, server_default="manual")
    # Пробный прогон imapsync --dry: ничего не копирует, только показывает план
    dry_run: Mapped[bool] = mapped_column(default=False)
    # Админ нажал «Отменить»; воркер завершит процесс imapsync
    cancel_requested: Mapped[bool] = mapped_column(default=False, server_default="0")

    # Для бэкапа и переноса — источник, для восстановления — чей бэкап берём
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True)
    mailbox: Mapped[Mailbox] = relationship(back_populates="runs")

    # Получатель при восстановлении и переносе (для бэкапа не заполняется)
    target_host: Mapped[str | None] = mapped_column(String(255))
    target_port: Mapped[int | None]
    target_security: Mapped[Security | None] = mapped_column(String(20))
    target_login: Mapped[str | None] = mapped_column(String(255))
    target_password_enc: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    pid: Mapped[int | None]
    exit_code: Mapped[int | None]
    log_path: Mapped[str | None] = mapped_column(String(500))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Живой прогресс, обновляется воркером по ходу работы imapsync
    progress_done: Mapped[int | None]
    progress_total: Mapped[int | None]
    eta_seconds: Mapped[int | None]

    # Итоговая статистика из лога imapsync — источник данных для графиков
    messages_transferred: Mapped[int | None]
    messages_skipped: Mapped[int | None]
    bytes_transferred: Mapped[int | None]
    bytes_skipped: Mapped[int | None]
    errors_count: Mapped[int | None]
    folders_synced: Mapped[int | None]
    host1_messages: Mapped[int | None]
    host1_bytes: Mapped[int | None]
    host2_messages: Mapped[int | None]
    host2_bytes: Mapped[int | None]
    duration_seconds: Mapped[float | None]


class Setting(Base):
    """Глобальные настройки ключ–значение (в т.ч. хеш пароля админа)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
