"""Общие фикстуры: каждое тестовое окружение живёт во временном каталоге."""

import json
import sys
from pathlib import Path

import pytest

from mail_backup import config, crypto, db, dovecot, imapcheck
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
    # restic в системе может быть, а может и нет: по умолчанию тесты считают, что его нет
    monkeypatch.setenv("MB_RESTIC_BIN", str(tmp_path / "no-restic"))
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


@pytest.fixture(autouse=True)
def host_messages(monkeypatch):
    """Счёт писем на хостере для защиты от проседания — без настоящего IMAP.

    Словарь можно менять в тесте: count — число, error — текст ошибки ImapError.
    """
    state = {"count": 3, "error": None, "calls": 0}

    def fake(*args, **kwargs):
        state["calls"] += 1
        if state["error"]:
            raise imapcheck.ImapError(state["error"])
        return state["count"]

    monkeypatch.setattr(imapcheck, "count_messages", fake)
    return state


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




# Фальшивый restic: пишет аргументы каждого вызова, хранит список снимков в JSON
# и отвечает на подкоманды так же по форме, как restic 0.18 (проверено вживую)
FAKE_RESTIC = f"""#!{sys.executable}
import json, os, pathlib, sys
from datetime import datetime, timezone
args = sys.argv[1:]
with open(os.environ["FAKE_RESTIC_RECORD"], "a") as f:
    f.write(json.dumps(args) + "\\n")
repo = args[args.index("-r") + 1] if "-r" in args else ""
rest, skip = [], False
for a in args:
    if skip:
        skip = False
    elif a in ("-r", "-o"):
        skip = True
    else:
        rest.append(a)
cmd = rest[0]
fail = os.environ.get("FAKE_RESTIC_FAIL", "")
if fail.startswith(cmd + ":"):
    print("Fatal: сбой " + cmd, file=sys.stderr)
    sys.exit(int(fail.split(":")[1]))
state = pathlib.Path(os.environ["FAKE_RESTIC_STATE"])
snaps = json.loads(state.read_text()) if state.exists() else []
def opt(name):
    return rest[rest.index(name) + 1] if name in rest else None
if cmd == "version":
    print("restic 0.18.1 compiled with go1.25.1 on linux/amd64")
elif cmd == "init":
    if not repo.startswith("sftp:"):
        os.makedirs(repo, exist_ok=True)
        open(os.path.join(repo, "config"), "w").close()
elif cmd == "cat":
    exists = os.environ.get("FAKE_REMOTE_EXISTS") if repo.startswith("sftp:") else os.path.exists(os.path.join(repo, "config"))
    if not exists:
        print("Fatal: repository does not exist", file=sys.stderr)
        sys.exit(10)
elif cmd == "backup":
    snap_id = f"{{len(snaps) + 1:064x}}"
    snaps.append({{"id": snap_id, "short_id": snap_id[:8], "time": datetime.now(timezone.utc).isoformat(),
                   "paths": [rest[1]], "tags": [opt("--tag")], "summary": {{"total_bytes_processed": 1234}}}})
    state.write_text(json.dumps(snaps))
    print(json.dumps({{"message_type": "summary", "data_added_packed": 999, "snapshot_id": snap_id}}))
elif cmd == "snapshots":
    tag = opt("--tag")
    ids = [a for a in rest[1:] if not a.startswith("-") and a != tag]
    print(json.dumps([s for s in snaps if (not tag or tag in s["tags"]) and (not ids or s["id"] in ids)]))
elif cmd == "restore":
    target = pathlib.Path(opt("--target")) / "Maildir" / "cur"
    target.mkdir(parents=True)
    (target / "1:2,S").write_text("письмо из снимка")
else:
    print(cmd + ": ok")
"""


@pytest.fixture
def fake_restic(tmp_path, monkeypatch):
    """restic-заглушка; возвращает функцию, отдающую список вызовов (аргументов)."""
    script = tmp_path / "restic"
    script.write_text(FAKE_RESTIC)
    script.chmod(0o755)
    record = tmp_path / "restic_calls.jsonl"
    monkeypatch.setenv("MB_RESTIC_BIN", str(script))
    monkeypatch.setenv("FAKE_RESTIC_RECORD", str(record))
    monkeypatch.setenv("FAKE_RESTIC_STATE", str(tmp_path / "restic_state.json"))
    config.get_settings.cache_clear()

    def calls() -> list[list[str]]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    return calls
