import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest
from conftest import SAMPLE, add_mailbox

from sqlalchemy.exc import OperationalError

from mail_backup import app_settings, crypto, dovecot, jobs
from mail_backup.config import get_settings
from mail_backup.db import new_session
from mail_backup.imapsync.command import Endpoint, Job, UnsafeCommandError, build_command
from mail_backup.imapsync.parser import OutputParser, status_from_exit
from mail_backup.imapsync.runner import run_imapsync
from mail_backup.jobs import execute_run
from mail_backup.models import Mailbox, Run, RunKind, RunStatus
from mail_backup.providers import Security

def ep(login="u@x.ru", password="secret"):
    return Endpoint("imap.x.ru", 993, Security.SSL, login, password)


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_backup_command_protects_source_and_mirror(tmp_path):
    job = Job(RunKind.BACKUP, ep(), Endpoint("127.0.0.1", 143, Security.PLAIN, "u@x.ru", "l"))
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path / "p1", passfile2=tmp_path / "p2", tmpdir=tmp_path)
    assert "--noexpunge1" in cmd
    # По умолчанию зеркало только накапливает: удалённое на хостере из бэкапа не удаляется
    assert not {"--delete2", "--delete2folders"} & set(cmd)
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


def test_db_locked_while_saving_progress_does_not_fail_run(fake_imapsync, monkeypatch):
    """«database is locked» при записи процентов не обрывает бэкап: прогресс допишется позже."""
    monkeypatch.setattr(jobs, "PROGRESS_SAVE_INTERVAL", 0)
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()

        real_commit, failed = session.commit, []

        def flaky_commit():
            # Первая запись прогресса натыкается на чужую блокировку
            if run.progress_done is not None and not failed:
                failed.append(run.progress_done)
                raise OperationalError("UPDATE runs", {}, sqlite3.OperationalError("database is locked"))
            real_commit()

        monkeypatch.setattr(session, "commit", flaky_commit)
        run = execute_run(session, run.id)

        assert failed == [1]
        assert run.status == RunStatus.SUCCESS, run.error_message
        assert (run.progress_done, run.progress_total) == (3, 3)


def test_runner_kills_imapsync_when_worker_fails(tmp_path):
    """Сбой в воркере во время работы imapsync не оставляет процесс без присмотра."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "imapsync"
    script.write_text(f"""#!{sys.executable}
import os, time
open({str(pidfile)!r}, "w").write(str(os.getpid()))
print("msg INBOX/1 {{1}} copied to INBOX/1 ETA: Wed Oct 14 18:00:02 2026  2 s  2/3 msgs left", flush=True)
time.sleep(60)
""")
    script.chmod(0o755)
    job = Job(RunKind.BACKUP, ep(), Endpoint("127.0.0.1", 143, Security.PLAIN, "u@x.ru", "l"))

    def broken_progress(_p):
        raise RuntimeError("БД недоступна")

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        run_imapsync(job, imapsync_bin=str(script), work_dir=tmp_path / "work",
                     log_path=tmp_path / "run.log", on_progress=broken_progress)
    assert time.monotonic() - started < 20
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert not (tmp_path / "work").exists()


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


def test_prod_refuses_backup_without_mounted_disk(fake_imapsync, monkeypatch):
    monkeypatch.setenv("MB_DEPLOY_MODE", "prod")
    get_settings.cache_clear()
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run = execute_run(session, run.id)
        assert run.status == RunStatus.FAILED
        assert "не смонтирован" in run.error_message
    # imapsync даже не запускался
    assert not fake_imapsync.exists()


def test_parser_reads_folder_sizes():
    parser = OutputParser()
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        parser.feed(line)
    assert [(f.name, f.size, f.messages, f.biggest) for f in parser.stats.folders] == [
        ("INBOX", 3300, 2, 2100),
        ("Отправленные", 2000, 1, 2000),
    ]


def test_estimate_command_copies_nothing(tmp_path):
    job = Job(RunKind.ESTIMATE, ep(), Endpoint("127.0.0.1", 143, Security.PLAIN, "u@x.ru", "l"))
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path / "p1", passfile2=tmp_path / "p2", tmpdir=tmp_path)
    assert "--justfoldersizes" in cmd and "--noexpunge1" in cmd
    assert not {"--delete2", "--delete2folders", "--automap"} & set(cmd)


def test_execute_estimate_run_saves_folders(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = Run(kind=RunKind.ESTIMATE, mailbox_id=box.id)
        session.add(run)
        session.commit()
        run = execute_run(session, run.id)
        assert run.status == RunStatus.SUCCESS
        assert (run.host1_messages, run.host1_bytes) == (3, 5300)
        assert json.loads(run.folder_stats)[1]["name"] == "Отправленные"
        assert "--justfoldersizes" in json.loads(fake_imapsync.read_text())["args"]


def test_transfer_subfolder2_replaces_automap(tmp_path):
    job = Job(RunKind.RESTORE, ep(), ep("to@x.ru"), subfolder2="Restored-2026-10-14")
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)
    assert opt(cmd, "--subfolder2") == "Restored-2026-10-14"
    assert "--automap" not in cmd and "--delete2" not in cmd

    job = Job(RunKind.MIGRATE, ep(), ep("to@x.ru"))
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)
    assert "--automap" in cmd and "--subfolder2" not in cmd


@pytest.mark.parametrize("bad", ["Восстановлено", "a/b", "a.b", "", " lead"])
def test_subfolder2_name_checked(tmp_path, bad):
    job = Job(RunKind.RESTORE, ep(), ep("to@x.ru"), subfolder2=bad)
    if bad == "":
        # Пустое имя — обычная раскладка по папкам
        cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)
        assert "--automap" in cmd
        return
    with pytest.raises(ValueError):
        build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path)


def test_parser_counts_dry_copies():
    parser = OutputParser()
    lines = [
        "msg INBOX/1 copying to INBOX \t(not really since --dry mode) ETA: Wed Oct 14 18:00:02 2026  2 s  1/2 msgs left",
        "msg INBOX/2 copying to INBOX \t(not really since --dry mode) ETA: Wed Oct 14 18:00:02 2026  0 s  0/2 msgs left",
        "Messages transferred                    : 0 ",
    ]
    progress = [parser.feed(line) for line in lines]
    assert parser.stats.messages_planned == 2 and parser.stats.messages_transferred == 0
    # Прогресс из тех же строк по-прежнему разбирается
    assert progress[1].done == 2


def _transfer_run(session, box, **fields):
    run = Run(
        kind=RunKind.RESTORE, mailbox_id=box.id, target_host="imap.mail.ru", target_port=993,
        target_security=Security.SSL, target_login="to@mail.ru",
        target_password_enc=crypto.encrypt("пароль-получателя"), **fields,
    )
    session.add(run)
    session.commit()
    return run


def test_execute_restore_reads_backup_and_forgets_target_password(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = execute_run(session, _transfer_run(session, box, target_subfolder="Restored").id)
        assert run.status == RunStatus.SUCCESS, run.error_message
        record = json.loads(fake_imapsync.read_text())
        # Источник — учётка ящика в локальном Dovecot, получатель — указанный ящик
        assert opt(record["args"], "--host1") == get_settings().local_imap_host
        assert opt(record["args"], "--user1") == box.local_login
        assert record["pass2"] == "пароль-получателя\n"
        assert opt(record["args"], "--subfolder2") == "Restored"
        assert run.target_password_enc is None

        dry = execute_run(session, _transfer_run(session, box, dry_run=True).id)
        assert "--dry" in json.loads(fake_imapsync.read_text())["args"]
        # После пробного пароль нужен для «Запустить по-настоящему»
        assert dry.target_password_enc is not None and dry.messages_planned == 0


def test_restore_into_local_dovecot_refused(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        run = _transfer_run(session, box)
        run.target_host = "127.0.0.1"
        session.commit()
        run = execute_run(session, run.id)
        assert run.status == RunStatus.FAILED
        assert "локальным хранилищем" in run.error_message
        assert not fake_imapsync.exists()


def test_forget_target_passwords():
    from datetime import timedelta

    from mail_backup.jobs import forget_target_passwords
    from mail_backup.models import utcnow

    with new_session() as session:
        box = add_mailbox(session)
        old = _transfer_run(session, box, dry_run=True, status=RunStatus.SUCCESS,
                            finished_at=utcnow() - timedelta(hours=25))
        fresh = _transfer_run(session, box, dry_run=True, status=RunStatus.SUCCESS, finished_at=utcnow())
        assert forget_target_passwords(session) == 1
        session.refresh(old)
        session.refresh(fresh)
        assert old.target_password_enc is None and fresh.target_password_enc is not None


def test_dovecot_view_account_read_only_and_networks():
    """Учётки imapsync — только с 127.0.0.1; учётка просмотра — на Maildir ящика, группа ACL «только чтение»."""
    with new_session() as session:
        box = add_mailbox(session)
        dovecot.sync_users(session)
        users = get_settings().dovecot_users_file.read_text().splitlines()
        assert users == [users[0]] and users[0].endswith("::::::allow_nets=127.0.0.1/32")

        app_settings.save(session, app_settings.AppSettings(view_allowed_nets="10.8.0.0/24"))
        box.view_password_enc = dovecot.new_view_password()
        session.commit()
        dovecot.sync_users(session)
        view = get_settings().dovecot_users_file.read_text().splitlines()[1]
        fields = view.split(":")
        # user:password:uid:gid:gecos:home:shell:extra
        assert fields[0] == "_view.user@yandex.ru" and len(fields) == 8
        assert fields[5] == str(get_settings().maildir_root / "user@yandex.ru")
        assert fields[7] == "allow_nets=127.0.0.1/32,10.8.0.0/24 userdb_acl_groups=mb-view"


def test_parse_nets():
    assert app_settings.parse_nets("10.8.0.1/24; 192.168.1.5 10.8.0.0/24") == ["10.8.0.0/24", "192.168.1.5/32"]
    assert app_settings.parse_nets("  ") == []
    for bad in ("10.8.0.300", "fd00::/64", "vpn"):
        with pytest.raises(ValueError):
            app_settings.parse_nets(bad)


def test_backup_cache_command(tmp_path):
    cache = tmp_path / "cache"
    job = Job(RunKind.BACKUP, ep(), ep(), cache_dir=cache)
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path / "p1", passfile2=tmp_path / "p2",
                        tmpdir=tmp_path / "run")
    # Кэш imapsync лежит в --tmpdir — постоянный каталог ящика; pid — во временном каталоге запуска
    assert "--usecache" in cmd and opt(cmd, "--tmpdir") == str(cache)
    assert opt(cmd, "--pidfile") == str(tmp_path / "run" / "imapsync.pid")
    # --useuid без заполненного кэша скопировал бы весь ящик заново
    assert "--useuid" not in cmd

    job = Job(RunKind.BACKUP, ep(), ep())
    cmd = build_command(job, imapsync_bin="imapsync", passfile1=tmp_path, passfile2=tmp_path, tmpdir=tmp_path / "run")
    assert "--usecache" not in cmd and opt(cmd, "--tmpdir") == str(tmp_path / "run")


def test_backup_run_keeps_cache_between_runs(fake_imapsync):
    with new_session() as session:
        box = add_mailbox(session)
        cache = jobs.mailbox_cache_dir(box)
        run = Run(kind=RunKind.BACKUP, mailbox_id=box.id)
        session.add(run)
        session.commit()
        assert execute_run(session, run.id).status == RunStatus.SUCCESS
        args = json.loads(fake_imapsync.read_text())["args"]
        assert "--usecache" in args and opt(args, "--tmpdir") == str(cache)
        # Временный каталог запуска удалён, кэш остался для следующего бэкапа
        assert cache.is_dir() and not list(get_settings().tmp_dir.glob("run_*"))

        # Оценке кэш не нужен
        estimate = Run(kind=RunKind.ESTIMATE, mailbox_id=box.id)
        session.add(estimate)
        session.commit()
        execute_run(session, estimate.id)
        assert "--usecache" not in json.loads(fake_imapsync.read_text())["args"]

        assert jobs.reset_cache(box) and not cache.exists()
        assert not jobs.reset_cache(box)


def test_parser_counts_copies_and_hides_eta_of_incremental_backup():
    parser = OutputParser()
    # Перед каждой папкой imapsync печатает ETA; на повторном бэкапе почти всё пропускается
    p = parser.feed("ETA: Wed Oct 14 18:00:02 2026  0 s  6000/6000 msgs left")
    assert (p.done, p.copied, p.eta_seconds) == (0, 0, None)
    p = parser.feed("msg INBOX/9 {300}   copied to INBOX/2   0.03 msgs/s  1 KiB/s 300 B copied "
                    "ETA: Wed Oct 21 09:22:52 2026  449347 s  100/6000 msgs left")
    # Прогноз «по скопированным» бессмыслен, когда 5899 из 5900 писем были сверены и пропущены
    assert (p.done, p.copied, p.copied_bytes, p.eta_seconds) == (5900, 1, 300, None)

    # Первый бэкап: копируется почти всё — прогноз imapsync показываем
    parser = OutputParser()
    progress = [p for line in SAMPLE.read_text(encoding="utf-8").splitlines() if (p := parser.feed(line))]
    assert (progress[0].eta_seconds, progress[-1].copied, progress[-1].copied_bytes) == (2, 3, 5300)
