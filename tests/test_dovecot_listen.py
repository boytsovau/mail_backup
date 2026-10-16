"""Адрес просмотра почты: проверка в панели, файл запроса, итог root-помощника.

Помощник deploy/dovecot-listen.sh запускается с поддельными ip, ss, systemctl и doveconf:
ss «слушает» те адреса, что записаны в его конфиге, — как Dovecot после перезагрузки.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from mail_backup import app_settings, cli, dovecot
from mail_backup.config import get_settings
from mail_backup.db import new_session

HELPER = Path(__file__).parent.parent / "deploy" / "dovecot-listen.sh"


def test_parse_listen():
    assert app_settings.parse_listen("  ", ["10.8.0.1"]) == ""
    assert app_settings.parse_listen(" 10.8.0.1 ", ["10.8.0.1"]) == "10.8.0.1"
    # Адреса интерфейсов узнать не удалось — не сверяем
    assert app_settings.parse_listen("10.8.0.9", None) == "10.8.0.9"
    for bad, text in [("127.0.0.1", "слушает всегда"), ("0.0.0.0", "интерфейсов"),
                      ("fd00::1", "IPv4"), ("server.lan", "IPv4"), ("10.8.0.9", "нет на этом сервере")]:
        with pytest.raises(ValueError, match=text):
            app_settings.parse_listen(bad, ["10.8.0.1"])


def test_request_listen_writes_only_changes():
    path = get_settings().dovecot_listen_file
    assert dovecot.request_listen("10.8.0.1") is True
    assert path.read_text() == "10.8.0.1\n"
    # Без изменений файл не трогаем: каждая запись — перезагрузка Dovecot
    assert dovecot.request_listen("10.8.0.1") is False
    assert dovecot.request_listen("") is True and path.read_text() == "\n"


def write_status(text: str) -> None:
    get_settings().dovecot_listen_status_file.write_text(text)


def test_listen_status():
    dovecot.request_listen("10.8.0.1")
    assert dovecot.listen_status() == dovecot.ListenStatus("pending", "10.8.0.1")
    write_status("state=ok\nmessage=\nrequested=10.8.0.1\ntime=1\n")
    assert dovecot.listen_status() == dovecot.ListenStatus("ok", "10.8.0.1")
    # Итог относится к прошлому запросу — новый ещё применяется
    dovecot.request_listen("10.8.0.2")
    assert dovecot.listen_status().state == "pending"
    write_status("state=error\nmessage=адреса нет\nrequested=10.8.0.2\ntime=1\n")
    assert dovecot.listen_status() == dovecot.ListenStatus("error", "10.8.0.2", "адреса нет")
    # Запрос не удалось прочитать — без requested, ошибка относится к текущему
    write_status("state=error\nmessage=в запросе не IPv4-адрес\ntime=1\n")
    assert dovecot.listen_status().state == "error"
    # Помощник не отвечает дольше минуты
    write_status("")
    old = time.time() - dovecot.LISTEN_APPLY_TIMEOUT - 5
    os.utime(get_settings().dovecot_listen_file, (old, old))
    assert dovecot.listen_status().stuck


def test_init_moves_legacy_env_address_to_panel(monkeypatch):
    monkeypatch.setenv("MB_VIEW_IMAP_HOST", "10.8.0.1")
    get_settings.cache_clear()
    cli._init_view_listen()
    with new_session() as session:
        assert app_settings.load(session).view_imap_host == "10.8.0.1"
    assert get_settings().dovecot_listen_file.read_text() == "10.8.0.1\n"

    # Дальше адрес меняется только в панели: старое значение из env его не перекрывает
    with new_session() as session:
        app_settings.save(session, app_settings.AppSettings(view_imap_host=""))
    cli._init_view_listen()
    assert get_settings().dovecot_listen_file.read_text() == "\n"


# --- Помощник deploy/dovecot-listen.sh ---------------------------------------------------

FAKE_SS = """#!/bin/sh
# Как Dovecot: слушает адреса из конфига (если он есть)
[ -f "$MB_LISTEN_CONF" ] || exit 0
sed -n 's/^listen = //p' "$MB_LISTEN_CONF" | tr ',' '\\n' | tr -d ' ' |
    while read -r a; do echo "LISTEN 0 100 $a:143 0.0.0.0:*"; done
"""


@pytest.fixture
def helper(tmp_path, monkeypatch):
    if shutil.which("bash") is None:
        pytest.skip("нет bash")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls = tmp_path / "systemctl.log"
    fakes = {
        "ip": "#!/bin/sh\necho '1: lo    inet 127.0.0.1/8 scope host lo'\n"
              "echo '2: eth0    inet 10.8.0.1/24 brd 10.8.0.255 scope global eth0'\n",
        "ss": FAKE_SS,
        "systemctl": f"#!/bin/sh\necho \"$*\" >>{calls}\n",
        "doveconf": "#!/bin/sh\nexit 0\n",
    }
    for name, text in fakes.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    conf_dir = tmp_path / "etc-dovecot"
    conf_dir.mkdir()
    conf = conf_dir / "mail_backup_listen.conf"
    settings = get_settings()
    settings.dovecot_listen_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "MB_LISTEN_CONF": str(conf)}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(HELPER), str(settings.data_dir), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    run.conf, run.calls = conf, calls
    return run


def test_helper_applies_address_and_reloads(helper):
    dovecot.request_listen("10.8.0.1")
    result = helper()
    assert result.returncode == 0, result.stderr
    assert "listen = 127.0.0.1, 10.8.0.1" in helper.conf.read_text()
    assert dovecot.listen_status() == dovecot.ListenStatus("ok", "10.8.0.1")
    # reload, а не restart: идущие бэкапы не рвутся
    assert "reload dovecot" in helper.calls.read_text() and "restart" not in helper.calls.read_text()

    dovecot.request_listen("")
    assert helper().returncode == 0
    assert helper.conf.read_text().splitlines()[-1] == "listen = 127.0.0.1"
    assert dovecot.listen_status() == dovecot.ListenStatus("ok", "")


def test_helper_rejects_foreign_address_and_keeps_previous(helper):
    dovecot.request_listen("10.8.0.1")
    assert helper().returncode == 0
    dovecot.request_listen("10.9.9.9")
    assert helper().returncode == 1
    assert "10.8.0.1" in helper.conf.read_text()
    status = dovecot.listen_status()
    assert (status.state, status.address) == ("error", "10.9.9.9") and "нет на этом сервере" in status.message


def test_helper_first_install_without_request_listens_locally(helper):
    # --no-reload: установщик сам перезапускает Dovecot
    assert helper("--no-reload").returncode == 0
    assert helper.conf.read_text().splitlines()[-1] == "listen = 127.0.0.1"
    assert not helper.calls.exists()


def test_helper_does_not_follow_symlinks(helper, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("root:hash\n")
    get_settings().dovecot_listen_file.symlink_to(secret)
    assert helper().returncode == 1
    status_text = get_settings().dovecot_listen_status_file.read_text()
    # Содержимое чужого файла в итог не попадает, Dovecot остаётся только на 127.0.0.1
    assert "root" not in status_text and "state=error" in status_text
    assert helper.conf.read_text().splitlines()[-1] == "listen = 127.0.0.1"


def test_helper_garbage_request_not_echoed(helper):
    get_settings().dovecot_listen_file.write_text("listen = *\n")
    assert helper().returncode == 1
    status_text = get_settings().dovecot_listen_status_file.read_text()
    assert "*" not in status_text and "requested=" not in status_text
    assert dovecot.listen_status().state == "error"
