"""Учётки ящиков в локальном Dovecot — хранилище бэкапов.

Каждому ящику соответствует пользователь Dovecot (Mailbox.local_login)
со случайным паролем. Файл пользователей (формат passwd-file) целиком
пересобирается из БД при каждом изменении.

Учётки imapsync (ящики и временные учётки восстановления) пускаются только
с 127.0.0.1. Если для ящика включён просмотр, у него есть вторая учётка
(Mailbox.view_login) на тот же Maildir: группа ACL VIEW_ACL_GROUP даёт ей
только чтение (см. шаблоны конфига Dovecot), вход — ещё и из сетей
app.view_allowed_nets.

Адрес, на котором Dovecot принимает почтовый клиент, задаётся в панели. Веб работает
без прав root, поэтому только пишет желаемый IP в файл запроса (request_listen), а
применяет его root-помощник deploy/dovecot-listen.sh: его запускает systemd
(mail-backup-dovecot-listen.path) при изменении файла. Итог — в файле состояния
(listen_status).
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import app_settings, crypto
from .config import get_settings
from .models import Mailbox, Run, RunStatus

# Воркер выполняет несколько запусков в потоках — файл пишет один поток за раз
_sync_lock = threading.Lock()

# Группа ACL учёток просмотра — права только «lr»; должна совпадать с конфигом Dovecot
VIEW_ACL_GROUP = "mb-view"
# Откуда пускать учётки imapsync. Только IPv4: «:» в адресе IPv6 сломал бы passwd-file
LOCAL_NETS = "127.0.0.1/32"

# Символы, недопустимые в имени пользователя: ломают формат passwd-file или путь Maildir
_BAD_LOGIN_CHARS = set(":/\\\n\r\t ") | {"\0"}


# Сколько ждать помощника, прежде чем показать, что адрес не применяется
LISTEN_APPLY_TIMEOUT = 60


def ssha512(password: str) -> str:
    """Хеш в схеме {SSHA512}, которую Dovecot понимает без доп. модулей."""
    salt = os.urandom(16)
    digest = hashlib.sha512(password.encode() + salt).digest()
    return "{SSHA512}" + base64.b64encode(digest + salt).decode()


def validate_local_login(login: str) -> None:
    if not login or login.startswith(".") or set(login) & _BAD_LOGIN_CHARS:
        raise ValueError(f"Недопустимое имя для локального хранилища: {login!r}")


def new_local_credentials(session: Session, login: str) -> tuple[str, str]:
    """Подбирает свободное имя пользователя Dovecot и пароль для нового ящика.

    Возвращает (local_login, local_password_enc).
    """
    base = login.strip().lower()
    validate_local_login(base)
    # С «_» начинаются временные учётки восстановления из снимка (Run.temp_login)
    if base.startswith("_"):
        raise ValueError(f"Логин не может начинаться с «_»: {login!r}")
    taken = set(session.scalars(select(Mailbox.local_login)))
    candidate, n = base, 1
    # Один и тот же адрес может быть заведён дважды (например, у разных хостеров)
    while candidate in taken:
        n += 1
        candidate = f"{base}.{n}"
    return candidate, crypto.encrypt(secrets.token_urlsafe(24))


def new_view_password() -> str:
    return crypto.encrypt(secrets.token_urlsafe(18))


def sync_users(session: Session) -> int:
    """Пересобирает файл пользователей Dovecot из БД. Возвращает число учёток."""
    settings = get_settings()
    path = settings.dovecot_users_file
    view_nets = ",".join([LOCAL_NETS, *app_settings.parse_nets(app_settings.load(session).view_allowed_nets)])
    lines = []
    for box in session.scalars(select(Mailbox).order_by(Mailbox.id)):
        validate_local_login(box.local_login)
        password = crypto.decrypt(box.local_password_enc)
        # user:password:uid:gid:gecos:home:shell:extra — uid/gid/home по умолчанию задаёт userdb
        lines.append(f"{box.local_login}:{ssha512(password)}::::::allow_nets={LOCAL_NETS}\n")
        if box.view_password_enc:
            # Домашний каталог — Maildir самого ящика; права ограничивает группа ACL
            home = settings.maildir_root / box.local_login
            lines.append(f"{box.view_login}:{ssha512(crypto.decrypt(box.view_password_enc))}::::{home}::"
                         f"allow_nets={view_nets} userdb_acl_groups={VIEW_ACL_GROUP}\n")
    # Временные учётки: снимок распакован в Maildir/_restore_<id>, imapsync читает его
    restores = select(Run).where(Run.snapshot_password_enc.is_not(None),
                                 Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
    for run in session.scalars(restores.order_by(Run.id)):
        password = crypto.decrypt(run.snapshot_password_enc)
        lines.append(f"{run.temp_login}:{ssha512(password)}::::::allow_nets={LOCAL_NETS}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    with _sync_lock:
        # Уникальное имя временного файла: веб и воркер — разные процессы
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".users.")
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            # Атомарная замена: Dovecot никогда не увидит недописанный файл
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    return len(lines)


def _write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def server_ipv4_addresses() -> list[str] | None:
    """IPv4-адреса интерфейсов сервера, кроме 127.*; None — узнать не удалось (нет ip)."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True,
                             timeout=5, check=True)
        interfaces = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    addresses = []
    for iface in interfaces:
        for info in iface.get("addr_info", []):
            addr = info.get("local", "")
            if addr and not addr.startswith("127.") and addr not in addresses:
                addresses.append(addr)
    return addresses


def request_listen(address: str) -> bool:
    """Записывает адрес просмотра для root-помощника; True — если запрос изменился.

    Файл не переписывается без изменений: каждая запись — это перезагрузка Dovecot.
    """
    path = get_settings().dovecot_listen_file
    try:
        if path.read_text().strip() == address:
            return False
    except FileNotFoundError:
        pass
    _write_atomic(path, address + "\n", 0o640)
    return True


@dataclass(frozen=True)
class ListenStatus:
    """Применён ли адрес просмотра из панели к Dovecot."""

    # ok — Dovecot слушает 127.0.0.1 и address; error — не применён (message);
    # pending — помощник ещё не обработал запрос
    state: str
    address: str = ""
    message: str = ""
    # pending дольше LISTEN_APPLY_TIMEOUT — помощник не запускается (не установлен?)
    stuck: bool = False


def listen_status() -> ListenStatus:
    settings = get_settings()
    try:
        request_path = settings.dovecot_listen_file
        requested = request_path.read_text().strip()
        requested_at = request_path.stat().st_mtime
    except FileNotFoundError:
        requested, requested_at = "", None
    status: dict[str, str] = {}
    try:
        for line in settings.dovecot_listen_status_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                status[key] = value
    except FileNotFoundError:
        pass
    # «requested» — запрос, который обработал помощник: если он не совпадает с текущим,
    # итог относится к прошлому запросу. Без «requested» — запрос не удалось даже прочитать
    if status.get("state") in ("ok", "error") and status.get("requested", requested) == requested:
        return ListenStatus(status["state"], requested, status.get("message", ""))
    stuck = requested_at is not None and time.time() - requested_at > LISTEN_APPLY_TIMEOUT
    return ListenStatus("pending", requested, stuck=stuck)
