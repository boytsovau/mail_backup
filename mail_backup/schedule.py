"""Расписание «ЧЧ:ММ[, ЧЧ:ММ]» в часовом поясе админки: разбор и ближайшие моменты."""

from datetime import datetime, time as dtime, timedelta


def parse_schedule(value: str | None) -> list[dtime]:
    """«03:00, 15:30» → [03:00, 15:30]. Пустая строка — расписания нет."""
    result = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours, minutes = part.split(":")
            result.append(dtime(int(hours), int(minutes)))
        except ValueError:
            raise ValueError(f"Неверное время «{part}», нужен формат ЧЧ:ММ") from None
    return sorted(set(result))


def latest_slot(times: list[dtime], now_local: datetime) -> datetime | None:
    """Последний наступивший момент расписания за сутки (с учётом вчерашнего дня)."""
    candidates = [
        datetime.combine(now_local.date() - timedelta(days=days_back), t, tzinfo=now_local.tzinfo)
        for days_back in (0, 1)
        for t in times
    ]
    passed = [c for c in candidates if c <= now_local]
    return max(passed) if passed else None


def next_slot(times: list[dtime], now_local: datetime) -> datetime | None:
    """Ближайший будущий момент расписания (для отображения в панели)."""
    candidates = [
        datetime.combine(now_local.date() + timedelta(days=days), t, tzinfo=now_local.tzinfo)
        for days in (0, 1)
        for t in times
    ]
    upcoming = [c for c in candidates if c > now_local]
    return min(upcoming) if upcoming else None
