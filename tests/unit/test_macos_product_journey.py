from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.macos_product_journey import (
    EXPECTED_ACTIONS,
    EXPECTED_VISUAL_STATES,
    JourneyIdentity,
    MacOSJourneyError,
    validate_isolated_product_state,
    validate_operator_evidence,
    validate_visual_analysis,
)
from scripts import (
    macos_full_product_test,
    macos_product_journey,
    macos_sidecar,
    macos_tauri_support,
)


ROOT = Path(__file__).resolve().parents[2]


IDENTITY = JourneyIdentity(
    source_sha="a" * 40,
    source_tree="b" * 40,
    session_nonce="nonce-0123456789abcdef",
    host_pid=4242,
    sidecar_pid=4243,
)


def valid_payload() -> dict[str, object]:
    screenshot_count = len(EXPECTED_VISUAL_STATES) + 2
    screenshots = [
        {
            "name": f"journey-{index}.png",
            "sha256": f"{index:064x}",
            "size": 20_000 + index,
            "width": (
                1366
                if index > len(EXPECTED_VISUAL_STATES)
                or EXPECTED_VISUAL_STATES[index - 1][2] == "normal"
                else 900
            ),
            "height": (
                768
                if index > len(EXPECTED_VISUAL_STATES)
                or EXPECTED_VISUAL_STATES[index - 1][2] == "normal"
                else 700
            ),
        }
        for index in range(1, screenshot_count + 1)
    ]
    routes = {
        "onboarding": "/market",
        "market": "/market",
        "formulas": "/formulas",
        "backtests": "/backtests/backtest-run-1",
        "analysis": "/analysis",
        "tasks": "/tasks",
        "settings": "/settings",
    }
    page_markers = {
        "onboarding": "可以开始使用了",
        "market": "行情工作区",
        "formulas": "公式工作台",
        "backtests": "回测运行",
        "analysis": "新建分析",
        "tasks": "刷新任务",
        "settings": "数据源连接",
    }
    visual_states = [
        {
            "page": page,
            "route": routes[page],
            "page_marker": page_markers[page],
            "observed": True,
            "navigation_action": (
                "launch-onboarding"
                if page == "onboarding"
                else f"click-navigation-{page}"
            ),
            "navigation_input_method": (
                "native-launch" if page == "onboarding" else "sky.click"
            ),
            "navigation_physical_mouse_click": page != "onboarding",
            "theme": theme,
            "theme_action": f"click-theme-{theme}",
            "theme_input_method": "sky.click",
            "theme_physical_mouse_click": True,
            "layout": layout,
            "layout_action": f"drag-window-{layout}",
            "layout_input_method": "sky.drag",
            "layout_physical_mouse_input": True,
            "viewport_width": screenshots[index]["width"],
            "viewport_height": screenshots[index]["height"],
            "screenshot_sha256": screenshots[index]["sha256"],
        }
        for index, (page, theme, layout) in enumerate(EXPECTED_VISUAL_STATES)
    ]
    visual_hash = {
        (item["page"], item["theme"], item["layout"]): item["screenshot_sha256"]
        for item in visual_states
    }
    action_hashes = [
        visual_hash[("onboarding", "light", "normal")],
        visual_hash[("market", "light", "normal")],
        visual_hash[("market", "light", "narrow")],
        visual_hash[("formulas", "light", "normal")],
        visual_hash[("backtests", "light", "normal")],
        screenshots[-2]["sha256"],
        screenshots[-1]["sha256"],
    ]
    return {
        "schema_version": "stock-desk-macos-full-product-operator-v3",
        "source_sha": IDENTITY.source_sha,
        "source_tree": IDENTITY.source_tree,
        "session_nonce": IDENTITY.session_nonce,
        "app_identifier": "com.baozijuan.stockdesk",
        "embedded_webview": "WKWebView",
        "driver": "codex-computer-use",
        "input_method": "codex-computer-use-sky",
        "physical_mouse_click": True,
        "host_pid": IDENTITY.host_pid,
        "sidecar_pid": IDENTITY.sidecar_pid,
        "real_market_data": True,
        "demo_mode": False,
        "providers": ["akshare", "baostock"],
        "symbols": ["000001.SS", "600519.SH"],
        "kline_cutoff": "2026-07-16",
        "formula_version_id": "formula-version-1",
        "backtest_run_id": "backtest-run-1",
        "backtest_report_id": "backtest-run-1",
        "screenshots": screenshots,
        "visual_states": visual_states,
        "actions": [
            {
                "action": action,
                "observed": True,
                "input_method": "sky.click",
                "physical_mouse_click": True,
                "screenshot_sha256": action_hashes[index],
            }
            for index, action in enumerate(EXPECTED_ACTIONS)
        ],
    }


def test_evidence_requires_real_data_formula_backtest_and_physical_clicks() -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)

    assert tuple(item.action for item in evidence.actions) == EXPECTED_ACTIONS
    assert evidence.real_market_data is True
    assert evidence.demo_mode is False
    assert evidence.physical_mouse_click is True
    assert evidence.symbols == ("000001.SS", "600519.SH")
    assert (
        tuple((item.page, item.theme, item.layout) for item in evidence.visual_states)
        == EXPECTED_VISUAL_STATES
    )


@pytest.mark.parametrize("field", ["source_sha", "source_tree", "session_nonce"])
def test_evidence_rejects_identity_mismatch(field: str) -> None:
    payload = valid_payload()
    payload[field] = "0" * 40

    with pytest.raises(MacOSJourneyError, match="identity"):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(extra="forbidden"), "shape"),
        (lambda value: value.update(embedded_webview="WebView2"), "WKWebView"),
        (lambda value: value.update(input_method="script"), "input method"),
        (lambda value: value.update(physical_mouse_click=False), "physical"),
        (lambda value: value.update(real_market_data=False), "real market"),
        (lambda value: value.update(demo_mode=True), "demo"),
        (lambda value: value.update(providers=["demo"]), "provider"),
        (lambda value: value.update(symbols=["000001.SS", "600519"]), "symbol"),
        (
            lambda value: value.update(symbols=["000001.SS", "600519.SZ"]),
            "symbol",
        ),
        (
            lambda value: value.update(symbols=["000001.SS", "000001.SH"]),
            "symbol",
        ),
        (lambda value: value.update(kline_cutoff=""), "K-line"),
        (lambda value: value.update(formula_version_id=""), "formula"),
        (lambda value: value.update(backtest_run_id=""), "backtest"),
        (lambda value: value.update(backtest_report_id=""), "backtest"),
        (
            lambda value: value.update(backtest_report_id="different-report"),
            "report identity",
        ),
        (lambda value: value.update(raw_token="secret"), "shape"),
        (
            lambda value: value.update(database_path="/Users/example/private.db"),
            "shape",
        ),
    ],
)
def test_evidence_fails_closed_for_malformed_or_sensitive_payloads(
    mutation: object, message: str
) -> None:
    payload = valid_payload()
    assert callable(mutation)
    mutation(payload)

    with pytest.raises(MacOSJourneyError, match=message):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_evidence_rejects_action_reordering_and_unbound_screenshot() -> None:
    payload = valid_payload()
    actions = payload["actions"]
    assert isinstance(actions, list)
    actions[0], actions[1] = actions[1], actions[0]

    with pytest.raises(MacOSJourneyError, match="action sequence"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    actions = payload["actions"]
    assert isinstance(actions, list)
    action = deepcopy(actions[0])
    assert isinstance(action, dict)
    action["screenshot_sha256"] = "f" * 64
    actions[0] = action

    with pytest.raises(MacOSJourneyError, match="screenshot"):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_evidence_requires_every_page_theme_and_layout_visual_state() -> None:
    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    states.pop()

    with pytest.raises(MacOSJourneyError, match="visual state"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    duplicate = deepcopy(states[0])
    assert isinstance(duplicate, dict)
    states[-1] = duplicate

    with pytest.raises(MacOSJourneyError, match="visual state"):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("route", "/settings", "route"),
        ("page_marker", "wrong page", "marker"),
        ("observed", False, "observed"),
        ("navigation_action", "script-navigation", "navigation"),
        ("navigation_input_method", "script", "navigation"),
        ("navigation_physical_mouse_click", False, "navigation"),
        ("theme_action", "script-theme", "theme"),
        ("theme_input_method", "script", "theme"),
        ("theme_physical_mouse_click", False, "theme"),
        ("layout_action", "script-resize", "layout"),
        ("layout_input_method", "script", "layout"),
        ("layout_physical_mouse_input", False, "layout"),
        ("viewport_width", 900, "viewport"),
    ],
)
def test_visual_state_binds_real_navigation_theme_layout_and_page_identity(
    field: str, value: object, message: str
) -> None:
    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    target = states[4]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(MacOSJourneyError, match=message):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_visual_state_viewport_must_match_bound_png() -> None:
    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    target = states[1]
    assert isinstance(target, dict)
    target["viewport_width"] = 899

    with pytest.raises(MacOSJourneyError, match="viewport"):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_visual_analysis_binds_screenshot_content_to_page_and_theme() -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    light_market = next(
        state
        for state in evidence.visual_states
        if (state.page, state.theme, state.layout) == ("market", "light", "normal")
    )

    assert validate_visual_analysis(
        {
            "recognized_text": ["Stock Desk", "行情工作区", "上证指数"],
            "median_luminance": 0.91,
        },
        state=light_market,
    ) == {"recognized_text": ("Stock Desk", "行情工作区", "上证指数"), "theme": "light"}

    with pytest.raises(MacOSJourneyError, match="theme"):
        validate_visual_analysis(
            {
                "recognized_text": ["Stock Desk", "行情工作区"],
                "median_luminance": 0.12,
            },
            state=light_market,
        )
    with pytest.raises(MacOSJourneyError, match="page marker"):
        validate_visual_analysis(
            {
                "recognized_text": ["Stock Desk", "公式工作台"],
                "median_luminance": 0.91,
            },
            state=light_market,
        )


def test_visual_analysis_rejects_ambiguous_or_extra_fields() -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    state = evidence.visual_states[0]

    with pytest.raises(MacOSJourneyError, match="luminance"):
        validate_visual_analysis(
            {"recognized_text": [state.page_marker], "median_luminance": 0.5},
            state=state,
        )
    with pytest.raises(MacOSJourneyError, match="shape"):
        validate_visual_analysis(
            {
                "recognized_text": [state.page_marker],
                "median_luminance": 0.9,
                "forged": True,
            },
            state=state,
        )


def test_visual_analysis_rejects_analysis_and_task_page_swaps() -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    analysis = next(
        state for state in evidence.visual_states if state.page == "analysis"
    )
    tasks = next(state for state in evidence.visual_states if state.page == "tasks")
    shared_navigation = ["智能分析", "任务中心"]

    with pytest.raises(MacOSJourneyError, match="page marker"):
        validate_visual_analysis(
            {
                "recognized_text": [*shared_navigation, "刷新任务", "全部任务"],
                "median_luminance": 0.9,
            },
            state=analysis,
        )
    with pytest.raises(MacOSJourneyError, match="page marker"):
        validate_visual_analysis(
            {
                "recognized_text": [*shared_navigation, "新建分析", "运行预检"],
                "median_luminance": 0.9,
            },
            state=tasks,
        )


def _isolated_database(data_root: Path) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    database = data_root / "stock-desk.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE market_dataset (
                dataset_version TEXT, source TEXT, symbol TEXT, period TEXT,
                data_cutoff TEXT, row_count INTEGER
            );
            CREATE TABLE market_dataset_timestamp (
                dataset_version TEXT, ordinal INTEGER, timestamp TEXT,
                open TEXT, high TEXT, low TEXT, close TEXT, volume INTEGER
            );
            CREATE TABLE market_routing_manifest (
                manifest_record_id TEXT, dataset_version TEXT, symbol TEXT,
                manifest_json TEXT
            );
            CREATE TABLE instrument_dataset_item (
                dataset_version TEXT, symbol TEXT, instrument_kind TEXT
            );
            CREATE TABLE formula_version (
                id TEXT, formula_id TEXT, version INTEGER, name TEXT,
                formula_type TEXT, placement TEXT, source TEXT, checksum TEXT
            );
            CREATE TABLE execution_status_dataset (
                dataset_version TEXT, source TEXT, symbol TEXT, snapshot_json TEXT
            );
            CREATE TABLE execution_status_routing_manifest (
                manifest_record_id TEXT, dataset_version TEXT, route_version TEXT,
                manifest_json TEXT
            );
            CREATE TABLE backtest_run (
                id TEXT, snapshot_json TEXT, status TEXT, stage TEXT,
                processed INTEGER, result_hash TEXT
            );
            CREATE TABLE backtest_trade (
                run_id TEXT, symbol TEXT, ordinal INTEGER, payload_json TEXT
            );
            CREATE TABLE backtest_aggregate_metric (
                run_id TEXT, metric_key TEXT, payload_json TEXT
            );
            """
        )
        for index, (symbol, provider) in enumerate(
            (("000001.SS", "akshare"), ("600519.SH", "baostock")), start=1
        ):
            version = f"dataset-{index}"
            connection.execute(
                "INSERT INTO market_dataset VALUES (?, ?, ?, '1d', ?, 2)",
                (version, provider, symbol, "2026-07-16 00:00:00"),
            )
            connection.executemany(
                "INSERT INTO market_dataset_timestamp VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (version, 0, "2026-07-15", "1", "2", "1", "2", 100),
                    (version, 1, "2026-07-16", "2", "3", "2", "3", 200),
                ],
            )
            connection.execute(
                "INSERT INTO market_routing_manifest VALUES (?, ?, ?, ?)",
                (
                    f"manifest-{index}",
                    version,
                    symbol,
                    json.dumps(
                        {
                            "schema_version": "stock-desk-routing-manifest-v1",
                            "selected_source": provider,
                            "upstream_data_cutoff": "2026-07-16T00:00:00Z",
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO instrument_dataset_item VALUES ('instruments', ?, 'stock')",
                (symbol,),
            )
        connection.execute(
            "INSERT INTO formula_version VALUES (?, 'formula-1', 1, 'MACD', "
            "'trading', 'subchart', ?, ?)",
            (
                "formula-version-1",
                "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);DEA:EMA(DIF,9);"
                "MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
                "sha256:" + "c" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO execution_status_dataset VALUES (?, ?, ?, ?)",
            (
                "status-dataset-1",
                "baostock",
                "600519.SH",
                json.dumps(
                    {
                        "dataset_version": "status-dataset-1",
                        "source": "baostock",
                        "evidence_level": "basic_no_price_limits",
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO execution_status_routing_manifest VALUES (?, ?, ?, ?)",
            (
                "status-manifest-1",
                "status-dataset-1",
                "status-route-1",
                json.dumps(
                    {
                        "schema_version": "stock-desk-routing-manifest-v1",
                        "selected_source": "baostock",
                        "upstream_dataset_version": "status-dataset-1",
                        "route_version": "status-route-1",
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO backtest_run VALUES (?, ?, 'succeeded', 'completed', 1, ?)",
            (
                "backtest-run-1",
                json.dumps(
                    {
                        "formula_version_id": "formula-version-1",
                        "execution_rules_version": "a-share-v2",
                        "symbol_inputs": [
                            {
                                "symbol": "600519.SH",
                                "execution_status_manifest_record_id": (
                                    "status-manifest-1"
                                ),
                                "execution_status_dataset_version": (
                                    "status-dataset-1"
                                ),
                                "execution_status_route_version": "status-route-1",
                                "execution_status_source": "baostock",
                                "execution_status_evidence_level": (
                                    "basic_no_price_limits"
                                ),
                            }
                        ],
                    }
                ),
                "sha256:" + "d" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO backtest_trade VALUES "
            "('backtest-run-1', '600519.SH', 0, '{\"side\":\"buy\"}')"
        )
        connection.execute(
            "INSERT INTO backtest_aggregate_metric VALUES "
            "('backtest-run-1', 'overview', '{\"total_return\":\"0.1\"}')"
        )
    return database


def test_isolated_state_independently_confirms_product_evidence(tmp_path: Path) -> None:
    _isolated_database(tmp_path)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)

    result = validate_isolated_product_state(tmp_path, evidence)

    assert result == {
        "backtest_report_id": "backtest-run-1",
        "backtest_run_id": "backtest-run-1",
        "daily_bar_rows": 4,
        "formula_version_id": "formula-version-1",
        "metric_rows": 1,
        "providers": ["akshare", "baostock"],
        "execution_status_evidence_level": "basic_no_price_limits",
        "execution_status_warning": "basic_execution_status",
        "symbols": ["000001.SS", "600519.SH"],
        "trade_rows": 1,
    }


def test_isolated_state_accepts_akshare_basic_execution_evidence(
    tmp_path: Path,
) -> None:
    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE execution_status_dataset SET source = 'akshare', "
            "snapshot_json = replace(snapshot_json, 'baostock', 'akshare')"
        )
        connection.execute(
            "UPDATE execution_status_routing_manifest SET manifest_json = "
            "replace(manifest_json, 'baostock', 'akshare')"
        )
        connection.execute(
            "UPDATE backtest_run SET snapshot_json = "
            "replace(snapshot_json, 'baostock', 'akshare')"
        )
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)

    result = validate_isolated_product_state(tmp_path, evidence)

    assert result["execution_status_evidence_level"] == "basic_no_price_limits"


def test_isolated_state_rejects_report_identity_not_bound_to_persisted_run(
    tmp_path: Path,
) -> None:
    _isolated_database(tmp_path)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    mismatched = replace(evidence, backtest_report_id="different-report")

    with pytest.raises(MacOSJourneyError, match="report identity"):
        validate_isolated_product_state(tmp_path, mismatched)


def test_isolated_state_rejects_operator_provider_not_present_in_database(
    tmp_path: Path,
) -> None:
    _isolated_database(tmp_path)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    overreported = replace(
        evidence,
        providers=(*evidence.providers, "tushare"),
    )

    with pytest.raises(MacOSJourneyError, match="provider.*match"):
        validate_isolated_product_state(tmp_path, overreported)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE market_dataset SET source = 'demo' WHERE symbol = '000001.SS'",
            "provider",
        ),
        (
            "DELETE FROM market_dataset_timestamp WHERE dataset_version = 'dataset-2'",
            "daily bars",
        ),
        (
            "UPDATE market_routing_manifest SET manifest_json = '{}' "
            "WHERE symbol = '600519.SH'",
            "manifest",
        ),
        ("DELETE FROM formula_version", "formula"),
        ("UPDATE formula_version SET formula_type = 'indicator'", "formula"),
        ("UPDATE backtest_run SET status = 'failed'", "backtest"),
        ("DELETE FROM execution_status_dataset", "execution-status dataset"),
        (
            "UPDATE execution_status_dataset SET snapshot_json = "
            '\'{"dataset_version":"status-dataset-1","source":"baostock",'
            '"evidence_level":"authoritative"}\'',
            "execution-status evidence",
        ),
        (
            "UPDATE execution_status_routing_manifest SET manifest_json = "
            '\'{"schema_version":"stock-desk-routing-manifest-v1",'
            '"selected_source":"tushare",'
            '"upstream_dataset_version":"status-dataset-1",'
            '"route_version":"status-route-1"}\'',
            "execution-status manifest",
        ),
        (
            "UPDATE backtest_run SET snapshot_json = replace(snapshot_json, "
            "'a-share-v2', 'a-share-v1')",
            "execution-status rule",
        ),
        ("DELETE FROM backtest_trade", "trade"),
        ("DELETE FROM backtest_aggregate_metric", "report"),
    ],
)
def test_isolated_state_fails_closed_when_product_proof_is_missing(
    tmp_path: Path, statement: str, message: str
) -> None:
    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)

    with pytest.raises(MacOSJourneyError, match=message):
        validate_isolated_product_state(tmp_path, evidence)


class _HostProcess:
    pid = 4242


def test_ready_state_uses_verified_runtime_inner_pid_not_outer_or_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary_root = tmp_path / "harness"
    temporary_root.mkdir()
    paths = macos_full_product_test.HarnessPaths.create(temporary_root)
    sidecar = paths.host_path.parent / "stock-desk-sidecar"
    rows = {
        4242: macos_tauri_support.ProcessInfo(
            4242, 1, "host-start", os.fspath(paths.host_path)
        ),
        4243: macos_tauri_support.ProcessInfo(
            4243, 4242, "outer-start", os.fspath(sidecar)
        ),
        4244: macos_tauri_support.ProcessInfo(
            4244, 4243, "inner-start", os.fspath(sidecar)
        ),
        4245: macos_tauri_support.ProcessInfo(
            4245, 4244, "worker-start", f"{sidecar} --multiprocessing-fork"
        ),
    }
    monkeypatch.setattr(macos_tauri_support, "process_table", lambda: rows.copy())
    context = macos_full_product_test.HarnessContext(paths, tmp_path / "output")
    context.host_process = _HostProcess()
    context.process_tree = macos_tauri_support.VerifiedProcessTree(
        4242, paths.host_path, temporary_root
    )
    runtime_record = paths.data_root / "runtime" / "runtime.json"
    runtime_record.parent.mkdir(parents=True)
    runtime_record.write_text(
        json.dumps({"pid": 4244, "host": "127.0.0.1", "port": 8765}),
        encoding="utf-8",
    )
    window = {
        "title": "Stock Desk",
        "layer": 0,
        "on_screen": True,
        "width": 1280,
        "height": 800,
    }
    monkeypatch.setattr(
        macos_tauri_support, "observe_native_window", lambda *_args: window
    )

    service_pid, observed_window = macos_full_product_test._wait_for_ready_state(
        context, 1
    )

    assert service_pid == 4244
    assert observed_window == window


def test_harness_separates_local_data_root_from_v11_product_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary_root = tmp_path / "harness"
    temporary_root.mkdir()
    paths = macos_full_product_test.HarnessPaths.create(temporary_root)
    context = macos_full_product_test.HarnessContext(
        paths=paths,
        output=tmp_path / "output",
    )
    paths.host_path.parent.mkdir(parents=True)
    paths.host_path.write_bytes(b"host")
    captured: dict[str, object] = {}

    class Process:
        pid = 4242

    def popen(*args: object, **kwargs: object) -> Process:
        captured["args"] = args
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(macos_full_product_test.subprocess, "Popen", popen)

    macos_full_product_test._launch_application(context, "a" * 40, "b" * 40, "nonce")

    assert paths.local_data_root == temporary_root / "data"
    assert paths.data_root == paths.local_data_root / "Stock Desk" / "v1.1"
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["STOCK_DESK_MACOS_TEST_DATA_ROOT"] == os.fspath(
        paths.local_data_root
    )
    assert environment["STOCK_DESK_MACOS_TEST_DATA_ROOT"] != os.fspath(paths.data_root)
    app_source = (ROOT / "src-tauri" / "src" / "app.rs").read_text(encoding="utf-8")
    sidecar_source = (ROOT / "src-tauri" / "src" / "sidecar.rs").read_text(
        encoding="utf-8"
    )
    assert 'local_data_root.join("Stock Desk").join("v1.1")' in app_source
    assert 'data_root: local_data_root.join("Stock Desk").join("v1.1")' in (
        sidecar_source
    )


def test_context_creation_removes_partial_temporary_root_on_baseexception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary_root = tmp_path / "partial-harness"
    temporary_root.mkdir()
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(temporary_root),
    )

    def fail_create(root: Path) -> object:
        (root / "partially-created").mkdir()
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        macos_full_product_test.HarnessPaths,
        "create",
        fail_create,
    )

    with pytest.raises(KeyboardInterrupt):
        macos_full_product_test._create_context(tmp_path / "output")

    assert not temporary_root.exists()


def test_context_creation_canonicalizes_mkdtemp_symlink_ancestors_before_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    canonical_parent = tmp_path / "private" / "var"
    canonical_parent.mkdir(parents=True)
    alias_parent = tmp_path / "var"
    alias_parent.symlink_to(canonical_parent, target_is_directory=True)
    canonical_root = canonical_parent / "stock-desk-created-root"
    canonical_root.mkdir()
    aliased_root = alias_parent / canonical_root.name
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(aliased_root),
    )

    context = macos_full_product_test._create_context(tmp_path / "output")

    assert context.paths.temporary_root == canonical_root
    assert context.paths.local_data_root == canonical_root / "data"
    assert context.paths.data_root == canonical_root / "data" / "Stock Desk" / "v1.1"
    assert context.paths.app_root == canonical_root / "app"
    assert context.paths.cargo == canonical_root / "cargo"

    macos_full_product_test._cleanup(context)

    assert not canonical_root.exists()
    assert not aliased_root.exists()
    assert alias_parent.is_symlink()


def test_context_creation_removes_raw_root_when_canonicalization_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_root = tmp_path / "raw-mkdtemp-root"
    raw_root.mkdir()
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(raw_root),
    )
    original_resolve = Path.resolve

    def fail_raw_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == raw_root:
            raise RuntimeError("canonicalization failed")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(macos_full_product_test.Path, "resolve", fail_raw_resolve)

    with pytest.raises(RuntimeError, match="canonicalization failed"):
        macos_full_product_test._create_context(tmp_path / "output")

    assert not raw_root.exists()


@pytest.mark.parametrize("cleanup_mode", ["raises", "leaves-residual"])
def test_context_creation_preserves_resolve_error_and_notes_raw_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_mode: str,
) -> None:
    raw_root = tmp_path / "unclean-raw-mkdtemp-root"
    raw_root.mkdir()
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(raw_root),
    )
    original_resolve = Path.resolve

    def fail_raw_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == raw_root:
            raise RuntimeError("canonicalization failed")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(macos_full_product_test.Path, "resolve", fail_raw_resolve)
    if cleanup_mode == "raises":
        monkeypatch.setattr(
            macos_full_product_test.shutil,
            "rmtree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("remove failed")),
        )
    else:
        monkeypatch.setattr(
            macos_full_product_test.shutil,
            "rmtree",
            lambda *_args, **_kwargs: None,
        )

    with pytest.raises(RuntimeError, match="canonicalization failed") as captured:
        macos_full_product_test._create_context(tmp_path / "output")

    notes = getattr(captured.value, "__notes__", [])
    assert any("context cleanup failed" in note for note in notes)
    assert any("temporary root remains" in note for note in notes)


@pytest.mark.parametrize("cleanup_mode", ["raises", "leaves-residual"])
def test_context_creation_preserves_original_error_and_notes_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_mode: str,
) -> None:
    temporary_root = tmp_path / "unclean-partial-harness"
    temporary_root.mkdir()
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(temporary_root),
    )

    def fail_create(root: Path) -> object:
        (root / "partially-created").mkdir()
        raise KeyboardInterrupt("initialization interrupted")

    monkeypatch.setattr(macos_full_product_test.HarnessPaths, "create", fail_create)
    if cleanup_mode == "raises":
        monkeypatch.setattr(
            macos_full_product_test.shutil,
            "rmtree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("remove failed")),
        )
    else:
        monkeypatch.setattr(
            macos_full_product_test.shutil,
            "rmtree",
            lambda *_args, **_kwargs: None,
        )

    with pytest.raises(
        KeyboardInterrupt, match="initialization interrupted"
    ) as captured:
        macos_full_product_test._create_context(tmp_path / "output")

    notes = getattr(captured.value, "__notes__", [])
    assert any("context cleanup failed" in note for note in notes)
    assert any("temporary root remains" in note for note in notes)


def test_operator_wait_observes_process_tree_before_accepting_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "operator-evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    observed: list[str] = []

    class Tree:
        def observe(self) -> tuple[object, ...]:
            observed.append("observe")
            return ()

    class Evidence:
        screenshots: tuple[object, ...] = ()
        visual_states: tuple[object, ...] = ()

    monkeypatch.setattr(
        macos_full_product_test,
        "validate_operator_evidence",
        lambda *_args: Evidence(),
    )

    result = macos_full_product_test._await_operator_evidence(
        evidence_path,
        IDENTITY,
        60,
        process_tree=Tree(),
        visual_analyzer=tmp_path / "visual-analyzer",
    )

    assert isinstance(result, Evidence)
    assert len(observed) >= 2


def test_operator_wait_binds_declared_viewport_to_png_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "operator-evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    screenshot_path = tmp_path / "market-light-normal.png"
    screenshot_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1366).to_bytes(4, "big")
        + (768).to_bytes(4, "big")
    )

    class Tree:
        def observe(self) -> tuple[object, ...]:
            return ()

    @dataclass(frozen=True)
    class Screenshot:
        name: str
        size: int
        sha256: str
        width: int
        height: int

    @dataclass(frozen=True)
    class Evidence:
        screenshots: tuple[Screenshot, ...]
        visual_states: tuple[object, ...] = ()

    evidence = Evidence(
        screenshots=(
            Screenshot(
                name=screenshot_path.name,
                size=screenshot_path.stat().st_size,
                sha256=macos_tauri_support.sha256_file(screenshot_path),
                width=1366,
                height=768,
            ),
        )
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "validate_operator_evidence",
        lambda *_args: evidence,
    )

    assert (
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            60,
            process_tree=Tree(),
            visual_analyzer=tmp_path / "visual-analyzer",
        )
        is evidence
    )

    forged = Evidence(screenshots=(replace(evidence.screenshots[0], width=900),))
    monkeypatch.setattr(
        macos_full_product_test,
        "validate_operator_evidence",
        lambda *_args: forged,
    )
    with pytest.raises(MacOSJourneyError, match="viewport.*PNG"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            60,
            process_tree=Tree(),
            visual_analyzer=tmp_path / "visual-analyzer",
        )


def _orchestration_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[tuple[object, ...]]]:
    output = ROOT / "test-results" / "macos-full-product-unit-test"
    temporary_root = tmp_path / "stock-desk-full-product"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        macos_full_product_test, "_preflight", lambda: calls.append(("preflight",))
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_source_identity",
        lambda expected=None: (
            calls.append(("source", expected)) or ("a" * 40, "b" * 40)
        ),
    )

    def mkdtemp(**_kwargs: object) -> str:
        temporary_root.mkdir()
        return os.fspath(temporary_root)

    monkeypatch.setattr(macos_full_product_test.tempfile, "mkdtemp", mkdtemp)

    def build(paths: object, timeout_seconds: int, source_sha: str) -> None:
        calls.append(("build", paths, timeout_seconds, source_sha))

    def launch(paths: object, source_sha: str, source_tree: str, nonce: str) -> object:
        calls.append(("launch", paths, source_sha, source_tree, nonce))
        assert isinstance(paths, macos_full_product_test.HarnessContext)

        class Tree:
            def observe(self) -> tuple[object, ...]:
                return ()

        paths.process_tree = Tree()  # type: ignore[assignment]
        return _HostProcess()

    monkeypatch.setattr(macos_full_product_test, "_build_application", build)
    monkeypatch.setattr(
        macos_full_product_test,
        "_build_visual_analyzer",
        lambda context: calls.append(("build-visual-analyzer", context)),
    )
    monkeypatch.setattr(macos_full_product_test, "_launch_application", launch)
    monkeypatch.setattr(
        macos_full_product_test,
        "_wait_for_sidecar_child",
        lambda *_args, **_kwargs: 4243,
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_wait_for_ready_state",
        lambda *_args, **_kwargs: (
            4244,
            {
                "title": "Stock Desk",
                "on_screen": True,
                "layer": 0,
                "width": 1280,
                "height": 800,
            },
        ),
    )

    def evidence(
        path: Path,
        identity: JourneyIdentity,
        timeout_seconds: int,
        *,
        process_tree: object,
        visual_analyzer: Path,
    ) -> object:
        assert process_tree is not None
        assert visual_analyzer.name == "macos-visual-analyzer"
        ready = json.loads((output / "interaction-ready.json").read_text())
        assert ready["session_nonce"] == identity.session_nonce
        assert ready["host_pid"] == 4242
        assert ready["sidecar_pid"] == 4244
        assert ready["expected_actions"] == list(EXPECTED_ACTIONS)
        visual_contracts = ready["expected_visual_states"]
        assert isinstance(visual_contracts, list)
        assert len(visual_contracts) == len(EXPECTED_VISUAL_STATES)
        assert visual_contracts[0] == {
            "page": "onboarding",
            "route": "/market",
            "page_marker": "可以开始使用了",
            "navigation_action": "launch-onboarding",
            "navigation_input_method": "native-launch",
            "theme": "light",
            "theme_action": "click-theme-light",
            "theme_input_method": "sky.click",
            "layout": "normal",
            "layout_action": "drag-window-normal",
            "layout_input_method": "sky.drag",
            "viewport": {"minimum_width": 1100, "minimum_height": 700},
        }
        assert visual_contracts[-1]["page"] == "settings"
        assert visual_contracts[-1]["route"] == "/settings"
        assert visual_contracts[-1]["layout"] == "narrow"
        calls.append(("evidence", path, timeout_seconds))
        payload = valid_payload()
        payload.update(
            source_sha=identity.source_sha,
            source_tree=identity.source_tree,
            session_nonce=identity.session_nonce,
            host_pid=identity.host_pid,
            sidecar_pid=identity.sidecar_pid,
        )
        return validate_operator_evidence(payload, identity=identity)

    monkeypatch.setattr(macos_full_product_test, "_await_operator_evidence", evidence)
    monkeypatch.setattr(
        macos_full_product_test,
        "validate_isolated_product_state",
        lambda *_args: {
            "providers": ["akshare", "baostock"],
            "symbols": ["000001.SS", "600519.SH"],
            "daily_bar_rows": 4,
            "formula_version_id": "formula-version-1",
            "backtest_run_id": "backtest-run-1",
            "backtest_report_id": "backtest-run-1",
            "trade_rows": 1,
            "metric_rows": 1,
        },
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_wait_for_graceful_exit",
        lambda *_args, **_kwargs: calls.append(("graceful-exit",)),
    )

    def cleanup(context: object) -> None:
        calls.append(("cleanup", context))
        macos_full_product_test.shutil.rmtree(temporary_root, ignore_errors=True)

    monkeypatch.setattr(macos_full_product_test, "_cleanup", cleanup)
    macos_full_product_test.shutil.rmtree(output, ignore_errors=True)
    return output, calls


def test_full_product_orchestration_emits_closed_report_and_always_cleans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, calls = _orchestration_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(macos_full_product_test.uuid, "uuid4", lambda: "nonce")

    try:
        result = macos_full_product_test.run_full_product_test(output, 300)

        assert result["schema_version"] == "stock-desk-macos-full-product-v1"
        assert result["source_sha"] == "a" * 40
        assert result["source_tree"] == "b" * 40
        assert result["embedded_webview"] == "WKWebView"
        assert result["screenshot_content_verified"] is True
        assert result["process_cleanup_confirmed"] is True
        assert result["temporary_root_removed"] is True
        assert (output / "macos-full-product.json").is_file()
        assert not (output / "interaction-ready.json").exists()
        assert not (output / "operator-evidence.json").exists()
        assert [call[0] for call in calls].count("cleanup") == 1
        assert [call[0] for call in calls].index("graceful-exit") < [
            call[0] for call in calls
        ].index("cleanup")
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_final_report_failure_removes_all_visual_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    original_write = macos_full_product_test.macos_tauri_support.atomic_write_json

    def fail_final_report(path: Path, payload: object) -> None:
        if path.name == "macos-full-product.json":
            (output / "journey-1.png").write_bytes(b"unverified")
            raise OSError("report write failed")
        original_write(path, payload)

    monkeypatch.setattr(
        macos_full_product_test.macos_tauri_support,
        "atomic_write_json",
        fail_final_report,
    )
    try:
        with pytest.raises(OSError, match="report write failed"):
            macos_full_product_test.run_full_product_test(output, 300)
        assert not list(output.glob("*.png"))
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_final_evidence_cleanup_failure_removes_success_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    original_remove = macos_full_product_test._remove_operator_intermediates

    def fail_preservation(path: Path, preserve: frozenset[str] = frozenset()) -> None:
        if preserve:
            raise OSError("preservation failed")
        original_remove(path, preserve)

    monkeypatch.setattr(
        macos_full_product_test,
        "_remove_operator_intermediates",
        fail_preservation,
    )
    try:
        with pytest.raises(OSError, match="preservation failed"):
            macos_full_product_test.run_full_product_test(output, 300)
        assert not (output / "macos-full-product.json").exists()
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_cleanup_preserves_final_visual_evidence(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "interaction-ready.json").write_text("{}", encoding="utf-8")
    (output / "operator-evidence.json").write_text("{}", encoding="utf-8")
    screenshot = output / "market-light-normal.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nvisual-evidence")
    unverified = output / "unverified.png"
    unverified.write_bytes(b"unverified")

    macos_full_product_test._remove_operator_intermediates(
        output, frozenset({screenshot.name})
    )

    assert not (output / "interaction-ready.json").exists()
    assert not (output / "operator-evidence.json").exists()
    assert screenshot.read_bytes() == b"\x89PNG\r\n\x1a\nvisual-evidence"
    assert not unverified.exists()


@pytest.mark.parametrize(
    ("stage", "raised"),
    [
        ("_build_application", RuntimeError("build failed")),
        ("_launch_application", RuntimeError("launch failed")),
        ("_wait_for_ready_state", TimeoutError("ready timed out")),
        ("_await_operator_evidence", TimeoutError("operator evidence timed out")),
        ("_await_operator_evidence", MacOSJourneyError("malicious evidence")),
        ("_await_operator_evidence", KeyboardInterrupt()),
    ],
)
def test_full_product_orchestration_cleans_every_failure_and_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    raised: BaseException,
) -> None:
    output, calls = _orchestration_fakes(monkeypatch, tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(macos_full_product_test, stage, fail)
    try:
        with pytest.raises(type(raised), match=str(raised) or None):
            macos_full_product_test.run_full_product_test(output, 300)
        assert [call[0] for call in calls].count("cleanup") == 1
        assert not (tmp_path / "stock-desk-full-product").exists()
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_full_product_orchestration_surfaces_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)

    def fail_cleanup(_context: object) -> None:
        raise macos_full_product_test.MacOSFullProductError("cleanup failed")

    monkeypatch.setattr(macos_full_product_test, "_cleanup", fail_cleanup)
    try:
        with pytest.raises(
            macos_full_product_test.MacOSFullProductError, match="cleanup failed"
        ):
            macos_full_product_test.run_full_product_test(output, 300)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_full_product_gate_has_one_non_release_public_command() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["desktop:test:macos:full"] == (
        "uv run --frozen --extra providers python scripts/macos_full_product_test.py"
    )


def test_full_product_cli_accepts_pnpm_argument_separator() -> None:
    with pytest.raises(SystemExit) as captured:
        macos_full_product_test.main(["--", "--help"])

    assert captured.value.code == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="v2"), "schema version"),
        (lambda value: value.update(app_identifier="example.invalid"), "identifier"),
        (lambda value: value.update(driver="script"), "driver"),
        (lambda value: value.update(host_pid=True), "identity"),
        (lambda value: value.update(sidecar_pid=4242), "identity"),
        (lambda value: value.update(providers=[]), "provider"),
        (lambda value: value.update(providers=["akshare", "akshare"]), "provider"),
        (lambda value: value.update(providers=[True]), "provider"),
        (lambda value: value.update(symbols="000001.SS"), "symbol.*shape"),
        (lambda value: value.update(symbols=["000001.SS"]), "symbol set"),
        (lambda value: value.update(symbols=["000001.SS", True]), "symbol set"),
        (lambda value: value.update(kline_cutoff=20260716), "K-line"),
        (lambda value: value.update(kline_cutoff="not-a-date"), "K-line"),
    ],
)
def test_evidence_schema_mutations_fail_closed(mutation: object, message: str) -> None:
    payload = valid_payload()
    assert callable(mutation)
    mutation(payload)

    with pytest.raises(MacOSJourneyError, match=message):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("recognized_text", "luminance", "message"),
    [
        ("行情工作区", 0.9, "text.*shape"),
        ([], 0.9, "text is invalid"),
        ([""], 0.9, "text is invalid"),
        ([True], 0.9, "text is invalid"),
        (["x" * 513], 0.9, "text is invalid"),
        (["行情工作区"], True, "luminance"),
        (["行情工作区"], "0.9", "luminance"),
        (["行情工作区"], -0.1, "luminance"),
        (["行情工作区"], 1.1, "luminance"),
    ],
)
def test_visual_analysis_rejects_malformed_text_and_luminance(
    recognized_text: object, luminance: object, message: str
) -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    state = next(
        item
        for item in evidence.visual_states
        if (item.page, item.theme, item.layout) == ("market", "light", "normal")
    )

    with pytest.raises(MacOSJourneyError, match=message):
        validate_visual_analysis(
            {"recognized_text": recognized_text, "median_luminance": luminance},
            state=state,
        )


def test_visual_analysis_accepts_dark_theme_and_normalizes_marker_spacing() -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    state = next(
        item
        for item in evidence.visual_states
        if (item.page, item.theme, item.layout) == ("market", "dark", "normal")
    )

    assert (
        validate_visual_analysis(
            {"recognized_text": ["行情  工作区"], "median_luminance": 0.1},
            state=state,
        )["theme"]
        == "dark"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("extra", True, "shape"),
        ("name", "../escape.png", "name"),
        ("name", "journey.txt", "format"),
        ("sha256", "invalid", "digest"),
        ("size", True, "size"),
        ("size", 1_023, "size"),
        ("width", True, "viewport"),
        ("width", 639, "viewport"),
        ("height", 2_161, "viewport"),
    ],
)
def test_screenshot_schema_mutations_fail_closed(
    field: str, value: object, message: str
) -> None:
    payload = valid_payload()
    screenshots = payload["screenshots"]
    assert isinstance(screenshots, list)
    screenshot = screenshots[0]
    assert isinstance(screenshot, dict)
    screenshot[field] = value

    with pytest.raises(MacOSJourneyError, match=message):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_duplicate_screenshot_and_action_bindings_fail_closed() -> None:
    payload = valid_payload()
    screenshots = payload["screenshots"]
    assert isinstance(screenshots, list)
    first = screenshots[0]
    second = screenshots[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["sha256"] = first["sha256"]
    with pytest.raises(MacOSJourneyError, match="screenshot is duplicated"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    actions = payload["actions"]
    assert isinstance(actions, list)
    first_action = actions[0]
    second_action = actions[1]
    assert isinstance(first_action, dict) and isinstance(second_action, dict)
    second_action["screenshot_sha256"] = first_action["screenshot_sha256"]
    with pytest.raises(MacOSJourneyError, match="action screenshots.*unique"):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("extra", True, "shape"),
        ("observed", False, "observed"),
        ("input_method", "script", "input method"),
        ("physical_mouse_click", False, "physical click"),
    ],
)
def test_action_schema_mutations_fail_closed(
    field: str, value: object, message: str
) -> None:
    payload = valid_payload()
    actions = payload["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    action[field] = value

    with pytest.raises(MacOSJourneyError, match=message):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_visual_state_schema_and_screenshot_binding_fail_closed() -> None:
    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    first = states[0]
    assert isinstance(first, dict)
    first["extra"] = True
    with pytest.raises(MacOSJourneyError, match="visual state shape"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    first = states[0]
    assert isinstance(first, dict)
    first["screenshot_sha256"] = "f" * 64
    with pytest.raises(MacOSJourneyError, match="visual state screenshot is unbound"):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("DELETE FROM market_dataset WHERE symbol = '600519.SH'", "daily bars"),
        (
            "UPDATE market_dataset SET row_count = 0 WHERE symbol = '600519.SH'",
            "daily bars",
        ),
        (
            "UPDATE market_dataset SET data_cutoff = '' WHERE symbol = '600519.SH'",
            "daily bars",
        ),
        (
            "UPDATE market_dataset SET data_cutoff = '2026-07-15' "
            "WHERE symbol = '600519.SH'",
            "cutoff",
        ),
        (
            "DELETE FROM market_routing_manifest WHERE symbol = '600519.SH'",
            "routing manifest",
        ),
        ("DELETE FROM instrument_dataset_item", "ordinary A-share"),
        ("DELETE FROM backtest_run", "backtest run"),
        ("UPDATE backtest_run SET snapshot_json = 'not-json'", "snapshot"),
        (
            "UPDATE backtest_run SET snapshot_json = "
            "json_set(snapshot_json, '$.symbol_inputs', 'invalid')",
            "evidence pin",
        ),
        (
            "UPDATE backtest_run SET snapshot_json = "
            "json_set(snapshot_json, '$.symbol_inputs', json('[]'))",
            "evidence pin",
        ),
        (
            "UPDATE backtest_run SET snapshot_json = replace("
            "snapshot_json, 'status-manifest-1', '')",
            "manifest identity",
        ),
        (
            "UPDATE execution_status_routing_manifest SET manifest_record_id = "
            "'different-manifest'",
            "status manifest is missing",
        ),
    ],
)
def test_isolated_state_additional_fail_closed_branches(
    tmp_path: Path, statement: str, message: str
) -> None:
    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)

    with pytest.raises(MacOSJourneyError, match=message):
        validate_isolated_product_state(tmp_path, evidence)


def test_isolated_state_rejects_unsafe_or_broken_database(tmp_path: Path) -> None:
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    with pytest.raises(MacOSJourneyError, match="missing or unsafe"):
        validate_isolated_product_state(tmp_path, evidence)

    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE market_dataset")
    with pytest.raises(MacOSJourneyError, match="database validation failed"):
        validate_isolated_product_state(tmp_path, evidence)


def _harness_context(tmp_path: Path) -> macos_full_product_test.HarnessContext:
    root = tmp_path / "harness"
    root.mkdir()
    return macos_full_product_test.HarnessContext(
        macos_full_product_test.HarnessPaths.create(root),
        tmp_path / "output",
    )


def test_macos_preflight_and_source_identity_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_full_product_test.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        macos_full_product_test.shutil, "which", lambda command: f"/bin/{command}"
    )
    monkeypatch.setattr(macos_tauri_support, "screen_is_locked", lambda: False)
    macos_full_product_test._preflight()

    expected = ("a" * 40, "b" * 40)
    monkeypatch.setattr(
        macos_tauri_support,
        "require_clean_source_identity",
        lambda root, supplied: (assert_root(root), supplied)[1],
    )
    assert macos_full_product_test._source_identity(expected) == expected

    monkeypatch.setattr(macos_full_product_test.platform, "system", lambda: "Linux")
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="requires Darwin"
    ):
        macos_full_product_test._preflight()


def assert_root(root: Path) -> tuple[str, str]:
    assert root == ROOT
    return ("a" * 40, "b" * 40)


def test_macos_preflight_rejects_missing_command_and_locked_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_full_product_test.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        macos_full_product_test.shutil,
        "which",
        lambda command: None if command == "pnpm" else f"/bin/{command}",
    )
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="missing: pnpm"
    ):
        macos_full_product_test._preflight()

    monkeypatch.setattr(
        macos_full_product_test.shutil, "which", lambda command: f"/bin/{command}"
    )
    monkeypatch.setattr(macos_tauri_support, "screen_is_locked", lambda: True)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="unlock the Mac"
    ):
        macos_full_product_test._preflight()


def test_build_application_assembles_disposable_bundle_with_bound_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    artifact = tmp_path / "built-sidecar"
    artifact.write_bytes(b"sidecar")
    captured: dict[str, object] = {}
    monkeypatch.setattr(macos_sidecar, "host_target_triple", lambda: "test-target")
    monkeypatch.setattr(
        macos_sidecar, "sidecar_filename", lambda target: f"sidecar-{target}"
    )
    monkeypatch.setattr(
        macos_sidecar, "build_native_sidecar", lambda *_args, **_kwargs: artifact
    )
    monkeypatch.setattr(macos_tauri_support, "copy_exclusive", lambda *_args: None)

    def run(command: object, **kwargs: object) -> object:
        captured["command"] = command
        captured.update(kwargs)
        built_app = (
            context.paths.cargo / "debug" / "bundle" / "macos" / "Stock Desk.app"
        )
        host = built_app / "Contents" / "MacOS" / "stock-desk-desktop"
        host.parent.mkdir(parents=True)
        host.write_bytes(b"host")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_full_product_test.subprocess, "run", run)

    macos_full_product_test._build_application(context, 120, "a" * 40)

    assert context.paths.host_path.read_bytes() == b"host"
    assert context.sidecar_copy is not None
    assert captured["timeout"] == 120
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["STOCK_DESK_SOURCE_REVISION"] == "a" * 40


@pytest.mark.parametrize("returncode", [1, 0])
def test_build_application_rejects_failed_or_missing_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int
) -> None:
    context = _harness_context(tmp_path)
    artifact = tmp_path / "built-sidecar"
    artifact.write_bytes(b"sidecar")
    monkeypatch.setattr(macos_sidecar, "host_target_triple", lambda: "test-target")
    monkeypatch.setattr(
        macos_sidecar, "sidecar_filename", lambda target: f"sidecar-{target}"
    )
    monkeypatch.setattr(
        macos_sidecar, "build_native_sidecar", lambda *_args, **_kwargs: artifact
    )
    monkeypatch.setattr(macos_tauri_support, "copy_exclusive", lambda *_args: None)
    monkeypatch.setattr(
        macos_full_product_test.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode),
    )

    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="app build failed"
    ):
        macos_full_product_test._build_application(context, 120, "a" * 40)


def test_build_application_rejects_bundle_without_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    artifact = tmp_path / "built-sidecar"
    artifact.write_bytes(b"sidecar")
    monkeypatch.setattr(macos_sidecar, "host_target_triple", lambda: "test-target")
    monkeypatch.setattr(
        macos_sidecar, "sidecar_filename", lambda target: f"sidecar-{target}"
    )
    monkeypatch.setattr(
        macos_sidecar, "build_native_sidecar", lambda *_args, **_kwargs: artifact
    )
    monkeypatch.setattr(macos_tauri_support, "copy_exclusive", lambda *_args: None)

    def run(*_args: object, **_kwargs: object) -> object:
        (context.paths.cargo / "debug" / "bundle" / "macos" / "Stock Desk.app").mkdir(
            parents=True
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_full_product_test.subprocess, "run", run)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="host executable"
    ):
        macos_full_product_test._build_application(context, 120, "a" * 40)


@pytest.mark.parametrize(
    "returncode,create_binary", [(0, True), (1, False), (0, False)]
)
def test_visual_analyzer_build_requires_successful_compiler_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    create_binary: bool,
) -> None:
    context = _harness_context(tmp_path)

    def run(*_args: object, **_kwargs: object) -> object:
        if create_binary:
            context.paths.visual_analyzer.write_bytes(b"analyzer")
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(macos_full_product_test.subprocess, "run", run)
    if returncode == 0 and create_binary:
        macos_full_product_test._build_visual_analyzer(context)
        assert context.paths.visual_analyzer.is_file()
    else:
        with pytest.raises(
            macos_full_product_test.MacOSFullProductError, match="analyzer build failed"
        ):
            macos_full_product_test._build_visual_analyzer(context)


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "{}", "analysis failed"),
        (0, "x" * 262_145, "analysis failed"),
        (0, "not-json", "analysis is invalid"),
    ],
)
def test_screenshot_analyzer_rejects_failed_oversized_or_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        macos_full_product_test.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    with pytest.raises(MacOSJourneyError, match=message):
        macos_full_product_test._analyze_screenshot(
            tmp_path / "analyzer", tmp_path / "image.png"
        )

    if returncode == 0 and stdout == "not-json":
        monkeypatch.setattr(
            macos_full_product_test.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout='{"recognized_text": [], "median_luminance": 0.9}'
            ),
        )
        assert isinstance(
            macos_full_product_test._analyze_screenshot(
                tmp_path / "analyzer", tmp_path / "image.png"
            ),
            dict,
        )


class _FakeTree:
    def __init__(self, sidecar_pid: int | None = None) -> None:
        self.pid = sidecar_pid
        self.calls: list[str] = []

    def observe(self) -> tuple[object, ...]:
        self.calls.append("observe")
        return ()

    def sidecar_pid(self) -> int | None:
        return self.pid

    def verified_sidecar_pid(self, pid: int) -> bool:
        return pid == self.pid

    def verify_absent(self) -> None:
        self.calls.append("verify-absent")

    def terminate(self) -> None:
        self.calls.append("terminate")


def _monotonic(monkeypatch: pytest.MonkeyPatch, *values: float) -> None:
    iterator = iter(values)
    monkeypatch.setattr(
        macos_full_product_test.time, "monotonic", lambda: next(iterator)
    )
    monkeypatch.setattr(macos_full_product_test.time, "sleep", lambda _seconds: None)


def test_sidecar_wait_requires_tree_and_accepts_verified_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="not initialized"
    ):
        macos_full_product_test._wait_for_sidecar_child(context, 1)

    tree = _FakeTree(4244)
    context.process_tree = tree  # type: ignore[assignment]
    _monotonic(monkeypatch, 0, 0.1)
    assert macos_full_product_test._wait_for_sidecar_child(context, 1) == 4244
    assert tree.calls == ["observe"]


def test_sidecar_wait_rejects_early_host_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    context.process_tree = _FakeTree()  # type: ignore[assignment]
    context.host_process = SimpleNamespace(poll=lambda: 1)
    _monotonic(monkeypatch, 0, 0.1)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="exited before sidecar"
    ):
        macos_full_product_test._wait_for_sidecar_child(context, 1)

    context.host_process = None
    _monotonic(monkeypatch, 0, 2)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="timed out"
    ):
        macos_full_product_test._wait_for_sidecar_child(context, 1)


def test_ready_wait_rejects_missing_tree_invalid_record_and_missing_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="tree is unavailable"
    ):
        macos_full_product_test._wait_for_ready_state(context, 1)

    tree = _FakeTree(4244)
    context.process_tree = tree  # type: ignore[assignment]
    record = context.paths.data_root / "runtime" / "runtime.json"
    record.parent.mkdir(parents=True)
    record.write_text("not-json", encoding="utf-8")
    _monotonic(monkeypatch, 0, 0.1, 2)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="timed out waiting"
    ):
        macos_full_product_test._wait_for_ready_state(context, 1)

    record.write_text(
        json.dumps({"pid": 4244, "host": "127.0.0.1", "port": 8765}),
        encoding="utf-8",
    )
    _monotonic(monkeypatch, 0, 0.1)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError,
        match="host process is unavailable",
    ):
        macos_full_product_test._wait_for_ready_state(context, 1)


def test_operator_evidence_wait_rejects_timeout_size_and_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _FakeTree()
    evidence_path = tmp_path / "operator-evidence.json"
    _monotonic(monkeypatch, 0, 2)
    with pytest.raises(MacOSJourneyError, match="timed out"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=tree,  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )

    evidence_path.write_bytes(b"x" * 1_048_577)
    _monotonic(monkeypatch, 0, 0.1)
    with pytest.raises(MacOSJourneyError, match="unexpectedly large"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=tree,  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )

    evidence_path.write_text("not-json", encoding="utf-8")
    _monotonic(monkeypatch, 0, 0.1)
    with pytest.raises(MacOSJourneyError, match="not valid JSON"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=tree,  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )


def _single_screenshot_evidence(
    screenshot_path: Path,
    *,
    size: int | None = None,
    digest: str | None = None,
    visual_states: tuple[object, ...] = (),
) -> object:
    screenshot = SimpleNamespace(
        name=screenshot_path.name,
        size=screenshot_path.stat().st_size if size is None else size,
        sha256=(
            macos_tauri_support.sha256_file(screenshot_path)
            if digest is None
            else digest
        ),
        width=1366,
        height=768,
    )
    return SimpleNamespace(screenshots=(screenshot,), visual_states=visual_states)


def test_operator_evidence_wait_rejects_missing_and_mismatched_screenshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "operator-evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.png"
    fake = SimpleNamespace(
        screenshots=(
            SimpleNamespace(
                name=missing.name,
                size=24,
                sha256="a" * 64,
                width=1366,
                height=768,
            ),
        ),
        visual_states=(),
    )
    monkeypatch.setattr(
        macos_full_product_test, "validate_operator_evidence", lambda *_args: fake
    )
    with pytest.raises(MacOSJourneyError, match="missing or unsafe"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=_FakeTree(),  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )

    screenshot = tmp_path / "image.png"
    screenshot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1366).to_bytes(4, "big")
        + (768).to_bytes(4, "big")
    )
    fake = _single_screenshot_evidence(screenshot, digest="b" * 64)
    monkeypatch.setattr(
        macos_full_product_test, "validate_operator_evidence", lambda *_args: fake
    )
    with pytest.raises(MacOSJourneyError, match="identity does not match"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=_FakeTree(),  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )


def test_operator_evidence_wait_runs_visual_analyzer_for_bound_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "operator-evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    screenshot = tmp_path / "image.png"
    screenshot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1366).to_bytes(4, "big")
        + (768).to_bytes(4, "big")
    )
    digest = macos_tauri_support.sha256_file(screenshot)
    state = SimpleNamespace(
        screenshot_sha256=digest, page_marker="行情工作区", theme="light"
    )
    fake = _single_screenshot_evidence(screenshot, visual_states=(state,))
    calls: list[str] = []
    monkeypatch.setattr(
        macos_full_product_test, "validate_operator_evidence", lambda *_args: fake
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_analyze_screenshot",
        lambda *_args: {"recognized_text": ["行情工作区"], "median_luminance": 0.9},
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "validate_visual_analysis",
        lambda *_args, **_kwargs: calls.append("validated"),
    )

    assert (
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=_FakeTree(),  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )
        is fake
    )
    assert calls == ["validated"]


@pytest.mark.parametrize(
    ("poll_result", "message"),
    [(None, "timed out"), (2, "did not exit gracefully")],
)
def test_graceful_exit_rejects_timeout_and_nonzero_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    poll_result: int | None,
    message: str,
) -> None:
    context = _harness_context(tmp_path)
    context.process_tree = _FakeTree()  # type: ignore[assignment]
    context.host_process = SimpleNamespace(poll=lambda: poll_result)
    if poll_result is None:
        _monotonic(monkeypatch, 0, 0.1, 2)
    else:
        _monotonic(monkeypatch, 0, 0.1)
    with pytest.raises(macos_full_product_test.MacOSFullProductError, match=message):
        macos_full_product_test._wait_for_graceful_exit(context, 1)


def test_graceful_exit_requires_identity_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="identity is incomplete"
    ):
        macos_full_product_test._wait_for_graceful_exit(context, 1)

    tree = _FakeTree()
    context.process_tree = tree  # type: ignore[assignment]
    context.host_process = SimpleNamespace(poll=lambda: 0)
    _monotonic(monkeypatch, 0, 0.1)
    macos_full_product_test._wait_for_graceful_exit(context, 1)
    assert tree.calls == ["observe", "verify-absent"]


def test_cleanup_terminates_only_supplied_fake_tree_and_removes_temp_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    tree = _FakeTree()
    context.process_tree = tree  # type: ignore[assignment]
    sidecar_copy = context.paths.temporary_root / "sidecar-copy"
    sidecar_copy.write_bytes(b"sidecar")
    context.sidecar_copy = sidecar_copy
    calls: list[Path] = []
    monkeypatch.setattr(
        macos_tauri_support, "unregister_bundle", lambda path: calls.append(path)
    )

    macos_full_product_test._cleanup(context)

    assert tree.calls == ["terminate"]
    assert calls == [context.paths.app_path]
    assert not context.paths.temporary_root.exists()


def test_cleanup_aggregates_all_failures_and_residuals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)

    class FailingTree(_FakeTree):
        def terminate(self) -> None:
            raise RuntimeError("terminate failed")

    class FailingSidecar:
        def unlink(self, *, missing_ok: bool) -> None:
            assert missing_ok is True
            raise OSError("unlink failed")

        def exists(self) -> bool:
            return True

    context.process_tree = FailingTree()  # type: ignore[assignment]
    context.sidecar_copy = FailingSidecar()  # type: ignore[assignment]
    monkeypatch.setattr(
        macos_tauri_support,
        "unregister_bundle",
        lambda _path: (_ for _ in ()).throw(OSError("unregister failed")),
    )
    monkeypatch.setattr(
        macos_full_product_test.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rmtree failed")),
    )

    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="cleanup failed"
    ) as captured:
        macos_full_product_test._cleanup(context)

    details = str(captured.value)
    assert "terminate failed" in details
    assert "unregister failed" in details
    assert "unlink failed" in details
    assert "rmtree failed" in details
    assert "sidecar remains" in details
    assert "root remains" in details


def test_operator_intermediate_cleanup_handles_png_directory_and_missing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    png_directory = output / "forged.png"
    png_directory.mkdir()
    (png_directory / "payload").write_text("x", encoding="utf-8")

    macos_full_product_test._remove_operator_intermediates(output)
    macos_full_product_test._remove_operator_intermediates(tmp_path / "missing")

    assert not png_directory.exists()


def test_full_product_timeout_and_cli_success_are_validated_before_harness_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(
        macos_full_product_test.MacOSFullProductError, match="between 60 and 1800"
    ):
        macos_full_product_test.run_full_product_test(tmp_path / "output", 59)

    report = {"schema_version": "test-report"}
    monkeypatch.setattr(
        macos_full_product_test, "run_full_product_test", lambda *_args: report
    )
    assert (
        macos_full_product_test.main(
            ["--output", os.fspath(tmp_path / "output"), "--timeout-seconds", "60"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_sha": "invalid"},
        {"source_tree": "invalid"},
        {"session_nonce": "unsafe nonce"},
        {"host_pid": True},
        {"sidecar_pid": 1},
        {"sidecar_pid": 4242},
    ],
)
def test_journey_identity_rejects_invalid_revision_nonce_and_processes(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "session_nonce": "nonce",
        "host_pid": 4242,
        "sidecar_pid": 4243,
    }
    values.update(kwargs)
    with pytest.raises(MacOSJourneyError, match="journey identity"):
        JourneyIdentity(**values)  # type: ignore[arg-type]


def test_evidence_rejects_non_string_keys_and_incomplete_sequences() -> None:
    with pytest.raises(MacOSJourneyError, match="top-level shape"):
        validate_operator_evidence({1: "invalid"}, identity=IDENTITY)

    payload = valid_payload()
    screenshots = payload["screenshots"]
    assert isinstance(screenshots, list)
    screenshots.pop()
    with pytest.raises(MacOSJourneyError, match="screenshot set is incomplete"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    actions = payload["actions"]
    assert isinstance(actions, list)
    actions.pop()
    with pytest.raises(MacOSJourneyError, match="action sequence"):
        validate_operator_evidence(payload, identity=IDENTITY)


def test_evidence_rejects_duplicate_visual_hash_and_unreferenced_png() -> None:
    payload = valid_payload()
    states = payload["visual_states"]
    assert isinstance(states, list)
    first = states[0]
    same_viewport = states[2]
    assert isinstance(first, dict) and isinstance(same_viewport, dict)
    same_viewport["screenshot_sha256"] = first["screenshot_sha256"]
    with pytest.raises(MacOSJourneyError, match="state screenshots.*unique"):
        validate_operator_evidence(payload, identity=IDENTITY)

    payload = valid_payload()
    actions = payload["actions"]
    states = payload["visual_states"]
    assert isinstance(actions, list) and isinstance(states, list)
    last_action = actions[-1]
    replacement_state = states[6]
    assert isinstance(last_action, dict) and isinstance(replacement_state, dict)
    last_action["screenshot_sha256"] = replacement_state["screenshot_sha256"]
    with pytest.raises(MacOSJourneyError, match="screenshot set is not fully bound"):
        validate_operator_evidence(payload, identity=IDENTITY)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE market_routing_manifest SET manifest_json = 1 "
            "WHERE symbol = '600519.SH'",
            "routing manifest is invalid",
        ),
        (
            "UPDATE market_routing_manifest SET manifest_json = '[]' "
            "WHERE symbol = '600519.SH'",
            "routing manifest is invalid",
        ),
        (
            "UPDATE backtest_run SET snapshot_json = replace("
            "snapshot_json, 'basic_no_price_limits', 'authoritative')",
            "not verified basic evidence",
        ),
    ],
)
def test_isolated_state_rejects_non_object_json_and_overclaimed_status(
    tmp_path: Path, statement: str, message: str
) -> None:
    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    with pytest.raises(MacOSJourneyError, match=message):
        validate_isolated_product_state(tmp_path, evidence)


def test_isolated_state_allows_older_index_cutoff_when_stock_matches(
    tmp_path: Path,
) -> None:
    database = _isolated_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE market_dataset SET data_cutoff = '2026-07-15' "
            "WHERE symbol = '000001.SS'"
        )
    evidence = validate_operator_evidence(valid_payload(), identity=IDENTITY)
    assert validate_isolated_product_state(tmp_path, evidence)["daily_bar_rows"] == 4


def test_sidecar_wait_retries_while_host_is_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)

    class DelayedTree(_FakeTree):
        def sidecar_pid(self) -> int | None:
            self.calls.append("sidecar")
            return 4244 if self.calls.count("sidecar") == 2 else None

    tree = DelayedTree()
    context.process_tree = tree  # type: ignore[assignment]
    context.host_process = SimpleNamespace(poll=lambda: None)
    _monotonic(monkeypatch, 0, 0.1, 0.2)

    assert macos_full_product_test._wait_for_sidecar_child(context, 1) == 4244


def test_operator_evidence_rejects_screenshot_outside_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "output" / "operator-evidence.json"
    evidence_path.parent.mkdir()
    evidence_path.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x" * 24)
    fake = SimpleNamespace(
        screenshots=(
            SimpleNamespace(
                name=os.fspath(outside),
                size=24,
                sha256="a" * 64,
                width=1366,
                height=768,
            ),
        ),
        visual_states=(),
    )
    monkeypatch.setattr(
        macos_full_product_test, "validate_operator_evidence", lambda *_args: fake
    )
    with pytest.raises(MacOSJourneyError, match="escaped output"):
        macos_full_product_test._await_operator_evidence(
            evidence_path,
            IDENTITY,
            1,
            process_tree=_FakeTree(),  # type: ignore[arg-type]
            visual_analyzer=tmp_path / "analyzer",
        )


def test_cleanup_tolerates_already_removed_temporary_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _harness_context(tmp_path)
    monkeypatch.setattr(macos_tauri_support, "unregister_bundle", lambda _path: None)
    macos_full_product_test.shutil.rmtree(context.paths.temporary_root)

    macos_full_product_test._cleanup(context)


def test_full_product_rejects_missing_process_tree_after_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_full_product_test,
        "_launch_application",
        lambda *_args, **_kwargs: _HostProcess(),
    )
    try:
        with pytest.raises(
            macos_full_product_test.MacOSFullProductError,
            match="process tree is unavailable",
        ):
            macos_full_product_test.run_full_product_test(output, 300)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_full_product_preserves_primary_error_and_notes_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_full_product_test,
        "_build_visual_analyzer",
        lambda _context: (_ for _ in ()).throw(RuntimeError("primary failed")),
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_cleanup",
        lambda _context: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="primary failed") as captured:
            macos_full_product_test.run_full_product_test(output, 300)
        assert any("cleanup also failed" in note for note in captured.value.__notes__)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)
        macos_full_product_test.shutil.rmtree(
            tmp_path / "stock-desk-full-product", ignore_errors=True
        )


def test_isolated_json_object_rejects_non_string_input() -> None:
    with pytest.raises(MacOSJourneyError, match="routing manifest is invalid"):
        macos_product_journey._json_object(1, "routing manifest")


def test_full_product_rejects_incomplete_evidence_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_full_product_test,
        "_await_operator_evidence",
        lambda *_args, **_kwargs: None,
    )
    try:
        with pytest.raises(
            macos_full_product_test.MacOSFullProductError,
            match="evidence is incomplete",
        ):
            macos_full_product_test.run_full_product_test(output, 300)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_full_product_rejects_temporary_root_left_by_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    temporary_root = tmp_path / "stock-desk-full-product"
    monkeypatch.setattr(macos_full_product_test, "_cleanup", lambda _context: None)
    try:
        with pytest.raises(
            macos_full_product_test.MacOSFullProductError,
            match="temporary full-product root remains",
        ):
            macos_full_product_test.run_full_product_test(output, 300)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)
        macos_full_product_test.shutil.rmtree(temporary_root, ignore_errors=True)


def test_primary_failure_notes_operator_intermediate_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_full_product_test,
        "_build_visual_analyzer",
        lambda _context: (_ for _ in ()).throw(RuntimeError("primary failed")),
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_remove_operator_intermediates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("purge failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="primary failed") as captured:
            macos_full_product_test.run_full_product_test(output, 300)
        assert any(
            "operator evidence cleanup also failed" in note
            for note in captured.value.__notes__
        )
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_final_report_failure_notes_evidence_and_report_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    original_write = macos_tauri_support.atomic_write_json
    original_unlink = Path.unlink

    def write(path: Path, payload: object) -> None:
        if path.name == "macos-full-product.json":
            raise OSError("report write failed")
        original_write(path, payload)

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == "macos-full-product.json":
            raise OSError("report cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(macos_tauri_support, "atomic_write_json", write)
    monkeypatch.setattr(
        macos_full_product_test,
        "_remove_operator_intermediates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("evidence cleanup failed")
        ),
    )
    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises(OSError, match="report write failed") as captured:
            macos_full_product_test.run_full_product_test(output, 300)
        notes = captured.value.__notes__
        assert any("operator evidence cleanup also failed" in note for note in notes)
        assert any("success report cleanup also failed" in note for note in notes)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)


def test_final_preservation_failure_notes_fallback_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _calls = _orchestration_fakes(monkeypatch, tmp_path)
    original_unlink = Path.unlink

    def remove(_path: Path, preserve: frozenset[str] = frozenset()) -> None:
        if preserve:
            raise OSError("preservation failed")
        raise OSError("fallback purge failed")

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == "macos-full-product.json":
            raise OSError("report cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(
        macos_full_product_test, "_remove_operator_intermediates", remove
    )
    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises(OSError, match="preservation failed") as captured:
            macos_full_product_test.run_full_product_test(output, 300)
        notes = captured.value.__notes__
        assert any("operator evidence cleanup also failed" in note for note in notes)
        assert any("success report cleanup also failed" in note for note in notes)
    finally:
        macos_full_product_test.shutil.rmtree(output, ignore_errors=True)
