"""Быстрая проверка входа в IMAP-ящик (кнопка «Проверить подключение»).

Только LOGIN/LOGOUT: ящик не открывается, письма не читаются.
"""

import imaplib
import socket
import ssl
from dataclasses import dataclass

from .providers import Security

TIMEOUT = 15


@dataclass
class CheckResult:
    ok: bool
    message: str


def check_login(host: str, port: int, security: Security, login: str, password: str) -> CheckResult:
    try:
        if security == Security.SSL:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
            if security == Security.STARTTLS:
                conn.starttls(ssl_context=ssl.create_default_context())
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
