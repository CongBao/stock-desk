from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Final, TypeAlias, cast
from urllib.parse import unquote

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from filelock import FileLock, Timeout as FileLockTimeout
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_CONFIG_PATH = Path(__file__).resolve().parents[3] / "alembic.ini"
_PACKAGED_CONFIG_PATH = _PACKAGE_ROOT / "alembic.ini"
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_SQLITE_DATABASE_IDENTITY_INFO_KEY = "stock_desk.sqlite_database_identity"
_MIGRATION_LOCK_TIMEOUT_SECONDS = 30
_MIGRATION_THREAD_LOCK = RLock()
_TIMESTAMP_DIGEST_AGGREGATE: Final = "stock_desk_timestamp_digest"
DatabaseIdentity: TypeAlias = tuple[object, ...]


class DatabaseIdentityError(RuntimeError):
    """A live database connection has no trustworthy frozen identity."""


def _canonical_utc_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed_value = value.removesuffix("Z") + (
            "+00:00" if value.endswith("Z") else ""
        )
        timestamp = datetime.fromisoformat(parsed_value)
    elif isinstance(value, datetime):
        timestamp = value
    else:
        raise TypeError("timestamp must be a datetime or ISO-8601 string")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_canonical_timestamps(timestamps: Sequence[str]) -> str:
    encoded = json.dumps(
        timestamps,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def timestamp_digest(timestamps: Sequence[datetime]) -> str:
    """Hash timestamps using the canonical representation sealed by MarketLake."""
    return _digest_canonical_timestamps(
        tuple(_canonical_utc_timestamp(timestamp) for timestamp in timestamps)
    )


class _TimestampDigestAggregate:
    """SQLite aggregate that binds ordinal order to canonical UTC timestamps."""

    def __init__(self) -> None:
        self._timestamps: dict[int, str] = {}
        self._valid = True

    def step(self, ordinal: object, timestamp: object) -> None:
        if not self._valid:
            return
        if type(ordinal) is not int or ordinal < 0 or ordinal in self._timestamps:
            self._valid = False
            return
        try:
            self._timestamps[ordinal] = _canonical_utc_timestamp(
                cast(datetime | str, timestamp)
            )
        except (TypeError, ValueError):
            self._valid = False

    def finalize(self) -> str | None:
        if not self._valid or not self._timestamps:
            return None
        ordinals = tuple(sorted(self._timestamps))
        if ordinals != tuple(range(len(ordinals))):
            return None
        return _digest_canonical_timestamps(
            tuple(self._timestamps[ordinal] for ordinal in ordinals)
        )


def _sqlite_database_identity(
    dbapi_connection: DBAPIConnection,
    rows: Sequence[Any],
) -> DatabaseIdentity:
    try:
        main_rows = tuple(row for row in rows if row[1] == "main")
    except Exception as error:
        raise DatabaseIdentityError(
            "SQLite database identity could not be determined"
        ) from error
    if len(main_rows) != 1 or len(main_rows[0]) != 3:
        raise DatabaseIdentityError("SQLite database identity could not be determined")
    filename = main_rows[0][2]
    if type(filename) is not str:
        raise DatabaseIdentityError("SQLite database identity could not be determined")
    if filename == "":
        return ("sqlite-memory", id(dbapi_connection))
    try:
        path = Path(filename).resolve(strict=True)
        file_status = path.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise DatabaseIdentityError(
            "SQLite database identity could not be determined"
        ) from error
    return ("sqlite-file", str(path), file_status.st_dev, file_status.st_ino)


def _configure_sqlite_connection(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    connection_record.info.pop(_SQLITE_DATABASE_IDENTITY_INFO_KEY, None)
    sqlite_connection = cast(sqlite3.Connection, dbapi_connection)
    sqlite_connection.create_aggregate(
        _TIMESTAMP_DIGEST_AGGREGATE,
        2,
        cast(Callable[[], Any], _TimestampDigestAggregate),
    )
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA database_list")
        identity = _sqlite_database_identity(dbapi_connection, cursor.fetchall())
        connection_record.info[_SQLITE_DATABASE_IDENTITY_INFO_KEY] = identity
    finally:
        cursor.close()


def _validated_database_identity(value: object) -> DatabaseIdentity:
    if (
        type(value) is tuple
        and len(value) == 2
        and value[0] == "sqlite-memory"
        and type(value[1]) is int
    ):
        return value
    if (
        type(value) is tuple
        and len(value) == 4
        and value[0] == "sqlite-file"
        and type(value[1]) is str
        and Path(value[1]).is_absolute()
        and type(value[2]) is int
        and type(value[3]) is int
    ):
        return value
    raise DatabaseIdentityError("SQLite connection database identity is invalid")


def connection_database_identity(connection: Connection) -> DatabaseIdentity:
    """Read a connection-bound identity without database or filesystem I/O."""
    if connection.engine.dialect.name != "sqlite":
        return (
            "non-sqlite-pool",
            connection.engine.dialect.name,
            id(connection.engine.pool),
        )
    try:
        identity = connection.info[_SQLITE_DATABASE_IDENTITY_INFO_KEY]
    except (KeyError, TypeError) as error:
        raise DatabaseIdentityError(
            "SQLite connection database identity is missing"
        ) from error
    return _validated_database_identity(identity)


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
    """Run Alembic safely within this process and for writable SQLite files.

    Alembic's context proxies require the thread lock for every backend. A portable
    process lock is additionally safe for writable file-backed SQLite databases;
    memory SQLite and server databases require coordination owned by their caller.
    """
    with migration_lock(url):
        operation(_alembic_config(url), revision)


@contextmanager
def migration_lock(
    url: str,
    *,
    timeout_seconds: float = _MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the same process/file lock used by every migration operation."""
    if timeout_seconds < 0:
        raise ValueError("migration lock timeout must be nonnegative")
    parsed_url = make_url(url)
    lock_path: Path | None = None
    if (
        parsed_url.get_backend_name() == "sqlite"
        and not _sqlite_is_memory(parsed_url)
        and not _sqlite_is_read_only(parsed_url)
    ):
        _prepare_sqlite_file(parsed_url)
        database_path = _sqlite_database_path(parsed_url)
        lock_path = database_path.with_name(f"{database_path.name}.migrate.lock")

    acquired = _MIGRATION_THREAD_LOCK.acquire(timeout=timeout_seconds)
    if not acquired:
        raise FileLockTimeout(str(lock_path or url))
    try:
        if lock_path is None:
            yield
            return
        with FileLock(lock_path, timeout=timeout_seconds):
            yield
    finally:
        _MIGRATION_THREAD_LOCK.release()


def migrate(url: str, revision: str = "head") -> None:
    """Upgrade the configured database to an Alembic revision."""
    _run_alembic_command(command.upgrade, url, revision)


def migration_head_revision() -> str:
    """Return the single packaged Alembic head required by this application."""
    config = Config(str(_alembic_config_path()))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError("Stock Desk requires exactly one Alembic head")
    return heads[0]


def downgrade(url: str, revision: str = "base") -> None:
    """Downgrade the configured database to an Alembic revision."""
    _run_alembic_command(command.downgrade, url, revision)
