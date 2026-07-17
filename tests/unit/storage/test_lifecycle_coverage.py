from __future__ import annotations

import os
from pathlib import Path

from filelock import Timeout as FileLockTimeout
import pytest

from stock_desk.storage import lifecycle


def test_safe_lock_file_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    target.touch()
    symlink = tmp_path / "symlink.lock"
    symlink.symlink_to(target)

    with pytest.raises(lifecycle.LifecycleCorruptionError, match="single-link"):
        lifecycle._safe_lock_file(symlink)

    hardlink = tmp_path / "hardlink.lock"
    os.link(target, hardlink)
    with pytest.raises(lifecycle.LifecycleCorruptionError, match="single-link"):
        lifecycle._safe_lock_file(target)


def test_service_directory_rejects_unsafe_permissions(tmp_path: Path) -> None:
    directory = tmp_path / ".stock-desk-services"
    directory.mkdir(mode=0o755)

    with pytest.raises(lifecycle.LifecycleCorruptionError, match="directory is unsafe"):
        lifecycle._service_directory(tmp_path)


def test_restore_lock_translates_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BusyLock:
        def __init__(self, path: Path, *, timeout: float) -> None:
            assert path == tmp_path / ".stock-desk-restore.lock"
            assert timeout == 2

        def acquire(self) -> None:
            raise FileLockTimeout("busy")

    monkeypatch.setattr(lifecycle, "FileLock", BusyLock)

    with pytest.raises(lifecycle.LifecycleBusyError, match="lock is busy"):
        lifecycle._restore_lock(tmp_path, 2)


def test_restore_lock_releases_when_lock_file_is_unsafe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    class Lock:
        def __init__(self, _path: Path, *, timeout: float) -> None:
            assert timeout == 0

        def acquire(self) -> None:
            calls.append("acquire")

        def release(self) -> None:
            calls.append("release")

    monkeypatch.setattr(lifecycle, "FileLock", Lock)
    monkeypatch.setattr(
        lifecycle,
        "_safe_lock_file",
        lambda _path: (_ for _ in ()).throw(
            lifecycle.LifecycleCorruptionError("unsafe lock")
        ),
    )

    with pytest.raises(lifecycle.LifecycleCorruptionError, match="unsafe lock"):
        lifecycle._restore_lock(tmp_path, 0)
    assert calls == ["acquire", "release"]


def test_active_service_scan_rejects_ambiguous_and_busy_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / ".stock-desk-services"
    directory.mkdir(mode=0o700)
    (directory / "unexpected.txt").touch()
    with pytest.raises(lifecycle.LifecycleCorruptionError, match="ambiguous"):
        lifecycle._reject_active_services(tmp_path)

    (directory / "unexpected.txt").unlink()
    marker = directory / "api.lock"
    marker.touch()

    class BusyLock:
        def __init__(self, path: Path, *, timeout: float) -> None:
            assert path == marker
            assert timeout == 0

        def acquire(self) -> None:
            raise FileLockTimeout("busy")

    monkeypatch.setattr(lifecycle, "FileLock", BusyLock)
    with pytest.raises(lifecycle.LifecycleBusyError, match="service is still running"):
        lifecycle._reject_active_services(tmp_path)


def test_restore_and_service_lifecycle_validate_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        with lifecycle.restore_lifecycle(tmp_path, timeout_seconds=-0.1):
            pass

    with pytest.raises(ValueError, match="role is invalid"):
        with lifecycle.service_lifecycle(tmp_path, role="Invalid Role"):
            pass


def test_service_lifecycle_tolerates_marker_removed_before_exit(tmp_path: Path) -> None:
    with lifecycle.service_lifecycle(tmp_path, role="api"):
        markers = tuple((tmp_path / ".stock-desk-services").glob("*.lock"))
        assert len(markers) == 1
        markers[0].unlink()

    assert tuple((tmp_path / ".stock-desk-services").glob("*.lock")) == ()


def test_application_content_ignores_only_lifecycle_artifacts(tmp_path: Path) -> None:
    (tmp_path / ".stock-desk-restore.lock").touch()
    (tmp_path / ".stock-desk-services").mkdir()
    assert lifecycle.has_application_or_operator_content(tmp_path) is False

    (tmp_path / "stock-desk.db").touch()
    assert lifecycle.has_application_or_operator_content(tmp_path) is True
