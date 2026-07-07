from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from pydantic import ValidationError
import pytest

from stock_desk.backtest.config import BacktestRequest
from stock_desk.backtest.snapshot import freeze_request
from stock_desk.backtest.types import FrozenSymbolGap, GapReason, PinnedMarketRef
from stock_desk.formula.signal_series import NormalizedParameter
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.types import Adjustment, BarQuery, Exchange, Period, ProviderId


UTC = timezone.utc
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64
DIGEST_F = "sha256:" + "f" * 64
DIGEST_0 = "sha256:" + "0" * 64


def _query(
    symbol: str = "600000.SH",
    *,
    period: Period = Period.DAY,
    adjustment: Adjustment = Adjustment.QFQ,
) -> BarQuery:
    return BarQuery(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=datetime(2020, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _pinned(symbol: str = "600000.SH") -> PinnedMarketRef:
    query = _query(symbol)
    return PinnedMarketRef(
        symbol=symbol,
        signal_manifest_record_id=DIGEST_A,
        signal_dataset_version=DIGEST_B,
        signal_route_version=DIGEST_C,
        signal_source=ProviderId.TUSHARE,
        signal_data_cutoff=datetime(2024, 12, 31, 7, tzinfo=UTC),
        signal_query=query,
        execution_manifest_record_id=DIGEST_D,
        execution_dataset_version=DIGEST_E,
        execution_route_version=DIGEST_F,
        execution_source=ProviderId.AKSHARE,
        execution_data_cutoff=datetime(2024, 12, 31, 8, tzinfo=UTC),
        execution_query=query,
        execution_status_manifest_record_id=DIGEST_F,
        execution_status_dataset_version=DIGEST_A,
        execution_status_route_version=DIGEST_B,
        execution_status_source=ProviderId.TDX_LOCAL,
        execution_status_data_cutoff=datetime(2024, 12, 31, 9, tzinfo=UTC),
        execution_status_query=ExecutionStatusQuery(
            symbol=symbol,
            exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
            start=query.start.date(),
            end=query.end.date(),
            period=query.period,
        ),
    )


def _replace_pinned(
    reference: PinnedMarketRef,
    **updates: object,
) -> PinnedMarketRef:
    return PinnedMarketRef.model_validate(reference.model_dump(mode="python") | updates)


def _gap(
    symbol: str = "000001.SZ",
    *,
    reason: GapReason = "missing_execution_status",
) -> FrozenSymbolGap:
    query = _query(symbol)
    return FrozenSymbolGap(
        symbol=symbol,
        reason=reason,
        signal_query=query,
        execution_query=query,
        checked_instrument_dataset_version=DIGEST_A,
        checked_signal_catalog_version=DIGEST_B,
        checked_execution_catalog_version=DIGEST_C,
        checked_status_catalog_version=DIGEST_D,
    )


def _replace_gap(gap: FrozenSymbolGap, **updates: object) -> FrozenSymbolGap:
    return FrozenSymbolGap.model_validate(gap.model_dump(mode="python") | updates)


def _request(**updates: object) -> BacktestRequest:
    values: dict[str, object] = {
        "scope_kind": "single",
        "scope_id": None,
        "scope_revision_or_snapshot_id": None,
        "instrument_dataset_version": DIGEST_A,
        "symbols": ("600000.SH",),
        "formula_version_id": "macd-v1",
        "formula_checksum": DIGEST_B,
        "formula_engine_version": "formula-engine-v1",
        "compatibility_version": "tdx-v1",
        "formula_parameters": (
            NormalizedParameter(name="FAST", kind="integer", value="12"),
            NormalizedParameter(name="SLOW", kind="integer", value="26"),
        ),
        "warmup_policy_version": "formula-warmup-v1",
        "symbol_inputs": (_pinned(),),
        "period": Period.DAY,
        "adjustment": Adjustment.QFQ,
        "scoring_start": datetime(2021, 1, 1, tzinfo=UTC),
        "scoring_end": datetime(2024, 1, 1, tzinfo=UTC),
        "quantity_shares": 1_000,
        "commission_bps": Decimal("2.5000"),
        "minimum_commission": Decimal("5.00"),
        "sell_tax_bps": Decimal("5.0"),
        "slippage_bps": Decimal("3.00"),
        "cost_model_version": "a-share-cost-v1",
        "backtest_engine_version": "backtest-engine-v1",
        "execution_rules_version": "a-share-v1",
    }
    values.update(updates)
    return BacktestRequest.model_validate(values)


def test_freeze_is_stable_immutable_and_normalizes_decimal_identity() -> None:
    original = freeze_request(_request())
    equivalent = freeze_request(
        _request(
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5.000"),
            slippage_bps=Decimal("3"),
        )
    )

    assert original == equivalent
    assert original.snapshot_schema_version == "backtest-snapshot-v1"
    assert original.quantity_shares == 1_000
    assert (
        original.snapshot_id
        == "sha256:" + hashlib.sha256(original.canonical_identity_bytes()).hexdigest()
    )
    assert original.canonical_bytes() == json.dumps(
        original.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    assert b'"commission_bps":"2.5"' in original.canonical_bytes()

    with pytest.raises(ValidationError, match="frozen"):
        original.quantity_shares = 2_000
    with pytest.raises(TypeError, match="does not accept update"):
        original.model_copy(update={"quantity_shares": 2_000})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("scope_kind", "custom"),
        ("instrument_dataset_version", DIGEST_C),
        ("formula_version_id", "macd-v2"),
        ("formula_checksum", DIGEST_C),
        ("formula_engine_version", "formula-engine-v2"),
        ("compatibility_version", "tdx-v2"),
        ("period", Period.WEEK),
        ("adjustment", Adjustment.HFQ),
        ("scoring_start", datetime(2021, 1, 2, tzinfo=UTC)),
        ("scoring_end", datetime(2023, 12, 31, tzinfo=UTC)),
        ("quantity_shares", 2_000),
        ("commission_bps", Decimal("3")),
        ("minimum_commission", Decimal("6")),
        ("sell_tax_bps", Decimal("6")),
        ("slippage_bps", Decimal("8")),
        ("backtest_engine_version", "backtest-engine-v2"),
    ],
)
def test_snapshot_hash_changes_for_each_material_execution_input(
    field: str,
    replacement: object,
) -> None:
    original_request = _request()
    changes: dict[str, object] = {field: replacement}
    if field == "scope_kind":
        changes.update(
            scope_id="my-pool",
            scope_revision_or_snapshot_id="revision-2",
        )
    if field == "period":
        changes["symbol_inputs"] = (
            _replace_pinned(
                _pinned(),
                signal_query=_query(period=Period.WEEK),
                execution_query=_query(period=Period.DAY),
            ),
        )
    if field == "adjustment":
        changes["symbol_inputs"] = (
            _replace_pinned(
                _pinned(),
                signal_query=_query(adjustment=Adjustment.HFQ),
                execution_query=_query(adjustment=Adjustment.HFQ),
            ),
        )

    changed_request = BacktestRequest.model_validate(
        original_request.model_dump(mode="python") | changes
    )

    assert (
        freeze_request(original_request).snapshot_id
        != freeze_request(changed_request).snapshot_id
    )


def test_snapshot_hash_covers_parameters_scope_composition_refs_and_gaps() -> None:
    original = freeze_request(_request())
    parameter_change = freeze_request(
        _request(
            formula_parameters=(
                NormalizedParameter(name="FAST", kind="integer", value="13"),
                NormalizedParameter(name="SLOW", kind="integer", value="26"),
            )
        )
    )
    changed_ref = _replace_pinned(_pinned(), signal_manifest_record_id=DIGEST_0)
    ref_change = freeze_request(_request(symbol_inputs=(changed_ref,)))
    pool_with_gap = freeze_request(
        _request(
            scope_kind="preset",
            scope_id="csi-300",
            scope_revision_or_snapshot_id=DIGEST_E,
            symbols=("600000.SH", "000001.SZ"),
            symbol_inputs=(_pinned(), _gap()),
        )
    )
    changed_gap = freeze_request(
        _request(
            scope_kind="preset",
            scope_id="csi-300",
            scope_revision_or_snapshot_id=DIGEST_E,
            symbols=("600000.SH", "000001.SZ"),
            symbol_inputs=(
                _pinned(),
                _gap(reason="missing_execution_data"),
            ),
        )
    )

    assert (
        len(
            {
                original.snapshot_id,
                parameter_change.snapshot_id,
                ref_change.snapshot_id,
                pool_with_gap.snapshot_id,
                changed_gap.snapshot_id,
            }
        )
        == 5
    )


def test_snapshot_hash_covers_every_gap_catalog_identity() -> None:
    base_request = _request(
        scope_kind="preset",
        scope_id="csi-300",
        scope_revision_or_snapshot_id=DIGEST_E,
        symbols=("600000.SH", "000001.SZ"),
        symbol_inputs=(_pinned(), _gap()),
    )
    original = freeze_request(base_request)
    changes: tuple[tuple[str, str], ...] = (
        ("checked_signal_catalog_version", DIGEST_C),
        ("checked_execution_catalog_version", DIGEST_D),
        ("checked_status_catalog_version", DIGEST_E),
    )
    changed_ids = {
        freeze_request(
            BacktestRequest.model_validate(
                base_request.model_dump(mode="python")
                | {
                    "symbol_inputs": (
                        _pinned(),
                        _replace_gap(_gap(), **{field: replacement}),
                    )
                }
            )
        ).snapshot_id
        for field, replacement in changes
    }
    changed_instrument = freeze_request(
        BacktestRequest.model_validate(
            base_request.model_dump(mode="python")
            | {
                "instrument_dataset_version": DIGEST_B,
                "symbol_inputs": (
                    _pinned(),
                    _replace_gap(_gap(), checked_instrument_dataset_version=DIGEST_B),
                ),
            }
        )
    )

    assert original.snapshot_id not in changed_ids
    assert changed_instrument.snapshot_id != original.snapshot_id
    assert len(changed_ids) == len(changes)


def test_gap_instrument_version_must_match_snapshot_catalog() -> None:
    with pytest.raises(ValidationError, match="instrument dataset version"):
        _request(
            scope_kind="preset",
            scope_id="csi-300",
            scope_revision_or_snapshot_id=DIGEST_E,
            symbols=("600000.SH", "000001.SZ"),
            symbol_inputs=(
                _pinned(),
                _replace_gap(_gap(), checked_instrument_dataset_version=DIGEST_B),
            ),
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"symbols": (), "symbol_inputs": ()}, "symbols"),
        ({"symbols": ("600000.SH", "600000.SH")}, "unique"),
        ({"quantity_shares": 0}, "quantity_shares"),
        ({"quantity_shares": 150}, "100-share"),
        ({"commission_bps": Decimal("-1")}, "cost"),
        ({"minimum_commission": Decimal("-1")}, "cost"),
        ({"sell_tax_bps": Decimal("10001")}, "basis points"),
        ({"slippage_bps": Decimal("NaN")}, "finite"),
        ({"scoring_end": datetime(2021, 1, 1, tzinfo=UTC)}, "scoring"),
        ({"scope_id": "unexpected"}, "single"),
        (
            {
                "scope_kind": "custom",
                "scope_id": None,
                "scope_revision_or_snapshot_id": None,
            },
            "scope",
        ),
    ],
)
def test_request_rejects_invalid_execution_contracts(
    updates: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**updates)


def test_request_requires_one_ordered_input_per_ordered_symbol() -> None:
    with pytest.raises(ValidationError, match="ordered symbol_inputs"):
        _request(
            scope_kind="custom",
            scope_id="watchlist",
            scope_revision_or_snapshot_id="4",
            symbols=("600000.SH", "000001.SZ"),
            symbol_inputs=(_gap(), _pinned()),
        )

    with pytest.raises(ValidationError, match="ordered symbol_inputs"):
        _request(symbol_inputs=())


def test_request_rejects_unsorted_parameters_and_inconsistent_queries() -> None:
    with pytest.raises(ValidationError, match="parameters"):
        _request(
            formula_parameters=(
                NormalizedParameter(name="SLOW", kind="integer", value="26"),
                NormalizedParameter(name="FAST", kind="integer", value="12"),
            )
        )

    mismatched = _replace_pinned(
        _pinned(), signal_query=_query(adjustment=Adjustment.NONE)
    )
    with pytest.raises(ValidationError, match="adjustment"):
        _request(symbol_inputs=(mismatched,))

    short_query = BarQuery.model_validate(
        _query().model_dump(mode="python") | {"start": datetime(2022, 1, 1, tzinfo=UTC)}
    )
    too_short = _replace_pinned(
        _pinned(),
        signal_query=short_query,
    )
    with pytest.raises(ValidationError, match="scoring range"):
        _request(symbol_inputs=(too_short,))


def test_reference_and_gap_reject_cross_symbol_queries() -> None:
    with pytest.raises(ValidationError, match="symbol"):
        _replace_pinned(_pinned(), symbol="000001.SZ")

    with pytest.raises(ValidationError, match="symbol"):
        FrozenSymbolGap(
            symbol="000001.SZ",
            reason="missing_signal_data",
            signal_query=_query("600000.SH"),
            execution_query=_query("000001.SZ"),
            checked_instrument_dataset_version=DIGEST_A,
            checked_signal_catalog_version=DIGEST_B,
            checked_execution_catalog_version=DIGEST_C,
            checked_status_catalog_version=DIGEST_D,
        )


def test_reference_revalidates_nested_query_instances() -> None:
    unsafe_query = _query().model_copy(update={"end": datetime(2071, 1, 1, tzinfo=UTC)})

    with pytest.raises(ValidationError, match="maximum calendar span"):
        _replace_pinned(_pinned(), signal_query=unsafe_query)


def test_snapshot_reader_rejects_tampered_id_and_noncanonical_bytes() -> None:
    snapshot = freeze_request(_request())
    tampered = snapshot.model_dump(mode="python")
    tampered["snapshot_id"] = DIGEST_0

    with pytest.raises(ValidationError, match="snapshot_id"):
        type(snapshot).model_validate(tampered)
    with pytest.raises(ValueError, match="canonical"):
        type(snapshot).from_canonical_bytes(b" " + snapshot.canonical_bytes())


def test_scoring_datetimes_are_normalized_to_utc() -> None:
    offset = timezone(timedelta(hours=8))
    snapshot = freeze_request(
        _request(
            scoring_start=datetime(2021, 1, 1, 8, tzinfo=offset),
            scoring_end=datetime(2024, 1, 1, 8, tzinfo=offset),
        )
    )

    assert snapshot.scoring_start == datetime(2021, 1, 1, tzinfo=UTC)
    assert snapshot.scoring_end == datetime(2024, 1, 1, tzinfo=UTC)
