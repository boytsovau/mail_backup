"""Пресеты почтовых хостеров.

Пресет только заполняет форму ящика: хост, порт и шифрование хранятся
в самом ящике, поэтому любой IMAP-сервер можно задать вручную («custom»).
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Security(StrEnum):
    SSL = "ssl"  # IMAP поверх TLS, обычно порт 993
    STARTTLS = "starttls"  # обычно порт 143
    PLAIN = "plain"  # без шифрования, только для локального Dovecot


@dataclass(frozen=True)
class Provider:
    key: str
    title: str
    host: str
    port: int
    security: Security
    # Подсказка админу, как подготовить ящик к доступу по IMAP
    note: str = ""
    # Дополнительные аргументы imapsync, когда ящик выступает источником (host1)
    extra_args_source: tuple[str, ...] = field(default_factory=tuple)
    # То же, когда ящик выступает получателем (host2)
    extra_args_target: tuple[str, ...] = field(default_factory=tuple)


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider(
            key="yandex",
            title="Яндекс Почта",
            host="imap.yandex.ru",
            port=993,
            security=Security.SSL,
            note=(
                "В настройках почты включить доступ по IMAP и создать "
                "пароль приложения (id.yandex.ru → Безопасность → Пароли приложений)."
            ),
        ),
        Provider(
            key="mailru",
            title="Mail.ru",
            host="imap.mail.ru",
            port=993,
            security=Security.SSL,
            note=(
                "Создать пароль для внешнего приложения "
                "(Настройки → Безопасность → Пароли для внешних приложений)."
            ),
        ),
        Provider(
            key="custom",
            title="Другой IMAP-сервер",
            host="",
            port=993,
            security=Security.SSL,
            note="Хост, порт и тип шифрования задаются вручную.",
        ),
    )
}


def get_provider(key: str) -> Provider:
    return PROVIDERS.get(key, PROVIDERS["custom"])
