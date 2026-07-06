from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast

import pytest

from stock_desk.api.tasks import TaskEventResponse, TaskResponse
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskRepository,
    TaskValidationError,
)


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'repository-json.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def _assert_nested_frozen(value: object) -> None:
    mapping = cast(Any, value)
    with pytest.raises(TypeError):
        mapping["new"] = "forbidden"
    with pytest.raises(TypeError):
        mapping["items"][0]["value"] = 99
    assert isinstance(mapping["items"], tuple)


def test_payload_is_deeply_detached_and_recursively_immutable(tmp_path: Path) -> None:
    nested = {"items": [{"value": 1}]}
    payload = {"nested": nested}
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.payload", payload)
        nested["items"][0]["value"] = 2
        nested["items"].append({"value": 3})

        assert cast(Any, created.payload["nested"])["items"][0]["value"] == 1
        _assert_nested_frozen(created.payload["nested"])
        fetched = repository.get(created.id)
        assert cast(Any, fetched.payload["nested"])["items"] == (
            created.payload["nested"]["items"][0],
        )
        _assert_nested_frozen(fetched.payload["nested"])
    finally:
        repository.close()


def test_result_and_error_snapshots_are_recursively_immutable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        succeeded = repository.create("json.result", {})
        assert repository.claim_next("worker-result") is not None
        result_nested = {"items": [{"value": 1}]}
        completed = repository.complete(succeeded.id, {"nested": result_nested})
        result_nested["items"][0]["value"] = 2
        assert completed.result is not None
        _assert_nested_frozen(completed.result["nested"])
        assert cast(Any, completed.result["nested"])["items"][0]["value"] == 1

        failed = repository.create("json.error", {})
        assert repository.claim_next("worker-error") is not None
        error_nested = {"items": [{"value": 1}]}
        terminal = repository.fail(failed.id, {"nested": error_nested})
        error_nested["items"][0]["value"] = 2
        assert terminal.error is not None
        _assert_nested_frozen(terminal.error["nested"])
        assert cast(Any, terminal.error["nested"])["items"][0]["value"] == 1
    finally:
        repository.close()


def test_progress_detail_is_detached_frozen_and_defaults_to_empty(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.progress", {})
        assert repository.claim_next("worker-progress") is not None
        detail_nested = {"items": [{"value": 1}]}

        repository.set_progress(
            created.id,
            0.25,
            detail={"nested": detail_nested},
        )
        detail_nested["items"][0]["value"] = 2
        repository.set_progress(created.id, 0.25)

        progressed = [
            event
            for event in repository.list_events(created.id)
            if event.event_name == "task.progressed"
        ]
        assert len(progressed) == 2
        _assert_nested_frozen(progressed[0].detail["nested"])
        assert cast(Any, progressed[0].detail["nested"])["items"][0]["value"] == 1
        assert progressed[1].detail == {}
    finally:
        repository.close()


def test_progress_is_nondecreasing_and_allowed_while_cancel_requested(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.progress", {})
        assert repository.claim_next("worker-progress") is not None

        assert repository.set_progress(created.id, 0.4).progress == 0.4
        assert repository.set_progress(created.id, 0.4).progress == 0.4
        with pytest.raises(TaskConflict):
            repository.set_progress(created.id, 0.3)
        assert repository.get(created.id).progress == 0.4

        cancelling = repository.request_cancel(created.id)
        assert cancelling.cancel_requested is True
        progressed = repository.set_progress(
            created.id,
            0.5,
            detail={"stage": "persisting"},
        )
        assert progressed.progress == 0.5
        assert progressed.cancel_requested is True
    finally:
        repository.close()


def test_recursively_immutable_arrays_still_serialize_as_json_arrays(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create(
            "json.api",
            {"items": [{"values": [1, 2]}]},
        )
        assert repository.claim_next("worker-api") is not None
        repository.set_progress(
            created.id,
            0.5,
            detail={"items": [{"values": [3, 4]}]},
        )
        completed = repository.complete(
            created.id,
            {"items": [{"values": [5, 6]}]},
        )
        progress_event = next(
            event
            for event in repository.list_events(created.id)
            if event.event_name == "task.progressed"
        )

        task_json = json.loads(TaskResponse.from_snapshot(completed).model_dump_json())
        event_json = json.loads(
            TaskEventResponse.from_snapshot(progress_event).model_dump_json()
        )

        assert task_json["payload"]["items"] == [{"values": [1, 2]}]
        assert task_json["result"]["items"] == [{"values": [5, 6]}]
        assert event_json["detail"]["items"] == [{"values": [3, 4]}]
    finally:
        repository.close()


@pytest.mark.parametrize(
    "detail",
    [
        cast(dict[str, Any], {"nested": {1: "invalid"}}),
        {"value": object()},
        {"value": math.nan},
    ],
)
def test_progress_rejects_invalid_nested_detail_without_writing_event(
    tmp_path: Path,
    detail: dict[str, Any],
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("json.progress", {})
        assert repository.claim_next("worker-progress") is not None
        before = repository.list_events(created.id)

        with pytest.raises(TaskValidationError):
            repository.set_progress(created.id, 0.25, detail=detail)

        assert repository.get(created.id).progress == 0.0
        assert repository.list_events(created.id) == before
    finally:
        repository.close()
