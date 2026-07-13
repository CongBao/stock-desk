from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Final


MANIFESTS: Final = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "src-tauri/Cargo.toml",
    "src-tauri/Cargo.lock",
)
PRODUCTION_ROOTS: Final = ("src/stock_desk", "src-tauri/src", "web/src")
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


class ZeroTelemetryError(ValueError):
    pass


def audit_repository(root: Path) -> tuple[str, ...]:
    """Return stable violations; missing inputs and unsafe links fail closed."""

    resolved_root = root.resolve()
    violations: list[str] = []
    for relative in MANIFESTS:
        path = resolved_root / relative
        if not path.is_file() or path.is_symlink():
            violations.append(f"missing-or-unsafe-manifest:{relative}")
            continue
        _scan(path, relative, violations)
    for relative_root in PRODUCTION_ROOTS:
        source_root = resolved_root / relative_root
        if not source_root.is_dir() or source_root.is_symlink():
            violations.append(f"missing-or-unsafe-source-root:{relative_root}")
            continue
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink():
                violations.append(
                    f"unsafe-source-link:{path.relative_to(resolved_root).as_posix()}"
                )
                continue
            if (
                path.is_file()
                and path.suffix in _SOURCE_SUFFIXES
                and ".test." not in path.name
                and ".spec." not in path.name
            ):
                _scan(path, path.relative_to(resolved_root).as_posix(), violations)
    return tuple(sorted(set(violations)))


def verify_repository(root: Path) -> None:
    violations = audit_repository(root)
    if violations:
        raise ZeroTelemetryError(
            "zero-telemetry policy failed: " + ", ".join(violations)
        )


def _scan(path: Path, label: str, violations: list[str]) -> None:
    try:
        payload = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        violations.append(f"unreadable:{label}")
        return
    is_lockfile = label.endswith((".lock", "lock.yaml"))
    sdk_pattern = _LOCKFILE_PACKAGE if is_lockfile else _FORBIDDEN_SDK
    if sdk_pattern.search(payload) is not None:
        violations.append(f"telemetry-sdk:{label}")
    if _FORBIDDEN_ENDPOINT.search(payload) is not None:
        violations.append(f"telemetry-endpoint:{label}")


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
