"""Учётная запись единственного администратора.

Логин и argon2-хеш пароля хранятся в таблице settings.
"""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy.orm import Session

from .models import Setting

ADMIN_LOGIN_KEY = "admin_login"
ADMIN_HASH_KEY = "admin_password_hash"

_hasher = PasswordHasher()


def set_admin(session: Session, login: str, password: str) -> None:
    session.merge(Setting(key=ADMIN_LOGIN_KEY, value=login))
    session.merge(Setting(key=ADMIN_HASH_KEY, value=_hasher.hash(password)))
    session.commit()


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
