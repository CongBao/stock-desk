from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from filelock import FileLock, Timeout
import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_read_roundtrips_multiyear_routed_bars_exactly(tmp_path: Path) -> None:
    routed = routed_daily_bars((date(2023, 12, 29), date(2024, 1, 2), date(2024, 1, 3)))
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)

        restored = lake.read(stored.manifest_record_id)

    assert restored == routed
    assert tuple(bar.timestamp for bar in restored.result.bars) == tuple(
        sorted({bar.timestamp for bar in restored.result.bars})
    )
    assert all(
        bar.timestamp.utcoffset() == timedelta(0) for bar in restored.result.bars
    )
    assert all(
        isinstance(price, Decimal)
        for bar in restored.result.bars
        for price in (bar.open, bar.high, bar.low, bar.close)
    )
    assert min(bar.low for bar in restored.result.bars) < 0
    assert restored.result.bars[-1].volume == 2**63 - 1


def test_read_missing_manifest_raises_typed_not_found(tmp_path: Path) -> None:
    not_found_error = getattr(lake_module, "MarketLakeNotFoundError")
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")

        with pytest.raises(not_found_error):
            lake.read(f"sha256:{'0' * 64}")


def test_read_reloads_catalog_snapshot_while_dataset_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        routed = routed_daily_bars((date(2024, 1, 2),))
        stored = lake.write(routed)
        original_load = lake._load_catalog_snapshot

        def assert_locked(record_id: str, dataset_version: object) -> object:
            digest = str(dataset_version).removeprefix("sha256:")
            contender = FileLock(lake._locks / f"{digest}.lock")
            with pytest.raises(Timeout):
                contender.acquire(timeout=0)
            return original_load(record_id, dataset_version)

        monkeypatch.setattr(lake, "_load_catalog_snapshot", assert_locked)

        assert lake.read(stored.manifest_record_id) == routed


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_read_does_not_mask_internal_programming_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))

        def fail_internally(_snapshot: object, **_kwargs: object) -> object:
            raise error_type("internal read bug")

        monkeypatch.setattr(lake, "_read_snapshot", fail_internally)

        with pytest.raises(error_type, match="internal read bug"):
            lake.read(stored.manifest_record_id)
