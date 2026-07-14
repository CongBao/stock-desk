from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Final


MANIFESTS: Final = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "src-tauri/Cargo.toml",
    "src-tauri/Cargo.lock",
)
REQUIRED_TAURI_JSON: Final = (
    "src-tauri/tauri.conf.json",
    "src-tauri/tauri.windows.conf.json",
    "src-tauri/capabilities/default.json",
)
PRIVACY_POLICY_PATH: Final = "config/desktop-network-privacy.json"
PRODUCTION_ROOTS: Final = ("src/stock_desk", "src-tauri/src", "web/src")
_NETWORK_EXACT_PATHS: Final = {
    "python": (
        "src/stock_desk/analysis/model_config.py",
        "src/stock_desk/analysis/model_settings.py",
        "src/stock_desk/analysis/providers/base.py",
        "src/stock_desk/analysis/providers/deepseek.py",
        "src/stock_desk/analysis/providers/ollama.py",
        "src/stock_desk/analysis/providers/openai_compatible.py",
        "src/stock_desk/analysis/sources/_akshare_worker.py",
        "src/stock_desk/analysis/sources/tushare.py",
        "src/stock_desk/desktop.py",
        "src/stock_desk/market/compositions.py",
        "src/stock_desk/market/providers/akshare.py",
        "src/stock_desk/market/providers/baostock.py",
        "src/stock_desk/market/providers/tushare.py",
    ),
    "rust": (
        "src-tauri/src/app.rs",
        "src-tauri/src/exit.rs",
        "src-tauri/src/proxy.rs",
        "src-tauri/src/updater.rs",
    ),
    "web": ("web/src/shared/api/client.ts",),
}
EXPECTED_PRIVACY_POLICY: Final = {
    "schema_version": 3,
    "active_phase": "trusted-updater-foundation",
    "phases": {
        "trusted-updater-foundation": {
            "telemetry": "disabled",
            "automatic_crash_upload": "disabled",
            "automatic_diagnostic_upload": "disabled",
            "diagnostics": {"creation": "explicit-user-action", "upload": "never"},
            "production_network_exact_paths": {
                key: list(paths) for key, paths in _NETWORK_EXACT_PATHS.items()
            },
            "updater": {
                "runtime_enabled": False,
                "implementation": "rust-host-only",
                "endpoint": "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json",
                "target": "windows-x86_64-nsis",
                "arch": "x86_64",
                "channel": "stable-only",
                "request": {
                    "identity": "anonymous",
                    "allowed_fields": ["target", "arch", "current_version"],
                    "stable_device_identifier": False,
                    "usage_behavior": False,
                    "local_data_digest": False,
                },
                "installation": {
                    "automatic_download": False,
                    "explicit_user_confirmation": True,
                    "forced_silent_update": False,
                },
            },
        }
    },
}
_FORBIDDEN_SDK = re.compile(
    r"(?i)(?:@sentry/|\bsentry[_-]sdk\b|\bposthog\b|\bopentelemetry\b|"
    r"\bdatadog\b|\bbugsnag\b|\brollbar\b|\bcrashlytics\b|"
    r"\bsegment[_-]analytics\b|\bamplitude\b)"
)
_FORBIDDEN_ENDPOINT = re.compile(
    r"(?i)https?://(?:[^/]*\.)?(?:sentry\.io|posthog\.com|"
    r"datadoghq\.com|bugsnag\.com|rollbar\.com|amplitude\.com)(?:/|\b)"
)
_SOURCE_SUFFIXES: Final = frozenset({".py", ".rs", ".ts", ".tsx", ".js", ".jsx"})
_LOCKFILE_PACKAGE = re.compile(
    r"(?im)^(?:name\s*=\s*[\"']|\s{2,}[\"']?)"
    r"(?:@sentry/|sentry[_-]sdk|posthog|opentelemetry|datadog|bugsnag|"
    r"rollbar|crashlytics|segment[_-]analytics|amplitude)"
)
_FORBIDDEN_UPDATER = re.compile(
    r"(?i)(?:@tauri-apps/plugin-updater|"
    r"\bupdater:(?:default|allow-|deny-)|\bcreateUpdaterArtifacts\b|"
    r"[\"']updater[\"']\s*:)"
)
_FORBIDDEN_STABLE_IDENTIFIER = re.compile(
    r"(?i)\b(?:installation[_-]?id|machine[_-]?guid|machine[_-]?uid|"
    r"hardware[_-]?uuid|persistent[_-]?device[_-]?id|analytics[_-]?id)\b"
)
_FORBIDDEN_AUTOMATIC_UPLOAD = re.compile(
    r"(?i)\b(?:upload[_-]?(?:diagnostic|crash)(?:[_-]?(?:bundle|report))?|"
    r"auto(?:matic)?[_-]?(?:diagnostic|crash)[_-]?upload)\s*\("
)
_FORBIDDEN_DIAGNOSTIC_NETWORK = re.compile(
    r"(?i)(?:\brequests\.(?:get|post|put|patch|delete)\s*\(|\bhttpx\d*\.|"
    r"\baiohttp\.|\burllib\.request|\bfetch\s*\(|\breqwest::|"
    r"\bTcpStream\b|\bsocket\.(?:socket|create_connection)\s*\()"
)
_PYTHON_NETWORK_MODULES: Final = frozenset(
    {
        "aiohttp",
        "akshare",
        "baostock",
        "httpx",
        "httpx2",
        "requests",
        "socket",
        "tushare",
        "urllib.request",
        "urllib3",
    }
)
_RUST_NETWORK = re.compile(
    r"\b(?:reqwest::|TcpStream\b|UdpSocket\b|ureq::|hyper::|tauri_plugin_updater::)"
)
_RUST_NETWORK_ALIAS = re.compile(
    r"\buse\s+(?P<module>reqwest|ureq|hyper)\s+as\s+"
    r"(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*;"
)
_RUST_NETWORK_ITEM_ALIAS = re.compile(
    r"\buse\s+(?:reqwest|ureq|hyper)::[A-Za-z_][A-Za-z0-9_]*\s+as\s+"
    r"(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*;"
)
_WEB_NETWORK = re.compile(
    r"\b(?:fetch\s*\(|XMLHttpRequest\b|WebSocket\s*\(|sendBeacon\s*\()"
)


class ZeroTelemetryError(ValueError):
    pass


def audit_repository(root: Path) -> tuple[str, ...]:
    """Return stable violations; missing inputs and unsafe links fail closed."""

    resolved_root = root.resolve()
    violations: list[str] = []
    _validate_privacy_policy(resolved_root, violations)
    _validate_updater_foundation(resolved_root, violations)
    for relative in MANIFESTS:
        path = resolved_root / relative
        if not path.is_file() or path.is_symlink():
            violations.append(f"missing-or-unsafe-manifest:{relative}")
            continue
        _scan(path, relative, violations)
    _audit_tauri_json(resolved_root, violations)
    detected: dict[str, set[str]] = {key: set() for key in _NETWORK_EXACT_PATHS}
    for relative_root in PRODUCTION_ROOTS:
        source_root = resolved_root / relative_root
        if not source_root.is_dir() or source_root.is_symlink():
            violations.append(f"missing-or-unsafe-source-root:{relative_root}")
            continue
        for path in sorted(source_root.rglob("*")):
            label = path.relative_to(resolved_root).as_posix()
            if path.is_symlink():
                violations.append(f"unsafe-source-link:{label}")
                continue
            if (
                not path.is_file()
                or path.suffix not in _SOURCE_SUFFIXES
                or ".test." in path.name
                or ".spec." in path.name
            ):
                continue
            payload = _scan(path, label, violations)
            if payload is None:
                continue
            language = _network_language(label)
            if language == "python" and _python_network_modules(
                payload, label, violations
            ):
                detected[language].add(label)
            elif language == "rust" and _rust_has_network_primitive(payload):
                detected[language].add(label)
            elif language == "web" and _WEB_NETWORK.search(payload):
                detected[language].add(label)
    _compare_network_paths(detected, violations)
    return tuple(sorted(set(violations)))


def verify_repository(root: Path) -> None:
    violations = audit_repository(root)
    if violations:
        raise ZeroTelemetryError(
            "zero-telemetry policy failed: " + ", ".join(violations)
        )


def _scan(path: Path, label: str, violations: list[str]) -> str | None:
    try:
        payload = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        violations.append(f"unreadable:{label}")
        return None
    is_lockfile = label.endswith((".lock", "lock.yaml"))
    sdk_pattern = _LOCKFILE_PACKAGE if is_lockfile else _FORBIDDEN_SDK
    if sdk_pattern.search(payload) is not None:
        violations.append(f"telemetry-sdk:{label}")
    if _FORBIDDEN_ENDPOINT.search(payload) is not None:
        violations.append(f"telemetry-endpoint:{label}")
    if _FORBIDDEN_UPDATER.search(payload) is not None and not (
        label == "src-tauri/tauri.conf.json"
        and _has_exact_inert_updater_config(payload)
    ):
        violations.append(f"updater-enabled:{label}")
    if _FORBIDDEN_STABLE_IDENTIFIER.search(payload) is not None:
        violations.append(f"stable-device-identifier:{label}")
    if _FORBIDDEN_AUTOMATIC_UPLOAD.search(payload) is not None:
        violations.append(f"automatic-diagnostic-upload:{label}")
    if _is_diagnostic_source(label) and _FORBIDDEN_DIAGNOSTIC_NETWORK.search(payload):
        violations.append(f"diagnostic-network-path:{label}")
    return payload


def _has_exact_inert_updater_config(payload: str) -> bool:
    try:
        config = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    plugins = config.get("plugins")
    return isinstance(plugins, dict) and plugins.get("updater") == {
        "endpoints": [],
        "pubkey": "",
    }


def _audit_tauri_json(root: Path, violations: list[str]) -> None:
    required = {root / relative for relative in REQUIRED_TAURI_JSON}
    for path in sorted(required):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or _has_symlink_component(path, root):
            violations.append(f"missing-or-unsafe-config:{relative}")
    discovered = set((root / "src-tauri").glob("tauri*.conf.json"))
    capabilities = root / "src-tauri/capabilities"
    if capabilities.is_dir() and not _has_symlink_component(capabilities, root):
        discovered.update(capabilities.rglob("*.json"))
        for path in capabilities.rglob("*"):
            if _has_symlink_component(path, root):
                violations.append(
                    f"unsafe-config-link:{path.relative_to(root).as_posix()}"
                )
    else:
        violations.append("missing-or-unsafe-config-root:src-tauri/capabilities")
    for path in sorted(required | discovered):
        label = path.relative_to(root).as_posix()
        if not path.is_file() or _has_symlink_component(path, root):
            continue
        payload = _scan(path, label, violations)
        if payload is None:
            continue
        try:
            json.loads(payload, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, ValueError):
            violations.append(f"invalid-config-json:{label}")


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Return whether a path or any existing ancestor below root is a symlink."""

    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _network_language(label: str) -> str:
    if label.endswith(".py"):
        return "python"
    if label.endswith(".rs"):
        return "rust"
    return "web"


def _python_network_modules(
    payload: str, label: str, violations: list[str]
) -> frozenset[str]:
    try:
        tree = ast.parse(payload, filename=label)
    except SyntaxError:
        violations.append(f"unparseable-python-source:{label}")
        return frozenset()
    aliases = _python_import_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_network_module(alias.name):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            candidates = (node.module,) + tuple(
                f"{node.module}.{alias.name}" for alias in node.names
            )
            found.update(name for name in candidates if _is_network_module(name))
        elif isinstance(node, ast.Call) and node.args:
            name = _resolve_python_alias(_call_name(node.func), aliases)
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                if name in {"__import__", "importlib.import_module"}:
                    if _is_network_module(target.value):
                        found.add(target.value)
                elif name == "import_optional_sdk" or name.endswith(
                    ".import_optional_sdk"
                ):
                    if target.value in {"akshare", "baostock", "tushare"}:
                        found.add(target.value)
    return frozenset(found)


def _python_import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    assignments: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                bound = imported.asname or imported.name.split(".", maxsplit=1)[0]
                aliases[bound] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for imported in node.names:
                if imported.name == "*":
                    continue
                bound = imported.asname or imported.name
                aliases[bound] = f"{node.module}.{imported.name}"
        elif isinstance(node, ast.Assign):
            source = _call_name(node.value)
            assignments.extend(
                (target.id, source)
                for target in node.targets
                if isinstance(target, ast.Name) and source
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            source = _call_name(node.value) if node.value is not None else ""
            if source:
                assignments.append((node.target.id, source))

    for _ in range(len(assignments) + 1):
        changed = False
        for target, source in assignments:
            resolved = _resolve_python_alias(source, aliases)
            if resolved in {"__import__", "importlib.import_module"}:
                if aliases.get(target) != resolved:
                    aliases[target] = resolved
                    changed = True
        if not changed:
            break
    return aliases


def _resolve_python_alias(name: str, aliases: dict[str, str]) -> str:
    if not name:
        return ""
    head, separator, tail = name.partition(".")
    resolved_head = aliases.get(head, head)
    return f"{resolved_head}.{tail}" if separator else resolved_head


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _rust_has_network_primitive(payload: str) -> bool:
    if _RUST_NETWORK.search(payload):
        return True
    for pattern in (_RUST_NETWORK_ALIAS, _RUST_NETWORK_ITEM_ALIAS):
        for match in pattern.finditer(payload):
            alias = re.escape(match.group("alias"))
            if re.search(rf"\b{alias}\s*::", payload):
                return True
    return False


def _is_network_module(name: str) -> bool:
    return any(
        name == module or name.startswith(f"{module}.")
        for module in _PYTHON_NETWORK_MODULES
    )


def _compare_network_paths(
    detected: dict[str, set[str]], violations: list[str]
) -> None:
    for language, expected_paths in _NETWORK_EXACT_PATHS.items():
        expected = set(expected_paths)
        for label in sorted(detected[language] - expected):
            violations.append(f"network-path-not-allowlisted:{label}")
        for label in sorted(expected - detected[language]):
            violations.append(f"network-allowlist-path-unused:{label}")


def _is_diagnostic_source(label: str) -> bool:
    return (
        label == "src-tauri/src/diagnostics.rs"
        or label == "src/stock_desk/api/diagnostics.py"
        or label.startswith("src/stock_desk/diagnostics/")
    )


def _rust_without_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if block_depth:
            if current == "/" and following == "*":
                block_depth += 1
                output.extend("  ")
                index += 2
                continue
            if current == "*" and following == "/":
                block_depth -= 1
                output.extend("  ")
                index += 2
                continue
            output.append("\n" if current == "\n" else " ")
            index += 1
            continue
        if not in_string and current == "/" and following == "/":
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if not in_string and current == "/" and following == "*":
            block_depth = 1
            output.extend("  ")
            index += 2
            continue
        output.append(current)
        if in_string:
            if current == '"' and not escaped:
                in_string = False
            escaped = current == "\\" and not escaped
            if current != "\\":
                escaped = False
        elif current == '"':
            in_string = True
            escaped = False
        index += 1
    return "".join(output)


def _validate_updater_foundation(root: Path, violations: list[str]) -> None:
    cargo_path = root / "src-tauri/Cargo.toml"
    updater_path = root / "src-tauri/src/updater.rs"
    main_path = root / "src-tauri/src/main.rs"
    web_package_path = root / "web/package.json"
    try:
        cargo = tomllib.loads(cargo_path.read_text(encoding="utf-8", errors="strict"))
        updater_source = updater_path.read_text(encoding="utf-8", errors="strict")
        main_source = main_path.read_text(encoding="utf-8", errors="strict")
        web_package = json.loads(
            web_package_path.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
        )
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        violations.append("invalid-trusted-updater-foundation")
        return

    dependency = cargo.get("dependencies", {}).get("tauri-plugin-updater")
    if dependency != {
        "version": "=2.10.1",
        "default-features": False,
        "features": ["native-tls", "zip"],
    }:
        violations.append("invalid-trusted-updater-dependency")
    if cargo.get("dependencies", {}).get("minisign-verify") != {"version": "=0.2.5"}:
        violations.append("invalid-trusted-updater-signature-dependency")
    rust_code = _rust_without_comments(updater_source)
    required_source_contracts = (
        r"pub\s+const\s+UPDATE_RUNTIME_ENABLED\s*:\s*bool\s*=\s*false\s*;",
        r'pub\s+const\s+UPDATE_TARGET\s*:\s*&str\s*=\s*"windows-x86_64-nsis"\s*;',
        r'pub\s+const\s+UPDATE_ARCH\s*:\s*&str\s*=\s*"x86_64"\s*;',
        r'pub\s+const\s+UPDATE_ENDPOINT\s*:\s*&str\s*=\s*"https://github\.com/CongBao/stock-desk/releases/latest/download/latest\.json"\s*;',
        r"const\s+_\s*:\s*\(\)\s*=\s*assert!\s*\(\s*!UPDATE_RUNTIME_ENABLED\s*\)\s*;",
        r'const\s+CURRENT_VERSION\s*:\s*&str\s*=\s*env!\s*\(\s*"CARGO_PKG_VERSION"\s*\)\s*;',
        r"const\s+TRUSTED_TAURI_PUBLIC_KEY\s*:\s*Option\s*<\s*&str\s*>\s*=\s*None\s*;",
        r"tauri_plugin_updater::Builder::new\(\)\.build\(\)",
        r"verify_downloaded_candidate\(",
        r"PublicKey::decode",
        r"verify_authenticode\(installer_path\)",
        "InstalledWatermark",
        "verified_pending",
        "installed-watermark.json",
    )
    if any(
        re.search(contract, rust_code, re.DOTALL) is None
        for contract in required_source_contracts
    ):
        violations.append("invalid-trusted-updater-runtime-contract")
    command_contracts = (
        r"pub\s+fn\s+desktop_check_for_updates\b[\s\S]*?if\s*!UPDATE_RUNTIME_ENABLED\s*\{\s*return\s+Ok\(machine\.state\(\)\.clone\(\)\);\s*\}",
        r"pub\s+fn\s+desktop_confirm_update\b[\s\S]*?gate_native_confirmation\s*\(\s*UPDATE_RUNTIME_ENABLED\s*,",
        r"fn\s+gate_native_confirmation\b[\s\S]*?if\s*!enabled\s*\{\s*return\s+Err\(\"desktop_updater_disabled\"\);\s*\}",
    )
    if any(re.search(contract, rust_code) is None for contract in command_contracts):
        violations.append("invalid-trusted-updater-command-guard")
    if "VerificationOutcome" in updater_source or "true, true, true" in updater_source:
        violations.append("claimable-trusted-updater-outcome")
    if (
        "PathBuf::new()" in updater_source
        or "verified-watermark.json" in updater_source
    ):
        violations.append("unsafe-trusted-updater-state-path")
    if ".plugin(updater::plugin())" not in main_source:
        violations.append("missing-trusted-updater-host-wiring")

    web_dependencies = {
        **web_package.get("dependencies", {}),
        **web_package.get("devDependencies", {}),
    }
    if "@tauri-apps/plugin-updater" in web_dependencies:
        violations.append("web-updater-bypasses-rust-host")


def _validate_privacy_policy(root: Path, violations: list[str]) -> None:
    path = root / PRIVACY_POLICY_PATH
    if not path.is_file() or path.is_symlink():
        violations.append(f"missing-or-unsafe-privacy-policy:{PRIVACY_POLICY_PATH}")
        return
    try:
        policy = json.loads(
            path.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        violations.append(f"invalid-privacy-policy:{PRIVACY_POLICY_PATH}")
        return
    if policy != EXPECTED_PRIVACY_POLICY:
        violations.append(f"invalid-privacy-policy:{PRIVACY_POLICY_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the zero-telemetry boundary.")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    options = parser.parse_args(argv)
    try:
        verify_repository(options.root)
    except ZeroTelemetryError as error:
        print(str(error))
        return 1
    print("zero-telemetry policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
