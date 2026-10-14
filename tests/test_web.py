import re

import pytest
from fastapi.testclient import TestClient

from mail_backup import app_settings, auth, crypto
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
