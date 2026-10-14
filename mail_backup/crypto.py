"""Шифрование паролей почтовых ящиков и работа с файлами ключей.

Пароли хранятся в БД только в зашифрованном виде (Fernet).
Ключ лежит отдельно от БД, поэтому копия одной только БД паролей не раскрывает.
"""

import os
import secrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from .config import get_settings


def _write_secret(path: Path, data: bytes) -> None:
    """Создаёт файл с секретом сразу с правами 600, не перезаписывая существующий."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def ensure_keys() -> None:
    """Создаёт ключ шифрования и ключ сессий, если их ещё нет."""
    settings = get_settings()
    if not settings.fernet_key_file.exists():
        _write_secret(settings.fernet_key_file, Fernet.generate_key())
    if not settings.session_key_file.exists():
        _write_secret(settings.session_key_file, secrets.token_urlsafe(48).encode())


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key_file.read_bytes().strip())


def session_secret() -> str:
    return get_settings().session_key_file.read_text().strip()


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
