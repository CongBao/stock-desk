from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import sqlite3

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
from scripts import macos_full_product_test, macos_tauri_support


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
