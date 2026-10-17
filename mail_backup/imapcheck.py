"""Быстрые обращения к IMAP-ящику без imapsync.

Проверка входа (кнопка «Проверить подключение») — только LOGIN/LOGOUT.
Подсчёт писем для защиты от проседания ящика — LIST и STATUS: ни одна папка
не открывается, письма не читаются, флаги не меняются.
"""

import base64
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass

from .providers import Security

TIMEOUT = 15

# Ответ LIST: (флаги) "разделитель" имя
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$', re.IGNORECASE)
_MESSAGES_RE = re.compile(rb"MESSAGES (\d+)", re.IGNORECASE)
# Папки, которые нельзя открыть (узел иерархии) или которых уже нет
_NO_SELECT = ("\\noselect", "\\nonexistent")


class ImapError(Exception):
    """Не удалось подключиться к ящику или получить ответ."""


@dataclass
class CheckResult:
    ok: bool
    message: str


def _connect(host: str, port: int, security: Security) -> imaplib.IMAP4:
    if security == Security.SSL:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
    if security == Security.STARTTLS:
        conn.starttls(ssl_context=ssl.create_default_context())
    return conn


def check_login(host: str, port: int, security: Security, login: str, password: str) -> CheckResult:
    try:
        conn = _connect(host, port, security)
    except (socket.gaierror, ConnectionRefusedError) as exc:
        return CheckResult(False, f"Сервер {host}:{port} недоступен: {exc}")
    except TimeoutError:
        return CheckResult(False, f"Сервер {host}:{port} не ответил за {TIMEOUT} с")
    except (ssl.SSLError, imaplib.IMAP4.error, OSError) as exc:
        return CheckResult(False, f"Ошибка соединения/TLS: {exc}")

    try:
        conn.login(login, password)
    except imaplib.IMAP4.error as exc:
        detail = exc.args[0].decode(errors="replace") if exc.args and isinstance(exc.args[0], bytes) else str(exc)
        return CheckResult(
            False,
            f"Сервер отклонил логин или пароль: {detail}. "
            "Для Яндекса и Mail.ru нужен пароль приложения и включённый доступ по IMAP.",
        )
    except (OSError, UnicodeEncodeError) as exc:
        return CheckResult(False, f"Ошибка входа: {exc}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return CheckResult(True, "Вход выполнен успешно")


def decode_mutf7(name: str) -> str:
    """Имя папки из modified UTF-7 (RFC 3501) в обычную строку; не вышло — как есть."""
    def chunk(m: re.Match) -> str:
        if not m.group(1):
            return "&"
        data = m.group(1).replace(",", "/")
        return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-16-be")
    try:
        return re.sub(r"&([A-Za-z0-9+,]*)-", chunk, name)
    except (ValueError, UnicodeDecodeError):
        return name


def _unquote(name: str) -> str:
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return re.sub(r"\\(.)", r"\1", name[1:-1])
    return name


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _folders(conn: imaplib.IMAP4) -> list[str]:
    """Имена папок, которые можно открыть (как их отдаёт сервер, в mUTF-7)."""
    typ, data = conn.list()
    if typ != "OK":
        raise ImapError(f"LIST: {typ}")
    names = []
    for item in data:
        # Имя литералом: (b'(флаги) "/" {N}', b'имя'); между такими записями — b')' или b''
        if isinstance(item, tuple):
            head, name = item[0].decode(errors="replace"), item[1].decode(errors="replace")
        elif isinstance(item, bytes):
            head, name = item.decode(errors="replace"), None
        else:
            continue
        m = _LIST_RE.match(head)
        if not m:
            continue
        if any(flag in m["flags"].lower() for flag in _NO_SELECT):
            continue
        names.append(name if name is not None else _unquote(m["name"].strip()))
    return names


def count_messages(host: str, port: int, security: Security, login: str, password: str,
                   exclude: str | None = None) -> int:
    """Сколько писем в ящике по всем папкам, кроме исключённых (STATUS MESSAGES).

    exclude — регулярное выражение папок ящика; проверяется и по имени, как его
    отдаёт сервер, и по раскодированному из mUTF-7 (в Python, а не в Perl, как у
    imapsync, — но счёт сравнивается только с таким же счётом прошлых бэкапов).
    Ошибка — ImapError.
    """
    try:
        pattern = re.compile(exclude) if exclude else None
    except re.error:
        pattern = None
    try:
        conn = _connect(host, port, security)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
        raise ImapError(f"нет соединения с {host}:{port}: {exc}") from exc
    try:
        conn.login(login, password)
        total = 0
        for name in _folders(conn):
            if pattern and (pattern.search(name) or pattern.search(decode_mutf7(name))):
                continue
            typ, data = conn.status(_quote(name), "(MESSAGES)")
            if typ != "OK":
                raise ImapError(f"STATUS {decode_mutf7(name)}: {typ}")
            raw = b" ".join(part if isinstance(part, bytes) else b" ".join(part) for part in data if part)
            m = _MESSAGES_RE.search(raw)
            if not m:
                raise ImapError(f"STATUS {decode_mutf7(name)}: нет числа писем в ответе")
            total += int(m.group(1))
        return total
    except (imaplib.IMAP4.error, OSError, UnicodeEncodeError) as exc:
        raise ImapError(str(exc)) from exc
    finally:
        try:
            conn.logout()
        except Exception:
            pass
