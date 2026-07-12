from pathlib import Path

import pytest

from stock_desk.guidance.models import GuidancePage, GuidanceStatus
from stock_desk.guidance.store import (
    GuidancePreferencesConflict,
    GuidancePreferencesStore,
    GuidancePreferencesStorageError,
)


def test_preferences_are_versioned_persisted_and_compare_and_swapped(
    tmp_path: Path,
) -> None:
    store = GuidancePreferencesStore(tmp_path / "guidance" / "preferences.json")

    initial = store.load()
    assert initial.schema_version == 1
    assert initial.revision == 0
    assert initial.pages == {}

    saved = store.update(
        expected_revision=0,
        page=GuidancePage.MARKET,
        content_version=1,
        status=GuidanceStatus.COMPLETED,
    )
    assert saved.revision == 1
    assert saved.pages[GuidancePage.MARKET].content_version == 1

    reloaded = GuidancePreferencesStore(
        tmp_path / "guidance" / "preferences.json"
    ).load()
    assert reloaded == saved

    with pytest.raises(GuidancePreferencesConflict):
        store.update(
            expected_revision=0,
            page=GuidancePage.FORMULA,
            content_version=1,
            status=GuidanceStatus.DISMISSED,
        )


def test_content_version_is_scoped_to_one_page(tmp_path: Path) -> None:
    store = GuidancePreferencesStore(tmp_path / "preferences.json")
    first = store.update(
        expected_revision=0,
        page=GuidancePage.MARKET,
        content_version=1,
        status=GuidanceStatus.COMPLETED,
    )
    second = store.update(
        expected_revision=first.revision,
        page=GuidancePage.FORMULA,
        content_version=2,
        status=GuidanceStatus.DISMISSED,
    )

    assert second.pages[GuidancePage.MARKET].content_version == 1
    assert second.pages[GuidancePage.FORMULA].content_version == 2


def test_preferences_path_and_persisted_payload_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        GuidancePreferencesStore(Path("relative/preferences.json"))

    path = tmp_path / "preferences.json"
    store = GuidancePreferencesStore(path)
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(GuidancePreferencesStorageError):
        store.load()
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(GuidancePreferencesStorageError):
        store.load()


def test_preference_write_and_cleanup_failures_remain_storage_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GuidancePreferencesStore(tmp_path / "replace-failure.json")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("stock_desk.guidance.store.os.replace", fail_replace)
    with pytest.raises(GuidancePreferencesStorageError):
        store.update(
            expected_revision=0,
            page=GuidancePage.MARKET,
            content_version=1,
            status=GuidanceStatus.COMPLETED,
        )
    assert not tuple(tmp_path.glob(".replace-failure.json.*.tmp"))


def test_cleanup_failure_does_not_leak_raw_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GuidancePreferencesStore(tmp_path / "open-failure.json")

    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise OSError("simulated open failure")

    def fail_unlink(
        _path: Path,
        missing_ok: bool = False,
    ) -> None:
        del missing_ok
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("stock_desk.guidance.store.os.open", fail_open)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(GuidancePreferencesStorageError):
        store.update(
            expected_revision=0,
            page=GuidancePage.MARKET,
            content_version=1,
            status=GuidanceStatus.COMPLETED,
        )


def test_open_descriptor_is_closed_when_file_handle_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GuidancePreferencesStore(tmp_path / "fdopen-failure.json")

    def fail_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr("stock_desk.guidance.store.os.fdopen", fail_fdopen)
    with pytest.raises(GuidancePreferencesStorageError):
        store.update(
            expected_revision=0,
            page=GuidancePage.MARKET,
            content_version=1,
            status=GuidanceStatus.COMPLETED,
        )
    assert not tuple(tmp_path.glob(".fdopen-failure.json.*.tmp"))
