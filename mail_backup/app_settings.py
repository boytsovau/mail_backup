"""Настройки, которые меняются в веб-панели (хранятся в таблице settings).

Базовые пути и режим развёртывания — в env-файле (config.py), всё остальное — здесь.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .models import Setting


@dataclass(frozen=True)
class AppSettings:
    # Сколько запусков imapsync идёт одновременно
    max_parallel_jobs: int = 2
    # Часовой пояс расписаний и отображения дат
    timezone: str = "Europe/Moscow"
    # Через сколько дней удалять логи запусков (статистика в БД остаётся)
    log_retention_days: int = 90

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# Префикс ключей в таблице settings, чтобы не пересекаться со служебными
_PREFIX = "app."


def load(session: Session) -> AppSettings:
    values = {}
    for name, default in AppSettings().__dict__.items():
        row = session.get(Setting, _PREFIX + name)
        if row is not None:
            values[name] = type(default)(row.value)
    return AppSettings(**values)


def validate(max_parallel_jobs: int, timezone: str, log_retention_days: int) -> AppSettings:
    """Проверяет значения из формы; ValueError с понятным текстом при ошибке."""
    if not 1 <= max_parallel_jobs <= 10:
        raise ValueError("Параллельных задач: от 1 до 10")
    if not 1 <= log_retention_days <= 3650:
        raise ValueError("Хранение логов: от 1 до 3650 дней")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Неизвестный часовой пояс: {timezone}") from None
    return AppSettings(max_parallel_jobs, timezone, log_retention_days)


def save(session: Session, settings: AppSettings) -> None:
    for name, value in settings.__dict__.items():
        session.merge(Setting(key=_PREFIX + name, value=str(value)))
    session.commit()
