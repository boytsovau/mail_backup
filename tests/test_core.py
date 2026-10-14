import stat

from fastapi.testclient import TestClient

from mail_backup import auth, crypto
from mail_backup.config import get_settings
from mail_backup.db import new_session
from mail_backup.main import app
from mail_backup.models import Mailbox, Run, RunKind, RunStatus
from mail_backup.providers import Security, get_provider


def test_encrypt_roundtrip():
    token = crypto.encrypt("пароль-123")
    assert token != "пароль-123"
    assert crypto.decrypt(token) == "пароль-123"


def test_key_files_private():
    mode = stat.S_IMODE(get_settings().fernet_key_file.stat().st_mode)
    assert mode == 0o600


def test_unknown_provider_falls_back_to_custom():
    assert get_provider("yandex").host == "imap.yandex.ru"
    assert get_provider("нет-такого").key == "custom"


def test_admin_login():
    with new_session() as session:
        assert not auth.check_admin(session, "admin", "x")
        auth.set_admin(session, "admin", "очень-секретно")
        assert auth.check_admin(session, "admin", "очень-секретно")
        assert not auth.check_admin(session, "admin", "неверно")
        assert not auth.check_admin(session, "other", "очень-секретно")


def test_mailbox_with_run():
    with new_session() as session:
        box = Mailbox(
            name="Тест",
            provider="mailru",
            host="imap.mail.ru",
            login="user@mail.ru",
            password_enc=crypto.encrypt("p"),
            local_login="user@mail.ru",
            local_password_enc=crypto.encrypt("l"),
        )
        session.add(box)
        session.flush()
        session.add(Run(kind=RunKind.BACKUP, mailbox_id=box.id))
        session.commit()

        run = session.query(Run).one()
        assert run.status == RunStatus.QUEUED
        assert run.mailbox.security == Security.SSL


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}
