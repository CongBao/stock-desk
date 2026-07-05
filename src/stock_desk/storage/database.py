from collections.abc import Callable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_CONFIG_PATH = _REPOSITORY_ROOT / "alembic.ini"


def _configure_sqlite_connection(
    dbapi_connection: Any, _connection_record: Any
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.fetchone()
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_engine_for_url(url: str) -> Engine:
    """Create an SQLAlchemy 2 engine with Stock Desk's connection policy."""
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _alembic_config(url: str) -> Config:
    config = Config(str(_ALEMBIC_CONFIG_PATH))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _run_alembic_command(
    operation: Callable[[Config, str], None], url: str, revision: str
) -> None:
    operation(_alembic_config(url), revision)


def migrate(url: str, revision: str = "head") -> None:
    """Upgrade the configured database to an Alembic revision."""
    _run_alembic_command(command.upgrade, url, revision)


def downgrade(url: str, revision: str = "base") -> None:
    """Downgrade the configured database to an Alembic revision."""
    _run_alembic_command(command.downgrade, url, revision)
