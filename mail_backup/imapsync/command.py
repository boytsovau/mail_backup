"""Сборка командной строки imapsync.

Здесь же — защита от опасных флагов: на ящик-источник (host1) imapsync
никогда не должен писать, а при восстановлении и переносе нельзя удалять
письма в ящике-получателе (host2).
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..models import RunKind
from ..providers import Security

# Флаги, при которых imapsync меняет host1 (удаляет/expunge письма и папки)
FORBIDDEN_HOST1_ARGS = frozenset({"--delete1", "--expunge1", "--delete1emptyfolders", "--expungeaftereach"})
# Флаги удаления на host2 — допустимы только при бэкапе в собственное хранилище
DELETE_HOST2_ARGS = frozenset({"--delete2", "--delete2folders", "--delete2duplicates", "--expunge2", "--uidexpunge2"})


class UnsafeCommandError(ValueError):
    """Команда imapsync могла бы изменить или удалить чужую почту."""


@dataclass(frozen=True)
class Endpoint:
    """Одна сторона синхронизации: IMAP-сервер и учётка."""

    host: str
    port: int
    security: Security
    login: str
    password: str = field(repr=False)


@dataclass
class Job:
    kind: RunKind
    source: Endpoint  # host1
    target: Endpoint  # host2
    dry_run: bool = False
    # Только проверить вход на обе стороны, ничего не копировать
    just_login: bool = False
    # Регулярное выражение для папок, которые пропускаем
    exclude_folders: str | None = None
    extra_args: list[str] = field(default_factory=list)


def _side_args(side: int, ep: Endpoint, passfile: Path) -> list[str]:
    args = [
        f"--host{side}", ep.host,
        f"--port{side}", str(ep.port),
        f"--user{side}", ep.login,
        # Пароль из файла, чтобы его не было видно в списке процессов
        f"--passfile{side}", str(passfile),
    ]
    match ep.security:
        case Security.SSL:
            args.append(f"--ssl{side}")
        case Security.STARTTLS:
            args += [f"--nossl{side}", f"--tls{side}"]
        case Security.PLAIN:
            args += [f"--nossl{side}", f"--notls{side}"]
    return args


def build_command(
    job: Job,
    *,
    imapsync_bin: str,
    passfile1: Path,
    passfile2: Path,
    tmpdir: Path,
) -> list[str]:
    cmd = [imapsync_bin]
    cmd += _side_args(1, job.source, passfile1)
    cmd += _side_args(2, job.target, passfile2)
    cmd += [
        # Лог пишем сами из stdout, свой файл imapsync не нужен
        "--nolog",
        "--tmpdir", str(tmpdir),
        "--pidfile", str(tmpdir / "imapsync.pid"),
        # Явно запрещаем expunge на источнике
        "--noexpunge1",
        # Письма без Message-Id (часто черновики) получают служебный заголовок,
        # иначе imapsync не сможет их опознать при повторных запусках
        "--addheader",
    ]

    if job.kind == RunKind.BACKUP:
        # Бэкап — зеркало ящика: удалённое на хостере удаляется и из зеркала.
        # История (в т.ч. удалённые письма) хранится в снимках restic.
        cmd += ["--delete2", "--delete2folders"]
    elif job.kind == RunKind.ESTIMATE:
        # Только размеры папок (EXAMINE + FETCH размеров), письма не копируются
        cmd.append("--justfoldersizes")
    else:
        # Почтовые серверы называют «Отправленные», «Спам» и т.п. по-разному
        cmd.append("--automap")

    if job.exclude_folders:
        cmd += ["--exclude", job.exclude_folders]
    if job.dry_run:
        cmd.append("--dry")
    if job.just_login:
        cmd.append("--justlogin")

    cmd += job.extra_args
    check_safe(job.kind, cmd)
    return cmd


def check_safe(kind: RunKind, cmd: list[str]) -> None:
    """Последний рубеж: отказ запускать команду, опасную для чужой почты."""
    args = set(cmd)
    if bad := args & FORBIDDEN_HOST1_ARGS:
        raise UnsafeCommandError(f"Запрещённые флаги для источника: {', '.join(sorted(bad))}")
    if kind != RunKind.BACKUP and (bad := args & DELETE_HOST2_ARGS):
        raise UnsafeCommandError(
            f"Удаление в ящике-получателе запрещено ({kind}): {', '.join(sorted(bad))}"
        )
