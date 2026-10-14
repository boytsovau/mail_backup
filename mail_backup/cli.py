"""Командная строка: mail-backup <команда>."""

import argparse
import getpass
import shutil
import subprocess
import sys

from . import auth
from .config import get_settings
from .crypto import ensure_keys
from .db import create_tables, new_session


def cmd_init(_args: argparse.Namespace) -> None:
    """Создаёт каталоги, ключи и таблицы БД. Повторный запуск безопасен."""
    settings = get_settings()
    for path in (settings.data_dir, settings.logs_dir, settings.tmp_dir, settings.maildir_root):
        path.mkdir(parents=True, exist_ok=True)
    ensure_keys()
    create_tables()
    print(f"БД: {settings.db_path}")
    print(f"Хранилище бэкапов: {settings.maildir_root}")


def cmd_set_admin(args: argparse.Namespace) -> None:
    password = getpass.getpass("Новый пароль админа: ")
    if len(password) < 10:
        sys.exit("Пароль должен быть не короче 10 символов")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    with new_session() as session:
        auth.set_admin(session, args.login, password)
    print(f"Админ «{args.login}» сохранён")


def cmd_check(_args: argparse.Namespace) -> None:
    """Показывает настройки и проверяет наличие imapsync."""
    settings = get_settings()
    print(f"Каталог данных:     {settings.data_dir}")
    print(f"Хранилище бэкапов:  {settings.backup_root}")
    print(f"Локальный Dovecot:  {settings.local_imap_host}:{settings.local_imap_port}")
    binary = shutil.which(settings.imapsync_bin)
    if binary is None:
        sys.exit(f"imapsync не найден: {settings.imapsync_bin}")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"imapsync:           {binary} (версия {version})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mail-backup")
    sub = parser.add_subparsers(required=True)

    sub.add_parser("init", help="создать каталоги, ключи и БД").set_defaults(func=cmd_init)

    p = sub.add_parser("set-admin", help="задать логин и пароль администратора")
    p.add_argument("login")
    p.set_defaults(func=cmd_set_admin)

    sub.add_parser("check", help="проверить настройки и imapsync").set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
