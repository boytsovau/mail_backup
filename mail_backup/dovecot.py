"""Учётки ящиков в локальном Dovecot — хранилище бэкапов.

Каждому ящику соответствует пользователь Dovecot (Mailbox.local_login)
со случайным паролем. Файл пользователей (формат passwd-file) целиком
пересобирается из БД при каждом изменении.
"""

import base64
import hashlib
import os
import secrets
import tempfile
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .models import Mailbox

# Воркер выполняет несколько запусков в потоках — файл пишет один поток за раз
_sync_lock = threading.Lock()

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
    taken = set(session.scalars(select(Mailbox.local_login)))
    candidate, n = base, 1
    # Один и тот же адрес может быть заведён дважды (например, у разных хостеров)
    while candidate in taken:
        n += 1
        candidate = f"{base}.{n}"
    return candidate, crypto.encrypt(secrets.token_urlsafe(24))


def sync_users(session: Session) -> int:
    """Пересобирает файл пользователей Dovecot из БД. Возвращает число учёток."""
    settings = get_settings()
    path = settings.dovecot_users_file
    lines = []
    for box in session.scalars(select(Mailbox).order_by(Mailbox.id)):
        validate_local_login(box.local_login)
        password = crypto.decrypt(box.local_password_enc)
        # user:password:uid:gid:gecos:home:shell:extra — uid/gid/home задаёт userdb static
        lines.append(f"{box.local_login}:{ssha512(password)}::::::\n")

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
