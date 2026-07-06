from __future__ import annotations

from datetime import date
import fcntl
import os
from pathlib import Path
import stat

import pytest

from stock_desk.market.lake import MarketLake
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _open_descriptor_count() -> int:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        if directory.is_dir():
            return len(tuple(directory.iterdir()))
    pytest.skip("descriptor filesystem is unavailable")


def _probe_namespace_guard(root: Path) -> None:
    descriptor = os.open(
        root / ".locks",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _exception_tree(error: BaseException) -> tuple[BaseException, ...]:
    collected = [error]
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            collected.extend(_exception_tree(nested))
    chained = error.__cause__ or error.__context__
    if chained is not None:
        collected.extend(_exception_tree(chained))
    return tuple(collected)


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("failure_layer", ["dataset", "namespace"])
def test_unlock_failure_still_releases_all_operation_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_layer: str,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        original_flock = fcntl.flock
        failed = False

        def fail_selected_unlock(descriptor: int, action: int) -> object:
            nonlocal failed
            metadata = os.fstat(descriptor)
            is_namespace = stat.S_ISDIR(metadata.st_mode)
            selected = (
                failure_layer == "namespace"
                if is_namespace
                else failure_layer == "dataset"
            )
            if not failed and action == fcntl.LOCK_UN and selected:
                failed = True
                raise OSError(f"injected {failure_layer} unlock failure")
            return original_flock(descriptor, action)

        monkeypatch.setattr(fcntl, "flock", fail_selected_unlock)

        def invoke() -> object:
            if operation == "read":
                return lake.read(stored.manifest_record_id)
            return lake.write(routed)

        baseline = _open_descriptor_count()
        with pytest.raises(OSError, match=f"{failure_layer} unlock failure"):
            invoke()

        assert failed
        _probe_namespace_guard(root)
        assert _open_descriptor_count() == baseline


def test_body_and_unlock_failures_remain_observable_without_leaking_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        original_flock = fcntl.flock
        failed_unlock = False

        def fail_body(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected read body failure")

        def fail_dataset_unlock(descriptor: int, action: int) -> object:
            nonlocal failed_unlock
            metadata = os.fstat(descriptor)
            if (
                not failed_unlock
                and action == fcntl.LOCK_UN
                and stat.S_ISREG(metadata.st_mode)
            ):
                failed_unlock = True
                raise OSError("injected dataset unlock failure")
            return original_flock(descriptor, action)

        monkeypatch.setattr(lake, "_read_snapshot", fail_body)
        monkeypatch.setattr(fcntl, "flock", fail_dataset_unlock)
        baseline = _open_descriptor_count()

        with pytest.raises(BaseException) as raised:
            lake.read(stored.manifest_record_id)

        errors = _exception_tree(raised.value)
        assert any(
            isinstance(error, RuntimeError)
            and str(error) == "injected read body failure"
            for error in errors
        )
        assert any(
            isinstance(error, OSError)
            and str(error) == "injected dataset unlock failure"
            for error in errors
        )
        _probe_namespace_guard(root)
        assert _open_descriptor_count() == baseline


def test_multiple_unlock_failures_are_grouped_after_context_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        original_flock = fcntl.flock
        failed_layers: set[str] = set()

        def fail_both_unlocks(descriptor: int, action: int) -> object:
            metadata = os.fstat(descriptor)
            layer = "namespace" if stat.S_ISDIR(metadata.st_mode) else "dataset"
            if action == fcntl.LOCK_UN and layer not in failed_layers:
                failed_layers.add(layer)
                raise OSError(f"injected {layer} unlock failure")
            return original_flock(descriptor, action)

        monkeypatch.setattr(fcntl, "flock", fail_both_unlocks)
        baseline = _open_descriptor_count()

        with pytest.raises(BaseExceptionGroup) as raised:
            lake.read(stored.manifest_record_id)

        errors = _exception_tree(raised.value)
        assert {str(error) for error in errors if isinstance(error, OSError)} >= {
            "injected dataset unlock failure",
            "injected namespace unlock failure",
        }
        assert failed_layers == {"dataset", "namespace"}
        _probe_namespace_guard(root)
        assert _open_descriptor_count() == baseline


def test_context_close_failure_still_closes_other_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        original_context = lake._open_operation_context
        original_close = os.close
        context_descriptors: list[tuple[int, int]] = []
        close_attempts: list[int] = []
        failed_close = False

        def record_context() -> object:
            context = original_context()
            context_descriptors.append(
                (context.locks_descriptor, context.root_descriptor)
            )
            return context

        def fail_first_context_close(descriptor: int) -> None:
            nonlocal failed_close
            if context_descriptors and descriptor in context_descriptors[0]:
                close_attempts.append(descriptor)
            locks_descriptor = (
                context_descriptors[0][0] if context_descriptors else None
            )
            if not failed_close and descriptor == locks_descriptor:
                failed_close = True
                original_close(descriptor)
                raise OSError("injected context close failure")
            original_close(descriptor)

        monkeypatch.setattr(lake, "_open_operation_context", record_context)
        monkeypatch.setattr(os, "close", fail_first_context_close)
        baseline = _open_descriptor_count()

        with pytest.raises(OSError, match="context close failure"):
            lake.read(stored.manifest_record_id)

        locks_descriptor, root_descriptor = context_descriptors[0]
        assert close_attempts == [locks_descriptor, root_descriptor]
        _probe_namespace_guard(root)
        assert _open_descriptor_count() == baseline
