from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, cast

from sqlalchemy import Connection, Table, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.sql.selectable import FromClause

from stock_desk.analysis.models import (
    AnalysisAttemptRow,
    AnalysisReportRow,
    AnalysisRunRow,
    AnalysisStageRow,
)
from stock_desk.analysis.report import (
    ResearchReport,
    clean_research_report_active_secrets,
)
from stock_desk.backtest.models import (
    BacktestAggregateMetricRow,
    BacktestFailureRow,
    BacktestGroupMetricRow,
    BacktestLogRow,
    BacktestOrderEventRow,
    BacktestRunRow,
    BacktestSymbolRow,
    BacktestTradeRow,
)
from stock_desk.config import Settings
from stock_desk.security.redaction import (
    LogSecretLease,
    SecretRedactor,
    scoped_log_redaction,
)
from stock_desk.security.secrets import SecretStore
from stock_desk.storage.database import create_engine_for_url
from stock_desk.storage.models import (
    AnalysisModelConfig,
    AppSetting,
    TaskEvent,
    TaskRun,
)


_TRIGGER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET_KEY_PREFIX = "secret."


@dataclass(frozen=True, slots=True)
class _Surface:
    table: Table
    fields: tuple[str, ...]
    hashes: Mapping[str, str]
    run_id_field: str | None = None


def _table(value: FromClause) -> Table:
    return cast(Table, value)


_SURFACES = (
    _Surface(
        _table(TaskRun.__table__),
        ("payload_json", "result_json", "error_json"),
        {},
    ),
    _Surface(_table(TaskEvent.__table__), ("detail_json",), {}),
    _Surface(_table(AnalysisRunRow.__table__), ("error_json",), {}),
    _Surface(
        _table(AnalysisStageRow.__table__),
        ("output_json", "trace_json"),
        {"output_json": "output_hash", "trace_json": "trace_hash"},
    ),
    _Surface(_table(AnalysisAttemptRow.__table__), ("error_json", "usage_json"), {}),
    _Surface(_table(BacktestRunRow.__table__), ("snapshot_json",), {}, "id"),
    _Surface(_table(BacktestSymbolRow.__table__), ("reference_json",), {}, "run_id"),
    _Surface(_table(BacktestTradeRow.__table__), ("payload_json",), {}, "run_id"),
    _Surface(_table(BacktestOrderEventRow.__table__), ("payload_json",), {}, "run_id"),
    _Surface(
        _table(BacktestFailureRow.__table__),
        ("reason", "detail_json"),
        {},
        "run_id",
    ),
    _Surface(
        _table(BacktestLogRow.__table__),
        ("message", "detail_json"),
        {},
        "run_id",
    ),
    _Surface(
        _table(BacktestAggregateMetricRow.__table__),
        ("payload_json",),
        {},
        "run_id",
    ),
    _Surface(
        _table(BacktestGroupMetricRow.__table__),
        ("payload_json",),
        {},
        "run_id",
    ),
)


def scrub_persisted_secrets_in_transaction(
    connection: Connection,
    secrets: tuple[str, ...],
) -> None:
    """Remove known plaintext credentials from persisted public output surfaces."""
    normalized = tuple(dict.fromkeys(value for value in secrets if value))
    if not normalized:
        return
    redactor = SecretRedactor(normalized)
    target_tables = frozenset(
        (
            *[surface.table.name for surface in _SURFACES],
            AnalysisReportRow.__tablename__,
        )
    )
    changed_backtests: set[str] = set()
    with _suspended_triggers(connection, target_tables):
        for surface in _SURFACES:
            changed_backtests.update(_scrub_surface(connection, surface, redactor))
        _scrub_analysis_reports(connection, normalized)
        _refresh_backtest_hashes(connection, changed_backtests)


def _scrub_surface(
    connection: Connection,
    surface: _Surface,
    redactor: SecretRedactor,
) -> set[str]:
    primary_keys = tuple(surface.table.primary_key.columns)
    selected = (*primary_keys, *(surface.table.c[name] for name in surface.fields))
    changed_runs: set[str] = set()
    for row in connection.execute(select(*selected)).mappings():
        values: dict[str, object] = {}
        for field_name in surface.fields:
            current = row[field_name]
            if current is None:
                continue
            cleaned = redactor.clean(current)
            if cleaned == current:
                continue
            values[field_name] = cleaned
            hash_field = surface.hashes.get(field_name)
            if hash_field is not None:
                if type(cleaned) is not str:
                    raise ValueError("hashed persisted output is invalid")
                values[hash_field] = _content_hash(cleaned)
        if not values:
            continue
        predicate = tuple(column == row[column.name] for column in primary_keys)
        connection.execute(update(surface.table).where(*predicate).values(**values))
        if surface.run_id_field is not None:
            run_id = row[surface.run_id_field]
            if type(run_id) is str:
                changed_runs.add(run_id)
    return changed_runs


def _scrub_analysis_reports(
    connection: Connection,
    secrets: tuple[str, ...],
) -> None:
    rows = tuple(
        connection.execute(
            select(
                AnalysisReportRow.run_id,
                AnalysisReportRow.report_id,
                AnalysisReportRow.report_json,
            )
        )
    )
    with scoped_log_redaction(*secrets):
        for run_id, old_report_id, encoded in rows:
            if type(encoded) is not str or not any(
                secret in encoded for secret in secrets
            ):
                continue
            report = ResearchReport.model_validate_json(encoded)
            cleaned = clean_research_report_active_secrets(report)
            cleaned_json = cleaned.model_dump_json()
            connection.execute(
                update(AnalysisReportRow)
                .where(AnalysisReportRow.run_id == run_id)
                .values(
                    report_id=cleaned.report_id,
                    report_json=cleaned_json,
                    report_hash=_content_hash(cleaned_json),
                )
            )
            _replace_task_report_reference(
                connection,
                run_id=str(run_id),
                old_report_id=str(old_report_id),
                new_report_id=cleaned.report_id,
            )


def _replace_task_report_reference(
    connection: Connection,
    *,
    run_id: str,
    old_report_id: str,
    new_report_id: str,
) -> None:
    task_id = connection.execute(
        select(AnalysisRunRow.task_id).where(AnalysisRunRow.id == run_id)
    ).scalar_one_or_none()
    if type(task_id) is not str:
        return
    result = connection.execute(
        select(TaskRun.result_json).where(TaskRun.id == task_id)
    ).scalar_one_or_none()
    replaced = _replace_exact(result, old_report_id, new_report_id)
    if replaced != result:
        connection.execute(
            update(TaskRun).where(TaskRun.id == task_id).values(result_json=replaced)
        )


def _replace_exact(value: Any, old: str, new: str) -> Any:
    if type(value) is str:
        return new if value == old else value
    if type(value) is list:
        return [_replace_exact(item, old, new) for item in value]
    if type(value) is dict:
        return {key: _replace_exact(item, old, new) for key, item in value.items()}
    return value


def _refresh_backtest_hashes(
    connection: Connection,
    run_ids: set[str],
) -> None:
    if not run_ids:
        return
    from stock_desk.backtest.repository import BacktestRepository

    rows = connection.execute(
        select(
            BacktestRunRow.id,
            BacktestRunRow.result_hash,
            BacktestRunRow.actual_warmup_start,
        ).where(BacktestRunRow.id.in_(run_ids))
    )
    for run_id, result_hash, actual_warmup_start in rows:
        if result_hash is None:
            continue
        refreshed = BacktestRepository._result_hash(
            connection,
            str(run_id),
            actual_warmup_start=actual_warmup_start,
        )
        connection.execute(
            update(BacktestRunRow)
            .where(BacktestRunRow.id == run_id)
            .values(result_hash=refreshed)
        )


@contextmanager
def _suspended_triggers(
    connection: Connection,
    table_names: frozenset[str],
) -> Iterator[None]:
    triggers: list[tuple[str, str]] = []
    for name, table_name, sql in connection.exec_driver_sql(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
    ):
        if table_name not in table_names:
            continue
        if (
            type(name) is not str
            or _TRIGGER_NAME.fullmatch(name) is None
            or type(sql) is not str
            or not sql.lstrip().upper().startswith("CREATE TRIGGER")
        ):
            raise ValueError("persisted output trigger is invalid")
        triggers.append((name, sql))
    try:
        for name, _sql in triggers:
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        yield
    finally:
        for _name, sql in triggers:
            connection.exec_driver_sql(sql)


def _content_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class StartupSecretHydrator:
    """Best-effort startup lease for readable credentials in an existing database."""

    def __init__(self) -> None:
        self._lease = LogSecretLease()

    @classmethod
    def open(cls, settings: Settings) -> StartupSecretHydrator:
        hydrator = cls()
        try:
            url = make_url(settings.database_url)
            database = url.database
            if (
                url.get_backend_name() != "sqlite"
                or database is None
                or database in {"", ":memory:"}
                or not Path(database).exists()
            ):
                return hydrator
            engine = create_engine_for_url(settings.database_url)
            try:
                store = SecretStore(engine, settings)
                with engine.begin() as connection:
                    references = tuple(
                        dict.fromkeys(
                            str(reference)
                            for reference in connection.execute(
                                select(AnalysisModelConfig.secret_reference_id).where(
                                    AnalysisModelConfig.secret_reference_id.is_not(None)
                                )
                            ).scalars()
                        )
                    )
                    tushare_key = f"{_SECRET_KEY_PREFIX}tushare_token"
                    has_tushare = (
                        connection.execute(
                            select(AppSetting.key).where(AppSetting.key == tushare_key)
                        ).scalar_one_or_none()
                        is not None
                    )
                    names = tuple(
                        dict.fromkeys(
                            (*references, *(("tushare_token",) if has_tushare else ()))
                        )
                    )
                    values = tuple(
                        store.read_secret_for_server_call_in_transaction(
                            name, connection
                        )
                        for name in names
                    )
                    hydrator._lease.replace(*values)
                    scrub_persisted_secrets_in_transaction(connection, values)
            finally:
                engine.dispose()
        except Exception:
            return hydrator
        return hydrator

    def close(self) -> None:
        self._lease.close()
