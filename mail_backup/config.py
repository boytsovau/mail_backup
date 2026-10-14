"""Настройки приложения.

Значения берутся из переменных окружения с префиксом MB_, из файла
/etc/mail_backup/mail_backup.env (сервер) и из .env в корне проекта (разработка).
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env ищем в корне проекта, а не в текущем каталоге: команды на сервере
# запускаются через sudo -u из любого места, в т.ч. из недоступного /root
PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MB_",
        # Более поздний файл имеет приоритет: .env перекрывает серверный
        env_file=("/etc/mail_backup/mail_backup.env", PROJECT_ENV_FILE),
        extra="ignore",
    )

    # Режим развёртывания: dev — тестовый сервер без отдельного диска,
    # prod — бэкапы обязаны лежать на отдельном смонтированном диске
    deploy_mode: Literal["dev", "prod"] = "dev"

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
    restic_bin: str = "restic"
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
    def restic_repo(self) -> Path:
        # Локальный репозиторий снимков — на том же диске, что и зеркало
        return self.backup_root / "restic"

    @property
    def restic_password_file(self) -> Path:
        return self.secrets_dir / "restic.pass"

    @property
    def restic_cache_dir(self) -> Path:
        return self.data_dir / "restic-cache"

    @property
    def ssh_dir(self) -> Path:
        # Не в secrets_dir: ключ создаётся из веб-панели, а /etc для сервисов
        # systemd только на чтение (ProtectSystem=full)
        return self.data_dir / "ssh"

    @property
    def fernet_key_file(self) -> Path:
        return self.secrets_dir / "secret.key"

    @property
    def session_key_file(self) -> Path:
        return self.secrets_dir / "session.key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
