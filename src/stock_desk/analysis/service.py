from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.engine import Connection

from stock_desk.analysis.evidence import EvidenceItem
from stock_desk.analysis.model_catalog import (
    AnalysisModelCatalog,
    VerifiedModelExecution,
)
from stock_desk.analysis.report import ResearchReport
from stock_desk.analysis.repository import (
    AnalysisConflict,
    AnalysisHistoryKey,
    AnalysisNotFound,
    AnalysisRepository,
    AnalysisRepositoryError,
    AnalysisRunSnapshot,
    AnalysisRunStatus,
    AnalysisStageSnapshot,
)
from stock_desk.analysis.retry import RetryPolicy
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskConflict, TaskRepository


class AnalysisServiceError(RuntimeError):
    pass


class AnalysisStateConflict(AnalysisServiceError):
    pass


class AnalysisReportNotReady(AnalysisServiceError):
    pass


class AnalysisReportUnavailable(AnalysisServiceError):
    pass


class AnalysisEvidenceNotFound(AnalysisServiceError):
    pass


class AnalysisServiceStorageError(AnalysisServiceError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisSubmission:
    run_id: str
    task_id: str
    parent_run_id: str | None
    requested_stage: str | None
    status: Literal["queued"]
    snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class AnalysisDetail:
    run: AnalysisRunSnapshot
    task: TaskSnapshot
    stages: tuple[AnalysisStageSnapshot, ...]
    retry_stages: frozenset[str]


@dataclass(frozen=True, slots=True)
class AnalysisHistoryPage:
    items: tuple[AnalysisDetail, ...]
    next_key: AnalysisHistoryKey | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisService:
    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        tasks: TaskRepository,
        model_catalog: AnalysisModelCatalog,
        execution_resolver: Callable[[Connection, str], VerifiedModelExecution]
        | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        identities = (
            getattr(repository, "database_identity", None),
            getattr(tasks, "database_identity", None),
            getattr(model_catalog, "database_identity", None),
        )
        if identities[0] is None or any(
            identity != identities[0] for identity in identities[1:]
        ):
            raise AnalysisServiceStorageError()
        self._repository = repository
        self._tasks = tasks
        self._model_catalog = model_catalog
        self._execution_resolver = (
            execution_resolver or model_catalog.require_verified_in_transaction
        )
        self._clock = clock
        self.database_identity = identities[0]
        self.analysis_repository_identity = identities[0]
        self.task_repository_identity = identities[1]
        self.model_catalog_identity = identities[2]

    def submit(
        self, *, symbol: str, model_config_id: str, max_retries: int
    ) -> AnalysisSubmission:
        with self._model_catalog.transaction() as connection:
            execution = self._execution_resolver(connection, model_config_id)
            config = execution.public_config
            enqueued = self._repository.enqueue_run_in_transaction(
                connection,
                symbol=symbol,
                retry_policy=RetryPolicy(max_retries=max_retries),
                model_config_id=execution.model_config_id,
                model_provider=config.provider.value,
                model_name=config.model,
                model_public_config=config,
                now=self._clock(),
            )
        return self._submission(enqueued.run)

    def detail(self, run_id: str) -> AnalysisDetail:
        detail = self._repository.get_detail(run_id)
        run = detail.run
        retry_stages: frozenset[str] = frozenset()
        if run.status is AnalysisRunStatus.PARTIAL:
            try:
                report = self._repository.get_report(run_id)
            except AnalysisNotFound:
                raise AnalysisRepositoryError(
                    "partial analysis report is missing"
                ) from None
            retry_stages = frozenset(item.stage.value for item in report.retry_actions)
        return AnalysisDetail(
            run=run,
            task=detail.task,
            stages=detail.stages,
            retry_stages=retry_stages,
        )

    def history(
        self,
        *,
        limit: int,
        after: AnalysisHistoryKey | None,
        symbol: str | None,
    ) -> AnalysisHistoryPage:
        page = self._repository.list_history_page(
            limit=limit, after=after, symbol=symbol
        )
        return AnalysisHistoryPage(
            items=tuple(
                AnalysisDetail(
                    run=item.run,
                    task=item.task,
                    stages=(),
                    retry_stages=frozenset(),
                )
                for item in page.items
            ),
            next_key=page.next_key,
        )

    def cancel(self, run_id: str) -> AnalysisDetail:
        current = self.detail(run_id)
        if current.run.status is AnalysisRunStatus.CANCELLED:
            return current
        if current.run.status not in {
            AnalysisRunStatus.QUEUED,
            AnalysisRunStatus.RUNNING,
        }:
            raise AnalysisStateConflict()
        try:
            self._tasks.request_cancel(current.task.id)
        except TaskConflict:
            refreshed = self.detail(run_id)
            if refreshed.run.status is AnalysisRunStatus.CANCELLED:
                return refreshed
            raise AnalysisStateConflict() from None
        return self.detail(run_id)

    def report(self, run_id: str) -> ResearchReport:
        run = self._repository.get_run(run_id)
        if run.status in {AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING}:
            raise AnalysisReportNotReady()
        if run.status in {AnalysisRunStatus.FAILED, AnalysisRunStatus.CANCELLED}:
            raise AnalysisReportUnavailable()
        try:
            return self._repository.get_report(run_id)
        except AnalysisNotFound:
            raise AnalysisRepositoryError("analysis report is missing") from None

    def evidence(self, run_id: str, evidence_id: str) -> EvidenceItem:
        self._repository.get_run(run_id)
        try:
            report = self._repository.get_report(run_id)
        except AnalysisNotFound:
            raise AnalysisEvidenceNotFound() from None
        item = next(
            (
                evidence
                for evidence in report.evidence_items
                if evidence.evidence_id == evidence_id
            ),
            None,
        )
        if item is None:
            raise AnalysisEvidenceNotFound()
        return item

    def retry(self, run_id: str, stage: str) -> AnalysisSubmission:
        run = self._repository.get_run(run_id)
        if run.status is not AnalysisRunStatus.PARTIAL:
            raise AnalysisStateConflict()
        report = self.report(run_id)
        allowed = frozenset(item.stage.value for item in report.retry_actions)
        if stage not in allowed:
            raise AnalysisStateConflict()
        stage_snapshot = self._repository.get_stage(run_id, stage)
        if stage_snapshot.status.value != "failed":
            raise AnalysisStateConflict()
        try:
            enqueued = self._repository.enqueue_retry(run_id, stage, now=self._clock())
        except AnalysisConflict:
            raise AnalysisStateConflict() from None
        return self._submission(enqueued.run)

    @staticmethod
    def _submission(run: AnalysisRunSnapshot) -> AnalysisSubmission:
        if run.status is not AnalysisRunStatus.QUEUED:
            raise AnalysisServiceStorageError()
        return AnalysisSubmission(
            run_id=run.id,
            task_id=run.task_id,
            parent_run_id=run.parent_run_id,
            requested_stage=run.requested_stage,
            status="queued",
            snapshot_id=run.snapshot_id,
        )
