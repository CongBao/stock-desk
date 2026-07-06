from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from stock_desk.api.tasks import TaskResponse
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskRepository, TaskValidationError
from stock_desk.tasks.worker import TaskWorker


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'repository-json-closure.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def _nested_payload(depth: int) -> dict[str, Any]:
    nested: dict[str, Any] = {"leaf": True}
    for _ in range(depth):
        nested = {"nested": nested}
    return {"root": nested}


class _ExplodingItemsMapping(dict[str, Any]):
    def items(self) -> Any:
        raise RuntimeError("hostile items")


class _ExplodingIterationMapping(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return "unreachable"

    def __iter__(self) -> Iterator[str]:
        raise RecursionError("hostile iteration")

    def __len__(self) -> int:
        return 1


class _ExplodingLengthList(list[Any]):
    def __len__(self) -> int:
        raise RecursionError("hostile length")


class _ExplodingIndexList(list[Any]):
    def __getitem__(self, index: Any) -> Any:
        raise RuntimeError("hostile index")


class _InterruptingLengthList(list[Any]):
    def __init__(self, interruption: type[BaseException]) -> None:
        super().__init__([1])
        self._interruption = interruption

    def __len__(self) -> int:
        raise self._interruption()


def test_frozen_snapshot_payload_is_closed_under_repository_input(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        source = repository.create(
            "json.source",
            {"nested": {"items": [{"values": [1, 2]}]}},
        )

        copied = repository.create("json.copy", source.payload)

        assert copied.payload == source.payload
        assert copied.payload is not source.payload
        assert copied.payload["nested"] is not source.payload["nested"]
    finally:
        repository.close()


def test_mappingproxy_tuple_aliases_are_normalized_to_independent_json_values(
    tmp_path: Path,
) -> None:
    shared = MappingProxyType({"values": (1, 2)})
    payload = MappingProxyType(
        {
            "left": shared,
            "right": shared,
            "items": (shared,),
        }
    )
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.alias", payload)

        assert created.payload["left"] == {"values": (1, 2)}
        assert created.payload["right"] == {"values": (1, 2)}
        assert created.payload["left"] is not created.payload["right"]
        assert created.payload["items"][0] is not created.payload["left"]
    finally:
        repository.close()


def test_worker_can_echo_frozen_nested_payload_and_api_serializes_json(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create(
            "json.echo",
            {"nested": {"items": [{"values": [1, 2]}]}},
        )
        worker = TaskWorker(repository, worker_id="worker-json-echo")

        def echo(task: TaskSnapshot) -> dict[str, object]:
            return {"echo": task.payload["nested"]}

        worker.register("json.echo", echo)
        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "succeeded"
        response = json.loads(TaskResponse.from_snapshot(completed).model_dump_json())
        assert response["result"] == {"echo": {"items": [{"values": [1, 2]}]}}
        assert repository.get(created.id).result == completed.result
    finally:
        repository.close()


def test_worker_huge_integer_result_fails_generically_instead_of_staying_running(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.huge-result", {})
        worker = TaskWorker(repository, worker_id="worker-json-huge")

        def huge_result(_task: TaskSnapshot) -> dict[str, int]:
            return {"number": 10**5000}

        worker.register("json.huge-result", huge_result)
        completed = worker.run_once()

        assert completed is not None
        assert completed.id == created.id
        assert completed.status == "failed"
        assert completed.error == {"code": "task_handler_failed"}
        assert [event.event_name for event in repository.list_events(created.id)] == [
            "task.created",
            "task.claimed",
            "task.failed",
        ]
    finally:
        repository.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "\ud800"},
        {"\udfff": "value"},
    ],
    ids=("surrogate-value", "surrogate-key"),
)
def test_isolated_surrogates_are_typed_and_never_persist(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    repository = _repository(tmp_path)
    try:
        before = repository.list_recent()
        with pytest.raises(TaskValidationError):
            repository.create("json.surrogate", payload)
        assert repository.list_recent() == before
    finally:
        repository.close()


@pytest.mark.parametrize(
    "hostile",
    [
        _ExplodingItemsMapping({"value": 1}),
        _ExplodingIterationMapping(),
        _ExplodingLengthList([1]),
        _ExplodingIndexList([1]),
    ],
    ids=("mapping-items", "mapping-iteration", "list-length", "list-index"),
)
def test_hostile_container_access_is_typed_and_never_persists(
    tmp_path: Path,
    hostile: object,
) -> None:
    repository = _repository(tmp_path)
    try:
        before = repository.list_recent()
        with pytest.raises(TaskValidationError):
            repository.create("json.hostile", {"nested": hostile})
        assert repository.list_recent() == before
    finally:
        repository.close()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_container_base_exceptions_are_not_converted(
    tmp_path: Path,
    interruption: type[BaseException],
) -> None:
    repository = _repository(tmp_path)
    try:
        before = repository.list_recent()
        with pytest.raises(interruption):
            repository.create(
                "json.interruption",
                {"nested": _InterruptingLengthList(interruption)},
            )
        assert repository.list_recent() == before
    finally:
        repository.close()


def test_explicit_json_depth_boundary_accepts_safe_depth(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.depth", _nested_payload(64))
        assert created.status == "queued"
    finally:
        repository.close()


@pytest.mark.parametrize("depth", [256, 995])
def test_json_depth_overflow_is_typed_and_never_persists(
    tmp_path: Path,
    depth: int,
) -> None:
    repository = _repository(tmp_path)
    try:
        before = repository.list_recent()
        with pytest.raises(TaskValidationError):
            repository.create("json.too-deep", _nested_payload(depth))
        assert repository.list_recent() == before
    finally:
        repository.close()


def test_mappingproxy_cycle_is_typed_and_never_persists(tmp_path: Path) -> None:
    cyclic: dict[str, Any] = {}
    proxy = MappingProxyType(cyclic)
    cyclic["self"] = proxy
    repository = _repository(tmp_path)
    try:
        before = repository.list_recent()
        with pytest.raises(TaskValidationError):
            repository.create("json.cycle", {"nested": proxy})
        assert repository.list_recent() == before
    finally:
        repository.close()
