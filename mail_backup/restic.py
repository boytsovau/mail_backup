"""Снимки restic: история зеркала, срок хранения, копии на другие серверы.

Модуль не знает о БД: запускает restic и разбирает его вывод. Пароль
репозиториев — файл MB_SECRETS_DIR/restic.pass (один для локального и для
удалённых), в командную строку он не попадает.

Снимок делается по каталогу Maildir ящика в локальном Dovecot, с тегом
mailbox-<id> и постоянным именем хоста — по ним работают forget и восстановление.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings

# --retry-lock появился в restic 0.16
MIN_VERSION = (0, 16)
SNAPSHOT_HOST = "mail_backup"
# Сколько ждать чужую блокировку репозитория (например, идёт очистка или выгрузка)
RETRY_LOCK = "3h"
# Короткие команды (список снимков, проверка подключения) не должны висеть вечно
SHORT_TIMEOUT = 120
# Строк конца вывода, по которым ищем итог и текст ошибки
TAIL_LINES = 50

# Коды выхода restic
EXIT_INCOMPLETE = 3  # снимок создан, но часть файлов прочитать не удалось
EXIT_NO_REPO = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12
EXIT_INTERRUPTED = 130

_EXIT_MESSAGES = {
    EXIT_NO_REPO: "репозиторий не найден",
    EXIT_LOCKED: "репозиторий заблокирован другой операцией",
    EXIT_WRONG_PASSWORD: "неверный пароль репозитория",
    EXIT_INTERRUPTED: "прервано",
}


class ResticError(RuntimeError):
    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# restic до 0.17 не знает кода 10: репозитория нет — код 1 и такой текст
# (локально «no such file or directory», по SFTP — «file does not exist»)
_NO_REPO_OLD_RE = re.compile(r"unable to open config file: .*(no such file or directory|does not exist)", re.I)


def is_no_repo(exc: ResticError) -> bool:
    """Ошибка означает, что репозитория по адресу ещё нет."""
    return exc.exit_code == EXIT_NO_REPO or (exc.exit_code == 1 and bool(_NO_REPO_OLD_RE.search(str(exc))))


@dataclass(frozen=True)
class Repo:
    """Репозиторий restic: путь или sftp-адрес и, для sftp, аргументы ssh."""

    url: str
    sftp_args: str | None = None

    def args(self) -> list[str]:
        args = ["-r", self.url]
        if self.sftp_args:
            args += ["-o", f"sftp.args={self.sftp_args}"]
        return args


@dataclass
class Snapshot:
    id: str
    short_id: str
    time: datetime  # наивный UTC, как всё в БД
    paths: list[str]
    tags: list[str]
    size: int | None  # объём файлов в снимке (restic ≥ 0.17 пишет его в summary)

    @property
    def mailbox_id(self) -> int | None:
        for tag in self.tags:
            if tag.startswith("mailbox-") and tag[8:].isdigit():
                return int(tag[8:])
        return None


@dataclass
class BackupSummary:
    snapshot_id: str
    added: int | None  # сколько байт добавилось в репозиторий (после сжатия)
    incomplete: bool = False


def tag(mailbox_id: int) -> str:
    return f"mailbox-{mailbox_id}"


# --- Окружение и запуск --------------------------------------------------------------------

def binary() -> str | None:
    return shutil.which(get_settings().restic_bin)


def env() -> dict[str, str]:
    settings = get_settings()
    # Чужие RESTIC_* (репозиторий, пароль) не должны подмешиваться
    result = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    result.update(
        RESTIC_PASSWORD_FILE=str(settings.restic_password_file),
        RESTIC_CACHE_DIR=str(settings.restic_cache_dir),
        # Без терминала restic молчит о прогрессе; раз в 10 секунд — строка в лог
        RESTIC_PROGRESS_FPS="0.1",
    )
    return result


def _cmd(repo: Repo | None, args: list[str]) -> list[str]:
    bin_path = binary()
    if bin_path is None:
        raise ResticError(f"restic не найден: {get_settings().restic_bin}")
    return [bin_path, *(repo.args() if repo else []), *args]


def _error_text(exit_code: int, lines: list[str]) -> str:
    messages = []
    for line in lines:
        line = line.strip()
        # С --json restic сообщает об ошибке объектом {"message_type": "exit_error", "message": ...}
        if line.startswith("{") and '"message_type"' in line:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("message_type") in ("exit_error", "error"):
                line = str(data.get("message") or (data.get("error") or {}).get("message", ""))
            else:
                continue
        if line:
            messages.append(line.splitlines()[0])
    fatal = [m for m in messages if m.startswith("Fatal:")]
    detail = (fatal[-1] if fatal else messages[-1] if messages else "").removeprefix("Fatal: ")
    known = _EXIT_MESSAGES.get(exit_code)
    return f"restic: {known or f'код выхода {exit_code}'}" + (f" — {detail}" if detail else "")


def run_short(repo: Repo | None, args: list[str], timeout: int = SHORT_TIMEOUT) -> str:
    """Короткая команда: возвращает stdout, при ошибке — ResticError с текстом restic."""
    try:
        proc = subprocess.run(_cmd(repo, args), capture_output=True, text=True, env=env(),
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ResticError(f"restic не ответил за {timeout} с") from None
    if proc.returncode != 0:
        raise ResticError(_error_text(proc.returncode, (proc.stderr + proc.stdout).splitlines()), proc.returncode)
    return proc.stdout


def run_logged(repo: Repo | None, args: list[str], log_path: Path,
               on_start: Callable[[int], None] | None = None) -> tuple[int, list[str]]:
    """Долгая команда: вывод дописывается в лог, PID отдаётся для отмены.

    Возвращает (код выхода, последние строки вывода); при ошибке — ResticError.
    Код 3 ошибкой не считается: снимок создан, хоть и без части файлов.
    """
    cmd = _cmd(repo, args)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: list[str] = []
    # Построчная запись: панель показывает лог, пока restic работает
    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        # Пароля в команде нет — только путь к файлу (RESTIC_PASSWORD_FILE)
        log.write("$ " + shlex.join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env(),
            # Своя группа процессов: при отмене restic завершается вместе с ssh
            start_new_session=True,
        )
        if on_start:
            on_start(proc.pid)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            tail.append(line)
            del tail[:-TAIL_LINES]
        exit_code = proc.wait()
        log.write(f"# код выхода restic: {exit_code}\n")
    if exit_code not in (0, EXIT_INCOMPLETE):
        raise ResticError(_error_text(exit_code, tail), exit_code)
    return exit_code, tail


# --- Состояние --------------------------------------------------------------------------

def version() -> str | None:
    """«0.18.1» или None, если restic не установлен."""
    if binary() is None:
        return None
    try:
        out = run_short(None, ["version"], timeout=20)
    except ResticError:
        return None
    match = re.search(r"restic (\d+\.\d+\.\d+)", out)
    return match.group(1) if match else out.strip()


def version_ok(ver: str | None) -> bool:
    if not ver:
        return False
    parts = re.findall(r"\d+", ver)[:2]
    return tuple(int(p) for p in parts) >= MIN_VERSION


def local_repo() -> Repo:
    return Repo(str(get_settings().restic_repo))


def local_initialized() -> bool:
    return (get_settings().restic_repo / "config").is_file()


def ready() -> bool:
    """Снимки можно делать: restic установлен, пароль и локальный репозиторий на месте."""
    return binary() is not None and get_settings().restic_password_file.is_file() and local_initialized()


def init_local() -> bool:
    """Создаёт локальный репозиторий, если его нет. True — создан сейчас."""
    if local_initialized():
        return False
    repo = get_settings().restic_repo
    repo.parent.mkdir(parents=True, exist_ok=True)
    run_short(local_repo(), ["init"])
    return True


def repo_size(path: Path) -> int:
    """Размер локального репозитория на диске (быстрее, чем restic stats)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# --- Снимки ------------------------------------------------------------------------------

def _parse_time(value: str) -> datetime:
    # restic пишет наносекунды; в старых Python fromisoformat понимает не больше 6 знаков
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def snapshots(repo: Repo, mailbox_id: int | None = None, snapshot_id: str | None = None) -> list[Snapshot]:
    """Снимки репозитория (новые первыми). --no-lock: чтение не ждёт очистку."""
    args = ["snapshots", "--json", "--no-lock"]
    if mailbox_id is not None:
        args += ["--tag", tag(mailbox_id)]
    if snapshot_id:
        args.append(snapshot_id)
    result = []
    for item in json.loads(run_short(repo, args) or "[]"):
        summary = item.get("summary") or {}
        result.append(Snapshot(
            id=item["id"],
            short_id=item.get("short_id", item["id"][:8]),
            time=_parse_time(item["time"]),
            paths=item.get("paths") or [],
            tags=item.get("tags") or [],
            size=summary.get("total_bytes_processed"),
        ))
    return sorted(result, key=lambda s: s.time, reverse=True)


def backup(path: Path, mailbox_id: int, log_path: Path, on_start: Callable[[int], None] | None = None) -> BackupSummary:
    """Снимок каталога ящика."""
    exit_code, tail = run_logged(local_repo(), [
        "backup", str(path), "--json", "--host", SNAPSHOT_HOST, "--tag", tag(mailbox_id),
        "--retry-lock", RETRY_LOCK,
    ], log_path, on_start)
    for line in reversed(tail):
        if line.startswith("{") and '"summary"' in line:
            data = json.loads(line)
            return BackupSummary(data["snapshot_id"], data.get("data_added_packed", data.get("data_added")),
                                 incomplete=exit_code == EXIT_INCOMPLETE)
    raise ResticError("restic не сообщил id снимка (нет итоговой строки в выводе)")


def forget(repo: Repo, mailbox_id: int, keep_days: int, log_path: Path,
           on_start: Callable[[int], None] | None = None) -> None:
    """Удаляет из списка снимки старше срока (место освобождает prune).

    --keep-within считается от самого нового снимка, поэтому последний снимок
    ящика не удаляется никогда, даже если бэкапы давно не идут.
    """
    run_logged(repo, ["forget", "--tag", tag(mailbox_id), "--keep-within", f"{keep_days}d",
                      "--retry-lock", RETRY_LOCK], log_path, on_start)


def prune(repo: Repo, log_path: Path, on_start: Callable[[int], None] | None = None) -> None:
    run_logged(repo, ["prune", "--retry-lock", RETRY_LOCK], log_path, on_start)


def check(repo: Repo, log_path: Path, on_start: Callable[[int], None] | None = None) -> None:
    run_logged(repo, ["check", "--retry-lock", RETRY_LOCK], log_path, on_start)


def restore(snapshot_id: str, path: str, target: Path, log_path: Path,
            on_start: Callable[[int], None] | None = None) -> None:
    """Распаковывает из снимка содержимое каталога path в target."""
    run_logged(local_repo(), ["restore", f"{snapshot_id}:{path}", "--target", str(target), "--no-lock"],
               log_path, on_start)


# --- Удалённые репозитории (SFTP) -------------------------------------------------------------

def ssh_key_path() -> Path:
    return get_settings().ssh_dir / "id_ed25519"


def known_hosts_path() -> Path:
    return get_settings().ssh_dir / "known_hosts"


def remote_repo(remote) -> Repo:
    """Repo для ResticRemote: ssh только с нашим ключом и нашим known_hosts."""
    ssh_args = [
        "-i", str(ssh_key_path()),
        "-F", "/dev/null",  # системные и пользовательские ssh_config не влияют
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",  # никаких вопросов про пароль — сразу ошибка
        # Ключ сервера запоминается при первом подключении; если он потом сменится — отказ
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts_path()}",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=60",
    ]
    return Repo(remote.url, shlex.join(ssh_args))


def remote_status(repo: Repo) -> tuple[bool, str]:
    """Проверка удалённого репозитория: (подключение удалось, текст для админа)."""
    try:
        run_short(repo, ["cat", "config", "--no-lock"], timeout=60)
    except ResticError as exc:
        if is_no_repo(exc):
            return True, "Подключение есть, репозитория ещё нет — он будет создан при первой выгрузке"
        return False, str(exc)
    return True, "Подключение есть, репозиторий на месте"


def init_remote(repo: Repo, log_path: Path) -> bool:
    """Создаёт удалённый репозиторий с параметрами нарезки локального (для дедупликации).

    True — создан сейчас, False — уже был.
    """
    try:
        run_short(repo, ["cat", "config", "--no-lock"], timeout=60)
        return False
    except ResticError as exc:
        if not is_no_repo(exc):
            raise
    settings = get_settings()
    run_logged(repo, ["init", "--from-repo", str(settings.restic_repo),
                      "--from-password-file", str(settings.restic_password_file),
                      "--copy-chunker-params"], log_path)
    return True


def copy_to(repo: Repo, log_path: Path, on_start: Callable[[int], None] | None = None) -> None:
    """Копирует в удалённый репозиторий снимки, которых там ещё нет."""
    settings = get_settings()
    run_logged(repo, ["copy", "--from-repo", str(settings.restic_repo),
                      "--from-password-file", str(settings.restic_password_file),
                      "--retry-lock", RETRY_LOCK], log_path, on_start)


def public_key() -> str | None:
    pub = ssh_key_path().with_suffix(".pub")
    return pub.read_text().strip() if pub.is_file() else None


def generate_ssh_key() -> str:
    """Создаёт ключ ed25519 для выгрузки (старый, если был, заменяется)."""
    key = ssh_key_path()
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (key, key.with_suffix(".pub")):
        path.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"mail_backup@{os.uname().nodename}",
                    "-f", str(key)], check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return public_key() or ""


def _known_host(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def host_fingerprints(host: str, port: int) -> list[str]:
    """Отпечатки ключей сервера, запомненные при первом подключении."""
    known = known_hosts_path()
    if not known.is_file():
        return []
    proc = subprocess.run(["ssh-keygen", "-l", "-F", _known_host(host, port), "-f", str(known)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # Строки «host ED25519 SHA256:... » после комментария «# Host … found»
    return [line.split(" ", 1)[1] for line in proc.stdout.splitlines() if line and not line.startswith("#")]


def forget_host(host: str, port: int) -> None:
    """Забыть ключ сервера (например, после его переустановки)."""
    known = known_hosts_path()
    if known.is_file():
        subprocess.run(["ssh-keygen", "-R", _known_host(host, port), "-f", str(known)],
                       capture_output=True, stdin=subprocess.DEVNULL)
        known.with_name(known.name + ".old").unlink(missing_ok=True)
