# ruff: noqa: E402

"""Prepare public deterministic inputs for packaged Windows backtest evidence.

This script only creates fixture state. Every backtest is submitted later from the
installed Tauri WebView through the authenticated host IPC bridge and is executed
by the packaged sidecar worker.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.v1_backtest_oracle import case_specs, load_inputs, load_oracle
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.formula.repository import FormulaRepository
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Exchange, InstrumentKind, Period, ProviderId
from stock_desk.onboarding.models import (
    OnboardingInstrument,
    OnboardingSource,
    OnboardingState,
    OnboardingStatus,
    OnboardingStep,
    OnboardingSynchronization,
    SynchronizationStatus,
)
from stock_desk.onboarding.store import OnboardingStateStore
from tests.backtest_test_helpers import (
    BacktestHarness,
    OPEN_ONLY_FORMULA,
    WAVE_FORMULA,
    intraday_timestamps,
    routed_bars_from_closes,
    routed_status,
    weekday_range,
    weekly_timestamps,
)
from stock_desk.storage.database import create_engine_for_url
from stock_desk.tasks.repository import TaskRepository


INPUTS_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"
ORACLE_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle.json"
GENERATOR_PATH = ROOT / "scripts/v1_backtest_oracle.py"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_FIXTURE_IDS = {
    "matrix_1d",
    "matrix_1w",
    "matrix_60m",
    "a_share_constraints_60m",
    "open_position_costs_1d",
    "partial_pool_gap_1d",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timeline(period: Period) -> Sequence[date | datetime]:
    start = date(2024, 1, 1)
    if period is Period.DAY:
        return weekday_range(start, date(2024, 6, 1))
    if period is Period.WEEK:
        return weekly_timestamps(start, 64)
    return intraday_timestamps(start, trading_days=45)


def _scoring_range(period: Period) -> tuple[str, str]:
    values = _timeline(period)
    raw_start = values[45]
    start = (
        raw_start
        if isinstance(raw_start, datetime)
        else datetime.combine(
            raw_start,
            datetime.min.time(),
            tzinfo=timezone(timedelta(hours=8)),
        )
    )
    raw_end = values[-1]
    end_value = (
        raw_end
        if isinstance(raw_end, datetime)
        else datetime.combine(
            raw_end,
            datetime.min.time(),
            tzinfo=timezone(timedelta(hours=8)),
        )
    )
    delta = (
        timedelta(days=7)
        if period is Period.WEEK
        else timedelta(hours=1)
        if period is Period.MIN60
        else timedelta(days=1)
    )
    return start.isoformat(), (end_value + delta).isoformat()


def prepare(destination: Path, *, source_sha: str, source_tree: str) -> dict[str, Any]:
    if _HEX40.fullmatch(source_sha) is None or _HEX40.fullmatch(source_tree) is None:
        raise ValueError("source SHA and tree must be exact lowercase Git object ids")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("packaged evidence destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    inputs = load_inputs(INPUTS_PATH)
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    matrix = inputs["matrix"]
    period_symbols = {period: tuple(matrix["symbols"]) for period in Period}
    all_matrix_symbols = tuple(
        dict.fromkeys(
            symbol for symbols in period_symbols.values() for symbol in symbols
        )
    )
    formula_records: dict[str, dict[str, object]] = {}
    with BacktestHarness.create(destination) as harness:
        harness.seed_instruments(*all_matrix_symbols)
        formulas = FormulaRepository(harness.engine)
        for item in matrix["formulas"]:
            version = formulas.create(
                item["name"],
                "trading",
                item["source"],
                item["parameter_schema"],
                placement="subchart",
            )
            formula_records[item["id"]] = {
                "name": item["name"],
                "formula_id": version.formula_id,
                "version_id": version.id,
                "checksum": version.checksum,
                "parameters": item["parameters"],
            }
        special_formula_specs = {
            "a_share_constraints_60m": ("约束事件链", "BUY:C=11;SELL:C=9;"),
            "open_position_costs_1d": ("未平仓成本", OPEN_ONLY_FORMULA),
            "partial_pool_gap_1d": ("部分数据池", WAVE_FORMULA),
        }
        special_formulas = {}
        for case_id, (name, source_code) in special_formula_specs.items():
            version = formulas.create(
                name, "trading", source_code, {}, placement="subchart"
            )
            special_formulas[case_id] = {
                "name": name,
                "formula_id": version.formula_id,
                "version_id": version.id,
                "checksum": version.checksum,
                "parameters": {},
            }
        preset_pool = harness.pools.publish_full_a(
            preset_key="packaged-all-a", display_name="打包回测全部A股"
        )

    database = destination / "backtest-harness.db"
    database.replace(destination / "stock-desk.db")
    now = datetime(2024, 7, 1, tzinfo=timezone.utc)
    evidence_digest = "sha256:" + _sha256(INPUTS_PATH)
    source = OnboardingSource(
        id=ProviderId.AKSHARE,
        label="打包回测公开确定性夹具",
        catalog_manifest_record_id=evidence_digest,
        catalog_dataset_version=evidence_digest,
        data_cutoff=now,
    )
    sync = OnboardingSynchronization(
        status=SynchronizationStatus.VERIFIED,
        provider_id=source.id,
        manifest_record_id=evidence_digest,
        dataset_version=evidence_digest,
        data_cutoff=now,
        row_count=2,
    )
    OnboardingStateStore(
        (destination / "onboarding" / "state-v1.json").resolve(), clock=lambda: now
    ).save(
        OnboardingState(
            revision=1,
            status=OnboardingStatus.COMPLETED,
            current_step=OnboardingStep.COMPLETED,
            source=source,
            instrument=OnboardingInstrument(
                symbol="600000.SH",
                name="浦发银行",
                exchange=Exchange.SH,
                instrument_kind=InstrumentKind.STOCK,
            ),
            sync=sync,
            updated_at=now,
        )
    )

    normal_cases = [item for item in case_specs(inputs) if item["kind"] == "matrix"]
    manifest: dict[str, Any] = {
        "schema_version": "stock-desk-packaged-backtest-seed-v1",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "public_fixture": True,
        "read_only_demo": False,
        "oracle": {
            "source": oracle["source"],
            "oracle_sha256": _sha256(ORACLE_PATH),
            "inputs_sha256": _sha256(INPUTS_PATH),
            "generator_sha256": _sha256(GENERATOR_PATH),
            "payload_digest": oracle["payload_digest"],
        },
        "matrix_case_ids": [str(item["id"]) for item in normal_cases],
        "oracle_case_semantic_digests": {
            str(item["id"]): oracle["cases"][str(item["id"])]["semantic_digest"]
            for item in case_specs(inputs)
        },
        "formulas": formula_records,
        "pools": {
            period.value: {
                "pool_id": preset_pool.pool_id,
                "snapshot_id": preset_pool.snapshot_id,
                "name": preset_pool.composition.display_name,
                "symbols": list(period_symbols[period]),
            }
            for period in (Period.DAY, Period.WEEK, Period.MIN60)
        },
        "special_cases": {
            "a_share_constraints_60m": {
                "formula": special_formulas["a_share_constraints_60m"],
                "scope": {"kind": "single", "symbol": "600000.SH"},
                "period": "60m",
                "scoring_start": "2024-01-02T09:30:00+08:00",
                "scoring_end": "2024-01-08T15:00:00+08:00",
            },
            "open_position_costs_1d": {
                "formula": special_formulas["open_position_costs_1d"],
                "scope": {"kind": "single", "symbol": "600000.SH"},
                "period": "1d",
                "scoring_start": "2024-01-08T00:00:00+08:00",
                "scoring_end": "2024-03-01T00:00:00+08:00",
            },
            "partial_pool_gap_1d": {
                "formula": special_formulas["partial_pool_gap_1d"],
                "scope": {
                    "kind": "preset",
                    "pool_id": preset_pool.pool_id,
                    "snapshot_id": preset_pool.snapshot_id,
                },
                "period": "1d",
                "scoring_start": "2024-01-08T00:00:00+08:00",
                "scoring_end": "2024-05-01T00:00:00+08:00",
            },
        },
        "costs": matrix["costs"],
        "periods": {
            period.value: dict(
                zip(
                    ("scoring_start", "scoring_end"),
                    _scoring_range(period),
                    strict=True,
                )
            )
            for period in (Period.DAY, Period.WEEK, Period.MIN60)
        },
    }
    output = destination / "packaged-backtest-seed.json"
    output.write_text(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _open_packaged_harness(destination: Path) -> BacktestHarness:
    engine = create_engine_for_url(f"sqlite:///{destination / 'stock-desk.db'}")
    return BacktestHarness(
        engine=engine,
        market=MarketLake(engine=engine, root=(destination / "market").resolve()),
        statuses=ExecutionStatusLake(engine),
        instruments=InstrumentRepository(engine),
        pools=PoolRepository(engine),
        tasks=TaskRepository(engine),
        formula_repository=FormulaRepository(engine),
        repository=BacktestRepository(engine),
    )


def switch_fixture(destination: Path, fixture_id: str) -> None:
    if fixture_id not in _FIXTURE_IDS:
        raise ValueError("unknown packaged fixture id")
    if not (destination / "stock-desk.db").is_file():
        raise ValueError("packaged fixture database is missing")
    with _open_packaged_harness(destination) as harness:
        # The installed sidecar polls TaskRepository.claim_next() in a separate
        # process. Coordinate evidence-only catalog publication through the same
        # cross-process gate used by production backup/restore so a SQLite write
        # lock cannot terminate that polling loop between fixture handshakes.
        with harness.tasks.hold_claim_gate(timeout_seconds=30):
            if fixture_id.startswith("matrix_"):
                period = Period(fixture_id.removeprefix("matrix_"))
                values = _timeline(period)
                for offset, symbol in enumerate(("600000.SH", "000001.SZ")):
                    harness.seed_symbol(symbol, period, values, phase_offset=offset * 3)
                return
            if fixture_id == "a_share_constraints_60m":
                timestamps = intraday_timestamps(date(2024, 1, 2), trading_days=5)
                days = tuple(
                    dict.fromkeys(timestamp.date() for timestamp in timestamps)
                )
                closes = [Decimal("10")] * len(timestamps)
                closes[0], closes[1], closes[7], closes[13] = (
                    Decimal("11"),
                    Decimal("9"),
                    Decimal("11"),
                    Decimal("9"),
                )
                bars = routed_bars_from_closes(
                    "600000.SH", Period.MIN60, timestamps, tuple(closes)
                )
                harness.market.write(bars)
                harness.statuses.write(
                    routed_status(
                        "600000.SH",
                        Period.MIN60,
                        bars,
                        suspended_days=frozenset({days[2]}),
                        raw_open_overrides={
                            timestamps[1]: Decimal("12"),
                            timestamps[12]: Decimal("12"),
                            timestamps[16]: Decimal("8"),
                        },
                    )
                )
                return
            if fixture_id == "open_position_costs_1d":
                days = weekday_range(date(2024, 1, 1), date(2024, 3, 1))
                harness.seed_symbol("600000.SH", Period.DAY, days)
                return
            days = weekday_range(date(2024, 1, 1), date(2024, 5, 1))
            harness.seed_symbol("600000.SH", Period.DAY, days)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--switch-fixture", choices=sorted(_FIXTURE_IDS))
    args = parser.parse_args(argv)
    destination = args.destination.resolve()
    if args.switch_fixture is None:
        manifest = prepare(
            destination,
            source_sha=args.source_sha,
            source_tree=args.source_tree,
        )
        print(json.dumps(manifest, ensure_ascii=True, sort_keys=True))
    else:
        switch_fixture(destination, args.switch_fixture)
        print(json.dumps({"fixture_id": args.switch_fixture}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
