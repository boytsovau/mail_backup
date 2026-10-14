"""Разбор вывода imapsync: живой прогресс и итоговая статистика.

Форматы строк сверены с исходником imapsync (функции eta() и stats()):
  msg INBOX/12 {3456} copied to INBOX/7 ... ETA: <дата>  120 s  80/200 msgs left
  ++++ Statistics
  Transfer time                           : 12.3 sec
  Messages transferred                    : 120
  ...
  Detected 0 errors
"""

import re
from dataclasses import asdict, dataclass, field, fields

from ..models import RunStatus

_ETA_RE = re.compile(r"ETA: .*?\s(\d+) s\s+(\d+)/(\d+) msgs left")

# В режиме --dry imapsync не увеличивает «Messages transferred», а на каждое письмо,
# которое скопировал бы, печатает (функция copy_message):
#   msg INBOX/12 copying to INBOX 	(not really since --dry mode) ETA: ...
_DRY_COPY_RE = re.compile(r"^msg .+ copying to .+not really since --dry mode")

# Поле статистики → регулярное выражение; при повторах побеждает последнее значение
# (размеры ящиков печатаются в начале и в конце работы, нам нужны итоговые)
_STAT_RES: dict[str, re.Pattern[str]] = {
    "duration_seconds": re.compile(r"^Transfer time\s*: ([\d.]+) sec"),
    "folders_synced": re.compile(r"^Folders synced\s*: (\d+)/\d+"),
    "messages_transferred": re.compile(r"^Messages transferred\s*: (\d+)"),
    "messages_skipped": re.compile(r"^Messages skipped\s*: (\d+)"),
    "bytes_transferred": re.compile(r"^Total bytes transferred\s*: (\d+)"),
    "bytes_skipped": re.compile(r"^Total bytes skipped\s*: (\d+)"),
    "errors_count": re.compile(r"^Detected (\d+) errors"),
    "host1_messages": re.compile(r"^Host1 Nb messages:\s*(\d+) messages"),
    "host2_messages": re.compile(r"^Host2 Nb messages:\s*(\d+) messages"),
    "host1_bytes": re.compile(r"^Host1 Total size:\s*(\d+) bytes"),
    "host2_bytes": re.compile(r"^Host2 Total size:\s*(\d+) bytes"),
}

# Строка размера папки источника:
#   Host1 folder     2/9 [&BB4EQgQ-] = [Отправленные]   Size:  2000 Messages:  1 Biggest:  2000
_FOLDER_RE = re.compile(
    r"^Host1 folder\s+\d+/\d+\s+(?P<name>.*?)\s+Size:\s*(?P<size>\d+)"
    r"\s+Messages:\s*(?P<messages>\d+)\s+Biggest:\s*(?P<biggest>\d+)"
)


def _folder_name(raw: str) -> str:
    """«[&BB4E...-] = [Отправленные]» → «Отправленные»; «[INBOX]» → «INBOX»."""
    if "] = [" in raw:
        raw = raw.split("] = [", 1)[1]
    return raw.strip().removeprefix("[").removesuffix("]")


@dataclass
class FolderSize:
    name: str
    size: int
    messages: int
    biggest: int


# Коды выхода imapsync (см. Readonly my $EXIT_* в исходнике)
EXIT_OK = 0
EXIT_WITH_ERRORS = 111  # синхронизация прошла, но часть писем с ошибками


@dataclass
class Progress:
    msgs_left: int
    msgs_total: int
    eta_seconds: int

    @property
    def done(self) -> int:
        return self.msgs_total - self.msgs_left


@dataclass
class Stats:
    duration_seconds: float | None = None
    folders_synced: int | None = None
    messages_transferred: int | None = None
    messages_planned: int | None = None
    messages_skipped: int | None = None
    bytes_transferred: int | None = None
    bytes_skipped: int | None = None
    errors_count: int | None = None
    host1_messages: int | None = None
    host2_messages: int | None = None
    host1_bytes: int | None = None
    host2_bytes: int | None = None

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "folders"}

    # Размеры папок источника (печатаются в начале и, при --foldersizesatend, в конце)
    folders: list[FolderSize] = field(default_factory=list)

    def folders_as_dicts(self) -> list[dict]:
        return [asdict(f) for f in self.folders]


def parse_progress(line: str) -> Progress | None:
    if m := _ETA_RE.search(line):
        eta, left, total = map(int, m.groups())
        return Progress(msgs_left=left, msgs_total=total, eta_seconds=eta)
    return None


class OutputParser:
    """Построчно накапливает статистику из потока вывода imapsync."""

    def __init__(self) -> None:
        self.stats = Stats()
        self.last_progress: Progress | None = None

    def feed(self, line: str) -> Progress | None:
        """Обрабатывает строку; возвращает прогресс, если строка его содержит."""
        line = line.strip()
        # Строка пробного копирования содержит и ETA — считаем её до разбора прогресса
        if _DRY_COPY_RE.match(line):
            self.stats.messages_planned = (self.stats.messages_planned or 0) + 1
        if progress := parse_progress(line):
            self.last_progress = progress
            return progress
        if m := _FOLDER_RE.match(line):
            folder = FolderSize(
                _folder_name(m["name"]), int(m["size"]), int(m["messages"]), int(m["biggest"])
            )
            # Повтор папки (размеры в конце работы) заменяет старое значение
            self.stats.folders = [f for f in self.stats.folders if f.name != folder.name] + [folder]
            return None
        for name, regex in _STAT_RES.items():
            if m := regex.match(line):
                value = m.group(1)
                setattr(self.stats, name, float(value) if name == "duration_seconds" else int(value))
                break
        return None


def status_from_exit(exit_code: int, cancelled: bool = False) -> RunStatus:
    # Отмена — только по запросу админа; гибель от сигнала без запроса — это сбой
    if cancelled:
        return RunStatus.CANCELLED
    if exit_code == EXIT_OK:
        return RunStatus.SUCCESS
    if exit_code == EXIT_WITH_ERRORS:
        return RunStatus.WARNING
    return RunStatus.FAILED
