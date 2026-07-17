from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3

import pytest

from scripts.macos_product_journey import (
    EXPECTED_ACTIONS,
    JourneyIdentity,
    MacOSJourneyError,
    validate_isolated_product_state,
    validate_operator_evidence,
)
from scripts import macos_full_product_test


ROOT = Path(__file__).resolve().parents[2]


IDENTITY = JourneyIdentity(
    source_sha="a" * 40,
    source_tree="b" * 40,
    session_nonce="nonce-0123456789abcdef",
    host_pid=4242,
    sidecar_pid=4243,
)


def valid_payload() -> dict[str, object]:
    screenshots = [
        {
            "name": f"journey-{index}.png",
            "sha256": f"{index:x}" * 64,
            "size": 20_000 + index,
        }
        for index in range(1, len(EXPECTED_ACTIONS) + 1)
    ]
    return {
        "schema_version": "stock-desk-macos-full-product-operator-v1",
        "source_sha": IDENTITY.source_sha,
        "source_tree": IDENTITY.source_tree,
        "session_nonce": IDENTITY.session_nonce,
        "app_identifier": "com.baozijuan.stockdesk",
        "embedded_webview": "WKWebView",
        "driver": "codex-computer-use",
        "input_method": "codex-computer-use-sky-click",
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
        "actions": [
            {
                "action": action,
                "observed": True,
                "input_method": "sky.click",
                "physical_mouse_click": True,
                "screenshot_sha256": screenshots[index]["sha256"],
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
        (lambda value: value.update(database_path="/Users/alice/private.db"), "shape"),
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
            "'indicator', 'subchart', ?, ?)",
            (
                "formula-version-1",
                "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;",
                "sha256:" + "c" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO backtest_run VALUES (?, ?, 'succeeded', 'completed', 1, ?)",
            (
                "backtest-run-1",
                json.dumps({"formula_version_id": "formula-version-1"}),
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
        ("UPDATE backtest_run SET status = 'failed'", "backtest"),
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
    )

    assert isinstance(result, Evidence)
    assert len(observed) >= 2


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
    monkeypatch.setattr(
        macos_full_product_test.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(temporary_root),
    )

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
    monkeypatch.setattr(macos_full_product_test, "_launch_application", launch)
    monkeypatch.setattr(
        macos_full_product_test,
        "_wait_for_sidecar_child",
        lambda *_args, **_kwargs: 4243,
    )
    monkeypatch.setattr(
        macos_full_product_test,
        "_wait_for_ready_state",
        lambda *_args, **_kwargs: {
            "title": "Stock Desk",
            "on_screen": True,
            "layer": 0,
            "width": 1280,
            "height": 800,
        },
    )

    def evidence(
        path: Path,
        identity: JourneyIdentity,
        timeout_seconds: int,
        *,
        process_tree: object,
    ) -> object:
        assert process_tree is not None
        ready = json.loads((output / "interaction-ready.json").read_text())
        assert ready["session_nonce"] == identity.session_nonce
        assert ready["host_pid"] == 4242
        assert ready["sidecar_pid"] == 4243
        assert ready["expected_actions"] == list(EXPECTED_ACTIONS)
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
        macos_full_product_test._remove_operator_intermediates(output)
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
