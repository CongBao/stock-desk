from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
import time
from typing import Any

from stock_desk.analysis.data_service import ResearchDataService
from stock_desk.analysis.evidence import EvidenceGraph
from stock_desk.analysis.providers.base import ModelProvider
from stock_desk.analysis.repository import (
    AnalysisExecutionConfig,
    AnalysisRepository,
    AnalysisRepositoryError,
)
from stock_desk.analysis.runner import AnalysisCancelled, AnalysisRunner
from stock_desk.analysis.snapshot import ResearchSnapshot
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import DesktopCheckpointPause


ANALYSIS_TASK_KIND = "analysis.run"


class AnalysisWorkerHandler:
    """Execute a claimed durable analysis task with injected runtime services."""

    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        provider_factory: Callable[[AnalysisExecutionConfig], ModelProvider],
        data_service_factory: Callable[[], ResearchDataService],
        evidence_factory: Callable[[ResearchSnapshot], EvidenceGraph],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        lease_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_interval_seconds: float = 10.0,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        self._repository = repository
        self._provider_factory = provider_factory
        self._data_service_factory = data_service_factory
        self._evidence_factory = evidence_factory
        self._clock = clock
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._lease_sleeper = lease_sleeper
        self._lease_interval_seconds = lease_interval_seconds
        self._lease_duration = lease_duration

    def __call__(self, claim: TaskClaim) -> Mapping[str, Any]:
        if claim.snapshot.kind != ANALYSIS_TASK_KIND:
            raise ValueError("analysis worker received an incompatible task")
        run = self._repository.get_run_by_task(claim.snapshot.id)
        try:
            execution = self._repository.load_execution_config(run.id)
            data_service: ResearchDataService | None = None
            if run.snapshot_id is None:
                data_service = self._data_service_factory()
                if not isinstance(data_service, ResearchDataService):
                    raise TypeError(
                        "analysis data service factory returned invalid service"
                    )
            provider = self._provider_factory(execution)
            if (
                provider.provider != execution.provider
                or provider.model != execution.model
            ):
                raise ValueError(
                    "analysis provider does not match the frozen run config"
                )
            runner = AnalysisRunner(
                repository=self._repository,
                provider=provider,
                retry_policy=execution.retry_policy,
                temperature=execution.public_config.temperature,
                model_timeout_seconds=execution.public_config.timeout_seconds,
                max_output_tokens=execution.public_config.max_output_tokens,
                sleeper=self._sleeper,
                clock=self._clock,
                monotonic=self._monotonic,
                lease_sleeper=self._lease_sleeper,
                lease_interval_seconds=self._lease_interval_seconds,
                lease_duration=self._lease_duration,
            )
            if run.snapshot_id is None:
                assert data_service is not None
                operation = runner.run_from_data(
                    claim=claim,
                    run_id=run.id,
                    symbol=run.symbol,
                    data_service=data_service,
                    evidence_factory=self._evidence_factory,
                )
            else:
                snapshot, graph = self._repository.load_inputs(run.id)
                operation = runner.run(
                    claim=claim,
                    run_id=run.id,
                    snapshot=snapshot,
                    evidence_graph=graph,
                )
            result = asyncio.run(operation)
        except AnalysisCancelled:
            cancelled = self._repository.get_run(run.id)
            return {
                "analysis_run_id": cancelled.id,
                "status": cancelled.status.value,
            }
        except DesktopCheckpointPause:
            raise
        except Exception:
            try:
                self._repository.fail_run(
                    claim,
                    run.id,
                    code="analysis_worker_failed",
                    safe_message="analysis worker failed",
                    now=self._clock(),
                )
            except AnalysisRepositoryError:
                pass
            raise
        return {
            "analysis_run_id": result.run.id,
            "report_id": result.report.report_id,
            "status": result.run.status.value,
        }


__all__ = ["ANALYSIS_TASK_KIND", "AnalysisWorkerHandler"]
