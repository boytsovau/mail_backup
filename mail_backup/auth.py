"""Учётная запись единственного администратора.

Логин и argon2-хеш пароля хранятся в таблице settings.
Первый вход — по одноразовой ссылке с токеном, которую печатает установщик.
"""

import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy.orm import Session

from .models import Setting, utcnow

ADMIN_LOGIN_KEY = "admin_login"
ADMIN_HASH_KEY = "admin_password_hash"
# Меняется при каждой смене пароля — старые сессии в браузерах становятся недействительны
SESSION_VERSION_KEY = "admin_session_version"
SETUP_TOKEN_KEY = "setup_token_sha256"
SETUP_EXPIRES_KEY = "setup_token_expires"
SETUP_TOKEN_TTL = timedelta(hours=24)
MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()


def validate_credentials(login: str, password: str) -> None:
    if not login.strip():
        raise ValueError("Логин не может быть пустым")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")


def admin_exists(session: Session) -> bool:
    return session.get(Setting, ADMIN_HASH_KEY) is not None


def set_admin(session: Session, login: str, password: str) -> None:
    validate_credentials(login, password)
    session.merge(Setting(key=ADMIN_LOGIN_KEY, value=login.strip()))
    session.merge(Setting(key=ADMIN_HASH_KEY, value=_hasher.hash(password)))
    session.merge(Setting(key=SESSION_VERSION_KEY, value=secrets.token_hex(8)))
    # Ссылка первичной настройки после задания пароля больше не нужна
    for key in (SETUP_TOKEN_KEY, SETUP_EXPIRES_KEY):
        if row := session.get(Setting, key):
            session.delete(row)
    session.commit()


def admin_login(session: Session) -> str | None:
    row = session.get(Setting, ADMIN_LOGIN_KEY)
    return row.value if row else None


def session_version(session: Session) -> str | None:
    row = session.get(Setting, SESSION_VERSION_KEY)
    return row.value if row else None


def check_admin(session: Session, login: str, password: str) -> bool:
    stored_login = session.get(Setting, ADMIN_LOGIN_KEY)
    stored_hash = session.get(Setting, ADMIN_HASH_KEY)
    if stored_login is None or stored_hash is None:
        return False
    try:
        # Хеш проверяем всегда, даже при неверном логине: время ответа одинаковое
        password_ok = _hasher.verify(stored_hash.value, password)
    except VerificationError:
        return False
    return password_ok and secrets.compare_digest(login.encode(), stored_login.value.encode())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_setup_token(session: Session) -> str:
    """Новый одноразовый токен первичной настройки (в БД — только его хеш)."""
    token = secrets.token_urlsafe(32)
    session.merge(Setting(key=SETUP_TOKEN_KEY, value=_sha256(token)))
    session.merge(Setting(key=SETUP_EXPIRES_KEY, value=(utcnow() + SETUP_TOKEN_TTL).isoformat()))
    session.commit()
    return token


def check_setup_token(session: Session, token: str) -> bool:
    """Токен действителен, только пока админ не создан и срок не истёк."""
    if admin_exists(session) or not token:
        return False
    stored = session.get(Setting, SETUP_TOKEN_KEY)
    expires = session.get(Setting, SETUP_EXPIRES_KEY)
    if stored is None or expires is None or utcnow().isoformat() > expires.value:
        return False
    return secrets.compare_digest(_sha256(token), stored.value)
