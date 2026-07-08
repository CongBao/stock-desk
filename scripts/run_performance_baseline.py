"""Run and validate the aggregate Stock Desk v1 performance release gate.

Raw action timings are produced by ``web/e2e/performance.spec.ts``.  This
orchestrator owns fixture seeding, environment capture, validation, comparison,
and the atomic current/baseline files; it intentionally exposes no CLI option
that can inject or override a timing value.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.performance.ten_year_a_share import (  # noqa: E402
    FIXTURE_PATH,
    MINIMUM_EFFECTIVE_MEMORY_BYTES,
    PerformanceGateError,
    generate_fixture_bars,
    load_fixture_metadata,
    validate_performance_result,
)


DEFAULT_OUTPUT = ROOT / "test-results" / "performance" / "current.json"
DEFAULT_BROWSER_OUTPUT = ROOT / "test-results" / "performance" / "browser-raw.json"
OFFICIAL_BASELINE = ROOT / "tests" / "performance" / "baseline.json"


class BaselineRecordingError(RuntimeError):
    """Baseline evidence did not satisfy non-performance trust preconditions."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure the fixed ten-year fixture and enforce absolute budgets."
    )
    parser.add_argument(
        "--fixture",
        choices=("ten-year-a-share",),
        default="ten-year-a-share",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--evidence-kind",
        choices=("reference", "target_baseline"),
        default="reference",
    )
    parser.add_argument(
        "--record-baseline",
        action="store_true",
        help="replace tests/performance/baseline.json after all trust gates pass",
    )
    return parser.parse_args(argv)


def atomic_write_json(path: Path, value: object) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def require_recording_preconditions(
    *,
    dirty: bool,
    digest_matches: bool,
    hardware_qualifies: bool,
    gate_passed: bool,
) -> None:
    if dirty:
        raise BaselineRecordingError("baseline recording refuses a dirty worktree")
    if not digest_matches:
        raise BaselineRecordingError(
            "baseline recording refuses a stale fixture digest"
        )
    if not hardware_qualifies:
        raise BaselineRecordingError("baseline recording requires qualifying hardware")
    if not gate_passed:
        raise BaselineRecordingError("baseline recording refuses a failing gate")


def _command_output(command: Sequence[str], default: str = "unavailable") -> str:
    executable = shutil.which(command[0])
    if executable is None:
        return default
    completed = subprocess.run(
        [executable, *command[1:]],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        return default
    return completed.stdout.strip() or default


def _physical_memory_bytes() -> int:
    if sys.platform == "darwin":
        value = _command_output(("sysctl", "-n", "hw.memsize"), "0")
        return int(value) if value.isdigit() else 0
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
    return int(pages) * int(page_size)


def _cpu_model() -> str:
    if sys.platform == "darwin":
        model = _command_output(("sysctl", "-n", "machdep.cpu.brand_string"), "")
        if model:
            return model
    return platform.processor() or platform.machine()


def _cgroup_memory_limit_bytes(physical: int) -> int:
    candidates = (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    )
    for candidate in candidates:
        try:
            raw = candidate.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if raw != "max" and raw.isdigit():
            limit = int(raw)
            if 0 < limit < (1 << 60):
                return min(physical, limit) if physical else limit
    return physical


def _effective_cpu_count() -> float:
    logical = os.cpu_count() or 0
    affinity = float(logical)
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is not None:
        affinity = float(len(get_affinity(0)))
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota, period = cpu_max.read_text(encoding="ascii").split()
    except (OSError, ValueError):
        return affinity
    if quota == "max":
        return affinity
    return min(affinity, int(quota) / int(period))


def collect_environment(*, browser_version: str) -> dict[str, object]:
    physical_memory = _physical_memory_bytes()
    browser = _command_output(
        (
            "pnpm",
            "exec",
            "playwright",
            "--version",
        )
    )
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_model": _cpu_model(),
        "logical_cpu_count": os.cpu_count() or 0,
        "effective_cpu_count": _effective_cpu_count(),
        "memory_bytes": physical_memory,
        "effective_memory_bytes": _cgroup_memory_limit_bytes(physical_memory),
        "python_version": platform.python_version(),
        "node_version": _command_output(("node", "--version")),
        "browser_version": browser_version,
        "runner": {
            "provider": (
                "github_actions"
                if os.environ.get("GITHUB_ACTIONS") == "true"
                else "local"
            ),
            "os": os.environ.get("RUNNER_OS", platform.system()),
            "arch": os.environ.get("RUNNER_ARCH", platform.machine()),
            "name": os.environ.get("RUNNER_NAME", platform.node() or "local"),
            "image_os": os.environ.get("ImageOS"),
            "image_version": os.environ.get("ImageVersion"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        },
        "tool_versions": {
            "duckdb": _command_output(
                (
                    "uv",
                    "run",
                    "--frozen",
                    "python",
                    "-c",
                    "import duckdb; print(duckdb.__version__)",
                )
            ),
            "playwright": browser,
            "pnpm": _command_output(("pnpm", "--version")),
        },
    }


def _git_is_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


def _verified_git_sha() -> str:
    candidate = _command_output(("git", "rev-parse", "HEAD"), "")
    if re.fullmatch(r"[0-9a-f]{40}", candidate) is None:
        raise BaselineRecordingError("git SHA is unavailable or malformed")
    object_type = _command_output(("git", "cat-file", "-t", candidate), "")
    current = _command_output(("git", "rev-parse", "HEAD"), "")
    if object_type != "commit":
        raise BaselineRecordingError("git SHA is not a commit object")
    if current != candidate:
        raise BaselineRecordingError("git SHA is not the current checkout")
    return candidate


def _run_browser_measurement(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["STOCK_DESK_PERFORMANCE_RAW_OUTPUT"] = str(output.resolve())
    environment["STOCK_DESK_PERFORMANCE_FIXTURE"] = str(FIXTURE_PATH.resolve())
    environment["STOCK_DESK_PERFORMANCE_MODE"] = "1"
    environment["STOCK_DESK_PERFORMANCE_PROCESS_FILE"] = str(
        (output.parent / "processes.json").resolve()
    )
    subprocess.run(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "web/e2e/performance.spec.ts",
            "--project=chromium",
            "--retries=0",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise PerformanceGateError(f"performance evidence is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _qualifying_environment(environment: dict[str, Any], evidence_kind: str) -> bool:
    effective_cpu = environment.get("effective_cpu_count")
    physical_memory = environment.get("memory_bytes")
    effective_memory = environment.get("effective_memory_bytes")
    if (
        not isinstance(effective_cpu, (int, float))
        or isinstance(effective_cpu, bool)
        or not math.isfinite(float(effective_cpu))
        or effective_cpu < 4
        or not isinstance(physical_memory, int)
        or isinstance(physical_memory, bool)
        or not isinstance(effective_memory, int)
        or isinstance(effective_memory, bool)
        or min(physical_memory, effective_memory) < MINIMUM_EFFECTIVE_MEMORY_BYTES
    ):
        return False
    if evidence_kind == "reference":
        return True
    runner = environment.get("runner")
    return (
        evidence_kind == "target_baseline"
        and effective_cpu == 4
        and physical_memory <= 17 * 1024**3
        and isinstance(runner, dict)
        and runner.get("provider") == "github_actions"
        and runner.get("os") == "Linux"
        and runner.get("arch") == "X64"
    )


def _ensure_supported_platform() -> None:
    if sys.platform == "win32":
        _fail("performance command is unsupported on Windows")
    if sys.platform not in {"darwin", "linux"}:
        _fail(f"performance command is unsupported on {sys.platform}")


def _require_real_tool_versions(environment: dict[str, object]) -> None:
    versions = environment.get("tool_versions")
    if not isinstance(versions, dict) or set(versions) != {
        "duckdb",
        "playwright",
        "pnpm",
    }:
        _fail("performance tool versions are incomplete before browser start")
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower() == "unavailable"
        for value in versions.values()
    ):
        _fail("performance tool version is unavailable before browser start")


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _ensure_supported_platform()
    preflight_environment = collect_environment(browser_version="preflight")
    _require_real_tool_versions(preflight_environment)
    if not _qualifying_environment(preflight_environment, args.evidence_kind):
        _fail(
            f"{args.evidence_kind} hardware/runner preflight failed before browser start"
        )
    preflight_sha = _verified_git_sha()
    fixture = load_fixture_metadata()
    generated = generate_fixture_bars(fixture)
    if generated.content_digest != fixture.content_digest:
        _fail("fixture metadata digest is stale; regenerate it before measuring")
    if len(generated.bars) != fixture.row_count:
        _fail("fixture metadata row count is stale")

    browser_output = DEFAULT_BROWSER_OUTPUT
    _run_browser_measurement(browser_output)
    raw = _load_json(browser_output)
    if not isinstance(raw, dict):
        _fail("browser performance evidence must be a JSON object")
    result: dict[str, Any] = dict(raw)
    browser_version = result.pop("browser_version", None)
    if not isinstance(browser_version, str) or not browser_version.startswith(
        "Chromium "
    ):
        _fail("browser evidence did not report the actual Chromium version")
    result["measured_at_utc"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    measured_sha = _verified_git_sha()
    if measured_sha != preflight_sha:
        _fail("git checkout changed during performance measurement")
    result["git"] = {"sha": measured_sha, "dirty": _git_is_dirty()}
    result["environment"] = {
        **preflight_environment,
        "browser_version": browser_version,
    }
    result["evidence_kind"] = args.evidence_kind
    result["fixture"] = {
        "fixture_id": fixture.fixture_id,
        "content_digest": fixture.content_digest,
        "row_count": fixture.row_count,
        "scoring_sessions": fixture.scoring_sessions,
        "scope_instrument_count": fixture.scope_instrument_count,
        "runnable_symbol_count": fixture.runnable_symbol_count,
        "network_policy": fixture.network_policy,
    }

    baseline = _load_json(args.compare) if args.compare is not None else None
    gate_passed = False
    try:
        validate_performance_result(
            result,
            expected_fixture_digest=fixture.content_digest,
            baseline=baseline,
        )
        gate_passed = True
    finally:
        atomic_write_json(Path(args.output), result)

    if args.record_baseline:
        require_recording_preconditions(
            dirty=_git_is_dirty(),
            digest_matches=result["fixture"]["content_digest"]
            == fixture.content_digest,
            hardware_qualifies=_qualifying_environment(
                result["environment"], args.evidence_kind
            ),
            gate_passed=gate_passed,
        )
        atomic_write_json(OFFICIAL_BASELINE, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
