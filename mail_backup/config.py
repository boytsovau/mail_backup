"""Настройки приложения.

Значения берутся из переменных окружения с префиксом MB_, из файла
/etc/mail_backup/mail_backup.env (сервер) и из .env (разработка).
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MB_",
        # Более поздний файл имеет приоритет: .env перекрывает серверный
        env_file=("/etc/mail_backup/mail_backup.env", ".env"),
        extra="ignore",
    )

    # Каталог данных приложения: БД, логи imapsync, временные файлы
    data_dir: Path = Path("/var/lib/mail_backup")
    # Корень хранилища бэкапов на отдельном диске (Maildir локального Dovecot)
    backup_root: Path = Path("/srv/mail_backup")
    # Каталог с секретами (права 700, владелец — пользователь сервиса)
    secrets_dir: Path = Path("/etc/mail_backup")

    # Локальный Dovecot, слушает только 127.0.0.1 без TLS
    local_imap_host: str = "127.0.0.1"
    local_imap_port: int = 143

    imapsync_bin: str = "imapsync"
    # Сколько задач imapsync может работать одновременно
    max_parallel_jobs: int = 2

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mail_backup.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def maildir_root(self) -> Path:
        return self.backup_root / "maildir"

    @property
    def dovecot_users_file(self) -> Path:
        # Каталог создаёт установщик с группой dovecot и setgid-битом,
        # чтобы Dovecot мог читать файл, а приложение — перезаписывать
        return self.data_dir / "dovecot" / "users"

    @property
    def fernet_key_file(self) -> Path:
        return self.secrets_dir / "secret.key"

    @property
    def session_key_file(self) -> Path:
        return self.secrets_dir / "session.key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
