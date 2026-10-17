import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import add_mailbox

from mail_backup import app_settings, config, crypto, restic, restic_tasks
from mail_backup.config import get_settings
from mail_backup.db import new_session
from mail_backup.imapsync.command import Endpoint, Job, UnsafeCommandError, build_command
from mail_backup.jobs import execute_run
from mail_backup.models import (
    ResticRemote, ResticTask, ResticTaskKind, Run, RunKind, RunStatus, RunTrigger, utcnow,
)
from mail_backup.providers import Security
from mail_backup.web.dashboard import Health, backup_health
from mail_backup.worker import Worker

MSK = ZoneInfo("Europe/Moscow")


def imapsync_args(record: Path) -> list[str]:
    return json.loads(record.read_text())["args"]


def opt(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def subcommands(calls) -> list[str]:
    """Подкоманды restic по порядку вызова (без глобальных -r/-o)."""
    result = []
    for args in calls():
        rest = [a for i, a in enumerate(args) if a not in ("-r", "-o") and (i == 0 or args[i - 1] not in ("-r", "-o"))]
        result.append(rest[0])
    return result


def new_run(session, box, **fields) -> Run:
    run = Run(kind=fields.pop("kind", RunKind.BACKUP), mailbox_id=box.id, **fields)
    session.add(run)
    session.commit()
    return run


@pytest.fixture
def restic_on(fake_restic):
    """restic «установлен», локальный репозиторий создан, снимки включены (по умолчанию)."""
    restic.init_local()
    assert restic.ready()
    return fake_restic


def test_mirror_deletions_flags(tmp_path):
    ep = Endpoint("h", 993, Security.SSL, "u", "p")
    kw = dict(imapsync_bin="imapsync", passfile1=tmp_path / "1", passfile2=tmp_path / "2", tmpdir=tmp_path)
    # По умолчанию зеркало ничего не удаляет
    cmd = build_command(Job(RunKind.BACKUP, ep, ep), **kw)
    assert "--delete2" not in cmd and "--delete2folders" not in cmd and "--noexpunge1" in cmd
    assert {"--delete2", "--delete2folders"} <= set(build_command(Job(RunKind.BACKUP, ep, ep, mirror_deletions=True), **kw))
    # Прочие удаления на host2 запрещены и в бэкапе
    with pytest.raises(UnsafeCommandError):
        build_command(Job(RunKind.BACKUP, ep, ep, extra_args=["--expunge2"]), **kw)


def test_settings_bool_roundtrip():
    with new_session() as session:
        assert app_settings.load_restic(session).enabled is True
        app_settings.save_restic(session, app_settings.load_restic(session), enabled=False, schedule="04:30")
        prefs = app_settings.load_restic(session)
        assert (prefs.enabled, prefs.schedule) == (False, "04:30")


def test_backup_makes_snapshot_and_applies_retention(fake_imapsync, restic_on):
    with new_session() as session:
        box = add_mailbox(session)
        box.retention_days = 30
        run = execute_run(session, new_run(session, box).id)

        assert run.status == RunStatus.SUCCESS, run.error_message
        assert run.snapshot_id and run.snapshot_added == 999 and run.snapshot_error is None
        assert run.phase is None and run.pid is None
        # Снимки включены, но удаление у ящика по умолчанию выключено
        assert "--delete2" not in imapsync_args(fake_imapsync)

        calls = restic_on()
        backup = next(c for c in calls if "backup" in c)
        assert str(get_settings().maildir_root / box.local_login) in backup
        assert opt(backup, "--tag") == f"mailbox-{box.id}" and opt(backup, "--host") == restic.SNAPSHOT_HOST
        forget = next(c for c in calls if "forget" in c)
        assert opt(forget, "--keep-within") == "30d" and opt(forget, "--tag") == f"mailbox-{box.id}"
        # Пароль — только файлом в окружении, не в командной строке
        assert not any(get_settings().restic_password_file.read_text() in a for c in calls for a in c)


def deleting_mailbox(session):
    box = add_mailbox(session)
    box.mirror_deletions = True
    session.commit()
    return box


def test_mirror_deletions_with_history(fake_imapsync, restic_on):
    with new_session() as session:
        run = execute_run(session, new_run(session, deleting_mailbox(session)).id)
        assert run.status == RunStatus.SUCCESS and run.snapshot_id
    assert "--delete2" in imapsync_args(fake_imapsync)


def test_snapshots_off_keeps_deleted_mail(fake_imapsync, restic_on):
    with new_session() as session:
        app_settings.save_restic(session, app_settings.load_restic(session), enabled=False)
        # Даже ящик с удалением: без истории удалённое пропало бы насовсем
        run = execute_run(session, new_run(session, deleting_mailbox(session)).id)
        assert run.status == RunStatus.SUCCESS and run.snapshot_id is None
    assert "--delete2" not in imapsync_args(fake_imapsync)
    assert "backup" not in subcommands(restic_on)


def test_no_repository_means_no_deletions(fake_imapsync, fake_restic):
    # restic есть, но репозиторий не создан: снимков нет — и удалений в зеркале тоже
    with new_session() as session:
        run = execute_run(session, new_run(session, deleting_mailbox(session)).id)
        assert run.status == RunStatus.SUCCESS and run.snapshot_id is None
    assert "--delete2" not in imapsync_args(fake_imapsync)
    assert "backup" not in subcommands(fake_restic)


def test_snapshot_before_deleting_unsnapshotted_mirror(fake_imapsync, restic_on):
    with new_session() as session:
        box = add_mailbox(session)
        # В зеркале уже есть письма (накопились без снимков)
        maildir = get_settings().maildir_root / box.local_login / "Maildir" / "cur"
        maildir.mkdir(parents=True)
        (maildir / "1:2,S").write_text("старое письмо")
        # Без удалений снимок до бэкапа не нужен
        execute_run(session, new_run(session, box).id)
        assert subcommands(restic_on).count("backup") == 1

        # Удаление включили: прошлый бэкап со снимком — снова только снимок после
        box.mirror_deletions = True
        session.commit()
        execute_run(session, new_run(session, box).id)
        assert subcommands(restic_on).count("backup") == 2

        # Прошлый бэкап остался без снимка (снимки выключали) — сначала снимок, потом удаления
        app_settings.save_restic(session, app_settings.load_restic(session), enabled=False)
        execute_run(session, new_run(session, box).id)
        app_settings.save_restic(session, app_settings.load_restic(session), enabled=True)
        run = execute_run(session, new_run(session, box).id)
        assert run.status == RunStatus.SUCCESS and run.snapshot_id
        assert subcommands(restic_on).count("backup") == 4


def test_failed_first_snapshot_disables_deletions(fake_imapsync, restic_on, monkeypatch):
    monkeypatch.setenv("FAKE_RESTIC_FAIL", "backup:1")
    with new_session() as session:
        box = deleting_mailbox(session)
        (get_settings().maildir_root / box.local_login).mkdir(parents=True)
        run = execute_run(session, new_run(session, box).id)
        # Почта скопирована, но без удалений и со снимком-ошибкой
        assert run.status == RunStatus.SUCCESS
        assert "сбой backup" in run.snapshot_error and run.snapshot_id is None
        assert backup_health(box, run, run, utcnow(), snapshots=True) == Health.SNAPSHOT
    assert "--delete2" not in imapsync_args(fake_imapsync)


def test_dry_run_makes_no_snapshot(fake_imapsync, restic_on):
    with new_session() as session:
        run = execute_run(session, new_run(session, add_mailbox(session), dry_run=True).id)
        assert run.status == RunStatus.SUCCESS and run.snapshot_id is None
    assert "backup" not in subcommands(restic_on)
    assert "--dry" in imapsync_args(fake_imapsync)


def test_restore_from_snapshot_uses_temporary_account(fake_imapsync, restic_on):
    with new_session() as session:
        box = add_mailbox(session)
        backup = execute_run(session, new_run(session, box).id)
        snap = restic.snapshots(restic.local_repo(), box.id)[0]
        run = new_run(session, box, kind=RunKind.RESTORE, source_snapshot=backup.snapshot_id,
                      source_snapshot_time=snap.time, target_host="imap.mail.ru", target_port=993,
                      target_security="ssl", target_login="new@mail.ru",
                      target_password_enc=crypto.encrypt("пароль-получателя"))
        run = execute_run(session, run.id)

        assert run.status == RunStatus.SUCCESS, run.error_message
        args = imapsync_args(fake_imapsync)
        assert opt(args, "--user1") == run.temp_login == f"_restore_{run.id}"
        assert opt(args, "--host1") == "127.0.0.1" and opt(args, "--user2") == "new@mail.ru"
        # Распаковывается каталог ящика из снимка во временный Maildir
        restore = next(c for c in restic_on() if "restore" in c)
        assert f"{snap.id}:{snap.paths[0]}" in restore
        assert opt(restore, "--target") == str(get_settings().maildir_root / run.temp_login)
        # После запуска — ни распакованного снимка, ни временной учётки, ни их паролей
        assert not (get_settings().maildir_root / run.temp_login).exists()
        assert run.snapshot_password_enc is None and run.target_password_enc is None
        assert "_restore_" not in get_settings().dovecot_users_file.read_text()


def test_restore_from_missing_snapshot_fails(fake_imapsync, restic_on):
    with new_session() as session:
        box = add_mailbox(session)
        run = new_run(session, box, kind=RunKind.RESTORE, source_snapshot="f" * 64, target_host="imap.mail.ru",
                      target_login="new@mail.ru", target_password_enc=crypto.encrypt("x"))
        run = execute_run(session, run.id)
        assert run.status == RunStatus.FAILED and "не найден" in run.error_message
        assert not fake_imapsync.exists()


def test_temp_login_prefix_reserved():
    from mail_backup import dovecot

    with new_session() as session, pytest.raises(ValueError):
        dovecot.new_local_credentials(session, "_restore_5")


def add_remote(session, **fields) -> ResticRemote:
    remote = ResticRemote(name="Офсайт", host="backup.example.com", port=2222, user="mb", path="/srv/restic/mail",
                          **fields)
    session.add(remote)
    session.commit()
    return remote


def test_remote_url_and_ssh_args():
    remote = ResticRemote(host="backup.example.com", port=2222, user="mb", path="/srv/restic")
    assert remote.url == "sftp://mb@backup.example.com:2222//srv/restic"
    assert ResticRemote(host="h", port=22, user="u", path="rel/dir").url == "sftp://u@h:22/rel/dir"
    assert ResticRemote(host="2001:db8::1", port=22, user="u", path="r").url == "sftp://u@[2001:db8::1]:22/r"
    args = restic.remote_repo(remote).args()
    assert args[:2] == ["-r", remote.url] and args[2] == "-o"
    assert str(restic.ssh_key_path()) in args[3] and "BatchMode=yes" in args[3]
    assert "StrictHostKeyChecking=accept-new" in args[3]


@pytest.mark.parametrize(("code", "text", "expected"), [
    (10, "Fatal: repository does not exist: unable to open config file", True),  # restic ≥ 0.17
    (1, "Fatal: unable to open config file: stat /r/config: no such file or directory", True),  # 0.16, локально
    (1, "Fatal: unable to open config file: Lstat: file does not exist", True),  # 0.16, SFTP
    (1, "Fatal: unable to open config file: open /r/config: permission denied", False),
    (12, "Fatal: wrong password or no key found", False),
])
def test_missing_remote_repo_detected_in_any_restic_version(monkeypatch, code, text, expected):
    def fail(*args, **kwargs):
        raise restic.ResticError(restic._error_text(code, [text]), code)

    monkeypatch.setattr(restic, "run_short", fail)
    ok, message = restic.remote_status(restic.Repo("/r"))
    assert ok is expected and ("репозитория ещё нет" in message) is expected


def test_copy_task_initializes_remote_and_applies_retention(restic_on):
    with new_session() as session:
        box = add_mailbox(session)
        box.retention_days = 14
        remote = add_remote(session)
        task = restic_tasks.execute_task(session, restic_tasks.enqueue_task(session, ResticTaskKind.COPY, remote).id)
        assert task.status == RunStatus.SUCCESS, task.error_message
        assert Path(task.log_path).exists()

    calls = [c for c in restic_on() if remote.url in c]
    names = subcommands(lambda: calls)
    assert names == ["cat", "init", "copy", "forget", "prune"]
    init = calls[1]
    assert "--copy-chunker-params" in init and opt(init, "--from-repo") == str(get_settings().restic_repo)
    assert opt(calls[3], "--keep-within") == "14d"


def test_prune_task_and_failure(restic_on, monkeypatch):
    with new_session() as session:
        add_mailbox(session)  # срок 0 — бессрочно, forget не вызывается
        task = restic_tasks.execute_task(session, restic_tasks.enqueue_task(session, ResticTaskKind.PRUNE).id)
        assert task.status == RunStatus.SUCCESS
        assert subcommands(restic_on)[-1] == "prune" and "forget" not in subcommands(restic_on)

        monkeypatch.setenv("FAKE_RESTIC_FAIL", "check:12")
        task = restic_tasks.execute_task(session, restic_tasks.enqueue_task(session, ResticTaskKind.CHECK).id)
        assert task.status == RunStatus.FAILED and "неверный пароль" in task.error_message


def test_maintenance_schedule(restic_on):
    prefs = app_settings.AppSettings(timezone="Europe/Moscow")
    rprefs = app_settings.ResticSettings(enabled=True, schedule="05:00")
    from datetime import datetime

    now = datetime(2026, 10, 14, 5, 1, tzinfo=MSK).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    with new_session() as session:
        add_remote(session)
        add_remote(session, enabled=False)
        tasks = restic_tasks.enqueue_due(session, prefs, rprefs, now)
        assert [(t.kind, t.trigger) for t in tasks] == [(ResticTaskKind.PRUNE, RunTrigger.SCHEDULE),
                                                        (ResticTaskKind.COPY, RunTrigger.SCHEDULE)]
        for t in tasks:
            t.created_at = now
        session.commit()
        assert restic_tasks.enqueue_due(session, prefs, rprefs, now + timedelta(minutes=10)) == []
        # Снимки выключены — обслуживания нет
        off = app_settings.ResticSettings(enabled=False, schedule="05:00")
        assert restic_tasks.enqueue_due(session, prefs, off, now + timedelta(days=1)) == []


def test_worker_runs_restic_task_and_recovers(restic_on):
    with new_session() as session:
        task = restic_tasks.enqueue_task(session, ResticTaskKind.PRUNE)
        # Такая же задача в очереди не дублируется
        assert restic_tasks.enqueue_task(session, ResticTaskKind.PRUNE).id == task.id
        task_id = task.id
    worker = Worker()
    with new_session() as session:
        worker.start_restic_task(session)
    worker.task[1].join(10)
    with new_session() as session:
        assert session.get(ResticTask, task_id).status == RunStatus.SUCCESS

    # После перезапуска: зависшая задача — сбой, распакованные снимки удалены
    leftover = get_settings().maildir_root / "_restore_99"
    leftover.mkdir(parents=True)
    with new_session() as session:
        session.add(ResticTask(kind=ResticTaskKind.COPY, status=RunStatus.RUNNING))
        session.commit()
    Worker().recover()
    with new_session() as session:
        assert session.query(ResticTask).filter_by(status=RunStatus.RUNNING).count() == 0
    assert not leftover.exists()


def test_ssh_key_and_known_hosts(tmp_path):
    if shutil.which("ssh-keygen") is None:
        pytest.skip("нет ssh-keygen")
    pub = restic.generate_ssh_key()
    assert pub.startswith("ssh-ed25519 ") and restic.public_key() == pub
    assert oct(os.stat(restic.ssh_key_path()).st_mode & 0o777) == "0o600"
    host_pub = restic.generate_ssh_key().split()  # любой ключ годится как ключ «сервера»
    restic.known_hosts_path().write_text(f"[backup.example.com]:2222 {host_pub[0]} {host_pub[1]}\n")
    assert restic.host_fingerprints("backup.example.com", 2222)[0].startswith("ED25519 SHA256:")
    assert restic.host_fingerprints("backup.example.com", 22) == []
    restic.forget_host("backup.example.com", 2222)
    assert restic.host_fingerprints("backup.example.com", 2222) == []


# --- Настоящий restic (если установлен): тот же путь от снимка до восстановления --------------

def real_restic_bin() -> str | None:
    return os.environ.get("MB_TEST_RESTIC_BIN") or shutil.which("restic")


@pytest.mark.skipif(real_restic_bin() is None, reason="restic не установлен (MB_TEST_RESTIC_BIN)")
def test_real_restic_snapshot_restore_and_copy(fake_imapsync, monkeypatch, tmp_path):
    monkeypatch.setenv("MB_RESTIC_BIN", real_restic_bin())
    config.get_settings.cache_clear()
    restic.init_local()
    with new_session() as session:
        # Снимок до imapsync делается только у ящика с удалением из зеркала
        box = deleting_mailbox(session)
        maildir = get_settings().maildir_root / box.local_login / "Maildir" / "cur"
        maildir.mkdir(parents=True)
        (maildir / "1:2,S").write_text("письмо")

        backup = execute_run(session, new_run(session, box).id)
        assert backup.snapshot_id and backup.snapshot_error is None, backup.snapshot_error
        snaps = restic.snapshots(restic.local_repo(), box.id)
        assert len(snaps) == 2 and snaps[0].mailbox_id == box.id  # снимок до и после

        # Распаковка снимка во временную учётку (imapsync — заглушка)
        run = new_run(session, box, kind=RunKind.RESTORE, source_snapshot=snaps[-1].id,
                      source_snapshot_time=snaps[-1].time, target_host="imap.mail.ru", target_login="n@mail.ru",
                      target_password_enc=crypto.encrypt("x"))
        run = execute_run(session, run.id)
        assert run.status == RunStatus.SUCCESS, run.error_message
        log = Path(run.log_path).read_text()
        assert "restore" in log and "код выхода restic: 0" in log

        # «Удалённый» репозиторий — локальный каталог: init с параметрами нарезки и copy
        remote = restic.Repo(str(tmp_path / "remote"))
        assert restic.init_remote(remote, tmp_path / "copy.log")
        restic.copy_to(remote, tmp_path / "copy.log")
        assert len(restic.snapshots(remote, box.id)) == 2
