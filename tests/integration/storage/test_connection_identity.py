from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from stock_desk.storage.database import _configure_sqlite_connection


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class _DbapiConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.test_cursor = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.test_cursor


class _ConnectionRecord:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(0, "main", ""), (1, "main", "")],
        [(0, "main")],
        [(0, "main", 7)],
    ],
    ids=("missing-main", "multiple-main", "missing-file", "invalid-file"),
)
def test_connect_policy_rejects_malformed_database_identity_and_closes_cursor(
    rows: list[tuple[Any, ...]],
) -> None:
    dbapi_connection = _DbapiConnection(rows)
    connection_record = _ConnectionRecord()

    with pytest.raises(RuntimeError, match="database identity"):
        _configure_sqlite_connection(
            cast(DBAPIConnection, dbapi_connection),
            cast(ConnectionPoolEntry, connection_record),
        )

    assert dbapi_connection.test_cursor.closed is True
    assert "stock_desk.sqlite_database_identity" not in connection_record.info
