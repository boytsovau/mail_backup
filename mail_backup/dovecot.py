"""Учётки ящиков в локальном Dovecot — хранилище бэкапов.

Каждому ящику соответствует пользователь Dovecot (Mailbox.local_login)
со случайным паролем. Файл пользователей (формат passwd-file) целиком
пересобирается из БД при каждом изменении.

Учётки imapsync (ящики и временные учётки восстановления) пускаются только
с 127.0.0.1. Если для ящика включён просмотр, у него есть вторая учётка
(Mailbox.view_login) на тот же Maildir: группа ACL VIEW_ACL_GROUP даёт ей
только чтение (см. шаблоны конфига Dovecot), вход — ещё и из сетей
app.view_allowed_nets.
"""

import base64
import hashlib
import os
import secrets
import tempfile
import threading

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
