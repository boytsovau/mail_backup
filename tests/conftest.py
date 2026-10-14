"""Общие фикстуры: каждое тестовое окружение живёт во временном каталоге."""

import sys
from pathlib import Path

import pytest

from mail_backup import config, crypto, db, dovecot
from mail_backup.models import Mailbox

SAMPLE = Path(__file__).parent / "data" / "imapsync_output.txt"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Ни серверный, ни локальный .env не должны влиять на тесты
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    monkeypatch.setenv("MB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MB_BACKUP_ROOT", str(tmp_path / "backup"))
    monkeypatch.setenv("MB_SECRETS_DIR", str(tmp_path / "secrets"))
    caches = (config.get_settings, crypto._fernet, db.get_engine, db._session_factory)
    for cached in caches:
        cached.cache_clear()
    (tmp_path / "data").mkdir()
    crypto.ensure_keys()
    db.create_tables()
    yield
    db.get_engine().dispose()
    for cached in caches:
        cached.cache_clear()


# Фальшивый imapsync: печатает образец вывода и сохраняет аргументы и пароли для проверки
FAKE_IMAPSYNC = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]
record = {{
    "args": args,
    "pass1": open(opt("--passfile1")).read(),
    "pass2": open(opt("--passfile2")).read(),
    "pass1_mode": oct(os.stat(opt("--passfile1")).st_mode & 0o777),
}}
json.dump(record, open(os.environ["FAKE_RECORD"], "w"))
# Режим «долгой работы» для проверки отмены: SIGTERM завершает процесс
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(open(os.environ["FAKE_OUTPUT"], encoding="utf-8").read())
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture
def fake_imapsync(tmp_path, monkeypatch):
    script = tmp_path / "imapsync"
    script.write_text(FAKE_IMAPSYNC)
    script.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("MB_IMAPSYNC_BIN", str(script))
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_OUTPUT", str(SAMPLE))
    config.get_settings.cache_clear()
    return record


def add_mailbox(session) -> Mailbox:
    local_login, local_password_enc = dovecot.new_local_credentials(session, "User@Yandex.ru")
    box = Mailbox(
        name="Тест",
        provider="yandex",
        host="imap.yandex.ru",
        login="user@yandex.ru",
        password_enc=crypto.encrypt("пароль-хостера"),
        local_login=local_login,
        local_password_enc=local_password_enc,
    )
    session.add(box)
    session.commit()
    return box


