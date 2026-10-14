"""Подключение к SQLite и фабрика сессий."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from .config import get_settings

# Сколько ждать чужую запись, прежде чем отдать «database is locked», секунд.
# Пока imapsync пишет на тот же диск гигабайты, fsync коммита бывает долгим
BUSY_TIMEOUT = 30


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # WAL: веб-интерфейс читает, пока воркер пишет статистику
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT * 1000}")
        # В режиме WAL этого достаточно для целостности; fsync — только при checkpoint
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def new_session() -> Session:
    return _session_factory()()


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: одна сессия на запрос."""
    with new_session() as session:
        yield session


def create_tables() -> None:
    """Создаёт таблицы и добавляет недостающие колонки в существующие.

    Это упрощённая миграция: только добавление колонок (они обязаны быть
    nullable или иметь server_default). Переименования и удаления колонок
    потребуют Alembic.
    """
    # Импорт нужен, чтобы модели зарегистрировались в Base.metadata
    from . import models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(f"Новая колонка {table.name}.{column.name} без server_default")
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
