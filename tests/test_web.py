import json
import re
from datetime import timedelta

import pytest
from conftest import add_mailbox
from fastapi.testclient import TestClient

from mail_backup import app_settings, auth, crypto, models
from mail_backup.db import new_session
from mail_backup.main import app
from mail_backup.models import Mailbox, Run, RunStatus
from mail_backup.web import views_auth

PASSWORD = "очень-надёжный-пароль"


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def client():
    views_auth._fails.clear()
    # Cookie сессии помечена Secure — клиент должен ходить по https
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def logged_in(client):
    with new_session() as session:
        auth.set_admin(session, "admin", PASSWORD)
    page = client.get("/login")
    r = client.post("/login", data={"login": "admin", "password": PASSWORD, "csrf_token": csrf_of(page.text)},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


def csrf(client) -> str:
    return csrf_of(client.get("/settings").text)


def test_without_admin_everything_leads_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    assert client.get("/setup?token=неверный").status_code == 403


def test_setup_with_token_creates_admin_once(client):
    with new_session() as session:
        token = auth.create_setup_token(session)
    page = client.get(f"/setup?token={token}")
    assert page.status_code == 200
    data = {"token": token, "login": "admin", "password": PASSWORD, "password2": PASSWORD,
            "csrf_token": csrf_of(page.text)}
    r = client.post("/setup", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    # Повторно ссылка не работает
    assert client.post("/setup", data=data).status_code in (403, 200)
    with new_session() as session:
        assert not auth.check_setup_token(session, token)


def test_short_password_rejected_on_setup(client):
    with new_session() as session:
        token = auth.create_setup_token(session)
    page = client.get(f"/setup?token={token}")
    r = client.post("/setup", data={"token": token, "login": "a", "password": "123", "password2": "123",
                                    "csrf_token": csrf_of(page.text)})
    assert r.status_code == 400 and "не короче" in r.text


def test_login_wrong_password_and_lockout(client):
    with new_session() as session:
        auth.set_admin(session, "admin", PASSWORD)
    token = csrf_of(client.get("/login").text)
    for _ in range(views_auth.MAX_FAILS):
        r = client.post("/login", data={"login": "admin", "password": "нет", "csrf_token": token})
        assert r.status_code == 401
    r = client.post("/login", data={"login": "admin", "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 429


def test_post_without_csrf_forbidden(logged_in):
    r = logged_in.post("/settings", data={"max_parallel_jobs": 3, "timezone": "UTC", "log_retention_days": 10})
    assert r.status_code == 403


def test_login_redirect_only_local(logged_in):
    logged_in.post("/logout", data={"csrf_token": csrf(logged_in)})
    page = logged_in.get("/login?next=//evil.example")
    assert 'value="/"' in page.text


def add_mailbox_via_form(client, **overrides):
    data = {
        "csrf_token": csrf(client), "provider": "yandex", "host": "imap.yandex.ru", "port": "993",
        "security": "ssl", "login": "user@yandex.ru", "password": "app-pass", "name": "Почта",
        "enabled": "on", "schedule": "3:00, 15:30", "retention_days": "30", "exclude_folders": "", "note": "",
    }
    data.update(overrides)
    return client.post("/mailboxes/new", data=data, follow_redirects=False)


def test_mailbox_create_edit_delete(logged_in):
    r = add_mailbox_via_form(logged_in)
    assert r.status_code == 303
    with new_session() as session:
        box = session.query(Mailbox).one()
        assert box.schedule == "03:00, 15:30"
        assert crypto.decrypt(box.password_enc) == "app-pass"
        box_id = box.id
    assert "Почта" in logged_in.get("/").text
    assert "Почта" in logged_in.get("/mailboxes").text

    # Пустой пароль при правке — пароль не меняется; логин подменить нельзя
    page = logged_in.get(f"/mailboxes/{box_id}/edit")
    assert page.status_code == 200
    r = logged_in.post(f"/mailboxes/{box_id}/edit", data={
        "csrf_token": csrf_of(page.text), "provider": "yandex", "host": "imap.yandex.ru", "port": "993",
        "security": "ssl", "login": "hacker@x.ru", "password": "", "name": "Новая", "schedule": "",
        "retention_days": "0",
    }, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        box = session.get(Mailbox, box_id)
        assert (box.name, box.login, box.enabled, box.schedule) == ("Новая", "user@yandex.ru", False, None)
        assert crypto.decrypt(box.password_enc) == "app-pass"

    r = logged_in.post(f"/mailboxes/{box_id}/delete", data={"csrf_token": csrf(logged_in)}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        assert session.query(Mailbox).count() == 0


def test_mailbox_form_validation(logged_in):
    r = add_mailbox_via_form(logged_in, host="", schedule="25:99", exclude_folders="(", password="")
    assert r.status_code == 400
    for text in ("Укажите IMAP-сервер", "Неверное время", "Неверное регулярное", "Укажите пароль"):
        assert text in r.text


def test_backup_button_queues_one_run(logged_in):
    add_mailbox_via_form(logged_in)
    with new_session() as session:
        box_id = session.query(Mailbox).one().id
    token = csrf(logged_in)
    r1 = logged_in.post(f"/mailboxes/{box_id}/backup", data={"csrf_token": token}, follow_redirects=False)
    r2 = logged_in.post(f"/mailboxes/{box_id}/backup", data={"csrf_token": token}, follow_redirects=False)
    # Второе нажатие не создаёт дубль, а открывает тот же запуск
    assert r1.headers["location"] == r2.headers["location"]
    with new_session() as session:
        run = session.query(Run).one()
        assert run.status == RunStatus.QUEUED
    page = logged_in.get(r1.headers["location"])
    assert "в очереди" in page.text and 'hx-trigger="every 2s"' in page.text

    r = logged_in.post(f"/runs/{run.id}/cancel", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        assert session.get(Run, run.id).cancel_requested
    assert logged_in.get(f"/runs/{run.id}/live").status_code == 200
    assert logged_in.get("/runs").status_code == 200


def test_settings_save_and_validate(logged_in):
    token = csrf(logged_in)
    logged_in.post("/settings", data={"csrf_token": token, "max_parallel_jobs": 3,
                                      "timezone": "Asia/Yekaterinburg", "log_retention_days": 30})
    with new_session() as session:
        assert app_settings.load(session) == app_settings.AppSettings(3, "Asia/Yekaterinburg", 30)
    r = logged_in.post("/settings", data={"csrf_token": token, "max_parallel_jobs": 3,
                                          "timezone": "Марс/Олимп", "log_retention_days": 30})
    assert "Неизвестный часовой пояс" in r.text


def test_password_change_ends_other_sessions(logged_in):
    other = TestClient(app, base_url="https://testserver")
    page = other.get("/login")
    other.post("/login", data={"login": "admin", "password": PASSWORD, "csrf_token": csrf_of(page.text)})
    assert other.get("/", follow_redirects=False).status_code == 200

    r = logged_in.post("/settings/account", data={
        "csrf_token": csrf(logged_in), "login": "admin", "current_password": PASSWORD,
        "password": "новый-пароль-123", "password2": "новый-пароль-123",
    })
    assert "обновлены" in r.text
    assert logged_in.get("/", follow_redirects=False).status_code == 200
    assert other.get("/", follow_redirects=False).status_code == 303


def test_system_page(logged_in):
    r = logged_in.get("/system")
    assert r.status_code == 200 and "MB_BACKUP_ROOT" in r.text


def test_unknown_page_404(logged_in):
    assert logged_in.get("/runs/999").status_code == 404


def test_estimate_button_and_storage_page(logged_in):
    add_mailbox_via_form(logged_in)
    with new_session() as session:
        box_id = session.query(Mailbox).one().id
    page = logged_in.get("/storage")
    assert "ещё не оценён" in page.text

    r = logged_in.post(f"/mailboxes/{box_id}/estimate", data={"csrf_token": csrf(logged_in)}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        run = session.query(Run).one()
        assert run.kind == "estimate" and run.status == RunStatus.QUEUED
        # Имитируем завершённую оценку
        run.status, run.host1_messages, run.host1_bytes = RunStatus.SUCCESS, 1000, 50 * 1024**2
        run.folder_stats = '[{"name": "INBOX", "size": 52428800, "messages": 1000, "biggest": 1048576}]'
        session.commit()
        run_id = run.id

    page = logged_in.get("/storage")
    assert "50.0 МБ" in page.text and "Рекомендуемый диск" in page.text
    detail = logged_in.get(f"/runs/{run_id}")
    assert "Папки источника" in detail.text and "INBOX" in detail.text


def test_estimate_all_skips_busy_mailboxes(logged_in):
    add_mailbox_via_form(logged_in)
    add_mailbox_via_form(logged_in, login="second@yandex.ru", name="Второй")
    with new_session() as session:
        first = session.query(Mailbox).filter_by(login="user@yandex.ru").one()
        session.add(Run(kind="backup", mailbox_id=first.id))
        session.commit()
    logged_in.post("/storage/estimate-all", data={"csrf_token": csrf(logged_in)})
    with new_session() as session:
        kinds = sorted(r.kind for r in session.query(Run))
        assert kinds == ["backup", "estimate"]


def _box_with_backup(client, *, backup=True) -> int:
    add_mailbox_via_form(client)
    with new_session() as session:
        box = session.query(Mailbox).one()
        if backup:
            session.add(Run(kind="backup", mailbox_id=box.id, status=RunStatus.SUCCESS, host2_messages=3))
            session.commit()
        return box.id


def transfer_data(client, **overrides):
    data = {"csrf_token": csrf(client), "mode": "restore", "target": "same", "provider": "mailru",
            "host": "imap.mail.ru", "port": "993", "security": "ssl", "login": "", "password": "",
            "placement": "as_is", "subfolder": "Restored", "action": "dry"}
    data.update(overrides)
    return data


def test_transfer_restore_needs_backup(logged_in):
    box_id = _box_with_backup(logged_in, backup=False)
    page = logged_in.get(f"/mailboxes/{box_id}/transfer")
    assert page.status_code == 200 and "Удачного бэкапа ещё не было" in page.text
    r = logged_in.post(f"/mailboxes/{box_id}/transfer", data=transfer_data(logged_in, target="other"))
    assert r.status_code == 400 and "восстанавливать нечего" in r.text


def test_transfer_dry_run_then_repeat(logged_in):
    box_id = _box_with_backup(logged_in)
    r = logged_in.post(f"/mailboxes/{box_id}/transfer", data=transfer_data(logged_in), follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        dry = session.query(Run).filter_by(kind="restore").one()
        assert dry.dry_run and dry.target_login == "user@yandex.ru" and dry.target_subfolder is None
        # «Этот же ящик» — пароль копируется из сохранённого
        assert crypto.decrypt(dry.target_password_enc) == "app-pass"
        dry.status, dry.messages_planned = RunStatus.SUCCESS, 7
        session.commit()
        dry_id = dry.id
    page = logged_in.get(f"/runs/{dry_id}")
    assert "будет скопировано писем: <strong>7" in page.text and "Запустить по-настоящему" in page.text

    r = logged_in.post(f"/runs/{dry_id}/repeat", data={"csrf_token": csrf(logged_in)}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        real = session.query(Run).filter_by(kind="restore", dry_run=False).one()
        assert r.headers["location"] == f"/runs/{real.id}"
        assert crypto.decrypt(real.target_password_enc) == "app-pass"
        assert session.get(Run, dry_id).target_password_enc is None


def test_transfer_other_mailbox_with_subfolder(logged_in):
    box_id = _box_with_backup(logged_in)
    data = transfer_data(logged_in, mode="migrate", target="other", login="to@mail.ru", password="p",
                         placement="subfolder", subfolder="From-Yandex", action="run")
    r = logged_in.post(f"/mailboxes/{box_id}/transfer", data=data, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        run = session.query(Run).filter_by(kind="migrate").one()
        assert not run.dry_run
        assert (run.target_host, run.target_login, run.target_subfolder) == ("imap.mail.ru", "to@mail.ru", "From-Yandex")
        assert crypto.decrypt(run.target_password_enc) == "p"
    assert "to@mail.ru" in logged_in.get("/runs").text


@pytest.mark.parametrize("overrides, error", [
    ({"mode": "migrate"}, "Перенос в тот же ящик"),
    ({"target": "other", "host": "127.0.0.1", "login": "x", "password": "p"}, "самого сервера бэкапов"),
    ({"target": "other", "host": "localhost", "login": "x", "password": "p"}, "самого сервера бэкапов"),
    ({"mode": "migrate", "target": "other", "host": "IMAP.yandex.ru", "login": "User@yandex.ru", "password": "p"},
     "тот же ящик, что и источник"),
    ({"target": "other", "login": "", "password": ""}, "Укажите логин"),
    ({"placement": "subfolder", "subfolder": "Восстановлено"}, "Только латинские"),
])
def test_transfer_validation(logged_in, overrides, error):
    box_id = _box_with_backup(logged_in)
    r = logged_in.post(f"/mailboxes/{box_id}/transfer", data=transfer_data(logged_in, **overrides))
    assert r.status_code == 400 and error in r.text
    with new_session() as session:
        assert session.query(Run).filter(Run.kind != "backup").count() == 0


def test_transfer_busy_mailbox_keeps_form(logged_in):
    box_id = _box_with_backup(logged_in)
    with new_session() as session:
        session.add(Run(kind="backup", mailbox_id=box_id))
        session.commit()
    r = logged_in.post(f"/mailboxes/{box_id}/transfer",
                       data=transfer_data(logged_in, target="other", login="to@mail.ru", password="p"))
    assert r.status_code == 409 and "Ящик сейчас занят" in r.text and 'value="to@mail.ru"' in r.text


def test_repeat_without_password_opens_prefilled_wizard(logged_in):
    box_id = _box_with_backup(logged_in)
    with new_session() as session:
        run = Run(kind="migrate", mailbox_id=box_id, dry_run=True, status=RunStatus.SUCCESS,
                  target_host="imap.mail.ru", target_port=993, target_security="ssl",
                  target_login="to@mail.ru", target_subfolder="Old")
        session.add(run)
        session.commit()
        run_id = run.id
    r = logged_in.post(f"/runs/{run_id}/repeat", data={"csrf_token": csrf(logged_in)}, follow_redirects=False)
    assert r.headers["location"] == f"/mailboxes/{box_id}/transfer?from_run={run_id}"
    page = logged_in.get(r.headers["location"])
    assert 'value="to@mail.ru"' in page.text and 'value="Old"' in page.text
    assert '<option value="mailru" selected' in page.text


def test_charts_page(logged_in):
    assert "Ящиков пока нет" in logged_in.get("/charts").text
    with new_session() as session:
        box = add_mailbox(session)
        session.add(Run(kind="backup", mailbox_id=box.id, status=RunStatus.SUCCESS, finished_at=models.utcnow(),
                        bytes_transferred=5 * 1024**2, messages_transferred=7, host1_bytes=10 * 1024**2,
                        host1_messages=70, duration_seconds=90))
        session.commit()
        box_id = box.id
    page = logged_in.get("/charts?days=7")
    assert page.status_code == 200
    data = json.loads(re.search(r'id="chart-data">(.*?)</script>', page.text, re.S).group(1))
    assert len(data["labels"]) == 7 and data["charts"]["volume"]["datasets"][0]["data"][-1] == 5 * 1024**2
    assert 'data-chart="size"' in page.text and "5.0 МБ" in page.text
    # Неизвестный период и чужой ящик — значения по умолчанию, а не ошибка
    page = logged_in.get(f"/charts?days=5&mailbox={box_id + 100}")
    assert page.status_code == 200 and '<option value="30" selected>' in page.text


def test_dashboard_shows_stale_backup(logged_in):
    with new_session() as session:
        box = add_mailbox(session)
        box.schedule = "03:00"
        box.created_at = models.utcnow() - timedelta(days=5)
        session.add(Run(kind="backup", mailbox_id=box.id, status=RunStatus.SUCCESS,
                        finished_at=models.utcnow() - timedelta(days=3)))
        session.commit()
    page = logged_in.get("/")
    assert "Требуют внимания" in page.text and "просрочен" in page.text
    assert 'id="overview-chart"' in page.text


def test_restic_page_toggle_and_settings(logged_in):
    page = logged_in.get("/restic")
    assert page.status_code == 200 and "restic не установлен" in page.text and "включены" in page.text
    token = csrf(logged_in)
    r = logged_in.post("/restic/toggle", data={"csrf_token": token, "enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        assert app_settings.load_restic(session).enabled is False
    assert "зеркало <strong>не удаляет</strong>" in logged_in.get("/restic").text

    logged_in.post("/restic/settings", data={"csrf_token": token, "schedule": "4:30, 23:00"})
    with new_session() as session:
        assert app_settings.load_restic(session).schedule == "04:30, 23:00"
    r = logged_in.post("/restic/settings", data={"csrf_token": token, "schedule": "25:00"})
    assert "Неверное время" in r.text


def test_restic_password_only_on_request(logged_in):
    from mail_backup.config import get_settings

    password = get_settings().restic_password_file.read_text().strip()
    assert password not in logged_in.get("/restic").text
    r = logged_in.post("/restic/password", data={"csrf_token": csrf(logged_in)})
    assert password in r.text and "Сохраните пароль" in r.text


def test_restic_remotes(logged_in, fake_restic):
    token = csrf(logged_in)
    bad = logged_in.post("/restic/remotes", data={"csrf_token": token, "host": "bad host", "port": "0",
                                                   "user": "-x", "path": "../etc"})
    assert bad.status_code == 400 and "Порт" in bad.text and "без пробелов" in bad.text

    r = logged_in.post("/restic/remotes", data={"csrf_token": token, "name": "", "host": "backup.example.com",
                                                 "port": "2222", "user": "mb", "path": "/srv/restic/"},
                       follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        remote = session.query(models.ResticRemote).one()
        assert (remote.name, remote.path, remote.enabled) == ("backup.example.com", "/srv/restic", True)
        remote_id = remote.id
    # Без ключа проверка сразу просит его создать
    check = logged_in.post(f"/restic/remotes/{remote_id}/check", headers={"X-CSRF-Token": token})
    assert "SSH-ключ" in check.text

    logged_in.post("/restic/ssh-key", data={"csrf_token": token})
    page = logged_in.get("/restic")
    assert "ssh-ed25519 " in page.text and "sftp://mb@backup.example.com:2222//srv/restic" in page.text
    check = logged_in.post(f"/restic/remotes/{remote_id}/check", headers={"X-CSRF-Token": token})
    assert "будет создан при первой выгрузке" in check.text

    r = logged_in.post(f"/restic/remotes/{remote_id}/upload", data={"csrf_token": token}, follow_redirects=False)
    task_url = r.headers["location"]
    assert "Выгрузка" in logged_in.get(task_url).text
    # Пока выгрузка в очереди, сервер не удаляется; отменённую — можно
    logged_in.post(f"/restic/remotes/{remote_id}/delete", data={"csrf_token": token})
    logged_in.post(task_url + "/cancel", data={"csrf_token": token})
    with new_session() as session:
        assert session.query(models.ResticTask).one().cancel_requested
        session.query(models.ResticTask).update({"status": RunStatus.CANCELLED})
        session.commit()
    logged_in.post(f"/restic/remotes/{remote_id}/delete", data={"csrf_token": token})
    with new_session() as session:
        assert session.query(models.ResticRemote).count() == 0


def test_transfer_from_snapshot(logged_in, fake_restic):
    from mail_backup import restic

    restic.init_local()
    with new_session() as session:
        box = add_mailbox(session)
        box_id = box.id
    snap_path = "/srv/mail_backup/maildir/user@yandex.ru"
    restic.run_short(restic.local_repo(), ["backup", snap_path, "--tag", f"mailbox-{box_id}"])
    snap = restic.snapshots(restic.local_repo(), box_id)[0]

    page = logged_in.get(f"/mailboxes/{box_id}/transfer?mode=snapshot")
    assert 'value="snapshot" checked' in page.text and snap.short_id in page.text
    data = {"csrf_token": csrf(logged_in), "mode": "snapshot", "snapshot": "нет-такого", "target": "same",
            "placement": "as_is", "action": "dry"}
    assert "Выберите снимок" in logged_in.post(f"/mailboxes/{box_id}/transfer", data=data).text

    data["snapshot"] = snap.id
    r = logged_in.post(f"/mailboxes/{box_id}/transfer", data=data, follow_redirects=False)
    assert r.status_code == 303
    with new_session() as session:
        run = session.query(Run).one()
        assert run.kind == "restore" and run.source_snapshot == snap.id and run.dry_run
        assert run.source_snapshot_time == snap.time and run.target_login == "user@yandex.ru"
    assert "снимок restic от" in logged_in.get(r.headers["location"]).text


def test_dashboard_warns_about_missing_snapshot(logged_in, fake_restic):
    from mail_backup import restic

    restic.init_local()
    with new_session() as session:
        box = add_mailbox(session)
        session.add(Run(kind="backup", mailbox_id=box.id, status=RunStatus.SUCCESS, finished_at=models.utcnow(),
                        snapshot_error="restic: репозиторий заблокирован"))
        session.commit()
    page = logged_in.get("/")
    assert "нет снимка" in page.text and "репозиторий заблокирован" in page.text
    assert "restic" in logged_in.get("/system").text
