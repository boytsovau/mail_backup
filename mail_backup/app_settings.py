"""Настройки, которые меняются в веб-панели (хранятся в таблице settings).

Базовые пути и режим развёртывания — в env-файле (config.py), всё остальное — здесь.
"""

import ipaddress
from dataclasses import dataclass, fields, replace
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
    # Сети, из которых можно смотреть бэкап почтовым клиентом (через запятую);
    # пусто — только с самого сервера (SSH-туннель)
    view_allowed_nets: str = ""
    # IP сервера, на котором Dovecot принимает почтовый клиент (кроме 127.0.0.1);
    # пусто — только локально. Применяет root-помощник (см. dovecot.request_listen)
    view_imap_host: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class ResticSettings:
    """Снимки restic (страница «Снимки»)."""

    # Выключено — снимков, очистки и выгрузки нет, а зеркало не удаляет письма,
    # удалённые на хостере (иначе без истории их было бы не вернуть)
    enabled: bool = True
    # Ежедневная очистка по сроку хранения и выгрузка на удалённые серверы, «ЧЧ:ММ[, ЧЧ:ММ]»
    schedule: str = "05:00"


# Префиксы ключей в таблице settings, чтобы не пересекаться со служебными
_PREFIX = "app."
_RESTIC_PREFIX = "restic."


def _load(session: Session, cls, prefix: str):
    values = {}
    for f in fields(cls):
        row = session.get(Setting, prefix + f.name)
        if row is None:
            continue
        # bool("False") — истина, поэтому логические значения разбираем сами
        values[f.name] = row.value == "True" if f.type is bool else f.type(row.value)
    return cls(**values)


def _save(session: Session, obj, prefix: str) -> None:
    for f in fields(obj):
        session.merge(Setting(key=prefix + f.name, value=str(getattr(obj, f.name))))
    session.commit()


def load(session: Session) -> AppSettings:
    return _load(session, AppSettings, _PREFIX)


def load_restic(session: Session) -> ResticSettings:
    return _load(session, ResticSettings, _RESTIC_PREFIX)


def save_restic(session: Session, settings: ResticSettings, **changes) -> ResticSettings:
    settings = replace(settings, **changes)
    _save(session, settings, _RESTIC_PREFIX)
    return settings


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


def parse_nets(text: str) -> list[str]:
    """Разбирает список сетей «10.8.0.0/24, 192.168.1.5»; ValueError при ошибке.

    Только IPv4: адреса IPv6 содержат «:», а это разделитель полей
    в файле учёток Dovecot (passwd-file).
    """
    nets = []
    for item in text.replace(";", ",").replace(" ", ",").split(","):
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise ValueError(f"Не сеть и не IP-адрес: {item}") from None
        if net.version != 4:
            raise ValueError(f"Поддерживаются только сети IPv4: {item}")
        if str(net) not in nets:
            nets.append(str(net))
    return nets


def parse_listen(text: str, available: list[str] | None) -> str:
    """Проверяет адрес просмотра почты; пусто — только 127.0.0.1. ValueError при ошибке.

    available — IPv4-адреса интерфейсов сервера (None — узнать не удалось, не сверяем):
    адреса, которого нет на сервере, Dovecot не откроет — и бэкапы встанут.
    """
    text = text.strip()
    if not text:
        return ""
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        raise ValueError(f"Нужен IPv4-адрес сервера, а не «{text}»") from None
    if addr.is_loopback:
        raise ValueError("127.0.0.1 Dovecot слушает всегда — укажите IP сервера в VPN или локальной сети")
    if addr.is_unspecified or addr.is_multicast:
        raise ValueError(f"Нужен адрес одного из интерфейсов сервера, а не {addr}")
    if available is not None and str(addr) not in available:
        raise ValueError(f"Адреса {addr} нет на этом сервере")
    return str(addr)


def save(session: Session, settings: AppSettings) -> None:
    _save(session, settings, _PREFIX)
