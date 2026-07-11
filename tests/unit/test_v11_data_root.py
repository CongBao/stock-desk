from pathlib import Path

import pytest

from stock_desk.config import resolve_v11_data_root


def test_v11_windows_data_root_is_versioned_under_current_user_local_app_data(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "用户 名" / "AppData" / "Local"

    result = resolve_v11_data_root(
        platform_name="Windows",
        known_folder_resolver=lambda: local_app_data,
    )

    assert result == local_app_data / "Stock Desk" / "v1.1"


def test_v11_data_root_fails_closed_without_windows_current_user_folder() -> None:
    def unavailable() -> Path:
        raise OSError("known folder unavailable")

    with pytest.raises(RuntimeError, match="current-user application data"):
        resolve_v11_data_root(
            platform_name="Windows",
            known_folder_resolver=unavailable,
        )


def test_v11_data_root_resolution_does_not_touch_the_v1_directory(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "Local App Data"
    old_root = local_app_data / "stock-desk"
    old_root.mkdir(parents=True)
    sentinel = old_root / "v1-canary.txt"
    sentinel.write_bytes(b"do-not-read-or-change")
    before = sentinel.stat()

    result = resolve_v11_data_root(
        platform_name="Windows",
        known_folder_resolver=lambda: local_app_data,
    )

    assert result == local_app_data / "Stock Desk" / "v1.1"
    assert not result.exists()
    assert sentinel.read_bytes() == b"do-not-read-or-change"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns


def test_v11_data_root_rejects_non_windows_hosts() -> None:
    with pytest.raises(RuntimeError, match="Windows-only"):
        resolve_v11_data_root(platform_name="Darwin")


def test_v11_data_root_ignores_environment_and_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_folder = tmp_path / "known folder"
    poisoned = tmp_path / "poisoned"
    poisoned.mkdir()
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv("LOCALAPPDATA", str(poisoned / "legacy-env-root"))

    result = resolve_v11_data_root(
        platform_name="Windows",
        known_folder_resolver=lambda: known_folder,
    )

    assert result == known_folder / "Stock Desk" / "v1.1"
