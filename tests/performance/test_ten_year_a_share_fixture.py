from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from stock_desk.market.types import ProviderId
from tests.performance.ten_year_a_share import (
    FIXTURE_PATH,
    generate_fixture_bars,
    load_fixture_metadata,
)


def test_fixture_is_deterministic_cc0_network_forbidden_and_ten_year_daily() -> None:
    metadata = load_fixture_metadata()
    first = generate_fixture_bars(metadata)
    second = generate_fixture_bars(metadata)

    assert metadata.label == "SYNTHETIC PERFORMANCE FIXTURE — NOT VENDOR DATA"
    assert metadata.license == "CC0-1.0"
    assert metadata.network_policy == "forbidden"
    assert metadata.source is ProviderId.STOCK_DESK_DEMO
    assert metadata.scoring_start == date(2016, 1, 1)
    assert metadata.scoring_end == date(2026, 1, 1)
    assert metadata.scope_instrument_count == 5_000
    assert metadata.runnable_symbol_count == 40
    assert len(first.bars) >= 2_400
    assert first == second
    assert metadata.row_count == len(first.bars)
    assert metadata.content_digest == first.content_digest


def test_fixture_file_contains_metadata_not_a_committed_giant_dataset() -> None:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert "bars" not in raw
    assert FIXTURE_PATH.stat().st_size < 8_192
    assert Path(raw["generator"]) == Path("tests/performance/ten_year_a_share.py")
    assert raw["scope_instrument_count"] == 5_000
    assert raw["runnable_symbol_count"] == 40
