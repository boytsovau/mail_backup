"""Данные для графиков: статистика бэкапов по дням и ящикам.

Источник — таблица runs. День запуска — дата его окончания в часовом поясе админки.
В графики идут только бэкапы: восстановление и перенос к состоянию бэкапов
не относятся, а пробные прогоны ничего не копируют (их размеры ящиков всё же
учитываются — imapsync их честно посчитал).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Mailbox, Run, RunKind, RunStatus
from .web.common import STATUS_LABELS

# Периоды на странице графиков, дней
PERIODS = (7, 30, 90, 365)
DEFAULT_PERIOD = 30
# Цветов для ящиков — 8 (палитра --series-N в app.css). Цвет закреплён за ящиком
# по порядку добавления; если ящиков больше, восьмой цвет — серый «Другие»
MAX_SERIES = 8
OTHER = "other"

DONE = (RunStatus.SUCCESS, RunStatus.WARNING, RunStatus.FAILED, RunStatus.CANCELLED)
DONE_OK = (RunStatus.SUCCESS, RunStatus.WARNING)


@dataclass
class Series:
    """Линия или слой графика: один ящик или «Другие»."""

    label: str
    color: str  # номер цвета палитры «1»…«8» или «other»
    box_ids: list[int]


@dataclass
class BoxTotals:
    """Итоги ящика за период — таблица под графиками."""

    box: Mailbox
    backups: int = 0
    ok: int = 0
    warning: int = 0
    failed: int = 0
    messages: int = 0
    bytes: int = 0
    errors: int = 0
    duration: float = 0.0
    size: int | None = None  # размер ящика на хостере на конец периода
    size_messages: int | None = None

    @property
    def avg_duration(self) -> float | None:
        return self.duration / self.backups if self.backups else None


@dataclass
class ChartData:
    days: list[date]
    series: list[Series]
    # ключ графика → {"unit", "type", "datasets": [{"label", "color", "data"}]}
    charts: dict[str, dict] = field(default_factory=dict)
    totals: list[BoxTotals] = field(default_factory=list)

    def has_data(self, name: str) -> bool:
        return any(v for ds in self.charts[name]["datasets"] for v in ds["data"])

    def to_json(self) -> dict:
        """То, что получает app.js: подписи дней и наборы данных графиков."""
        return {"labels": [d.isoformat() for d in self.days], "charts": self.charts}


def color_slots(boxes: list[Mailbox]) -> dict[int, str]:
    """Цвет каждого ящика по порядку добавления — не зависит от фильтров и объёмов."""
    ordered = sorted(boxes, key=lambda b: b.id)
    own = MAX_SERIES if len(ordered) <= MAX_SERIES else MAX_SERIES - 1
    return {box.id: str(i + 1) if i < own else OTHER for i, box in enumerate(ordered)}


def make_series(boxes: list[Mailbox], slots: dict[int, str]) -> list[Series]:
    series = [Series(b.name, slots[b.id], [b.id]) for b in sorted(boxes, key=lambda b: b.id) if slots[b.id] != OTHER]
    rest = [b.id for b in boxes if slots[b.id] == OTHER]
    if len(rest) == 1 and not series:
        # Фильтр по одному ящику из «Других» — показываем его под своим именем
        box = next(b for b in boxes if b.id == rest[0])
        series.append(Series(box.name, OTHER, rest))
    elif rest:
        series.append(Series(f"Другие ({len(rest)})", OTHER, rest))
    return series


def local_day(value: datetime, tz: ZoneInfo) -> date:
    # В БД — наивный UTC (models.utcnow)
    return value.replace(tzinfo=timezone.utc).astimezone(tz).date()


def day_start_utc(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def _is_measurement(run: Run) -> bool:
    """Запуск, в котором imapsync посчитал ящик на хостере (оценка или бэкап, в т.ч. пробный)."""
    return run.status in DONE_OK and run.host1_bytes is not None


def _seed_sizes(session: Session, box_ids: list[int], before: datetime) -> dict[int, Run]:
    """Последний замер размера каждого ящика до начала периода — от него тянем линию."""
    seeds = {}
    for box_id in box_ids:
        run = session.scalars(
            select(Run)
            .where(
                Run.mailbox_id == box_id,
                Run.kind.in_([RunKind.BACKUP, RunKind.ESTIMATE]),
                Run.status.in_(DONE_OK),
                Run.host1_bytes.is_not(None),
                Run.finished_at < before,
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        ).first()
        if run is not None:
            seeds[box_id] = run
    return seeds


def collect(session: Session, tz: ZoneInfo, days: int = DEFAULT_PERIOD, mailbox_id: int | None = None,
            today: date | None = None) -> ChartData:
    """Статистика за `days` дней по всем ящикам или по одному."""
    all_boxes = list(session.scalars(select(Mailbox)))
    slots = color_slots(all_boxes)
    boxes = [b for b in all_boxes if mailbox_id is None or b.id == mailbox_id]
    box_ids = [b.id for b in boxes]

    today = today or datetime.now(tz).date()
    first = today - timedelta(days=days - 1)
    day_list = [first + timedelta(days=i) for i in range(days)]
    start = day_start_utc(first, tz)
    series = make_series(boxes, slots)
    series_of = {box_id: s for s in series for box_id in s.box_ids}
    totals = {b.id: BoxTotals(b) for b in boxes}

    def zeros() -> dict[int, list]:
        return {id(s): [0] * days for s in series}

    volume, duration, errors = zeros(), zeros(), zeros()
    outcomes = {status: [0] * days for status in DONE}
    # Размер по ящикам и дням: значение последнего за день замера
    size_points: dict[int, dict[int, Run]] = {b: {} for b in box_ids}

    runs = session.scalars(
        select(Run)
        .where(
            Run.mailbox_id.in_(box_ids),
            Run.kind.in_([RunKind.BACKUP, RunKind.ESTIMATE]),
            Run.status.in_(DONE),
            Run.finished_at >= start,
        )
        .order_by(Run.finished_at)
    )
    for run in runs:
        i = (local_day(run.finished_at, tz) - first).days
        if not 0 <= i < days:
            continue
        if _is_measurement(run):
            size_points[run.mailbox_id][i] = run
        if run.kind != RunKind.BACKUP or run.dry_run:
            continue
        key = id(series_of[run.mailbox_id])
        outcomes[run.status][i] += 1
        volume[key][i] += run.bytes_transferred or 0
        duration[key][i] += run.duration_seconds or 0
        errors[key][i] += run.errors_count or 0

        t = totals[run.mailbox_id]
        t.backups += 1
        t.ok += run.status == RunStatus.SUCCESS
        t.warning += run.status == RunStatus.WARNING
        t.failed += run.status in (RunStatus.FAILED, RunStatus.CANCELLED)
        t.messages += run.messages_transferred or 0
        t.bytes += run.bytes_transferred or 0
        t.errors += run.errors_count or 0
        t.duration += run.duration_seconds or 0

    # Размер ящика — состояние, а не событие: между замерами держим последнее значение
    seeds = _seed_sizes(session, box_ids, start)
    filled: dict[int, list[int | None]] = {}
    for box_id in box_ids:
        last = seeds.get(box_id)
        values = []
        for i in range(days):
            last = size_points[box_id].get(i, last)
            values.append(last.host1_bytes if last else None)
        filled[box_id] = values
        if last is not None:
            totals[box_id].size = last.host1_bytes
            totals[box_id].size_messages = last.host1_messages

    def size_of(s: Series) -> list[int | None]:
        result = []
        for i in range(days):
            known = [filled[b][i] for b in s.box_ids if filled[b][i] is not None]
            result.append(sum(known) if known else None)
        return result

    def datasets(values: dict[int, list], scale: float = 1.0) -> list[dict]:
        return [{"label": s.label, "color": s.color,
                 "data": [round(v / scale, 1) if scale != 1 else v for v in values[id(s)]]}
                for s in series]

    status_colors = {RunStatus.SUCCESS: "ok", RunStatus.WARNING: "warn",
                     RunStatus.FAILED: "error", RunStatus.CANCELLED: "muted"}
    charts = {
        "volume": {"type": "bar", "unit": "bytes", "datasets": datasets(volume)},
        "size": {"type": "area", "unit": "bytes",
                 "datasets": [{"label": s.label, "color": s.color, "data": size_of(s)} for s in series]},
        "duration": {"type": "bar", "unit": "minutes", "datasets": datasets(duration, scale=60)},
        "outcomes": {"type": "bar", "unit": "count",
                     "datasets": [{"label": STATUS_LABELS[st], "color": status_colors[st], "data": outcomes[st]}
                                  for st in DONE]},
        "errors": {"type": "bar", "unit": "count", "datasets": datasets(errors)},
    }
    return ChartData(day_list, series, charts, [totals[b] for b in box_ids])
