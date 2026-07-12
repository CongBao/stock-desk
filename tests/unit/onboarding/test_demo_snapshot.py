from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from stock_desk.onboarding import demo_snapshot
from stock_desk.onboarding.demo_snapshot import (
    BundledDemoManifest,
    BundledDemoMarket,
    load_bundled_demo_manifest,
)
from stock_desk.market.types import Adjustment, Period, ProviderId


def test_bundled_demo_snapshot_is_synthetic_traceable_and_immediately_readable(
    tmp_path: Path,
) -> None:
    demo = BundledDemoMarket.open(tmp_path / "v1.1")
    try:
        assert demo.label == "公开合成演示数据 · 非真实行情"
        assert demo.instrument.symbol == "600000.SH"
        assert "合成演示" in demo.instrument.name

        catalog = demo.services.instruments.get("600000.SH")
        assert catalog.manifest.source is ProviderId.STOCK_DESK_DEMO
        routed = demo.services.lake.read_latest_series(
            "600000.SH",
            Period.DAY,
            Adjustment.NONE,
        )
        assert routed is not None
        assert routed.result.provenance.source is ProviderId.STOCK_DESK_DEMO
        assert len(routed.result.bars) >= 60
    finally:
        demo.close()


def test_bundled_demo_snapshot_is_idempotent_and_isolated_from_real_database(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "v1.1"
    first = BundledDemoMarket.open(data_dir)
    first_identity = first.services.database_identity
    first.close()

    second = BundledDemoMarket.open(data_dir)
    try:
        assert second.services.database_identity == first_identity
        assert (data_dir / "demo-market" / "stock-desk-demo.db").is_file()
        assert not (data_dir / "stock-desk.db").exists()
    finally:
        second.close()


def test_bundled_demo_snapshot_rejects_incomplete_or_tampered_storage(
    tmp_path: Path,
) -> None:
    incomplete_data = tmp_path / "incomplete"
    (incomplete_data / "demo-market").mkdir(parents=True)
    with pytest.raises(ValueError, match="storage is incomplete"):
        BundledDemoMarket.open(incomplete_data)

    tampered_data = tmp_path / "tampered"
    demo = BundledDemoMarket.open(tampered_data)
    demo.close()
    marker = tampered_data / "demo-market" / ".stock-desk-bundled-demo-v1.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["fixture_id"] = "unexpected-fixture"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="storage identity mismatch"):
        BundledDemoMarket.open(tampered_data)


def test_bundled_demo_manifest_requires_an_aware_generation_time() -> None:
    payload = load_bundled_demo_manifest().model_dump(mode="python")
    generated_at = payload["generated_at"]
    assert isinstance(generated_at, datetime)
    payload["generated_at"] = generated_at.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-aware"):
        BundledDemoManifest.model_validate(payload)


def test_bundled_demo_seed_failure_removes_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "failed-seed"

    def fail_seed(_root: Path, _manifest: BundledDemoManifest) -> None:
        raise RuntimeError("simulated seed failure")

    monkeypatch.setattr(demo_snapshot, "_seed", fail_seed)
    with pytest.raises(RuntimeError, match="simulated seed failure"):
        BundledDemoMarket.open(data_dir)

    assert not (data_dir / "demo-market").exists()
    assert not tuple(data_dir.glob(".demo-market-staging-*"))
