from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_CONFIG_PATH = Path(__file__).resolve().parents[3] / "alembic.ini"
_PACKAGED_CONFIG_PATH = _PACKAGE_ROOT / "alembic.ini"
_SQLITE_BUSY_TIMEOUT_MS = 5_000


def _configure_sqlite_connection(
    dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _sqlite_is_memory(url: URL) -> bool:
    database = url.database
    return (
        database is None
        or database == ""
        or database == ":memory:"
        or database.startswith("file::memory:")
        or url.query.get("mode") == "memory"
    )


def _sqlite_is_read_only(url: URL) -> bool:
    return url.query.get("mode") == "ro" or url.query.get("immutable") == "1"


def _sqlite_database_path(url: URL) -> Path:
    database = url.database
    if database is None:
        raise ValueError("File-backed SQLite URL requires a database path")
    if database.startswith("file:"):
        database = database.removeprefix("file:")
    return Path(unquote(database))


def _prepare_sqlite_file(url: URL) -> bool:
    if url.get_backend_name() != "sqlite":
        return False
    if _sqlite_is_memory(url) or _sqlite_is_read_only(url):
        return False

    parent = _sqlite_database_path(url).parent
    if parent != Path("."):
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return True


def _initialize_sqlite_wal(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql(
                "PRAGMA journal_mode=WAL"
            ).scalar_one()
        if str(journal_mode).lower() != "wal":
            raise RuntimeError("SQLite did not enable WAL journal mode")
    except Exception:
        engine.dispose()
        raise


def create_engine_for_url(url: str) -> Engine:
    """Create an SQLAlchemy 2 engine with Stock Desk's connection policy."""
    parsed_url = make_url(url)
    initialize_wal = _prepare_sqlite_file(parsed_url)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
        if initialize_wal:
            _initialize_sqlite_wal(engine)
    return engine


def _alembic_config_path() -> Path:
    if _PACKAGED_CONFIG_PATH.is_file():
        return _PACKAGED_CONFIG_PATH
    if _REPOSITORY_CONFIG_PATH.is_file():
        return _REPOSITORY_CONFIG_PATH
    raise FileNotFoundError("Stock Desk Alembic configuration is not installed")


def _alembic_config(url: str) -> Config:
    config = Config(str(_alembic_config_path()))
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
