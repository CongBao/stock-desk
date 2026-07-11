from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from stock_desk.onboarding.models import OnboardingStatus, OnboardingStep
from stock_desk.onboarding.store import (
    OnboardingStateStorageError,
    OnboardingStateStore,
)


NOW = datetime(2026, 7, 12, 4, tzinfo=timezone.utc)


def test_state_store_atomically_resumes_the_last_committed_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OnboardingStateStore(
        tmp_path / "state" / "state-v1.json", clock=lambda: NOW
    )
    original = store.load().evolved(
        now=NOW,
        status=OnboardingStatus.IN_PROGRESS,
        current_step=OnboardingStep.DATA_PREPARATION,
    )
    store.save(original)
    replacement = original.evolved(
        now=NOW,
        current_step=OnboardingStep.INSTRUMENT_SELECTION,
    )

    def crash_before_publish(_source: object, _target: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", crash_before_publish)
    with pytest.raises(OnboardingStateStorageError):
        store.save(replacement)

    assert store.load() == original


def test_state_store_fails_closed_for_unknown_or_corrupt_schema(tmp_path: Path) -> None:
    path = tmp_path / "state-v1.json"
    path.write_text('{"schema_version":2}', encoding="utf-8")
    store = OnboardingStateStore(path, clock=lambda: NOW)

    with pytest.raises(OnboardingStateStorageError):
        store.load()
