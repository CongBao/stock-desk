from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim, TaskSnapshot
from stock_desk.tasks.repository import TaskConflict, TaskRepository
from stock_desk.tasks.worker import TaskWorker


LEASE = timedelta(seconds=30)
START = datetime(2026, 7, 7, 1, 2, 3, tzinfo=timezone.utc)


def _repository(tmp_path: Path) -> tuple[TaskRepository, object]:
    url = f"sqlite:///{tmp_path / 'leases.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    return TaskRepository(engine), engine


def test_backtest_claim_has_private_rotating_lease_and_preserves_started_at(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create(
            "backtest.run", {"run_id": "run-1", "snapshot_id": "snapshot-1"}
        )

        first = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(first, TaskClaim)
        assert first.snapshot.id == created.id
        assert first.snapshot.started_at == START
        assert first.attempt_count == 1
        assert first.lease_expires_at == START + LEASE

        reclaimed = repository.claim_next(
            "worker-2", now=START + LEASE, lease_duration=LEASE
        )
        assert isinstance(reclaimed, TaskClaim)
        assert reclaimed.snapshot.id == created.id
        assert reclaimed.claim_token != first.claim_token
        assert reclaimed.attempt_count == 2
        assert reclaimed.snapshot.started_at == START

        public = repository.get(created.id)
        assert isinstance(public, TaskSnapshot)
        assert not hasattr(public, "claim_token")
        assert all(
            "claim_token" not in event.detail
            for event in repository.list_events(created.id)
        )
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_heartbeat_and_every_terminal_write_are_fenced_by_claim_token(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("backtest.run", {"run_id": "run-1"})
        stale = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(stale, TaskClaim)
        current = repository.claim_next(
            "worker-2", now=START + LEASE, lease_duration=LEASE
        )
        assert isinstance(current, TaskClaim)

        with pytest.raises(TaskConflict):
            repository.heartbeat(
                created.id,
                stale.claim_token,
                now=START + LEASE + timedelta(seconds=1),
                lease_duration=LEASE,
            )
        with pytest.raises(TaskConflict):
            repository.set_progress(created.id, 0.5, claim_token=stale.claim_token)
        with pytest.raises(TaskConflict):
            repository.complete(created.id, {"ok": True}, claim_token=stale.claim_token)
        with pytest.raises(TaskConflict):
            repository.fail(
                created.id,
                {"code": "stale"},
                claim_token=stale.claim_token,
            )

        heartbeat = repository.heartbeat(
            created.id,
            current.claim_token,
            now=START + LEASE + timedelta(seconds=1),
            lease_duration=LEASE,
        )
        assert heartbeat.claim_token == current.claim_token
        assert heartbeat.lease_expires_at == START + LEASE + timedelta(seconds=31)
        completed = repository.complete(
            created.id,
            {"ok": True},
            claim_token=current.claim_token,
            now=START + LEASE + timedelta(seconds=2),
        )
        assert completed.status == "succeeded"
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_legacy_tasks_keep_snapshot_claims_and_null_lease_fields(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 3})
        claimed = repository.claim_next(
            "worker-legacy", now=START, lease_duration=LEASE
        )
        assert isinstance(claimed, TaskSnapshot)
        assert claimed.id == created.id

        with engine.connect() as connection:  # type: ignore[union-attr]
            row = connection.execute(
                text(
                    "SELECT claim_token, lease_expires_at, heartbeat_at, "
                    "attempt_count FROM task_run WHERE id = :task_id"
                ),
                {"task_id": created.id},
            ).one()
        assert row == (None, None, None, 0)
        assert repository.complete(created.id, {"value": 6}).status == "succeeded"
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_nonexpired_backtest_claim_cannot_be_stolen(tmp_path: Path) -> None:
    repository, engine = _repository(tmp_path)
    try:
        repository.create("backtest.run", {"run_id": "run-1"})
        first = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(first, TaskClaim)
        assert (
            repository.claim_next(
                "worker-2",
                now=START + LEASE - timedelta(microseconds=1),
                lease_duration=LEASE,
            )
            is None
        )
    finally:
        engine.dispose()  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ["progress", "complete", "fail"])
def test_expired_unreclaimed_claim_cannot_write(
    tmp_path: Path,
    operation: str,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("backtest.run", {"run_id": "run-1"})
        claim = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(claim, TaskClaim)
        expired_at = START + LEASE

        with pytest.raises(TaskConflict):
            if operation == "progress":
                repository.set_progress(
                    created.id,
                    0.5,
                    claim_token=claim.claim_token,
                    now=expired_at,
                )
            elif operation == "complete":
                repository.complete(
                    created.id,
                    {"ok": True},
                    claim_token=claim.claim_token,
                    now=expired_at,
                )
            else:
                repository.fail(
                    created.id,
                    {"code": "expired"},
                    claim_token=claim.claim_token,
                    now=expired_at,
                )

        assert repository.get(created.id).status == "running"
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_worker_claimed_handler_exception_cannot_fail_replacement_owner(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("backtest.run", {"run_id": "run-1"})
        replacement: list[TaskClaim] = []
        worker = TaskWorker(repository, worker_id="worker-1")

        def lose_lease_then_fail(claim: TaskClaim) -> dict[str, object]:
            stolen = repository.claim_next(
                "worker-2",
                now=claim.lease_expires_at,
                lease_duration=LEASE,
            )
            assert isinstance(stolen, TaskClaim)
            replacement.append(stolen)
            raise RuntimeError("simulated stale handler")

        worker.register_claimed("backtest.run", lose_lease_then_fail)
        observed = worker.run_once()

        assert replacement
        assert observed is not None
        assert observed.id == created.id
        assert observed.status == "running"
        assert observed.worker_id == "worker-2"
        assert repository.get(created.id) == observed
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_expired_cancel_requested_backtest_is_terminalized_without_handler(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("backtest.run", {"run_id": "run-1"})
        stale = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(stale, TaskClaim)
        repository.request_cancel(created.id)

        assert (
            repository.claim_next(
                "worker-2",
                now=START + LEASE,
                lease_duration=LEASE,
            )
            is None
        )
        terminal = repository.get(created.id)
        assert terminal.status == "cancelled"
        assert terminal.finished_at == START + LEASE
        with engine.connect() as connection:  # type: ignore[union-attr]
            lease = connection.execute(
                text(
                    "SELECT claim_token, lease_expires_at, heartbeat_at "
                    "FROM task_run WHERE id = :task_id"
                ),
                {"task_id": created.id},
            ).one()
        assert lease == (None, None, None)
        with pytest.raises(TaskConflict):
            repository.fail(
                created.id,
                {"code": "stale"},
                claim_token=stale.claim_token,
                now=START + LEASE,
            )
        assert [event.event_name for event in repository.list_events(created.id)] == [
            "task.created",
            "task.claimed",
            "task.cancel_requested",
            "task.cancelled",
        ]
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_transaction_checkpoint_guard_requires_current_unexpired_claim(
    tmp_path: Path,
) -> None:
    repository, engine = _repository(tmp_path)
    try:
        created = repository.create("backtest.run", {"run_id": "run-1"})
        stale = repository.claim_next("worker-1", now=START, lease_duration=LEASE)
        assert isinstance(stale, TaskClaim)
        current = repository.claim_next(
            "worker-2", now=START + LEASE, lease_duration=LEASE
        )
        assert isinstance(current, TaskClaim)

        with pytest.raises(TaskConflict):
            with engine.begin() as connection:  # type: ignore[union-attr]
                repository.guard_claim_in_transaction(
                    connection,
                    created.id,
                    stale.claim_token,
                    progress=0.5,
                    now=START + LEASE + timedelta(seconds=1),
                )

        with engine.begin() as connection:  # type: ignore[union-attr]
            guarded = repository.guard_claim_in_transaction(
                connection,
                created.id,
                current.claim_token,
                progress=0.5,
                now=START + LEASE + timedelta(seconds=1),
            )
        assert guarded.progress == 0.5
    finally:
        engine.dispose()  # type: ignore[union-attr]
