"""Общие фикстуры: каждое тестовое окружение живёт во временном каталоге."""

import pytest

from mail_backup import config, crypto, db


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
