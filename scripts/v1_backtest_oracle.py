from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import UUID


V1_COMMIT = "cd62bd3ba87518c79c027791e8e370c359f3e645"
V1_TREE = "fcda96626c1482cebff1de4fb3803573bb3a7d19"
EXPECTED_ALLOWED_DIFFERENCE_IDS = {
    "instrument-kind-identity-v1.1",
    "content-addressed-runtime-identities",
    "a-share-price-canonicalization-v1.1",
    "desktop-checkpoint-extension-v1.1",
}
EXPECTED_ALLOWED_DIFFERENCES = [
    {
        "id": "instrument-kind-identity-v1.1",
        "paths": ["snapshot.symbol_inputs[].instrument_kind"],
        "normalization": "excluded-from-v1-semantic-projection-but-validated-by-current-domain-contract",
    },
    {
        "id": "content-addressed-runtime-identities",
        "paths": [
            "run.run_id",
            "run.task_id",
            "run.result_hash",
            "snapshot.snapshot_id",
            "snapshot.formula_version_id",
            "snapshot.scope_id",
            "snapshot.scope_revision_or_snapshot_id",
            "snapshot.symbol_inputs[].manifest_record_id",
            "snapshot.symbol_inputs[].dataset_version",
            "snapshot.symbol_inputs[].route_version",
            "symbols[].signal_series_id",
            "payloads[].formula_version_id",
            "payloads[].signal_series_id",
            "payloads[].market_manifest_ids",
            "payloads[].status_manifest_ids",
            "report.provenance_digest",
        ],
        "normalization": "replace-with-typed-identity-token-and-validate-shape-and-cross-reference",
    },
    {
        "id": "a-share-price-canonicalization-v1.1",
        "paths": [
            "order_events[].payload.price",
            "order_events[].payload.entry_price",
            "order_events[].payload.mark_price",
            "order_events[].payload.floating_pnl",
        ],
        "normalization": "round-versioned-market-prices-to-the-v1.1-four-decimal-contract-before-semantic-comparison",
    },
    {
        "id": "desktop-checkpoint-extension-v1.1",
        "paths": [
            "checkpoint.resume_metadata",
            "collections.logs[message=run_started,detail.attempt>1]",
            "collections.logs[].ordinal_after_resume",
        ],
        "normalization": "remove-only-the-exact-extra-run_started-attempt-and-renumber-log-ordinals-before-comparing-resumed-v1.1-to-uninterrupted-v1.0",
    },
]
EXPECTED_MATRIX = {
    "formulas": [
        {
            "id": "macd",
            "name": "MACD 金叉死叉",
            "source": "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
            "parameter_schema": {},
            "parameters": {},
        },
        {
            "id": "custom",
            "name": "参数化自定义均线",
            "source": "MID:MA(C,N);BUY:CROSS(C,MID);SELL:CROSS(MID,C);",
            "parameter_schema": {"N": {"kind": "integer", "default": 5}},
            "parameters": {"N": 7},
        },
    ],
    "scopes": ["single", "pool"],
    "periods": ["1d", "1w", "60m"],
    "symbols": ["600000.SH", "000001.SZ"],
    "costs": {
        "quantity_shares": 1000,
        "commission_bps": "2.5",
        "minimum_commission": "5",
        "sell_tax_bps": "5",
        "slippage_bps": "3",
    },
}
EXPECTED_SPECIAL_CASES = [
    {"id": "a_share_constraints_60m", "kind": "a_share_constraints"},
    {"id": "open_position_costs_1d", "kind": "open_position_costs"},
    {"id": "partial_pool_gap_1d", "kind": "partial_pool_gap"},
]
ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle.json"
_INPUT_SCHEMA = "stock-desk-v1-backtest-oracle-input-v1"
_ORACLE_SCHEMA = "stock-desk-v1-backtest-oracle-v1"
_IDENTITY_KEYS = {
    "formula_version_id": ("formula_version", "uuid"),
    "signal_series_id": ("signal_series", "sha256"),
    "market_manifest_ids": ("market_manifest", "sha256"),
    "status_manifest_ids": ("status_manifest", "sha256"),
}
_PRICE_KEYS = {"price", "entry_price", "mark_price", "floating_pnl"}


class OracleValidationError(ValueError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OracleValidationError(f"oracle JSON is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise OracleValidationError("oracle JSON root must be an object")
    return value


def _expected_case_ids(inputs: Mapping[str, Any]) -> set[str]:
    matrix = inputs["matrix"]
    return {
        f"{formula['id']}_{scope}_{period}"
        for formula in matrix["formulas"]
        for scope in matrix["scopes"]
        for period in matrix["periods"]
    } | {item["id"] for item in inputs["special_cases"]}


def load_inputs(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema_version") != _INPUT_SCHEMA:
        raise OracleValidationError("oracle input schema is unsupported")
    if value.get("source") != {
        "tag": "v1.0.0",
        "commit": V1_COMMIT,
        "tree": V1_TREE,
    }:
        raise OracleValidationError("oracle inputs do not pin immutable v1.0.0 source")
    differences = value.get("allowed_versioned_differences")
    if differences != EXPECTED_ALLOWED_DIFFERENCES:
        raise OracleValidationError(
            "allowed versioned differences are not authoritative"
        )
    if value.get("matrix") != EXPECTED_MATRIX:
        raise OracleValidationError("oracle input matrix is not authoritative")
    if value.get("special_cases") != EXPECTED_SPECIAL_CASES:
        raise OracleValidationError("oracle special cases are not authoritative")
    if len(_expected_case_ids(value)) != 15:
        raise OracleValidationError("oracle input case inventory is incomplete")
    return value


def load_oracle(path: Path, *, inputs_path: Path) -> dict[str, Any]:
    inputs = load_inputs(inputs_path)
    value = _read_json(path)
    payload_digest = value.pop("payload_digest", None)
    if payload_digest != _digest(value):
        raise OracleValidationError("oracle payload digest is invalid")
    if value.get("schema_version") != _ORACLE_SCHEMA:
        raise OracleValidationError("oracle schema is unsupported")
    if value.get("source") != inputs["source"]:
        raise OracleValidationError("oracle source identity is invalid")
    if value.get("input_digest") != _file_digest(inputs_path):
        raise OracleValidationError("oracle input digest is invalid")
    expected_generator = {
        "schema_version": "stock-desk-v1-backtest-oracle-generator-v1",
        "path": "scripts/v1_backtest_oracle.py",
        "sha256": _file_digest(ROOT / "scripts/v1_backtest_oracle.py"),
        "projection_schema": "stock-desk-backtest-semantic-projection-v1",
    }
    if value.get("generator") != expected_generator:
        raise OracleValidationError("oracle generator identity is invalid")
    if (
        value.get("allowed_versioned_differences")
        != inputs["allowed_versioned_differences"]
    ):
        raise OracleValidationError("oracle allowed differences do not match inputs")
    cases = value.get("cases")
    if not isinstance(cases, dict) or set(cases) != _expected_case_ids(inputs):
        raise OracleValidationError("oracle case inventory is invalid")
    if value.get("case_count") != len(cases):
        raise OracleValidationError("oracle case count is invalid")
    specs = {str(spec["id"]): spec for spec in case_specs(inputs)}
    for case_id, case in cases.items():
        if (
            not isinstance(case, dict)
            or set(case) != {"input_digest", "semantic", "semantic_digest"}
            or case.get("input_digest") != _digest(specs[case_id])
            or case.get("semantic_digest") != _digest(case.get("semantic"))
        ):
            raise OracleValidationError(f"oracle case digest is invalid: {case_id}")
    value["payload_digest"] = payload_digest
    return value


def _git_identity(root: Path) -> tuple[str, str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise OracleValidationError(
            "immutable v1.0.0 source cannot be identified"
        ) from error
    return commit, tree


def validate_capture_source(
    root: Path,
    *,
    identity_reader: Any = _git_identity,
) -> None:
    if identity_reader(root) != (V1_COMMIT, V1_TREE):
        raise OracleValidationError(
            "capture requires the immutable v1.0.0 source commit and tree"
        )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


class _IdentityRegistry:
    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

    def token(self, category: str, value: object, *, kind: str) -> object:
        if value is None:
            return None
        values = (
            value
            if isinstance(value, Sequence) and not isinstance(value, str)
            else (value,)
        )
        result: list[str] = []
        for item in values:
            self._validate(item, kind=kind, category=category)
            key = (category, item)
            token = self._tokens.get(key)
            if token is None:
                ordinal = self._counts.get(category, 0) + 1
                self._counts[category] = ordinal
                token = f"<{category}:{ordinal}>"
                self._tokens[key] = token
            result.append(token)
        return result[0] if len(result) == 1 else result

    @staticmethod
    def _validate(value: object, *, kind: str, category: str) -> None:
        if not isinstance(value, str):
            raise OracleValidationError(f"{category} is not a string identity")
        if kind == "uuid":
            try:
                UUID(value)
            except ValueError as error:
                raise OracleValidationError(f"{category} UUID is invalid") from error
        elif kind == "sha256":
            if not value.startswith("sha256:") or len(value) != 71:
                raise OracleValidationError(f"{category} sha256 identity is invalid")
        elif kind == "bounded":
            if (
                not value
                or len(value) > 256
                or any(character.isspace() for character in value)
            ):
                raise OracleValidationError(f"{category} identity is invalid")
        else:
            raise OracleValidationError("oracle identity kind is unsupported")


def _identity_token(
    registry: _IdentityRegistry,
    key: str,
    value: object,
) -> object:
    category, kind = _IDENTITY_KEYS[key]
    return registry.token(category, value, kind=kind)


def _normalize(
    value: object,
    *,
    identities: _IdentityRegistry,
    order_event: bool = False,
) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise OracleValidationError("naive datetime is not canonical oracle data")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return getattr(value, "value")
    if is_dataclass(value):
        return _normalize(asdict(value), identities=identities, order_event=order_event)
    if hasattr(value, "model_dump"):
        return _normalize(
            value.model_dump(mode="python"),
            identities=identities,
            order_event=order_event,
        )
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key in sorted(value):
            key = str(raw_key)
            item = value[raw_key]
            if key in _IDENTITY_KEYS:
                result[key] = _identity_token(identities, key, item)
            elif order_event and key in _PRICE_KEYS and item is not None:
                rounded = Decimal(str(item)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                result[key] = _decimal_text(rounded)
            else:
                result[key] = _normalize(
                    item,
                    identities=identities,
                    order_event=order_event,
                )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize(item, identities=identities, order_event=order_event)
            for item in value
        ]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise OracleValidationError(
            "floating-point values are not canonical oracle data"
        )
    raise OracleValidationError(f"unsupported oracle value: {type(value).__name__}")


def _project_symbol(item: Any, identities: _IdentityRegistry) -> dict[str, object]:
    if item.status == "succeeded":
        if item.failure_reason is not None or item.signal_series_id is None:
            raise OracleValidationError("succeeded symbol identity state is invalid")
        signal_identity: object = _identity_token(
            identities, "signal_series_id", item.signal_series_id
        )
    elif item.status == "failed":
        if item.failure_reason is None or item.signal_series_id is not None:
            raise OracleValidationError("failed symbol identity state is invalid")
        signal_identity = "<absent>"
    else:
        raise OracleValidationError("terminal oracle contains a pending symbol")
    return {
        "ordinal": item.ordinal,
        "symbol": item.symbol,
        "status": item.status,
        "warmup_start": item.warmup_start,
        "failure_reason": item.failure_reason,
        "signal_series_identity": signal_identity,
    }


def _query_projection(query: Any) -> dict[str, object]:
    payload = dict(query.model_dump(mode="python"))
    instrument_kind = payload.pop("instrument_kind", None)
    if instrument_kind is not None:
        value = getattr(instrument_kind, "value", instrument_kind)
        if value != "stock":
            raise OracleValidationError(
                "v1.1 instrument-kind upgrade disagrees with the fixture symbol"
            )
    return payload


def _page_all(repository: Any, run_id: str, collection: str) -> list[object]:
    items: list[object] = []
    cursor = None
    while True:
        page = repository.page(
            run_id,
            collection=collection,
            limit=100,
            cursor=cursor,
        )
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def project_completed(completed: Any, harness: Any) -> dict[str, object]:
    from sqlalchemy import select
    from stock_desk.backtest.models import BacktestOrderEventRow
    from stock_desk.backtest.types import FrozenSymbolGap

    run = completed.run
    snapshot = run.snapshot
    identities = _IdentityRegistry()
    symbol_inputs = []
    for item in snapshot.symbol_inputs:
        if isinstance(item, FrozenSymbolGap):
            symbol_inputs.append(
                {
                    "kind": "gap",
                    "symbol": item.symbol,
                    "reason": item.reason,
                    "signal_query": _query_projection(item.signal_query),
                    "execution_query": _query_projection(item.execution_query),
                }
            )
        else:
            symbol_inputs.append(
                {
                    "kind": "runnable",
                    "symbol": item.symbol,
                    "signal_source": item.signal_source,
                    "execution_source": item.execution_source,
                    "status_source": item.execution_status_source,
                    "signal_query": _query_projection(item.signal_query),
                    "execution_query": _query_projection(item.execution_query),
                    "status_query": item.execution_status_query,
                }
            )
    with harness.engine.connect() as connection:
        order_events = [
            {
                "symbol": row["symbol"],
                "ordinal": row["ordinal"],
                "event_type": row["event_type"],
                "payload": row["payload_json"],
            }
            for row in connection.execute(
                select(BacktestOrderEventRow)
                .where(BacktestOrderEventRow.run_id == run.id)
                .order_by(BacktestOrderEventRow.symbol, BacktestOrderEventRow.ordinal)
            ).mappings()
        ]
    report = completed.report
    return {
        "identity_graph": {
            "run": {
                "run_id": identities.token("run", run.id, kind="uuid"),
                "task_id": identities.token("task", run.task_id, kind="uuid"),
                "result_hash": identities.token(
                    "result", run.result_hash, kind="sha256"
                ),
            },
            "snapshot": {
                "snapshot_id": identities.token(
                    "snapshot", snapshot.snapshot_id, kind="sha256"
                ),
                "formula_version_id": identities.token(
                    "formula_version", snapshot.formula_version_id, kind="uuid"
                ),
                "scope_id": identities.token(
                    "scope", snapshot.scope_id, kind="bounded"
                ),
                "scope_revision_or_snapshot_id": identities.token(
                    "scope_revision",
                    snapshot.scope_revision_or_snapshot_id,
                    kind="bounded",
                ),
                "instrument_dataset_version": identities.token(
                    "instrument_dataset",
                    snapshot.instrument_dataset_version,
                    kind="sha256",
                ),
                "symbol_inputs": [
                    (
                        {
                            "kind": "gap",
                            "instrument_dataset": identities.token(
                                "instrument_dataset",
                                item.checked_instrument_dataset_version,
                                kind="sha256",
                            ),
                            "signal_catalog": identities.token(
                                "signal_catalog",
                                item.checked_signal_catalog_version,
                                kind="sha256",
                            ),
                            "execution_catalog": identities.token(
                                "execution_catalog",
                                item.checked_execution_catalog_version,
                                kind="sha256",
                            ),
                            "status_catalog": identities.token(
                                "status_catalog",
                                item.checked_status_catalog_version,
                                kind="sha256",
                            ),
                        }
                        if isinstance(item, FrozenSymbolGap)
                        else {
                            "kind": "runnable",
                            "signal_manifest": identities.token(
                                "market_manifest",
                                item.signal_manifest_record_id,
                                kind="sha256",
                            ),
                            "signal_dataset": identities.token(
                                "market_dataset",
                                item.signal_dataset_version,
                                kind="sha256",
                            ),
                            "signal_route": identities.token(
                                "market_route",
                                item.signal_route_version,
                                kind="sha256",
                            ),
                            "execution_manifest": identities.token(
                                "market_manifest",
                                item.execution_manifest_record_id,
                                kind="sha256",
                            ),
                            "execution_dataset": identities.token(
                                "market_dataset",
                                item.execution_dataset_version,
                                kind="sha256",
                            ),
                            "execution_route": identities.token(
                                "market_route",
                                item.execution_route_version,
                                kind="sha256",
                            ),
                            "status_manifest": identities.token(
                                "status_manifest",
                                item.execution_status_manifest_record_id,
                                kind="sha256",
                            ),
                            "status_dataset": identities.token(
                                "status_dataset",
                                item.execution_status_dataset_version,
                                kind="sha256",
                            ),
                            "status_route": identities.token(
                                "status_route",
                                item.execution_status_route_version,
                                kind="sha256",
                            ),
                        }
                    )
                    for item in snapshot.symbol_inputs
                ],
                "provenance_digest": identities.token(
                    "provenance", completed.report.provenance_digest, kind="sha256"
                ),
            },
        },
        "run": _normalize(
            {
                "status": run.status,
                "stage": run.stage,
                "total": run.total,
                "processed": run.processed,
                "failed": run.failed,
                "actual_warmup_start": run.actual_warmup_start,
            },
            identities=identities,
        ),
        "snapshot": _normalize(
            {
                "scope_kind": snapshot.scope_kind,
                "symbols": snapshot.symbols,
                "formula_checksum": snapshot.formula_checksum,
                "formula_engine_version": snapshot.formula_engine_version,
                "compatibility_version": snapshot.compatibility_version,
                "formula_parameters": snapshot.formula_parameters,
                "warmup_policy_version": snapshot.warmup_policy_version,
                "symbol_inputs": symbol_inputs,
                "period": snapshot.period,
                "adjustment": snapshot.adjustment,
                "scoring_start": snapshot.scoring_start,
                "scoring_end": snapshot.scoring_end,
                "quantity_shares": snapshot.quantity_shares,
                "commission_bps": snapshot.commission_bps,
                "minimum_commission": snapshot.minimum_commission,
                "sell_tax_bps": snapshot.sell_tax_bps,
                "slippage_bps": snapshot.slippage_bps,
                "cost_model_version": snapshot.cost_model_version,
                "backtest_engine_version": snapshot.backtest_engine_version,
                "execution_rules_version": snapshot.execution_rules_version,
            },
            identities=identities,
        ),
        "symbols": _normalize(
            [_project_symbol(item, identities) for item in run.symbols],
            identities=identities,
        ),
        "report": _normalize(
            {
                "formula_checksum": report.formula_checksum,
                "formula_parameters": report.formula_parameters,
                "formula_engine_version": report.formula_engine_version,
                "compatibility_version": report.compatibility_version,
                "backtest_engine_version": report.backtest_engine_version,
                "symbol_count": report.symbol_count,
                "runnable_count": report.runnable_count,
                "gap_count": report.gap_count,
                "signal_source_ids": report.signal_source_ids,
                "execution_source_ids": report.execution_source_ids,
                "status_source_ids": report.status_source_ids,
                "period": report.period,
                "adjustment": report.adjustment,
                "quantity_shares": report.quantity_shares,
                "commission_bps": report.commission_bps,
                "minimum_commission": report.minimum_commission,
                "sell_tax_bps": report.sell_tax_bps,
                "slippage_bps": report.slippage_bps,
                "execution_rules_version": report.execution_rules_version,
                "cost_model_version": report.cost_model_version,
                "sizing_version": report.sizing_version,
                "warmup_policy_version": report.warmup_policy_version,
                "metrics": report.metrics,
                "disclaimer": report.disclaimer,
                "outcomes": report.outcomes,
            },
            identities=identities,
        ),
        "collections": {
            collection: _normalize(
                _page_all(harness.repository, run.id, collection),
                identities=identities,
            )
            for collection in ("groups", "trades", "open", "failures", "logs")
        },
        "order_events": _normalize(
            order_events,
            identities=identities,
            order_event=True,
        ),
    }


def _timeline(period: str) -> tuple[Sequence[date | datetime], datetime, datetime]:
    from stock_desk.market.types import Period
    from tests.backtest_test_helpers import (
        intraday_timestamps,
        local_time,
        weekday_range,
        weekly_timestamps,
    )

    start = date(2024, 1, 1)
    if period == Period.DAY.value:
        values = weekday_range(start, date(2024, 6, 1))
        return (
            values,
            local_time(values[45]),
            local_time(values[-1]) + timedelta(days=1),
        )
    if period == Period.WEEK.value:
        values = weekly_timestamps(start, 64)
        return values, values[45], values[-1] + timedelta(days=7)
    values = intraday_timestamps(start, trading_days=45)
    return values, values[45], values[-1] + timedelta(hours=1)


def _intent(
    harness: Any,
    *,
    scope: str,
    formula_id: str,
    parameters: Mapping[str, int | float],
    period: Any,
    symbols: tuple[str, ...],
    scoring_start: datetime,
    scoring_end: datetime,
    costs: Mapping[str, Any],
) -> Any:
    from stock_desk.backtest.service import BacktestIntent
    from stock_desk.market.types import Adjustment

    scope_id = revision = None
    symbol = symbols[0] if scope == "single" else None
    scope_kind = "single"
    if scope == "pool":
        published = harness.pools.publish_full_a()
        scope_id, revision = published.pool_id, published.snapshot_id
        scope_kind = "preset"
    return BacktestIntent(
        scope_kind=scope_kind,
        symbol=symbol,
        scope_id=scope_id,
        scope_revision_or_snapshot_id=revision,
        formula_version_id=formula_id,
        formula_parameters=parameters,
        period=period,
        adjustment=Adjustment.NONE,
        scoring_start=scoring_start,
        scoring_end=scoring_end,
        quantity_shares=int(costs["quantity_shares"]),
        commission_bps=Decimal(costs["commission_bps"]),
        minimum_commission=Decimal(costs["minimum_commission"]),
        sell_tax_bps=Decimal(costs["sell_tax_bps"]),
        slippage_bps=Decimal(costs["slippage_bps"]),
    )


def prepare_matrix_case(harness: Any, case: Mapping[str, Any]) -> Any:
    from stock_desk.market.types import Period

    formula = case["formula"]
    symbols = tuple(case["symbols"] if case["scope"] == "pool" else case["symbols"][:1])
    values, scoring_start, scoring_end = _timeline(case["period"])
    harness.seed_instruments(*symbols)
    for index, symbol in enumerate(symbols):
        harness.seed_symbol(
            symbol,
            Period(case["period"]),
            values,
            phase_offset=index * 3,
        )
    version = harness.formula_repository.create(
        formula["name"],
        "trading",
        formula["source"],
        formula["parameter_schema"],
        placement="subchart",
    )
    return _intent(
        harness,
        scope=case["scope"],
        formula_id=version.id,
        parameters=formula["parameters"],
        period=Period(case["period"]),
        symbols=symbols,
        scoring_start=scoring_start,
        scoring_end=scoring_end,
        costs=case["costs"],
    )


def _run_matrix_case(case: Mapping[str, Any], root: Path) -> dict[str, object]:
    from tests.backtest_test_helpers import BacktestHarness

    with BacktestHarness.create(root) as harness:
        completed = harness._run(prepare_matrix_case(harness, case))
        return project_completed(completed, harness)


def _run_special_case(case_id: str, root: Path) -> dict[str, object]:
    from stock_desk.market.types import Period
    from tests.backtest_test_helpers import (
        BacktestHarness,
        OPEN_ONLY_FORMULA,
        WAVE_FORMULA,
        intraday_timestamps,
        routed_bars_from_closes,
        routed_status,
        weekday_range,
    )

    with BacktestHarness.create(root) as harness:
        if case_id == "a_share_constraints_60m":
            timestamps = intraday_timestamps(date(2024, 1, 2), trading_days=5)
            days = tuple(dict.fromkeys(timestamp.date() for timestamp in timestamps))
            closes = [Decimal("10")] * len(timestamps)
            closes[0], closes[1], closes[7], closes[13] = (
                Decimal("11"),
                Decimal("9"),
                Decimal("11"),
                Decimal("9"),
            )
            harness.seed_instruments("600000.SH")
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
            version = harness.create_formula("约束事件链", "BUY:C=11;SELL:C=9;")
            completed = harness.run_single(
                version.id,
                symbol="600000.SH",
                period=Period.MIN60,
                scoring_start=timestamps[0],
                scoring_end=timestamps[-1] + timedelta(hours=1),
            )
        elif case_id == "open_position_costs_1d":
            days = weekday_range(date(2024, 1, 1), date(2024, 3, 1))
            harness.seed_instruments("600000.SH")
            harness.seed_symbol("600000.SH", Period.DAY, days)
            version = harness.create_formula("未平仓成本", OPEN_ONLY_FORMULA)
            completed = harness.run_single(
                version.id,
                symbol="600000.SH",
                period=Period.DAY,
                scoring_start=datetime.combine(
                    days[5], datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                ),
                scoring_end=datetime.combine(
                    days[-1], datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                )
                + timedelta(days=1),
            )
        elif case_id == "partial_pool_gap_1d":
            days = weekday_range(date(2024, 1, 1), date(2024, 5, 1))
            harness.seed_instruments("600000.SH", "000001.SZ")
            harness.seed_symbol("600000.SH", Period.DAY, days)
            version = harness.create_formula("部分数据池", WAVE_FORMULA)
            completed = harness.run_pool(
                version.id,
                symbols=("600000.SH", "000001.SZ"),
                period=Period.DAY,
                scoring_start=datetime.combine(
                    days[5], datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                ),
                scoring_end=datetime.combine(
                    days[-1], datetime.min.time(), tzinfo=timezone(timedelta(hours=8))
                )
                + timedelta(days=1),
            )
        else:
            raise OracleValidationError(f"unknown special oracle case: {case_id}")
        return project_completed(completed, harness)


def case_specs(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    matrix = inputs["matrix"]
    return [
        {
            "id": f"{formula['id']}_{scope}_{period}",
            "kind": "matrix",
            "formula": formula,
            "scope": scope,
            "period": period,
            "symbols": matrix["symbols"],
            "costs": matrix["costs"],
        }
        for formula in matrix["formulas"]
        for scope in matrix["scopes"]
        for period in matrix["periods"]
    ] + [dict(item) for item in inputs["special_cases"]]


def run_case(spec: Mapping[str, Any], output_root: Path) -> dict[str, object]:
    case_root = output_root / str(spec["id"])
    case_root.mkdir(parents=True)
    if spec.get("kind") == "matrix":
        return _run_matrix_case(spec, case_root)
    return _run_special_case(str(spec["id"]), case_root)


def capture(*, repo_root: Path, inputs_path: Path, output_path: Path) -> None:
    validate_capture_source(repo_root)
    inputs = load_inputs(inputs_path)
    resolved_repo = repo_root.resolve()
    current_code_roots = {
        ROOT.resolve(),
        (ROOT / "src").resolve(),
        (ROOT / "scripts").resolve(),
    }
    sys.path[:] = [
        str(resolved_repo / "src"),
        str(resolved_repo),
        *[
            item
            for item in sys.path
            if item and Path(item).resolve() not in current_code_roots
        ],
    ]
    with tempfile.TemporaryDirectory(prefix="stock-desk-v1-oracle-") as temp:
        cases: dict[str, object] = {}
        for spec in case_specs(inputs):
            semantic = run_case(spec, Path(temp))
            cases[str(spec["id"])] = {
                "input_digest": _digest(spec),
                "semantic": semantic,
                "semantic_digest": _digest(semantic),
            }
    payload: dict[str, object] = {
        "schema_version": _ORACLE_SCHEMA,
        "source": inputs["source"],
        "input_digest": _file_digest(inputs_path),
        "generator": {
            "schema_version": "stock-desk-v1-backtest-oracle-generator-v1",
            "path": "scripts/v1_backtest_oracle.py",
            "sha256": _file_digest(ROOT / "scripts/v1_backtest_oracle.py"),
            "projection_schema": "stock-desk-backtest-semantic-projection-v1",
        },
        "allowed_versioned_differences": inputs["allowed_versioned_differences"],
        "case_count": len(cases),
        "cases": cases,
    }
    payload["payload_digest"] = _digest(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture the frozen v1.0 backtest oracle"
    )
    parser.add_argument("command", choices=("capture", "verify"))
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument(
        "--inputs",
        type=Path,
        default=ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json",
    )
    parser.add_argument("--output", type=Path, default=ORACLE_PATH)
    args = parser.parse_args()
    if args.command == "capture":
        if args.repo_root is None:
            parser.error("capture requires --repo-root")
        capture(
            repo_root=args.repo_root, inputs_path=args.inputs, output_path=args.output
        )
    else:
        load_oracle(args.output, inputs_path=args.inputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
