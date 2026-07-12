from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from stock_desk.workspace.models import (
    WorkspaceInstrument,
    WorkspacePreferences,
    WorkspaceState,
)
from stock_desk.workspace.store import WorkspaceStateStorageError, WorkspaceStateStore


NOW = datetime(2026, 7, 12, 6, tzinfo=timezone.utc)


def _state() -> WorkspaceState:
    return WorkspaceState(
        revision=1,
        updated_at=NOW,
        preferences=WorkspacePreferences(
            instrument=WorkspaceInstrument.default(),
        ),
    )


def test_store_atomically_preserves_last_committed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStateStore(tmp_path / "workspace" / "state-v1.json")
    committed = store.save(_state())

    def crash_before_publish(_source: object, _target: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", crash_before_publish)
    with pytest.raises(WorkspaceStateStorageError):
        store.save(committed.model_copy(update={"revision": 2}))

    assert store.load() == committed


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":2}',
        b'{"schema_version":1,"session_key":"secret"}',
        b"not-json",
        b"{" + (b" " * (64 * 1024)) + b"}",
    ],
)
def test_store_rejects_old_corrupt_oversized_or_unknown_state(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "state-v1.json"
    path.write_bytes(payload)

    with pytest.raises(WorkspaceStateStorageError):
        WorkspaceStateStore(path).load()


def test_persisted_json_contains_only_version_metadata_and_allowlisted_preferences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace" / "state-v1.json"
    WorkspaceStateStore(path).save(_state())

    raw = path.read_text(encoding="utf-8")
    assert '"schema_version":1' in raw
    assert '"current_page":"/market"' in raw
    for forbidden in ("token", "session", "secret", "http://", "https://", "?", "#"):
        assert forbidden not in raw.casefold()
