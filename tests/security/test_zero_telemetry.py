from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import socket

import pytest

from scripts.verify_zero_telemetry import (
    EXPECTED_PRIVACY_POLICY,
    EXPECTED_UPDATER_RUNTIME_CONFIG,
    MANIFESTS,
    PRODUCTION_ROOTS,
    UPDATER_RUNTIME_CONFIG_PATH,
    ZeroTelemetryError,
    audit_repository,
    verify_repository,
)
from stock_desk.diagnostics.models import (
    DiagnosticConfiguration,
    DiagnosticSnapshotService,
)
from stock_desk.runtime_identity import new_worker_id


ROOT = Path(__file__).resolve().parents[2]


PRIVACY_POLICY = copy.deepcopy(EXPECTED_PRIVACY_POLICY)


def _minimal_repository(root: Path) -> None:
    for relative in MANIFESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# locked dependency input\n", encoding="utf-8")
    (root / "src-tauri/Cargo.toml").write_text(
        "[dependencies]\n"
        'tauri-plugin-updater = { version = "=2.10.1", '
        'default-features = false, features = ["native-tls", "zip"] }\n'
        'minisign-verify = { version = "=0.2.5" }\n',
        encoding="utf-8",
    )
    (root / "web/package.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "web/package.json").write_text(
        '{"dependencies": {}, "devDependencies": {}}\n', encoding="utf-8"
    )
    for relative in PRODUCTION_ROOTS:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "safe.py").write_text("VALUE = 'local-only'\n", encoding="utf-8")
    allowlist = PRIVACY_POLICY["phases"]["trusted-updater-foundation"][
        "production_network_exact_paths"
    ]
    for relative in allowlist["python"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import socket as network\nnetwork.socket()\n", encoding="utf-8"
        )
    for relative in allowlist["rust"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "fn network() { let _ = reqwest::Client::new(); }\n", encoding="utf-8"
        )
    updater = root / "src-tauri/src/updater.rs"
    updater.write_text(
        'pub const UPDATE_TARGET: &str = "windows-x86_64-nsis";\n'
        'pub const UPDATE_ARCH: &str = "x86_64";\n'
        'const CURRENT_VERSION: &str = env!("CARGO_PKG_VERSION");\n'
        'const RUNTIME_CONFIG_JSON: &str = include_str!("../../config/tauri-updater-runtime.json");\n'
        'pub const UPDATE_ENDPOINT: &str = "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json";\n'
        "struct RuntimeConfig { enabled: bool }\n"
        "fn runtime_config() { let _ = RUNTIME_CONFIG_JSON; }\n"
        "struct InstalledWatermark;\n"
        'const INSTALLED_WATERMARK_FILE: &str = "installed-watermark.json";\n'
        "fn pending() { let _ = verified_pending; }\n"
        'fn verify() { let _ = PublicKey::decode(""); '
        "updater_windows::verify_authenticode(); "
        "updater_windows::launch_verified_installer(); "
        "updater_journal::persist_pending_install(); }\n"
        "fn fetch_release_candidate() {}\n"
        "fn download_trusted_asset() {}\n"
        "fn plugin() { tauri_plugin_updater::Builder::new().build(); }\n"
        "pub async fn desktop_check_for_updates(app: AppHandle) { let controller = state(); "
        "if !controller.config.enabled { return desktop_update_state(app); } }\n"
        "pub async fn desktop_confirm_update() { let controller = state(); "
        "gate_native_confirmation(controller.config.enabled, || prompt()); }\n"
        "fn gate_native_confirmation(enabled: bool) { if !enabled { "
        'return Err("desktop_updater_disabled"); } }\n',
        encoding="utf-8",
    )
    main = root / "src-tauri/src/main.rs"
    main.write_text(
        "fn main() { builder.plugin(updater::plugin()); }\n", encoding="utf-8"
    )
    for relative in allowlist["web"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "export const request = () => fetch('/api');\n", encoding="utf-8"
        )
    policy = root / "config/desktop-network-privacy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps(PRIVACY_POLICY), encoding="utf-8")
    runtime_config = root / UPDATER_RUNTIME_CONFIG_PATH
    runtime_config.write_text(
        json.dumps(EXPECTED_UPDATER_RUNTIME_CONFIG), encoding="utf-8"
    )
    tauri_config = root / "src-tauri/tauri.conf.json"
    tauri_config.write_text("{}\n", encoding="utf-8")
    windows_config = root / "src-tauri/tauri.windows.conf.json"
    windows_config.write_text("{}\n", encoding="utf-8")
    capability = root / "src-tauri/capabilities/default.json"
    capability.parent.mkdir(parents=True, exist_ok=True)
    capability.write_text('{"permissions": []}\n', encoding="utf-8")


def test_current_locked_application_has_no_telemetry_or_crash_sdk() -> None:
    assert audit_repository(ROOT) == ()


@pytest.mark.parametrize(
    ("original", "replacement", "commented_contract", "expected"),
    [
        (
            'include_str!("../../config/tauri-updater-runtime.json")',
            'include_str!("../../config/untrusted.json")',
            '// include_str!("../../config/tauri-updater-runtime.json")',
            "runtime-contract",
        ),
        (
            "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json",
            "https://example.invalid/latest.json",
            '/* pub const UPDATE_ENDPOINT: &str = "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json"; */',
            "runtime-contract",
        ),
        (
            "if !controller.config.enabled { return desktop_update_state(app); }",
            "if controller.config.enabled { return desktop_update_state(app); }",
            "// if !controller.config.enabled { return desktop_update_state(app); }",
            "command-guard",
        ),
    ],
)
def test_commented_updater_contract_cannot_spoof_the_runtime_gate(
    tmp_path: Path,
    original: str,
    replacement: str,
    commented_contract: str,
    expected: str,
) -> None:
    _minimal_repository(tmp_path)
    updater = tmp_path / "src-tauri/src/updater.rs"
    source = updater.read_text(encoding="utf-8")
    assert original in source
    updater.write_text(
        source.replace(original, replacement, 1) + commented_contract + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ZeroTelemetryError, match=expected):
        verify_repository(tmp_path)


def test_production_worker_ids_use_random_session_identity_not_host_identity() -> None:
    paths = (
        "src/stock_desk/desktop.py",
        "src/stock_desk/sidecar.py",
        "src/stock_desk/tasks/worker.py",
        "src/stock_desk/market/worker_runtime.py",
    )
    for relative in paths:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "socket.gethostname()" not in source
        assert "new_worker_id(" in source


def test_new_worker_id_has_random_unique_session_shape() -> None:
    worker_ids = {new_worker_id("market") for _ in range(64)}

    assert len(worker_ids) == 64
    assert all(re.fullmatch(r"market-[0-9a-f]{32}", item) for item in worker_ids)


@pytest.mark.parametrize(
    "prefix",
    ("", "Market", "market_worker", "1market", "a" * 33),
)
def test_new_worker_id_rejects_invalid_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="short lowercase slug"):
        new_worker_id(prefix)


@pytest.mark.parametrize(
    ("relative", "payload", "expected"),
    [
        ("pyproject.toml", 'sentry-sdk = "1"', "telemetry-sdk"),
        ("web/src/hidden.ts", 'fetch("https://app.posthog.com/capture")', "telemetry"),
        ("src-tauri/Cargo.toml", 'opentelemetry = "1"', "telemetry-sdk"),
    ],
)
def test_hidden_sdk_or_collection_endpoint_fails_static_policy(
    tmp_path: Path,
    relative: str,
    payload: str,
    expected: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ZeroTelemetryError, match=expected):
        verify_repository(tmp_path)


def test_missing_manifest_or_source_symlink_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "uv.lock").unlink()
    linked = tmp_path / "src/stock_desk/linked.py"
    linked.symlink_to(tmp_path / "outside.py")

    violations = audit_repository(tmp_path)
    assert "missing-or-unsafe-manifest:uv.lock" in violations
    assert "unsafe-source-link:src/stock_desk/linked.py" in violations


@pytest.mark.parametrize(
    ("relative", "payload", "expected"),
    [
        (
            "src/stock_desk/identity.py",
            "installation_id = read_machine_guid()\n",
            "stable-device-identifier",
        ),
        (
            "web/src/diagnostics.ts",
            "uploadDiagnosticBundle(bundle);\n",
            "automatic-diagnostic-upload",
        ),
        (
            "src/stock_desk/diagnostics/sender.py",
            'import requests as transport\ntransport.post("https://example.invalid", data=b"bundle")\n',
            "network-path-not-allowlisted",
        ),
        (
            "src-tauri/Cargo.toml",
            'tauri-plugin-updater = "2"\n',
            "trusted-updater-dependency",
        ),
        (
            "src-tauri/capabilities/default.json",
            '{"permissions": ["updater:default"]}\n',
            "updater-enabled",
        ),
        (
            "src-tauri/tauri.conf.json",
            '{"plugins": {"updater": {"endpoints": ["https://example.invalid"]}}}\n',
            "updater-enabled",
        ),
        (
            "src-tauri/tauri.conf.json",
            '{"plugins": {"updater": {"endpoints": [], "pubkey": "", '
            '"dangerousAcceptInvalidCerts": true}}}\n',
            "updater-enabled",
        ),
        (
            "src-tauri/tauri.conf.json",
            '{"plugins": {"updater": {"endpoints": [], "pubkey": "untrusted"}}}\n',
            "updater-enabled",
        ),
        (
            "src-tauri/tauri.conf.json",
            '{"plugins": {"updater": {"endpoints": [], "pubkey": "", '
            '"dangerousInsecureTransportProtocol": true}}}\n',
            "updater-enabled",
        ),
        (
            "src-tauri/tauri.conf.json",
            '[{"updater": {"endpoints": [], "pubkey": ""}}]\n',
            "updater-enabled",
        ),
        (
            UPDATER_RUNTIME_CONFIG_PATH,
            json.dumps({**EXPECTED_UPDATER_RUNTIME_CONFIG, "enabled": True}),
            "updater-runtime-config",
        ),
    ],
)
def test_device_identity_automatic_upload_or_updater_enablement_fails_closed(
    tmp_path: Path,
    relative: str,
    payload: str,
    expected: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ZeroTelemetryError, match=expected):
        verify_repository(tmp_path)


def test_privacy_policy_is_required_and_future_updates_must_remain_anonymous(
    tmp_path: Path,
) -> None:
    _minimal_repository(tmp_path)
    policy_path = tmp_path / "config/desktop-network-privacy.json"
    policy_path.unlink()
    with pytest.raises(ZeroTelemetryError, match="privacy-policy"):
        verify_repository(tmp_path)

    _minimal_repository(tmp_path)
    unsafe = copy.deepcopy(PRIVACY_POLICY)
    unsafe["phases"]["trusted-updater-foundation"]["updater"]["request"]["identity"] = (
        "stable-device"
    )
    policy_path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ZeroTelemetryError, match="privacy-policy"):
        verify_repository(tmp_path)


def test_phase_policy_and_recursive_tauri_json_fail_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    policy_path = tmp_path / "config/desktop-network-privacy.json"
    unsafe = copy.deepcopy(PRIVACY_POLICY)
    unsafe["active_phase"] = "anonymous-updater"
    policy_path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ZeroTelemetryError, match="privacy-policy"):
        verify_repository(tmp_path)

    _minimal_repository(tmp_path)
    nested = tmp_path / "src-tauri/capabilities/nested/update.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text('{"permissions": ["updater:default"]}\n', encoding="utf-8")
    with pytest.raises(ZeroTelemetryError, match="updater-enabled"):
        verify_repository(tmp_path)


def test_capabilities_symlink_ancestor_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    capabilities = tmp_path / "src-tauri/capabilities"
    (capabilities / "default.json").unlink()
    capabilities.rmdir()
    outside = tmp_path / "outside-capabilities"
    outside.mkdir()
    (outside / "default.json").write_text('{"permissions": []}\n', encoding="utf-8")
    capabilities.symlink_to(outside, target_is_directory=True)

    violations = audit_repository(tmp_path)
    assert "missing-or-unsafe-config-root:src-tauri/capabilities" in violations
    assert "missing-or-unsafe-config:src-tauri/capabilities/default.json" in violations


@pytest.mark.parametrize(
    "payload",
    (
        "import httpx2 as transport\nclient = transport.AsyncClient()\n",
        "from urllib import request as transport\ntransport.urlopen('https://example.invalid')\n",
        "import importlib as loader\nloader.import_module('requests')\n",
        "load = __import__\nload('requests')\n",
        "from stock_desk.market.providers.sdk import import_optional_sdk\n"
        "module = import_optional_sdk('akshare')\n",
    ),
)
def test_python_network_aliases_require_an_exact_allowlisted_path(
    tmp_path: Path, payload: str
) -> None:
    _minimal_repository(tmp_path)
    hidden = tmp_path / "src/stock_desk/hidden_transport.py"
    hidden.write_text(payload, encoding="utf-8")
    with pytest.raises(ZeroTelemetryError, match="network-path-not-allowlisted"):
        verify_repository(tmp_path)


@pytest.mark.parametrize(
    ("relative", "payload"),
    (
        (
            "src-tauri/src/hidden_transport.rs",
            "use reqwest as transport; fn call() { let _ = transport::Client::new(); }\n",
        ),
        (
            "src-tauri/src/hidden_client.rs",
            "use reqwest::Client as HttpClient; fn call() { let _ = HttpClient::new(); }\n",
        ),
        (
            "web/src/hidden-beacon.ts",
            "navigator.sendBeacon('/telemetry', payload);\n",
        ),
    ),
)
def test_direct_network_aliases_require_an_exact_allowlisted_path(
    tmp_path: Path, relative: str, payload: str
) -> None:
    _minimal_repository(tmp_path)
    hidden = tmp_path / relative
    hidden.write_text(payload, encoding="utf-8")
    with pytest.raises(ZeroTelemetryError, match="network-path-not-allowlisted"):
        verify_repository(tmp_path)


def test_snapshot_and_failure_collection_make_no_network_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[object] = []

    def reject_connect(_socket: socket.socket, address: object) -> None:
        attempts.append(address)
        raise AssertionError("diagnostic collection attempted network access")

    monkeypatch.setattr(socket.socket, "connect", reject_connect)

    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision="a" * 40,
        configuration_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("local configuration failure")
        ),
        health_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("local health failure")
        ),
        platform_system="Windows",
        platform_machine="AMD64",
    )
    snapshot = service.snapshot()

    assert attempts == []
    assert snapshot.configuration == DiagnosticConfiguration(available=False)
    assert snapshot.privacy.telemetry_enabled is False
    assert snapshot.privacy.automatic_crash_upload is False
    assert snapshot.privacy.automatic_diagnostic_upload is False
