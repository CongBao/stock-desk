"""Closed evidence schema for the operator-driven macOS product journey."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import json
import re
from pathlib import Path
import sqlite3
from typing import Any


APP_IDENTIFIER = "com.baozijuan.stockdesk"
EMBEDDED_WEBVIEW = "WKWebView"
EXPECTED_ACTIONS = (
    "complete-onboarding-default-index",
    "open-default-index-real-kline",
    "select-ordinary-a-share-real-kline",
    "save-and-preview-macd-formula",
    "run-macd-backtest-to-report",
    "titlebar-close-cancel",
    "titlebar-close-confirm-exit",
)
_SCHEMA_VERSION = "stock-desk-macos-full-product-operator-v1"
_REAL_PROVIDERS = frozenset({"akshare", "baostock", "tushare"})
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_ORDINARY_A_SHARE = re.compile(r"(?:[036]\d{5})\.(?:SH|SZ)\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class MacOSJourneyError(RuntimeError):
    """The macOS full-product evidence failed closed validation."""


@dataclass(frozen=True, slots=True)
class JourneyIdentity:
    source_sha: str
    source_tree: str
    session_nonce: str
    host_pid: int
    sidecar_pid: int

    def __post_init__(self) -> None:
        if not _HEX_40.fullmatch(self.source_sha) or not _HEX_40.fullmatch(
            self.source_tree
        ):
            raise MacOSJourneyError("journey identity source revision is invalid")
        _require_safe_id(self.session_nonce, "journey identity nonce")
        if not _positive_pid(self.host_pid) or not _positive_pid(self.sidecar_pid):
            raise MacOSJourneyError("journey identity process ID is invalid")
        if self.host_pid == self.sidecar_pid:
            raise MacOSJourneyError("journey identity process IDs must be distinct")


@dataclass(frozen=True, slots=True)
class JourneyScreenshot:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class JourneyAction:
    action: str
    observed: bool
    input_method: str
    physical_mouse_click: bool
    screenshot_sha256: str


@dataclass(frozen=True, slots=True)
class JourneyEvidence:
    schema_version: str
    source_sha: str
    source_tree: str
    session_nonce: str
    app_identifier: str
    embedded_webview: str
    driver: str
    input_method: str
    physical_mouse_click: bool
    host_pid: int
    sidecar_pid: int
    real_market_data: bool
    demo_mode: bool
    providers: tuple[str, ...]
    symbols: tuple[str, str]
    kline_cutoff: str
    formula_version_id: str
    backtest_run_id: str
    backtest_report_id: str
    screenshots: tuple[JourneyScreenshot, ...]
    actions: tuple[JourneyAction, ...]


_TOP_LEVEL_FIELDS = frozenset(JourneyEvidence.__dataclass_fields__)
_ACTION_FIELDS = frozenset(JourneyAction.__dataclass_fields__)
_SCREENSHOT_FIELDS = frozenset(JourneyScreenshot.__dataclass_fields__)


def _positive_pid(value: object) -> bool:
    return type(value) is int and value > 1


def _require_safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise MacOSJourneyError(f"{label} is invalid")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise MacOSJourneyError(f"operator evidence {label} shape is invalid")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MacOSJourneyError(f"operator evidence {label} shape is invalid")
    return value


def _validate_screenshots(value: object) -> tuple[JourneyScreenshot, ...]:
    records = _sequence(value, "screenshot")
    if len(records) != len(EXPECTED_ACTIONS):
        raise MacOSJourneyError("operator evidence screenshot set is incomplete")
    screenshots: list[JourneyScreenshot] = []
    seen_hashes: set[str] = set()
    for raw in records:
        item = _mapping(raw, "screenshot")
        if set(item) != _SCREENSHOT_FIELDS:
            raise MacOSJourneyError("operator evidence screenshot shape is invalid")
        name = item["name"]
        digest = item["sha256"]
        size = item["size"]
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
            raise MacOSJourneyError("operator evidence screenshot name is invalid")
        if not name.lower().endswith(".png"):
            raise MacOSJourneyError("operator evidence screenshot format is invalid")
        if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
            raise MacOSJourneyError("operator evidence screenshot digest is invalid")
        if type(size) is not int or size < 1_024:
            raise MacOSJourneyError("operator evidence screenshot size is invalid")
        if digest in seen_hashes:
            raise MacOSJourneyError("operator evidence screenshot is duplicated")
        seen_hashes.add(digest)
        screenshots.append(JourneyScreenshot(name=name, sha256=digest, size=size))
    return tuple(screenshots)


def _validate_actions(
    value: object, screenshot_hashes: frozenset[str]
) -> tuple[JourneyAction, ...]:
    records = _sequence(value, "action")
    if len(records) != len(EXPECTED_ACTIONS):
        raise MacOSJourneyError("operator evidence action sequence is invalid")
    actions: list[JourneyAction] = []
    for raw, expected in zip(records, EXPECTED_ACTIONS, strict=True):
        item = _mapping(raw, "action")
        if set(item) != _ACTION_FIELDS:
            raise MacOSJourneyError("operator evidence action shape is invalid")
        if item["action"] != expected:
            raise MacOSJourneyError("operator evidence action sequence is invalid")
        if item["observed"] is not True:
            raise MacOSJourneyError("operator evidence action was not observed")
        if item["input_method"] != "sky.click":
            raise MacOSJourneyError("operator evidence action input method is invalid")
        if item["physical_mouse_click"] is not True:
            raise MacOSJourneyError("operator evidence action was not a physical click")
        screenshot_sha256 = item["screenshot_sha256"]
        if screenshot_sha256 not in screenshot_hashes:
            raise MacOSJourneyError("operator evidence action screenshot is unbound")
        actions.append(
            JourneyAction(
                action=expected,
                observed=True,
                input_method="sky.click",
                physical_mouse_click=True,
                screenshot_sha256=screenshot_sha256,
            )
        )
    if len({action.screenshot_sha256 for action in actions}) != len(actions):
        raise MacOSJourneyError("operator evidence action screenshots are not unique")
    return tuple(actions)


def validate_operator_evidence(
    payload: object, identity: JourneyIdentity
) -> JourneyEvidence:
    """Validate and freeze nonce-bound Computer Use journey evidence."""

    value = _mapping(payload, "top-level")
    if set(value) != _TOP_LEVEL_FIELDS:
        raise MacOSJourneyError("operator evidence top-level shape is invalid")
    if (
        value["source_sha"] != identity.source_sha
        or value["source_tree"] != identity.source_tree
        or value["session_nonce"] != identity.session_nonce
        or value["host_pid"] != identity.host_pid
        or value["sidecar_pid"] != identity.sidecar_pid
    ):
        raise MacOSJourneyError("operator evidence identity does not match")
    if value["schema_version"] != _SCHEMA_VERSION:
        raise MacOSJourneyError("operator evidence schema version is invalid")
    if value["app_identifier"] != APP_IDENTIFIER:
        raise MacOSJourneyError("operator evidence app identifier is invalid")
    if value["embedded_webview"] != EMBEDDED_WEBVIEW:
        raise MacOSJourneyError("operator evidence must use WKWebView")
    if value["driver"] != "codex-computer-use":
        raise MacOSJourneyError("operator evidence driver is invalid")
    if value["input_method"] != "codex-computer-use-sky-click":
        raise MacOSJourneyError("operator evidence input method is invalid")
    if value["physical_mouse_click"] is not True:
        raise MacOSJourneyError("operator evidence did not use physical mouse clicks")
    if value["real_market_data"] is not True:
        raise MacOSJourneyError("operator evidence did not prove real market data")
    if value["demo_mode"] is not False:
        raise MacOSJourneyError("operator evidence used demo mode")

    raw_providers = _sequence(value["providers"], "provider")
    providers = tuple(raw_providers)
    if (
        not providers
        or any(type(provider) is not str for provider in providers)
        or len(set(providers)) != len(providers)
        or not set(providers).issubset(_REAL_PROVIDERS)
    ):
        raise MacOSJourneyError("operator evidence provider is not a real provider")

    raw_symbols = _sequence(value["symbols"], "symbol")
    if len(raw_symbols) != 2 or any(type(symbol) is not str for symbol in raw_symbols):
        raise MacOSJourneyError("operator evidence symbol set is invalid")
    symbols = (str(raw_symbols[0]), str(raw_symbols[1]))
    if symbols[0] != "000001.SS" or not _ORDINARY_A_SHARE.fullmatch(symbols[1]):
        raise MacOSJourneyError("operator evidence symbols are not canonical")

    cutoff = value["kline_cutoff"]
    if not isinstance(cutoff, str):
        raise MacOSJourneyError("operator evidence K-line cutoff is invalid")
    try:
        date.fromisoformat(cutoff)
    except ValueError as error:
        raise MacOSJourneyError("operator evidence K-line cutoff is invalid") from error

    formula_version_id = _require_safe_id(
        value["formula_version_id"], "formula version"
    )
    backtest_run_id = _require_safe_id(value["backtest_run_id"], "backtest run")
    backtest_report_id = _require_safe_id(
        value["backtest_report_id"], "backtest report"
    )
    screenshots = _validate_screenshots(value["screenshots"])
    actions = _validate_actions(
        value["actions"], frozenset(item.sha256 for item in screenshots)
    )
    return JourneyEvidence(
        schema_version=_SCHEMA_VERSION,
        source_sha=identity.source_sha,
        source_tree=identity.source_tree,
        session_nonce=identity.session_nonce,
        app_identifier=APP_IDENTIFIER,
        embedded_webview=EMBEDDED_WEBVIEW,
        driver="codex-computer-use",
        input_method="codex-computer-use-sky-click",
        physical_mouse_click=True,
        host_pid=identity.host_pid,
        sidecar_pid=identity.sidecar_pid,
        real_market_data=True,
        demo_mode=False,
        providers=tuple(str(provider) for provider in providers),
        symbols=symbols,
        kline_cutoff=cutoff,
        formula_version_id=formula_version_id,
        backtest_run_id=backtest_run_id,
        backtest_report_id=backtest_report_id,
        screenshots=screenshots,
        actions=actions,
    )


def _json_object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise MacOSJourneyError(f"isolated {label} is invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise MacOSJourneyError(f"isolated {label} is invalid") from error
    if not isinstance(decoded, Mapping):
        raise MacOSJourneyError(f"isolated {label} is invalid")
    return decoded


def validate_isolated_product_state(
    data_root: Path, evidence: JourneyEvidence
) -> dict[str, Any]:
    """Independently validate product proof in the disposable local database."""

    resolved_root = data_root.resolve()
    database = resolved_root / "stock-desk.db"
    if data_root.is_symlink() or not database.is_file() or database.is_symlink():
        raise MacOSJourneyError("isolated database is missing or unsafe")
    daily_bar_rows = 0
    providers: set[str] = set()
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for symbol in evidence.symbols:
                dataset = connection.execute(
                    "SELECT dataset_version, source, data_cutoff, row_count "
                    "FROM market_dataset WHERE symbol = ? AND period = '1d' "
                    "ORDER BY data_cutoff DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                if dataset is None:
                    raise MacOSJourneyError(
                        "isolated canonical symbol daily bars are missing"
                    )
                provider = dataset["source"]
                if (
                    provider not in _REAL_PROVIDERS
                    or provider not in evidence.providers
                ):
                    raise MacOSJourneyError("isolated market provider is invalid")
                if (
                    type(dataset["row_count"]) is not int
                    or dataset["row_count"] < 1
                    or not dataset["data_cutoff"]
                ):
                    raise MacOSJourneyError("isolated daily bars are empty")
                cutoff = str(dataset["data_cutoff"])[:10]
                if cutoff == evidence.kline_cutoff:
                    pass
                elif symbol == evidence.symbols[-1]:
                    raise MacOSJourneyError("isolated K-line cutoff does not match")
                row_count = connection.execute(
                    "SELECT COUNT(*) FROM market_dataset_timestamp "
                    "WHERE dataset_version = ? AND open IS NOT NULL "
                    "AND high IS NOT NULL AND low IS NOT NULL "
                    "AND close IS NOT NULL AND volume IS NOT NULL",
                    (dataset["dataset_version"],),
                ).fetchone()[0]
                if type(row_count) is not int or row_count < 1:
                    raise MacOSJourneyError("isolated daily bars are empty")
                manifest_row = connection.execute(
                    "SELECT manifest_json FROM market_routing_manifest "
                    "WHERE dataset_version = ? AND symbol = ? LIMIT 1",
                    (dataset["dataset_version"], symbol),
                ).fetchone()
                if manifest_row is None:
                    raise MacOSJourneyError("isolated routing manifest is missing")
                manifest = _json_object(
                    manifest_row["manifest_json"], "routing manifest"
                )
                if (
                    manifest.get("schema_version") != "stock-desk-routing-manifest-v1"
                    or manifest.get("selected_source") != provider
                    or not manifest.get("upstream_data_cutoff")
                ):
                    raise MacOSJourneyError("isolated routing manifest is invalid")
                providers.add(str(provider))
                daily_bar_rows += row_count

            instrument = connection.execute(
                "SELECT instrument_kind FROM instrument_dataset_item "
                "WHERE symbol = ? LIMIT 1",
                (evidence.symbols[1],),
            ).fetchone()
            if instrument is None or instrument["instrument_kind"] != "stock":
                raise MacOSJourneyError("isolated ordinary A-share symbol is invalid")

            formula = connection.execute(
                "SELECT version, name, formula_type, placement, source, checksum "
                "FROM formula_version WHERE id = ? LIMIT 1",
                (evidence.formula_version_id,),
            ).fetchone()
            if formula is None:
                raise MacOSJourneyError("isolated formula version is missing")
            formula_source = str(formula["source"]).upper()
            if (
                type(formula["version"]) is not int
                or formula["version"] < 1
                or formula["formula_type"] != "indicator"
                or formula["placement"] != "subchart"
                or any(token not in formula_source for token in ("DIF", "DEA", "MACD"))
                or not str(formula["checksum"]).startswith("sha256:")
            ):
                raise MacOSJourneyError("isolated MACD formula version is invalid")

            run = connection.execute(
                "SELECT snapshot_json, status, stage, processed, result_hash "
                "FROM backtest_run WHERE id = ? LIMIT 1",
                (evidence.backtest_run_id,),
            ).fetchone()
            if run is None:
                raise MacOSJourneyError("isolated backtest run is missing")
            snapshot = _json_object(run["snapshot_json"], "backtest snapshot")
            if (
                run["status"] not in {"succeeded", "partial_failed"}
                or run["stage"] != "completed"
                or type(run["processed"]) is not int
                or run["processed"] < 1
                or not str(run["result_hash"]).startswith("sha256:")
                or snapshot.get("formula_version_id") != evidence.formula_version_id
            ):
                raise MacOSJourneyError("isolated backtest evidence is invalid")
            trade_rows = connection.execute(
                "SELECT COUNT(*) FROM backtest_trade WHERE run_id = ?",
                (evidence.backtest_run_id,),
            ).fetchone()[0]
            if type(trade_rows) is not int or trade_rows < 1:
                raise MacOSJourneyError("isolated backtest trade evidence is empty")
            metric_rows = connection.execute(
                "SELECT COUNT(*) FROM backtest_aggregate_metric "
                "WHERE run_id = ? AND metric_key = 'overview' "
                "AND length(payload_json) > 2",
                (evidence.backtest_run_id,),
            ).fetchone()[0]
            if type(metric_rows) is not int or metric_rows < 1:
                raise MacOSJourneyError("isolated backtest report evidence is empty")
    except sqlite3.Error as error:
        raise MacOSJourneyError("isolated database validation failed") from error

    return {
        "backtest_report_id": evidence.backtest_report_id,
        "backtest_run_id": evidence.backtest_run_id,
        "daily_bar_rows": daily_bar_rows,
        "formula_version_id": evidence.formula_version_id,
        "metric_rows": metric_rows,
        "providers": sorted(providers),
        "symbols": list(evidence.symbols),
        "trade_rows": trade_rows,
    }
