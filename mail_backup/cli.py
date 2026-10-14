"""Командная строка: mail-backup <команда>."""

import argparse
import getpass
import os
import shutil
import subprocess
import sys

from sqlalchemy import select

from . import auth, crypto, dovecot, restic
from .config import get_settings
from .crypto import ensure_keys
from .db import create_tables, new_session
from .jobs import check_backup_storage, execute_run
from .models import Mailbox, Run, RunKind, RunTrigger
from .providers import PROVIDERS, Security, get_provider


def cmd_init(_args: argparse.Namespace) -> None:
    """Создаёт каталоги, ключи и таблицы БД. Повторный запуск безопасен."""
    settings = get_settings()
    for path in (
        settings.data_dir,
        settings.logs_dir,
        settings.tmp_dir,
        settings.maildir_root,
        settings.dovecot_users_file.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    settings.ssh_dir.mkdir(mode=0o700, exist_ok=True)
    ensure_keys()
    create_tables()
    print(f"БД: {settings.db_path}")
    print(f"Хранилище бэкапов: {settings.maildir_root}")
    _init_restic()


def _init_restic() -> None:
    """Локальный репозиторий restic на диске бэкапов (если restic установлен)."""
    ver = restic.version()
    if ver is None:
        print("restic не установлен — снимки работать не будут")
        return
    if not restic.version_ok(ver):
        print(f"restic {ver} слишком старый, нужен ≥ {'.'.join(map(str, restic.MIN_VERSION))}")
        return
    check_backup_storage()
    created = restic.init_local()
    print(f"Репозиторий restic: {get_settings().restic_repo}" + (" (создан)" if created else ""))


def cmd_setup_link(args: argparse.Namespace) -> None:
    """Одноразовая ссылка для создания администратора в веб-панели."""
    with new_session() as session:
        if auth.admin_exists(session):
            if args.if_no_admin:
                return
            sys.exit("Администратор уже создан. Сброс пароля: mail-backup set-admin <логин>")
        token = auth.create_setup_token(session)
    print(f"{args.base_url.rstrip('/')}/setup?token={token}")


def cmd_worker(_args: argparse.Namespace) -> None:
    from .worker import main as worker_main

    worker_main()


def cmd_set_admin(args: argparse.Namespace) -> None:
    password = getpass.getpass("Новый пароль админа: ")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    with new_session() as session:
        try:
            auth.set_admin(session, args.login, password)
        except ValueError as exc:
            sys.exit(str(exc))
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
    ver = restic.version()
    state = "репозиторий готов" if restic.ready() else "репозиторий не создан — mail-backup init"
    print(f"restic:             {ver or 'не установлен'}" + (f", {state}" if ver else ""))


def cmd_mailbox_add(args: argparse.Namespace) -> None:
    """Добавляет ящик (временная замена формы в админке)."""
    preset = get_provider(args.provider)
    host = args.host or preset.host
    if not host:
        sys.exit("Для провайдера custom нужно указать --host")
    password = getpass.getpass(f"Пароль (пароль приложения) для {args.login}: ")
    with new_session() as session:
        local_login, local_password_enc = dovecot.new_local_credentials(session, args.login)
        box = Mailbox(
            name=args.name or args.login,
            provider=preset.key,
            host=host,
            port=args.port or preset.port,
            security=Security(args.security or preset.security),
            login=args.login,
            password_enc=crypto.encrypt(password),
            local_login=local_login,
            local_password_enc=local_password_enc,
        )
        session.add(box)
        session.commit()
        dovecot.sync_users(session)
        print(f"Ящик #{box.id} добавлен ({box.host}:{box.port}, {box.security})")
        if preset.note:
            print(f"Подсказка: {preset.note}")


def cmd_mailbox_list(_args: argparse.Namespace) -> None:
    with new_session() as session:
        for box in session.scalars(select(Mailbox).order_by(Mailbox.id)):
            state = "вкл" if box.enabled else "выкл"
            print(f"#{box.id:<3} {box.login:<35} {box.host}:{box.port} [{state}]")


def cmd_backup(args: argparse.Namespace) -> None:
    """Запускает бэкап ящика синхронно и печатает итог."""
    with new_session() as session:
        box = session.get(Mailbox, args.mailbox_id)
        if box is None:
            sys.exit(f"Ящик #{args.mailbox_id} не найден")
        kind = RunKind.ESTIMATE if args.estimate else RunKind.BACKUP
        run = Run(kind=kind, mailbox_id=box.id, trigger=RunTrigger.MANUAL, dry_run=args.dry)
        session.add(run)
        session.commit()
        print(f"Запуск #{run.id}: {kind} {box.login}, лог пишется в каталог {get_settings().logs_dir}")
        run = execute_run(session, run.id, just_login=args.check)
        print(f"Статус: {run.status} (код выхода {run.exit_code})")
        if run.error_message:
            print(f"Ошибка: {run.error_message}")
        print(f"Лог: {run.log_path}")
        if args.estimate:
            print(f"В ящике: {run.host1_messages} писем, {run.host1_bytes} байт")
        elif not args.check:
            print(
                f"Писем перенесено: {run.messages_transferred}, пропущено: {run.messages_skipped}, "
                f"байт: {run.bytes_transferred}, ошибок: {run.errors_count}"
            )


def cmd_restic(args: argparse.Namespace) -> None:
    """restic с локальным репозиторием и паролем приложения — для диагностики."""
    binary = restic.binary()
    if binary is None:
        sys.exit("restic не установлен")
    os.environ.update(restic.env())
    os.execv(binary, [binary, *restic.local_repo().args(), *args.restic_args])


def cmd_dovecot_sync(_args: argparse.Namespace) -> None:
    with new_session() as session:
        count = dovecot.sync_users(session)
    print(f"Учёток в {get_settings().dovecot_users_file}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mail-backup")
    sub = parser.add_subparsers(required=True)

    sub.add_parser("init", help="создать каталоги, ключи и БД").set_defaults(func=cmd_init)

    p = sub.add_parser("setup-link", help="ссылка для создания администратора в веб-панели")
    p.add_argument("--base-url", default="https://<адрес-сервера>", help="адрес панели")
    p.add_argument("--if-no-admin", action="store_true", help="молча выйти, если админ уже есть")
    p.set_defaults(func=cmd_setup_link)

    sub.add_parser("worker", help="воркер: очередь, расписание (запускает systemd)").set_defaults(func=cmd_worker)

    p = sub.add_parser("set-admin", help="сбросить логин и пароль администратора")
    p.add_argument("login")
    p.set_defaults(func=cmd_set_admin)

    sub.add_parser("check", help="проверить настройки и imapsync").set_defaults(func=cmd_check)

    p = sub.add_parser("mailbox-add", help="добавить почтовый ящик")
    p.add_argument("login", help="адрес / логин ящика")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="custom")
    p.add_argument("--name", help="отображаемое имя")
    p.add_argument("--host", help="IMAP-сервер (по умолчанию из пресета)")
    p.add_argument("--port", type=int)
    p.add_argument("--security", choices=[s.value for s in Security])
    p.set_defaults(func=cmd_mailbox_add)

    sub.add_parser("mailbox-list", help="список ящиков").set_defaults(func=cmd_mailbox_list)

    p = sub.add_parser("backup", help="запустить бэкап ящика сейчас")
    p.add_argument("mailbox_id", type=int)
    p.add_argument("--dry", action="store_true", help="пробный прогон без копирования")
    p.add_argument("--check", action="store_true", help="только проверить вход на обе стороны")
    p.add_argument("--estimate", action="store_true", help="только оценить объём (ничего не копировать)")
    p.set_defaults(func=cmd_backup)

    sub.add_parser("dovecot-sync", help="пересобрать файл учёток Dovecot").set_defaults(func=cmd_dovecot_sync)

    p = sub.add_parser("restic", help="restic с локальным репозиторием (например: restic snapshots)")
    p.add_argument("restic_args", nargs=argparse.REMAINDER, help="аргументы restic")
    p.set_defaults(func=cmd_restic)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
