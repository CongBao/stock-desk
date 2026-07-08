from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, and_, func, insert, or_, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.models import (
    AnalysisAttemptRow,
    AnalysisReportRow,
    AnalysisRunRow,
    AnalysisStageRow,
)
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    DEEPSEEK_BASE_URL,
    ModelProviderKind,
    OLLAMA_BASE_URL,
)
from stock_desk.analysis.evidence import EvidenceGraph
from stock_desk.analysis.report import (
    ReportStatus,
    ResearchReport,
    clean_research_report_active_secrets,
)
from stock_desk.analysis.retry import RetryDecision, RetryPolicy
from stock_desk.analysis.roles import RoleOutput, clean_role_output_active_secrets
from stock_desk.analysis.snapshot import ResearchSnapshot
from stock_desk.analysis.snapshot import MissingResearchSection, ResearchSection
from stock_desk.analysis.workflow import WorkflowStageTrace
from stock_desk.tasks.models import TaskClaim, TaskSnapshot
from stock_desk.tasks.repository import TaskConflict, TaskRepository
from stock_desk.storage.models import TaskRun


class AnalysisRepositoryError(RuntimeError):
    pass


class AnalysisConflict(AnalysisRepositoryError):
    pass


class AnalysisNotFound(AnalysisRepositoryError):
    pass


class AnalysisRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalysisStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    REUSED = "reused"
    CANCELLED = "cancelled"


class AnalysisAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AnalysisRunSnapshot:
    id: str
    task_id: str
    parent_run_id: str | None
    requested_stage: str | None
    symbol: str
    model_config_id: str
    model_provider: str
    model_name: str
    config_fingerprint: str
    status: AnalysisRunStatus
    current_stage: str | None
    snapshot_id: str | None
    report_id: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(
            0.0,
            (self.finished_at - self.started_at).total_seconds() * 1_000.0,
        )


@dataclass(frozen=True, slots=True)
class AnalysisStageSnapshot:
    run_id: str
    role: str
    ordinal: int
    status: AnalysisStageStatus
    source_run_id: str | None
    failure_code: str | None
    retryable: bool | None
    attempt_count: int
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(
            0.0,
            (self.finished_at - self.started_at).total_seconds() * 1_000.0,
        )


@dataclass(frozen=True, slots=True)
class AnalysisHistoryKey:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class AnalysisHistoryPage:
    items: tuple[AnalysisOverviewSnapshot, ...]
    next_key: AnalysisHistoryKey | None


@dataclass(frozen=True, slots=True)
class AnalysisOverviewSnapshot:
    run: AnalysisRunSnapshot
    task: TaskSnapshot

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def symbol(self) -> str:
        return self.run.symbol

    @property
    def created_at(self) -> datetime:
        return self.run.created_at


@dataclass(frozen=True, slots=True)
class AnalysisDetailSnapshot:
    run: AnalysisRunSnapshot
    task: TaskSnapshot
    stages: tuple[AnalysisStageSnapshot, ...]


@dataclass(frozen=True, slots=True)
class AnalysisAttemptSnapshot:
    run_id: str
    role: str
    attempt_no: int
    status: AnalysisAttemptStatus
    request_hash: str | None
    safe_error: Mapping[str, object] | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class AnalysisStageArtifact:
    output: RoleOutput
    trace: WorkflowStageTrace


@dataclass(frozen=True, slots=True)
class AnalysisExecutionConfig:
    model_config_id: str
    provider: str
    model: str
    config_fingerprint: str
    retry_policy: RetryPolicy
    public_config: AnalysisModelPublicConfig


@dataclass(frozen=True, slots=True)
class EnqueuedAnalysisRun:
    task: TaskSnapshot
    run: AnalysisRunSnapshot


_STAGES = (
    ("market", -4),
    ("fundamentals", -3),
    ("announcements", -2),
    ("news", -1),
    ("technical", 0),
    ("fundamental_news", 1),
    ("bull", 2),
    ("bear", 3),
    ("risk_decision", 4),
)
_DATA_STAGE_ROLES = frozenset({"market", "fundamentals", "announcements", "news"})

_RETRY_CLOSURES = {
    "technical": frozenset({"technical", "bull", "bear", "risk_decision"}),
    "fundamental_news": frozenset(
        {"fundamental_news", "bull", "bear", "risk_decision"}
    ),
    "bull": frozenset({"bull", "risk_decision"}),
    "bear": frozenset({"bear", "risk_decision"}),
    "risk_decision": frozenset({"risk_decision"}),
}

_TERMINAL_STAGE_STATUSES = frozenset(
    status.value
    for status in AnalysisStageStatus
    if status not in {AnalysisStageStatus.PENDING, AnalysisStageStatus.RUNNING}
)


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AnalysisRepositoryError("analysis timestamp is invalid")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_hash(encoded: str) -> str:
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _validate_reusable_stage(row: RowMapping, role: str) -> None:
    status = cast(str, row["status"])
    output_json = row["output_json"]
    output_hash = row["output_hash"]
    if not isinstance(output_json, str) or output_hash != _content_hash(output_json):
        raise AnalysisConflict("analysis retry source artifact is corrupted")
    try:
        if status == AnalysisStageStatus.FAILED.value and role in _DATA_STAGE_ROLES:
            missing = MissingResearchSection.model_validate_json(output_json)
            if missing.kind.value != role:
                raise ValueError
            return
        if status != AnalysisStageStatus.SUCCEEDED.value:
            raise ValueError
        trace_json = row["trace_json"]
        if not isinstance(trace_json, str) or row["trace_hash"] != _content_hash(
            trace_json
        ):
            raise ValueError
        if role in _DATA_STAGE_ROLES:
            if ResearchSection.model_validate_json(output_json).kind.value != role:
                raise ValueError
        else:
            if RoleOutput.model_validate_json(output_json).role.value != role:
                raise ValueError
            if WorkflowStageTrace.model_validate_json(trace_json).role.value != role:
                raise ValueError
    except ValueError:
        raise AnalysisConflict("analysis retry source artifact is corrupted") from None


def _run_values(
    *,
    run_id: str,
    task_id: str,
    symbol: str,
    retry_policy: RetryPolicy,
    model_config_id: str | None,
    model_provider: str,
    model_name: str,
    model_public_config: AnalysisModelPublicConfig | None,
    now: datetime,
    parent_run_id: str | None = None,
    requested_stage: str | None = None,
    inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    policy_json = retry_policy.model_dump_json()
    if model_public_config is None:
        try:
            provider_kind = ModelProviderKind(model_provider)
        except ValueError:
            raise AnalysisConflict("analysis model provider is unsupported") from None
        base_url = {
            ModelProviderKind.DEEPSEEK: DEEPSEEK_BASE_URL,
            ModelProviderKind.OLLAMA: OLLAMA_BASE_URL,
            ModelProviderKind.OPENAI_COMPATIBLE: "https://models.example.com/v1",
        }[provider_kind]
        public_config = AnalysisModelPublicConfig(
            provider=provider_kind,
            base_url=base_url,
            model=model_name,
            temperature=0.1,
            timeout_seconds=90.0,
            max_output_tokens=4096,
        )
    else:
        public_config = model_public_config
    if public_config.provider != model_provider or public_config.model != model_name:
        raise AnalysisConflict("analysis model config identity is inconsistent")
    model_config_json = _canonical_json(public_config.model_dump(mode="json"))
    model_config_hash = _content_hash(model_config_json)
    effective_model_config_id = model_config_id or model_config_hash
    if effective_model_config_id != model_config_hash:
        raise AnalysisConflict("analysis model config id is inconsistent")
    config_json = _canonical_json(
        {
            "model_config_id": effective_model_config_id,
            "model_config_hash": model_config_hash,
            "model_name": model_name,
            "model_provider": model_provider,
            "retry_policy": retry_policy.model_dump(mode="json"),
            "symbol": symbol,
        }
    )
    values: dict[str, object] = {
        "id": run_id,
        "task_id": task_id,
        "parent_run_id": parent_run_id,
        "requested_stage": requested_stage,
        "symbol": symbol,
        "model_config_id": effective_model_config_id,
        "model_provider": model_provider,
        "model_name": model_name,
        "model_config_json": model_config_json,
        "model_config_hash": model_config_hash,
        "status": AnalysisRunStatus.QUEUED.value,
        "current_stage": _STAGES[0][0],
        "error_json": None,
        "config_fingerprint": _content_hash(config_json),
        "snapshot_id": None,
        "snapshot_json": None,
        "snapshot_hash": None,
        "evidence_graph_json": None,
        "evidence_graph_hash": None,
        "retry_policy_json": policy_json,
        "retry_policy_hash": _content_hash(policy_json),
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    if inputs is not None:
        values.update(inputs)
    return values


def _pending_stage_values(run_id: str, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "run_id": run_id,
            "role": role,
            "ordinal": ordinal,
            "status": AnalysisStageStatus.PENDING.value,
            "source_run_id": None,
            "source_role": None,
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        for role, ordinal in _STAGES
    ]


def _stage_progress(
    stages: Iterable[Mapping[str, object] | RowMapping],
) -> tuple[float, str | None]:
    ordered = sorted(stages, key=lambda stage: cast(int, stage["ordinal"]))
    terminal_count = sum(
        cast(str, stage["status"]) in _TERMINAL_STAGE_STATUSES for stage in ordered
    )
    current_stage = next(
        (
            cast(str, stage["role"])
            for stage in ordered
            if cast(str, stage["status"]) not in _TERMINAL_STAGE_STATUSES
        ),
        None,
    )
    return terminal_count / len(_STAGES), current_stage


def _run_snapshot(row: RowMapping) -> AnalysisRunSnapshot:
    failure_code: str | None = None
    raw_error = row["error_json"]
    if raw_error is not None:
        try:
            decoded = json.loads(cast(str, raw_error))
        except (TypeError, ValueError):
            raise AnalysisRepositoryError("analysis failure is invalid") from None
        candidate = decoded.get("code") if type(decoded) is dict else None
        if (
            type(candidate) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate) is None
        ):
            raise AnalysisRepositoryError("analysis failure is invalid")
        failure_code = candidate
    return AnalysisRunSnapshot(
        id=cast(str, row["id"]),
        task_id=cast(str, row["task_id"]),
        parent_run_id=cast(str | None, row["parent_run_id"]),
        requested_stage=cast(str | None, row["requested_stage"]),
        symbol=cast(str, row["symbol"]),
        model_config_id=cast(str, row["model_config_id"]),
        model_provider=cast(str, row["model_provider"]),
        model_name=cast(str, row["model_name"]),
        config_fingerprint=cast(str, row["config_fingerprint"]),
        status=AnalysisRunStatus(cast(str, row["status"])),
        current_stage=cast(str | None, row["current_stage"]),
        snapshot_id=cast(str | None, row["snapshot_id"]),
        report_id=cast(str | None, row.get("_report_id")),
        failure_code=failure_code,
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
        started_at=_utc(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=(
            _utc(row["finished_at"]) if row["finished_at"] is not None else None
        ),
    )


def _stage_snapshot(
    row: RowMapping | Mapping[str, object],
) -> AnalysisStageSnapshot:
    return AnalysisStageSnapshot(
        run_id=cast(str, row["run_id"]),
        role=cast(str, row["role"]),
        ordinal=cast(int, row["ordinal"]),
        status=AnalysisStageStatus(cast(str, row["status"])),
        source_run_id=cast(str | None, row["source_run_id"]),
        failure_code=cast(str | None, row["failure_code"]),
        retryable=cast(bool | None, row["retryable"]),
        attempt_count=cast(int, row["attempt_count"]),
        started_at=(_utc(row["started_at"]) if row["started_at"] is not None else None),
        finished_at=(
            _utc(row["finished_at"]) if row["finished_at"] is not None else None
        ),
    )


def _attempt_snapshot(row: RowMapping) -> AnalysisAttemptSnapshot:
    safe_error = row["error_json"]
    decoded = (
        MappingProxyType(cast(dict[str, object], json.loads(safe_error)))
        if isinstance(safe_error, str)
        else None
    )
    return AnalysisAttemptSnapshot(
        run_id=cast(str, row["run_id"]),
        role=cast(str, row["role"]),
        attempt_no=cast(int, row["attempt_no"]),
        status=AnalysisAttemptStatus(cast(str, row["status"])),
        request_hash=cast(str | None, row["request_hash"]),
        safe_error=decoded,
        started_at=_utc(row["started_at"]),
        finished_at=(
            _utc(row["finished_at"]) if row["finished_at"] is not None else None
        ),
    )


_TASK_PROJECTION_FIELDS = (
    "id",
    "kind",
    "status",
    "progress",
    "payload_json",
    "result_json",
    "error_json",
    "cancel_requested",
    "worker_id",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)
_RUN_PROJECTION_FIELDS = (
    "id",
    "task_id",
    "parent_run_id",
    "requested_stage",
    "symbol",
    "model_config_id",
    "model_provider",
    "model_name",
    "status",
    "current_stage",
    "error_json",
    "config_fingerprint",
    "snapshot_id",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)
_STAGE_PROJECTION_FIELDS = (
    "run_id",
    "role",
    "ordinal",
    "status",
    "source_run_id",
    "failure_code",
    "retryable",
    "attempt_count",
    "started_at",
    "finished_at",
)


def _run_projection_columns() -> tuple[Any, ...]:
    return tuple(
        AnalysisRunRow.__table__.columns[field] for field in _RUN_PROJECTION_FIELDS
    )


def _task_projection_columns() -> tuple[Any, ...]:
    return tuple(
        TaskRun.__table__.columns[field].label(f"_task_{field}")
        for field in _TASK_PROJECTION_FIELDS
    )


def _stage_projection_columns() -> tuple[Any, ...]:
    return tuple(
        AnalysisStageRow.__table__.columns[field].label(f"_stage_{field}")
        for field in _STAGE_PROJECTION_FIELDS
    )


def _task_projection(row: RowMapping, tasks: TaskRepository) -> TaskSnapshot:
    if row["_task_id"] is None:
        raise AnalysisRepositoryError("analysis task projection is missing")
    return tasks.snapshot_from_mapping(
        {field: row[f"_task_{field}"] for field in _TASK_PROJECTION_FIELDS}
    )


def _stage_projection(row: RowMapping) -> AnalysisStageSnapshot:
    if row["_stage_run_id"] is None:
        raise AnalysisRepositoryError("analysis stage projection is missing")
    return _stage_snapshot(
        {field: row[f"_stage_{field}"] for field in _STAGE_PROJECTION_FIELDS}
    )


class AnalysisRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._tasks = TaskRepository(engine)

    @property
    def database_identity(self) -> object:
        return self._tasks.database_identity

    def close(self) -> None:
        self._engine.dispose()

    def cancellation_requested(self, claim: TaskClaim) -> bool:
        task = self._tasks.get(claim.snapshot.id)
        return task.cancel_requested or task.status == "cancelled"

    def heartbeat(
        self,
        claim: TaskClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        try:
            self._tasks.heartbeat(
                claim.snapshot.id,
                claim.claim_token,
                now=now,
                lease_duration=lease_duration,
            )
        except TaskConflict:
            raise AnalysisConflict("analysis worker claim is not current") from None

    def cancel_run(
        self,
        claim: TaskClaim,
        run_id: str,
        *,
        now: datetime,
    ) -> AnalysisRunSnapshot:
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            connection.execute(
                update(AnalysisAttemptRow)
                .where(
                    AnalysisAttemptRow.run_id == run_id,
                    AnalysisAttemptRow.status == AnalysisAttemptStatus.RUNNING.value,
                )
                .values(
                    status=AnalysisAttemptStatus.CANCELLED.value,
                    error_json=None,
                    retryable=None,
                    backoff_seconds=None,
                    finished_at=now,
                )
            )
            connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.status.in_(
                        (
                            AnalysisStageStatus.PENDING.value,
                            AnalysisStageStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=AnalysisStageStatus.CANCELLED.value,
                    failure_code=None,
                    retryable=None,
                    updated_at=now,
                    finished_at=now,
                )
            )
            row = (
                connection.execute(
                    update(AnalysisRunRow)
                    .where(
                        AnalysisRunRow.id == run_id,
                        AnalysisRunRow.task_id == claim.snapshot.id,
                        AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
                    )
                    .values(
                        status=AnalysisRunStatus.CANCELLED.value,
                        current_stage=None,
                        updated_at=now,
                        finished_at=now,
                    )
                    .returning(AnalysisRunRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis run cannot be cancelled")
            self._tasks.complete_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {},
                now=now,
            )
        return _run_snapshot(row)

    def fail_run(
        self,
        claim: TaskClaim,
        run_id: str,
        *,
        code: str,
        safe_message: str,
        now: datetime,
    ) -> AnalysisRunSnapshot:
        safe_error = _canonical_json({"code": code, "message": safe_message})
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            connection.execute(
                update(AnalysisAttemptRow)
                .where(
                    AnalysisAttemptRow.run_id == run_id,
                    AnalysisAttemptRow.status == AnalysisAttemptStatus.RUNNING.value,
                )
                .values(
                    status=AnalysisAttemptStatus.INTERRUPTED.value,
                    error_json=_canonical_json(
                        {
                            "code": "worker_interrupted",
                            "message": "analysis worker was interrupted",
                            "retryable": True,
                        }
                    ),
                    retryable=True,
                    finished_at=now,
                )
            )
            connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.status.in_(
                        (
                            AnalysisStageStatus.PENDING.value,
                            AnalysisStageStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=AnalysisStageStatus.CANCELLED.value,
                    failure_code=None,
                    retryable=None,
                    updated_at=now,
                    finished_at=now,
                )
            )
            row = (
                connection.execute(
                    update(AnalysisRunRow)
                    .where(
                        AnalysisRunRow.id == run_id,
                        AnalysisRunRow.task_id == claim.snapshot.id,
                        AnalysisRunRow.status.in_(
                            (
                                AnalysisRunStatus.QUEUED.value,
                                AnalysisRunStatus.RUNNING.value,
                            )
                        ),
                    )
                    .values(
                        status=AnalysisRunStatus.FAILED.value,
                        current_stage=None,
                        error_json=safe_error,
                        started_at=func.coalesce(AnalysisRunRow.started_at, now),
                        updated_at=now,
                        finished_at=now,
                    )
                    .returning(AnalysisRunRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis run cannot be failed")
            self._tasks.fail_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {"code": code, "message": safe_message},
                now=now,
            )
        return _run_snapshot(row)

    def _create_run_for_existing_task(
        self,
        *,
        task_id: str,
        symbol: str,
        retry_policy: RetryPolicy,
        now: datetime,
        model_config_id: str | None = None,
        model_provider: str = ModelProviderKind.OPENAI_COMPATIBLE.value,
        model_name: str = "vendor-chat",
        model_public_config: AnalysisModelPublicConfig | None = None,
        parent_run_id: str | None = None,
        requested_stage: str | None = None,
    ) -> AnalysisRunSnapshot:
        run_id = str(uuid4())
        values = _run_values(
            run_id=run_id,
            task_id=task_id,
            symbol=symbol,
            retry_policy=retry_policy,
            model_config_id=model_config_id,
            model_provider=model_provider,
            model_name=model_name,
            model_public_config=model_public_config,
            now=now,
            parent_run_id=parent_run_id,
            requested_stage=requested_stage,
        )
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    insert(AnalysisRunRow).values(**values).returning(AnalysisRunRow)
                )
                .mappings()
                .one()
            )
            connection.execute(
                insert(AnalysisStageRow),
                _pending_stage_values(run_id, now),
            )
        return _run_snapshot(row)

    def enqueue_run(
        self,
        *,
        symbol: str,
        retry_policy: RetryPolicy,
        now: datetime,
        model_config_id: str | None = None,
        model_provider: str = ModelProviderKind.OPENAI_COMPATIBLE.value,
        model_name: str = "vendor-chat",
        model_public_config: AnalysisModelPublicConfig | None = None,
    ) -> EnqueuedAnalysisRun:
        with self._engine.begin() as connection:
            return self.enqueue_run_in_transaction(
                connection,
                symbol=symbol,
                retry_policy=retry_policy,
                now=now,
                model_config_id=model_config_id,
                model_provider=model_provider,
                model_name=model_name,
                model_public_config=model_public_config,
            )

    def enqueue_run_in_transaction(
        self,
        connection: Connection,
        *,
        symbol: str,
        retry_policy: RetryPolicy,
        now: datetime,
        model_config_id: str | None = None,
        model_provider: str = ModelProviderKind.OPENAI_COMPATIBLE.value,
        model_name: str = "vendor-chat",
        model_public_config: AnalysisModelPublicConfig | None = None,
    ) -> EnqueuedAnalysisRun:
        """Create a task, run, and stages in a caller-owned transaction."""
        task_id = str(uuid4())
        run_id = str(uuid4())
        task = self._tasks.enqueue_in_transaction(
            connection,
            "analysis.run",
            {"symbol": symbol},
            task_id=task_id,
            now=now,
        )
        row = (
            connection.execute(
                insert(AnalysisRunRow)
                .values(
                    **_run_values(
                        run_id=run_id,
                        task_id=task_id,
                        symbol=symbol,
                        retry_policy=retry_policy,
                        model_config_id=model_config_id,
                        model_provider=model_provider,
                        model_name=model_name,
                        model_public_config=model_public_config,
                        now=now,
                    )
                )
                .returning(AnalysisRunRow)
            )
            .mappings()
            .one()
        )
        connection.execute(insert(AnalysisStageRow), _pending_stage_values(run_id, now))
        return EnqueuedAnalysisRun(task=task, run=_run_snapshot(row))

    def load_execution_config(self, run_id: str) -> AnalysisExecutionConfig:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    AnalysisRunRow.symbol,
                    AnalysisRunRow.model_config_id,
                    AnalysisRunRow.model_provider,
                    AnalysisRunRow.model_name,
                    AnalysisRunRow.model_config_json,
                    AnalysisRunRow.model_config_hash,
                    AnalysisRunRow.config_fingerprint,
                    AnalysisRunRow.retry_policy_json,
                    AnalysisRunRow.retry_policy_hash,
                ).where(AnalysisRunRow.id == run_id)
            ).one_or_none()
        if row is None or not isinstance(row[7], str):
            raise AnalysisNotFound("analysis execution config was not found")
        if (
            row[8] != _content_hash(row[7])
            or row[5] != _content_hash(row[4])
            or row[1] != row[5]
        ):
            raise AnalysisRepositoryError("analysis retry policy hash is invalid")
        try:
            policy = RetryPolicy.model_validate_json(row[7])
            public_config = AnalysisModelPublicConfig.model_validate_json(row[4])
        except ValueError:
            raise AnalysisRepositoryError("analysis retry policy is invalid") from None
        expected = _content_hash(
            _canonical_json(
                {
                    "model_config_id": row[1],
                    "model_config_hash": row[5],
                    "model_name": row[3],
                    "model_provider": row[2],
                    "retry_policy": policy.model_dump(mode="json"),
                    "symbol": row[0],
                }
            )
        )
        if row[6] != expected:
            raise AnalysisRepositoryError("analysis config fingerprint is invalid")
        return AnalysisExecutionConfig(
            model_config_id=cast(str, row[1]),
            provider=cast(str, row[2]),
            model=cast(str, row[3]),
            config_fingerprint=cast(str, row[6]),
            retry_policy=policy,
            public_config=public_config,
        )

    def start_run(
        self, claim: TaskClaim, run_id: str, *, now: datetime
    ) -> AnalysisRunSnapshot:
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisRunRow)
                    .where(
                        AnalysisRunRow.id == run_id,
                        AnalysisRunRow.task_id == claim.snapshot.id,
                        AnalysisRunRow.status == AnalysisRunStatus.QUEUED.value,
                    )
                    .values(
                        status=AnalysisRunStatus.RUNNING.value,
                        started_at=now,
                        updated_at=now,
                    )
                    .returning(AnalysisRunRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis run cannot be started")
        return _run_snapshot(row)

    def get_run(self, run_id: str) -> AnalysisRunSnapshot:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        *_run_projection_columns(),
                        AnalysisReportRow.report_id.label("_report_id"),
                    )
                    .outerjoin(
                        AnalysisReportRow,
                        AnalysisReportRow.run_id == AnalysisRunRow.id,
                    )
                    .where(AnalysisRunRow.id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise AnalysisNotFound("analysis run was not found")
        return _run_snapshot(row)

    def get_run_by_task(self, task_id: str) -> AnalysisRunSnapshot:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        *_run_projection_columns(),
                        AnalysisReportRow.report_id.label("_report_id"),
                    )
                    .outerjoin(
                        AnalysisReportRow,
                        AnalysisReportRow.run_id == AnalysisRunRow.id,
                    )
                    .where(AnalysisRunRow.task_id == task_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise AnalysisNotFound("analysis run was not found")
        return _run_snapshot(row)

    def get_detail(self, run_id: str) -> AnalysisDetailSnapshot:
        statement = (
            select(
                *_run_projection_columns(),
                AnalysisReportRow.report_id.label("_report_id"),
                *_task_projection_columns(),
                *_stage_projection_columns(),
            )
            .outerjoin(TaskRun, TaskRun.id == AnalysisRunRow.task_id)
            .outerjoin(AnalysisReportRow, AnalysisReportRow.run_id == AnalysisRunRow.id)
            .outerjoin(AnalysisStageRow, AnalysisStageRow.run_id == AnalysisRunRow.id)
            .where(AnalysisRunRow.id == run_id)
            .order_by(AnalysisStageRow.ordinal)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        if not rows:
            raise AnalysisNotFound("analysis run was not found")
        run = _run_snapshot(rows[0])
        task = _task_projection(rows[0], self._tasks)
        stages = tuple(_stage_projection(row) for row in rows)
        if (
            len(stages) != len(_STAGES)
            or tuple((stage.role, stage.ordinal) for stage in stages) != _STAGES
        ):
            raise AnalysisRepositoryError("analysis stage projection is inconsistent")
        return AnalysisDetailSnapshot(run=run, task=task, stages=stages)

    def list_history_page(
        self,
        *,
        limit: int = 50,
        after: AnalysisHistoryKey | None = None,
        symbol: str | None = None,
    ) -> AnalysisHistoryPage:
        if not 1 <= limit <= 100:
            raise ValueError("analysis history limit must be between 1 and 100")
        statement = (
            select(
                *_run_projection_columns(),
                AnalysisReportRow.report_id.label("_report_id"),
                *_task_projection_columns(),
            )
            .join(TaskRun, TaskRun.id == AnalysisRunRow.task_id)
            .outerjoin(AnalysisReportRow, AnalysisReportRow.run_id == AnalysisRunRow.id)
        )
        if symbol is not None:
            statement = statement.where(AnalysisRunRow.symbol == symbol)
        if after is not None:
            after_time = _utc(after.created_at)
            statement = statement.where(
                or_(
                    AnalysisRunRow.created_at < after_time,
                    and_(
                        AnalysisRunRow.created_at == after_time,
                        AnalysisRunRow.id < after.id,
                    ),
                )
            )
        statement = statement.order_by(
            AnalysisRunRow.created_at.desc(), AnalysisRunRow.id.desc()
        ).limit(limit + 1)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        has_more = len(rows) > limit
        items = tuple(
            AnalysisOverviewSnapshot(
                run=_run_snapshot(row), task=_task_projection(row, self._tasks)
            )
            for row in rows[:limit]
        )
        next_key = (
            AnalysisHistoryKey(created_at=items[-1].run.created_at, id=items[-1].run.id)
            if has_more and items
            else None
        )
        return AnalysisHistoryPage(items=items, next_key=next_key)

    def enqueue_retry(
        self,
        parent_run_id: str,
        stage: str,
        *,
        now: datetime,
    ) -> EnqueuedAnalysisRun:
        rerun = _RETRY_CLOSURES.get(stage)
        if rerun is None:
            raise AnalysisConflict("analysis retry stage is invalid")
        task_id = str(uuid4())
        run_id = str(uuid4())
        try:
            with self._engine.begin() as connection:
                parent = (
                    connection.execute(
                        select(AnalysisRunRow).where(
                            AnalysisRunRow.id == parent_run_id,
                            AnalysisRunRow.status == AnalysisRunStatus.PARTIAL.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                failed = connection.execute(
                    select(AnalysisStageRow.role).where(
                        AnalysisStageRow.run_id == parent_run_id,
                        AnalysisStageRow.role == stage,
                        AnalysisStageRow.status == AnalysisStageStatus.FAILED.value,
                    )
                ).scalar_one_or_none()
                if parent is None or failed is None or parent["snapshot_id"] is None:
                    raise AnalysisConflict("analysis stage is not eligible for retry")
                if (
                    parent["snapshot_hash"]
                    != _content_hash(cast(str, parent["snapshot_json"]))
                    or parent["evidence_graph_hash"]
                    != _content_hash(cast(str, parent["evidence_graph_json"]))
                    or parent["retry_policy_hash"]
                    != _content_hash(cast(str, parent["retry_policy_json"]))
                    or parent["model_config_hash"]
                    != _content_hash(cast(str, parent["model_config_json"]))
                    or parent["model_config_id"] != parent["model_config_hash"]
                ):
                    raise AnalysisConflict("analysis retry parent is corrupted")
                policy = RetryPolicy.model_validate_json(
                    cast(str, parent["retry_policy_json"])
                )
                public_config = AnalysisModelPublicConfig.model_validate_json(
                    cast(str, parent["model_config_json"])
                )
                expected_fingerprint = _content_hash(
                    _canonical_json(
                        {
                            "model_config_id": parent["model_config_id"],
                            "model_config_hash": parent["model_config_hash"],
                            "model_name": parent["model_name"],
                            "model_provider": parent["model_provider"],
                            "retry_policy": policy.model_dump(mode="json"),
                            "symbol": parent["symbol"],
                        }
                    )
                )
                if parent["config_fingerprint"] != expected_fingerprint:
                    raise AnalysisConflict("analysis retry parent is corrupted")
                parent_stages = {
                    cast(str, item["role"]): item
                    for item in connection.execute(
                        select(AnalysisStageRow).where(
                            AnalysisStageRow.run_id == parent_run_id
                        )
                    ).mappings()
                }
                rerun = frozenset().union(
                    *(
                        _RETRY_CLOSURES[role]
                        for role, parent_stage in parent_stages.items()
                        if role in _RETRY_CLOSURES
                        and parent_stage["status"] == AnalysisStageStatus.FAILED.value
                    )
                )
                stage_values = _pending_stage_values(run_id, now)
                by_role = {cast(str, item["role"]): item for item in stage_values}
                for role, parent_stage in parent_stages.items():
                    if role in rerun:
                        continue
                    is_data = role in _DATA_STAGE_ROLES
                    status = cast(str, parent_stage["status"])
                    if is_data and status == AnalysisStageStatus.FAILED.value:
                        source_run_id = parent_run_id
                    elif status in {
                        AnalysisStageStatus.SUCCEEDED.value,
                        AnalysisStageStatus.REUSED.value,
                    }:
                        source_run_id = (
                            cast(str, parent_stage["source_run_id"])
                            if status == AnalysisStageStatus.REUSED.value
                            else parent_run_id
                        )
                    else:
                        raise AnalysisConflict(
                            "analysis retry dependencies are incomplete"
                        )
                    source_stage = (
                        connection.execute(
                            select(AnalysisStageRow).where(
                                AnalysisStageRow.run_id == source_run_id,
                                AnalysisStageRow.role == role,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if source_stage is None:
                        raise AnalysisConflict(
                            "analysis retry source artifact was not found"
                        )
                    source_identity = connection.execute(
                        select(
                            AnalysisRunRow.snapshot_hash,
                            AnalysisRunRow.evidence_graph_hash,
                            AnalysisRunRow.model_config_hash,
                            AnalysisRunRow.retry_policy_hash,
                            AnalysisRunRow.config_fingerprint,
                        ).where(AnalysisRunRow.id == source_run_id)
                    ).one_or_none()
                    expected_identity = (
                        parent["snapshot_hash"],
                        parent["evidence_graph_hash"],
                        parent["model_config_hash"],
                        parent["retry_policy_hash"],
                        parent["config_fingerprint"],
                    )
                    if (
                        source_identity is None
                        or tuple(source_identity) != expected_identity
                    ):
                        raise AnalysisConflict(
                            "analysis retry source configuration is inconsistent"
                        )
                    _validate_reusable_stage(source_stage, role)
                    by_role[role].update(
                        {
                            "status": AnalysisStageStatus.REUSED.value,
                            "source_run_id": source_run_id,
                            "source_role": role,
                            "finished_at": now,
                        }
                    )
                initial_progress, current_stage = _stage_progress(stage_values)
                task = self._tasks.enqueue_in_transaction(
                    connection,
                    "analysis.run",
                    {
                        "parent_run_id": parent_run_id,
                        "requested_stage": stage,
                        "symbol": parent["symbol"],
                    },
                    task_id=task_id,
                    now=now,
                )
                connection.execute(
                    update(TaskRun)
                    .where(TaskRun.id == task_id)
                    .values(progress=initial_progress)
                )
                task = replace(task, progress=initial_progress)
                inputs = {
                    "snapshot_id": parent["snapshot_id"],
                    "snapshot_json": parent["snapshot_json"],
                    "snapshot_hash": parent["snapshot_hash"],
                    "evidence_graph_json": parent["evidence_graph_json"],
                    "evidence_graph_hash": parent["evidence_graph_hash"],
                }
                run_values = _run_values(
                    run_id=run_id,
                    task_id=task_id,
                    symbol=cast(str, parent["symbol"]),
                    retry_policy=policy,
                    model_config_id=cast(str, parent["model_config_id"]),
                    model_provider=cast(str, parent["model_provider"]),
                    model_name=cast(str, parent["model_name"]),
                    model_public_config=public_config,
                    now=now,
                    parent_run_id=parent_run_id,
                    requested_stage=stage,
                    inputs=inputs,
                )
                run_values["current_stage"] = current_stage
                row = (
                    connection.execute(
                        insert(AnalysisRunRow)
                        .values(**run_values)
                        .returning(AnalysisRunRow)
                    )
                    .mappings()
                    .one()
                )
                connection.execute(insert(AnalysisStageRow), stage_values)
            return EnqueuedAnalysisRun(task=task, run=_run_snapshot(row))
        except IntegrityError:
            raise AnalysisConflict("analysis stage retry is already active") from None

    def bind_inputs(
        self,
        claim: TaskClaim,
        run_id: str,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
        *,
        now: datetime,
    ) -> None:
        frozen_snapshot = ResearchSnapshot.model_validate_json(
            snapshot.model_dump_json(by_alias=True)
        )
        frozen_graph = EvidenceGraph.model_validate_json(
            evidence_graph.model_dump_json(by_alias=True)
        )
        if (
            frozen_graph.snapshot.canonical_json_bytes()
            != frozen_snapshot.canonical_json_bytes()
        ):
            raise AnalysisConflict("analysis inputs do not share one snapshot")
        snapshot_json = frozen_snapshot.canonical_json_bytes().decode("utf-8")
        graph_json = _canonical_json(
            frozen_graph.model_dump(mode="json", by_alias=True)
        )
        section_by_kind = {
            section.kind.value: section for section in frozen_snapshot.sections
        }
        missing_by_kind = {
            section.kind.value: section for section in frozen_snapshot.missing_sections
        }
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            changed = connection.execute(
                update(AnalysisRunRow)
                .where(
                    AnalysisRunRow.id == run_id,
                    AnalysisRunRow.task_id == claim.snapshot.id,
                    AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
                    AnalysisRunRow.snapshot_id.is_(None),
                )
                .values(
                    snapshot_id=frozen_snapshot.snapshot_id,
                    snapshot_json=snapshot_json,
                    snapshot_hash=_content_hash(snapshot_json),
                    evidence_graph_json=graph_json,
                    evidence_graph_hash=_content_hash(graph_json),
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise AnalysisConflict("analysis inputs are already bound")
            for role, _ordinal in _STAGES[:4]:
                outcome = section_by_kind.get(role) or missing_by_kind.get(role)
                if outcome is None:
                    raise AnalysisConflict("analysis data outcome is incomplete")
                output_json = _canonical_json(
                    outcome.model_dump(mode="json", by_alias=True)
                )
                trace_json = _canonical_json(
                    {
                        "section_kind": role,
                        "snapshot_id": frozen_snapshot.snapshot_id,
                        "status": (
                            "available" if role in section_by_kind else "missing"
                        ),
                    }
                )
                connection.execute(
                    update(AnalysisStageRow)
                    .where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == role,
                        AnalysisStageRow.status == AnalysisStageStatus.PENDING.value,
                    )
                    .values(
                        status=AnalysisStageStatus.SUCCEEDED.value,
                        output_json=output_json,
                        output_hash=_content_hash(output_json),
                        trace_json=trace_json,
                        trace_hash=_content_hash(trace_json),
                        updated_at=now,
                        started_at=now,
                        finished_at=now,
                    )
                )
            self._checkpoint_progress(connection, claim, run_id, now)

    def resume_run(
        self, claim: TaskClaim, run_id: str, *, now: datetime
    ) -> AnalysisRunSnapshot:
        safe_error = _canonical_json(
            {
                "code": "worker_interrupted",
                "message": "analysis worker was interrupted",
                "retryable": True,
            }
        )
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            run_row = (
                connection.execute(
                    select(AnalysisRunRow).where(
                        AnalysisRunRow.id == run_id,
                        AnalysisRunRow.task_id == claim.snapshot.id,
                        AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run_row is None:
                raise AnalysisConflict("analysis run cannot be resumed")
            running = (
                connection.execute(
                    select(AnalysisAttemptRow).where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.RUNNING.value,
                    )
                )
                .mappings()
                .all()
            )
            for attempt in running:
                connection.execute(
                    update(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.role == attempt["role"],
                        AnalysisAttemptRow.attempt_no == attempt["attempt_no"],
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AnalysisAttemptStatus.INTERRUPTED.value,
                        error_json=safe_error,
                        retryable=True,
                        finished_at=now,
                    )
                )
                connection.execute(
                    update(AnalysisStageRow)
                    .where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == attempt["role"],
                        AnalysisStageRow.status == AnalysisStageStatus.RUNNING.value,
                    )
                    .values(status=AnalysisStageStatus.PENDING.value, updated_at=now)
                )
        return _run_snapshot(run_row)

    def start_attempt(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        *,
        provider: str | None,
        model: str | None,
        request_hash: str | None,
        template_version: str | None = None,
        template_hash: str | None = None,
        now: datetime,
    ) -> AnalysisAttemptSnapshot:
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            stage = (
                connection.execute(
                    select(AnalysisStageRow).where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == role,
                        AnalysisStageRow.status == AnalysisStageStatus.PENDING.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if stage is None:
                raise AnalysisConflict("analysis stage cannot start an attempt")
            attempt_no = cast(int, stage["attempt_count"]) + 1
            row = (
                connection.execute(
                    insert(AnalysisAttemptRow)
                    .values(
                        run_id=run_id,
                        role=role,
                        attempt_no=attempt_no,
                        status=AnalysisAttemptStatus.RUNNING.value,
                        provider=provider,
                        model=model,
                        request_hash=request_hash,
                        error_json=None,
                        retryable=None,
                        backoff_seconds=None,
                        template_version=template_version,
                        template_hash=template_hash,
                        usage_json=None,
                        started_at=now,
                        finished_at=None,
                    )
                    .returning(AnalysisAttemptRow)
                )
                .mappings()
                .one()
            )
            changed = connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.PENDING.value,
                )
                .values(
                    status=AnalysisStageStatus.RUNNING.value,
                    attempt_count=attempt_no,
                    started_at=now,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise AnalysisConflict("analysis stage changed concurrently")
        return _attempt_snapshot(row)

    def finish_attempt_failure(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        attempt_no: int,
        decision: RetryDecision,
        *,
        exhausted: bool,
        backoff_seconds: float | None = None,
        missing_section: MissingResearchSection | None = None,
        now: datetime,
    ) -> AnalysisAttemptSnapshot:
        safe_error_payload: dict[str, object] = {
            "code": decision.code,
            "message": decision.safe_message,
            "retryable": decision.retryable,
        }
        missing_json: str | None = None
        missing_hash: str | None = None
        if missing_section is not None:
            if missing_section.kind.value != role or not exhausted:
                raise AnalysisConflict("analysis missing data checkpoint is invalid")
            missing_json = _canonical_json(missing_section.model_dump(mode="json"))
            missing_hash = _content_hash(missing_json)
        safe_error = _canonical_json(safe_error_payload)
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.role == role,
                        AnalysisAttemptRow.attempt_no == attempt_no,
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AnalysisAttemptStatus.FAILED.value,
                        error_json=safe_error,
                        retryable=decision.retryable,
                        backoff_seconds=backoff_seconds,
                        finished_at=now,
                    )
                    .returning(AnalysisAttemptRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis attempt is not running")
            stage_status = (
                AnalysisStageStatus.FAILED.value
                if exhausted
                else AnalysisStageStatus.PENDING.value
            )
            changed = connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.RUNNING.value,
                )
                .values(
                    status=stage_status,
                    failure_code=decision.code if exhausted else None,
                    retryable=decision.retryable if exhausted else None,
                    output_json=missing_json if exhausted else None,
                    output_hash=missing_hash if exhausted else None,
                    finished_at=now if exhausted else None,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise AnalysisConflict("analysis stage is not running")
            if exhausted:
                self._checkpoint_progress(connection, claim, run_id, now)
        return _attempt_snapshot(row)

    def get_missing_section(self, run_id: str, role: str) -> MissingResearchSection:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    AnalysisStageRow.output_json,
                    AnalysisStageRow.output_hash,
                ).where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.FAILED.value,
                )
            ).one_or_none()
        if row is None or not isinstance(row[0], str):
            raise AnalysisNotFound("analysis missing data checkpoint was not found")
        if row[1] != _content_hash(row[0]):
            raise AnalysisRepositoryError(
                "analysis missing data checkpoint hash is invalid"
            )
        try:
            missing = MissingResearchSection.model_validate_json(row[0])
        except ValueError:
            raise AnalysisRepositoryError(
                "analysis missing data checkpoint is invalid"
            ) from None
        if missing.kind.value != role:
            raise AnalysisRepositoryError(
                "analysis missing data checkpoint identity is invalid"
            )
        return missing

    def finish_attempt_success(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        attempt_no: int,
        output: RoleOutput,
        trace: WorkflowStageTrace,
        *,
        now: datetime,
    ) -> RoleOutput:
        canonical_output = clean_role_output_active_secrets(
            RoleOutput.model_validate_json(output.model_dump_json())
        )
        canonical_trace = WorkflowStageTrace.model_validate_json(
            trace.model_dump_json()
        )
        if canonical_output.role.value != role or canonical_trace.role.value != role:
            raise AnalysisConflict("analysis stage artifact role is inconsistent")
        output_json = _canonical_json(canonical_output.model_dump(mode="json"))
        trace_json = _canonical_json(canonical_trace.model_dump(mode="json"))
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.role == role,
                        AnalysisAttemptRow.attempt_no == attempt_no,
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AnalysisAttemptStatus.SUCCEEDED.value,
                        usage_json=_canonical_json(
                            canonical_trace.usage.model_dump(mode="json")
                        ),
                        finished_at=now,
                    )
                    .returning(AnalysisAttemptRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis attempt is not running")
            changed = connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.RUNNING.value,
                )
                .values(
                    status=AnalysisStageStatus.SUCCEEDED.value,
                    output_json=output_json,
                    output_hash=_content_hash(output_json),
                    trace_json=trace_json,
                    trace_hash=_content_hash(trace_json),
                    updated_at=now,
                    finished_at=now,
                )
            ).rowcount
            if changed != 1:
                raise AnalysisConflict("analysis stage is not running")
            self._checkpoint_progress(connection, claim, run_id, now)
        return canonical_output

    def finish_data_attempt_success(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        attempt_no: int,
        section: ResearchSection,
        *,
        now: datetime,
    ) -> AnalysisAttemptSnapshot:
        if section.kind.value != role or role not in {
            "market",
            "fundamentals",
            "announcements",
            "news",
        }:
            raise AnalysisConflict("analysis data stage artifact is inconsistent")
        output_json = _canonical_json(section.model_dump(mode="json", by_alias=True))
        trace_json = _canonical_json(
            {
                "section_kind": role,
                "status": "available",
                "dataset_version": section.dataset_version,
            }
        )
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.role == role,
                        AnalysisAttemptRow.attempt_no == attempt_no,
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AnalysisAttemptStatus.SUCCEEDED.value,
                        finished_at=now,
                    )
                    .returning(AnalysisAttemptRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis data attempt is not running")
            changed = connection.execute(
                update(AnalysisStageRow)
                .where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.RUNNING.value,
                )
                .values(
                    status=AnalysisStageStatus.SUCCEEDED.value,
                    output_json=output_json,
                    output_hash=_content_hash(output_json),
                    trace_json=trace_json,
                    trace_hash=_content_hash(trace_json),
                    updated_at=now,
                    finished_at=now,
                )
            ).rowcount
            if changed != 1:
                raise AnalysisConflict("analysis data stage is not running")
            self._checkpoint_progress(connection, claim, run_id, now)
        return _attempt_snapshot(row)

    def block_stage(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        *,
        failure_code: str,
        now: datetime,
    ) -> AnalysisStageSnapshot:
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisStageRow)
                    .where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == role,
                        AnalysisStageRow.status == AnalysisStageStatus.PENDING.value,
                    )
                    .values(
                        status=AnalysisStageStatus.BLOCKED.value,
                        failure_code=failure_code,
                        retryable=False,
                        updated_at=now,
                        finished_at=now,
                    )
                    .returning(AnalysisStageRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis stage cannot be blocked")
            self._checkpoint_progress(connection, claim, run_id, now)
        return _stage_snapshot(row)

    def exhaust_stage(
        self,
        claim: TaskClaim,
        run_id: str,
        role: str,
        *,
        failure_code: str,
        missing_section: MissingResearchSection | None = None,
        now: datetime,
    ) -> AnalysisStageSnapshot:
        if missing_section is not None and missing_section.kind.value != role:
            raise AnalysisConflict("analysis missing data checkpoint is invalid")
        missing_json = (
            _canonical_json(missing_section.model_dump(mode="json"))
            if missing_section is not None
            else None
        )
        with self._engine.begin() as connection:
            self._guard(connection, claim, now)
            row = (
                connection.execute(
                    update(AnalysisStageRow)
                    .where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == role,
                        AnalysisStageRow.status == AnalysisStageStatus.PENDING.value,
                        AnalysisStageRow.attempt_count >= 1,
                    )
                    .values(
                        status=AnalysisStageStatus.FAILED.value,
                        failure_code=failure_code,
                        retryable=False,
                        output_json=missing_json,
                        output_hash=(
                            _content_hash(missing_json)
                            if missing_json is not None
                            else None
                        ),
                        updated_at=now,
                        finished_at=now,
                    )
                    .returning(AnalysisStageRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis stage retry budget is not exhausted")
            self._checkpoint_progress(connection, claim, run_id, now)
        return _stage_snapshot(row)

    def list_stages(self, run_id: str) -> tuple[AnalysisStageSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(AnalysisStageRow)
                    .where(AnalysisStageRow.run_id == run_id)
                    .order_by(AnalysisStageRow.ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_stage_snapshot(row) for row in rows)

    def get_data_section(self, run_id: str, role: str) -> ResearchSection:
        if role not in {"market", "fundamentals", "announcements", "news"}:
            raise AnalysisNotFound("analysis data stage was not found")
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    AnalysisStageRow.output_json,
                    AnalysisStageRow.output_hash,
                ).where(
                    AnalysisStageRow.run_id == run_id,
                    AnalysisStageRow.role == role,
                    AnalysisStageRow.status == AnalysisStageStatus.SUCCEEDED.value,
                )
            ).one_or_none()
        if row is None or not isinstance(row[0], str):
            raise AnalysisNotFound("analysis data stage artifact was not found")
        if row[1] != _content_hash(row[0]):
            raise AnalysisRepositoryError(
                "analysis data stage artifact hash is invalid"
            )
        try:
            section = ResearchSection.model_validate_json(row[0])
        except ValueError:
            raise AnalysisRepositoryError(
                "analysis data stage artifact is invalid"
            ) from None
        if section.kind.value != role:
            raise AnalysisRepositoryError(
                "analysis data stage artifact identity is invalid"
            )
        return section

    def get_stage_artifact(self, run_id: str, role: str) -> AnalysisStageArtifact:
        current_run = run_id
        visited: set[str] = set()
        row: RowMapping | None = None
        with self._engine.connect() as connection:
            target_identity = connection.execute(
                select(
                    AnalysisRunRow.snapshot_hash,
                    AnalysisRunRow.evidence_graph_hash,
                ).where(AnalysisRunRow.id == run_id)
            ).one_or_none()
            while current_run not in visited:
                visited.add(current_run)
                row = (
                    connection.execute(
                        select(AnalysisStageRow).where(
                            AnalysisStageRow.run_id == current_run,
                            AnalysisStageRow.role == role,
                            AnalysisStageRow.status.in_(
                                (
                                    AnalysisStageStatus.SUCCEEDED.value,
                                    AnalysisStageStatus.REUSED.value,
                                )
                            ),
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None or row["status"] == AnalysisStageStatus.SUCCEEDED.value:
                    break
                source_run_id = row["source_run_id"]
                if not isinstance(source_run_id, str):
                    row = None
                    break
                current_run = source_run_id
            else:
                row = None
            source_identity = connection.execute(
                select(
                    AnalysisRunRow.snapshot_hash,
                    AnalysisRunRow.evidence_graph_hash,
                ).where(AnalysisRunRow.id == current_run)
            ).one_or_none()
        if (
            row is None
            or not isinstance(row["output_json"], str)
            or not isinstance(row["trace_json"], str)
        ):
            raise AnalysisNotFound("analysis stage artifact was not found")
        if (
            target_identity is None
            or source_identity is None
            or tuple(target_identity) != tuple(source_identity)
        ):
            raise AnalysisRepositoryError("analysis reuse inputs are inconsistent")
        if row["output_hash"] != _content_hash(row["output_json"]) or row[
            "trace_hash"
        ] != _content_hash(row["trace_json"]):
            raise AnalysisRepositoryError("analysis stage artifact hash is invalid")
        try:
            output = clean_role_output_active_secrets(
                RoleOutput.model_validate_json(row["output_json"])
            )
            trace = WorkflowStageTrace.model_validate_json(row["trace_json"])
        except ValueError:
            raise AnalysisRepositoryError(
                "analysis stage artifact is invalid"
            ) from None
        with self._engine.connect() as connection:
            snapshot_id = connection.execute(
                select(AnalysisRunRow.snapshot_id).where(
                    AnalysisRunRow.id == current_run
                )
            ).scalar_one_or_none()
            attempt = (
                connection.execute(
                    select(AnalysisAttemptRow).where(
                        AnalysisAttemptRow.run_id == current_run,
                        AnalysisAttemptRow.role == role,
                        AnalysisAttemptRow.status
                        == AnalysisAttemptStatus.SUCCEEDED.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            output.role.value != role
            or trace.role.value != role
            or output.snapshot_id != snapshot_id
            or attempt is None
            or trace.request_hash != attempt["request_hash"]
            or trace.template_version != attempt["template_version"]
            or trace.template_hash != attempt["template_hash"]
        ):
            raise AnalysisRepositoryError("analysis stage artifact identity is invalid")
        return AnalysisStageArtifact(output=output, trace=trace)

    def load_inputs(self, run_id: str) -> tuple[ResearchSnapshot, EvidenceGraph]:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    AnalysisRunRow.snapshot_id,
                    AnalysisRunRow.snapshot_json,
                    AnalysisRunRow.snapshot_hash,
                    AnalysisRunRow.evidence_graph_json,
                    AnalysisRunRow.evidence_graph_hash,
                ).where(AnalysisRunRow.id == run_id)
            ).one_or_none()
        if row is None or not isinstance(row[1], str) or not isinstance(row[3], str):
            raise AnalysisNotFound("analysis run inputs were not found")
        if row[2] != _content_hash(row[1]) or row[4] != _content_hash(row[3]):
            raise AnalysisRepositoryError("analysis run input hash is invalid")
        try:
            snapshot = ResearchSnapshot.model_validate_json(row[1])
            graph = EvidenceGraph.model_validate_json(row[3])
        except ValueError:
            raise AnalysisRepositoryError("analysis run inputs are invalid") from None
        if (
            snapshot.snapshot_id != row[0]
            or graph.snapshot.canonical_json_bytes() != snapshot.canonical_json_bytes()
        ):
            raise AnalysisRepositoryError("analysis run input identity is invalid")
        return snapshot, graph

    def finalize_run(
        self,
        claim: TaskClaim,
        run_id: str,
        status: AnalysisRunStatus,
        report: ResearchReport,
        *,
        now: datetime,
    ) -> AnalysisRunSnapshot:
        expected_status = {
            ReportStatus.COMPLETE: AnalysisRunStatus.SUCCEEDED,
            ReportStatus.PARTIAL: AnalysisRunStatus.PARTIAL,
            ReportStatus.INSUFFICIENT_EVIDENCE: (
                AnalysisRunStatus.INSUFFICIENT_EVIDENCE
            ),
        }[report.status]
        if status is not expected_status:
            raise AnalysisConflict("analysis report status does not match run outcome")
        if status not in {
            AnalysisRunStatus.SUCCEEDED,
            AnalysisRunStatus.PARTIAL,
            AnalysisRunStatus.INSUFFICIENT_EVIDENCE,
        }:
            raise AnalysisConflict("analysis report requires a successful task outcome")
        report = clean_research_report_active_secrets(report)
        report_json = report.model_dump_json()
        with self._engine.begin() as connection:
            task = self._guard(connection, claim, now)
            if task.cancel_requested:
                row = (
                    connection.execute(
                        update(AnalysisRunRow)
                        .where(
                            AnalysisRunRow.id == run_id,
                            AnalysisRunRow.task_id == claim.snapshot.id,
                            AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
                        )
                        .values(
                            status=AnalysisRunStatus.CANCELLED.value,
                            current_stage=None,
                            updated_at=now,
                            finished_at=now,
                        )
                        .returning(AnalysisRunRow)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise AnalysisConflict("analysis run cannot be cancelled")
                self._tasks.complete_claim_in_transaction(
                    connection,
                    claim.snapshot.id,
                    claim.claim_token,
                    {},
                    now=now,
                )
                return _run_snapshot(row)
            connection.execute(
                insert(AnalysisReportRow).values(
                    run_id=run_id,
                    report_id=report.report_id,
                    report_json=report_json,
                    report_hash=_content_hash(report_json),
                    created_at=now,
                )
            )
            row = (
                connection.execute(
                    update(AnalysisRunRow)
                    .where(
                        AnalysisRunRow.id == run_id,
                        AnalysisRunRow.task_id == claim.snapshot.id,
                        AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
                    )
                    .values(
                        status=status.value,
                        current_stage=None,
                        updated_at=now,
                        finished_at=now,
                    )
                    .returning(AnalysisRunRow)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AnalysisConflict("analysis run cannot be finalized")
            self._tasks.complete_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {
                    "analysis_run_id": run_id,
                    "report_id": report.report_id,
                    "status": status.value,
                },
                now=now,
            )
        return _run_snapshot(row)

    def get_report(self, run_id: str) -> ResearchReport:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    AnalysisReportRow.report_id,
                    AnalysisReportRow.report_json,
                    AnalysisReportRow.report_hash,
                    AnalysisRunRow.snapshot_id,
                    AnalysisRunRow.status,
                )
                .join(AnalysisRunRow, AnalysisRunRow.id == AnalysisReportRow.run_id)
                .where(AnalysisReportRow.run_id == run_id)
            ).one_or_none()
        if row is None or not isinstance(row[1], str):
            raise AnalysisNotFound("analysis report was not found")
        if row[2] != _content_hash(row[1]):
            raise AnalysisRepositoryError("analysis report hash is invalid")
        try:
            report = clean_research_report_active_secrets(
                ResearchReport.model_validate_json(row[1])
            )
        except ValueError:
            raise AnalysisRepositoryError("analysis report is invalid") from None
        if report.report_id != row[0] or report.snapshot_id != row[3]:
            raise AnalysisRepositoryError("analysis report identity is invalid")
        expected_run_status = {
            ReportStatus.COMPLETE: AnalysisRunStatus.SUCCEEDED,
            ReportStatus.PARTIAL: AnalysisRunStatus.PARTIAL,
            ReportStatus.INSUFFICIENT_EVIDENCE: (
                AnalysisRunStatus.INSUFFICIENT_EVIDENCE
            ),
        }[report.status]
        if row[4] != expected_run_status.value:
            raise AnalysisRepositoryError("analysis report status is inconsistent")
        return report

    def get_stage(self, run_id: str, role: str) -> AnalysisStageSnapshot:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(AnalysisStageRow).where(
                        AnalysisStageRow.run_id == run_id,
                        AnalysisStageRow.role == role,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise AnalysisNotFound("analysis stage was not found")
        return _stage_snapshot(row)

    def list_attempts(
        self, run_id: str, role: str
    ) -> tuple[AnalysisAttemptSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id == run_id,
                        AnalysisAttemptRow.role == role,
                    )
                    .order_by(AnalysisAttemptRow.attempt_no)
                )
                .mappings()
                .all()
            )
        return tuple(_attempt_snapshot(row) for row in rows)

    def _guard(
        self, connection: object, claim: TaskClaim, now: datetime
    ) -> TaskSnapshot:
        try:
            typed = cast("Connection", connection)
            progress = typed.execute(
                select(TaskRun.progress).where(TaskRun.id == claim.snapshot.id)
            ).scalar_one()
            return self._tasks.guard_claim_in_transaction(
                typed,
                claim.snapshot.id,
                claim.claim_token,
                progress=float(progress),
                now=now,
            )
        except TaskConflict:
            raise AnalysisConflict("analysis worker claim is not current") from None

    def _checkpoint_progress(
        self,
        connection: Connection,
        claim: TaskClaim,
        run_id: str,
        now: datetime,
    ) -> None:
        stages = (
            connection.execute(
                select(
                    AnalysisStageRow.role,
                    AnalysisStageRow.ordinal,
                    AnalysisStageRow.status,
                )
                .where(AnalysisStageRow.run_id == run_id)
                .order_by(AnalysisStageRow.ordinal)
            )
            .mappings()
            .all()
        )
        progress, current_stage = _stage_progress(stages)
        self._tasks.guard_claim_in_transaction(
            connection,
            claim.snapshot.id,
            claim.claim_token,
            progress=progress,
            now=now,
        )
        connection.execute(
            update(AnalysisRunRow)
            .where(
                AnalysisRunRow.id == run_id,
                AnalysisRunRow.task_id == claim.snapshot.id,
                AnalysisRunRow.status == AnalysisRunStatus.RUNNING.value,
            )
            .values(current_stage=current_stage, updated_at=now)
        )
