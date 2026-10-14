import json
import os
import stat
import sys
from pathlib import Path

import pytest

from mail_backup import crypto, dovecot
from mail_backup.config import get_settings
from mail_backup.db import new_session
from mail_backup.imapsync.command import Endpoint, Job, UnsafeCommandError, build_command
from mail_backup.imapsync.parser import OutputParser, status_from_exit
from mail_backup.jobs import execute_run
from mail_backup.models import Mailbox, Run, RunKind, RunStatus
from mail_backup.providers import Security

SAMPLE = Path(__file__).parent / "data" / "imapsync_output.txt"

# Фальшивый imapsync: печатает образец вывода и сохраняет аргументы и пароли для проверки
FAKE_IMAPSYNC = f"""#!{sys.executable}
import json, os, sys
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
sys.stdout.write(open(os.environ["FAKE_OUTPUT"], encoding="utf-8").read())
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def ep(login="u@x.ru", password="secret"):
    return Endpoint("imap.x.ru", 993, Security.SSL, login, password)


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_backup_command_mirrors_and_protects_source(tmp_path):
    job = Job(RunKind.BACKUP, ep(), Endpoint("127.0.0.1", 143, Security.PLAIN, "u@x.ru", "l"))
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path / "p1", passfile2=tmp_path / "p2", tmpdir=tmp_path)
    assert "--noexpunge1" in cmd
    assert {"--delete2", "--delete2folders"} <= set(cmd)
    assert "--ssl1" in cmd and {"--nossl2", "--notls2"} <= set(cmd)
    # Пароль никогда не попадает в командную строку
    assert "secret" not in cmd and "l" not in cmd
    assert opt(cmd, "--passfile1") == str(tmp_path / "p1")


def test_restore_never_deletes_on_target(tmp_path):
    job = Job(RunKind.RESTORE, ep(), ep(), extra_args=["--delete2"])
    with pytest.raises(UnsafeCommandError):
        build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)


@pytest.mark.parametrize("bad", ["--delete1", "--expunge1", "--delete1emptyfolders"])
def test_source_modifying_flags_rejected(tmp_path, bad):
    job = Job(RunKind.BACKUP, ep(), ep(), extra_args=[bad])
    with pytest.raises(UnsafeCommandError):
        build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)


def test_parser_reads_progress_and_final_stats():
    parser = OutputParser()
    progress = [p for line in SAMPLE.read_text(encoding="utf-8").splitlines() if (p := parser.feed(line))]
    assert [(p.done, p.msgs_total) for p in progress] == [(1, 3), (2, 3), (3, 3)]
    s = parser.stats
    assert (s.messages_transferred, s.messages_skipped, s.errors_count) == (3, 0, 0)
    assert (s.bytes_transferred, s.folders_synced, s.duration_seconds) == (5300, 2, 2.4)
    # Размеры ящиков берутся из итогового блока, а не из стартового
    assert (s.host1_messages, s.host2_messages, s.host2_bytes) == (3, 3, 5300)


def test_exit_codes():
    assert status_from_exit(0) == RunStatus.SUCCESS
    assert status_from_exit(111) == RunStatus.WARNING
    assert status_from_exit(16) == RunStatus.FAILED
    assert status_from_exit(-15) == RunStatus.FAILED
    assert status_from_exit(-15, cancelled=True) == RunStatus.CANCELLED


@pytest.fixture
def fake_imapsync(tmp_path, monkeypatch):
    script = tmp_path / "imapsync"
    script.write_text(FAKE_IMAPSYNC)
    script.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("MB_IMAPSYNC_BIN", str(script))
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_OUTPUT", str(SAMPLE))
    get_settings.cache_clear()
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


def test_execute_backup_run(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()

        run = execute_run(session, run.id)

        assert run.status == RunStatus.SUCCESS, run.error_message
        assert (run.messages_transferred, run.bytes_transferred, run.host1_messages) == (3, 5300, 3)
        assert (run.progress_done, run.progress_total) == (3, 3)
        assert run.finished_at is not None and run.pid is None

        record = json.loads(fake_imapsync.read_text())
        assert record["pass1"] == "пароль-хостера\n"
        assert record["pass2"] == crypto.decrypt(box.local_password_enc) + "\n"
        assert record["pass1_mode"] == "0o600"
        assert opt(record["args"], "--user2") == "user@yandex.ru"

        # Файлы паролей удалены, лог на месте, учётка Dovecot создана
        assert not list(get_settings().tmp_dir.glob("run_*"))
        assert "Detected 0 errors" in Path(run.log_path).read_text(encoding="utf-8")
        users = get_settings().dovecot_users_file.read_text()
        assert users.startswith("user@yandex.ru:{SSHA512}")


def test_execute_run_with_message_errors(fake_imapsync, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "111")
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        assert execute_run(session, run.id).status == RunStatus.WARNING


def test_restore_without_target_fails_cleanly(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.RESTORE, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run = execute_run(session, run.id)
        assert run.status == RunStatus.FAILED
        assert "получатель" in run.error_message


def test_local_login_unique_and_validated():
    with new_session() as session:
        add_mailbox(session)
        login, _ = dovecot.new_local_credentials(session, "user@yandex.ru")
        assert login == "user@yandex.ru.2"
        with pytest.raises(ValueError):
            dovecot.new_local_credentials(session, "evil:user")


def test_dovecot_users_file_permissions():
    with new_session() as session:
        add_mailbox(session)
        dovecot.sync_users(session)
    path = get_settings().dovecot_users_file
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
