from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Final, Protocol


ROOT: Final = Path(__file__).resolve().parent.parent
WINDOWS_TARGET: Final = "x86_64-pc-windows-msvc"
SIDECAR_EXE: Final = f"stock-desk-sidecar-{WINDOWS_TARGET}.exe"
SHA_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
PE_X64_MACHINE: Final = 0x8664
FORBIDDEN_ROOT_COMPONENTS: Final = frozenset(
    {
        "browser",
        "docs",
        "node_modules",
        "openspec",
        "scripts",
        "src",
        "tests",
        "web",
    }
)
FORBIDDEN_ANY_COMPONENTS: Final = frozenset({".git"})
FORBIDDEN_DEV_FILES: Final = frozenset(
    {
        ".gitignore",
        "cargo.lock",
        "cargo.toml",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "rust-toolchain.toml",
    }
)
FORBIDDEN_DEV_SUFFIXES: Final = (".map", ".spec.ts", ".test.ts", ".tsx")
FORBIDDEN_RUNTIME_MODULES: Final = frozenset({"stock_desk.desktop", "stock_desk.web"})


@dataclass(frozen=True)
class SourceIdentity:
    revision: str
    epoch: int


class CaptureResult(Protocol):
    stdout: str


class Capture(Protocol):
    def __call__(self, arguments: list[str]) -> CaptureResult: ...


class Runner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> None: ...


InventoryReader = Callable[[Path], Iterable[str]]


def _capture(arguments: list[str]) -> CaptureResult:
    return subprocess.run(  # noqa: S603
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def git_source_identity(root: Path, *, capture: Capture = _capture) -> SourceIdentity:
    root = root.resolve()
    common = ["git", "-C", os.fspath(root)]
    try:
        revision = capture(common + ["rev-parse", "--verify", "HEAD"]).stdout.strip()
        status = capture(
            common + ["status", "--porcelain=v1", "--untracked-files=all"]
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("unable to identify clean desktop build source") from error
    if SHA_PATTERN.fullmatch(revision) is None:
        raise RuntimeError("desktop build source revision is invalid")
    if status:
        raise RuntimeError("desktop build source tree is not clean")
    try:
        epoch_text = capture(common + ["show", "-s", "--format=%ct", "HEAD"])
        epoch = int(epoch_text.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise RuntimeError("desktop build source epoch is invalid") from error
    if epoch < 0:
        raise RuntimeError("desktop build source epoch is invalid")
    return SourceIdentity(revision, epoch)


def _require_windows_x64(system: str, machine: str) -> None:
    if system != "Windows" or machine.casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError(
            f"desktop bundle requires a native Windows x64 host, got {system}/{machine}"
        )


def _resolve_pnpm() -> str:
    for candidate in ("pnpm.cmd", "pnpm"):
        if executable := shutil.which(candidate):
            return executable
    raise RuntimeError("pnpm executable is unavailable")


def _default_runner(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(arguments, cwd=cwd, env=env, check=True)  # noqa: S603


def _default_inventory_reader(artifact: Path) -> Iterable[str]:
    from PyInstaller.archive.readers import CArchiveReader  # type: ignore[import-untyped]

    archive = CArchiveReader(os.fspath(artifact))
    entries = [str(entry) for entry in archive.toc]
    for entry in tuple(entries):
        if not entry.casefold().endswith(".pyz"):
            continue
        embedded = archive.open_embedded_archive(entry)
        entries.extend(str(module) for module in embedded.toc)
    return entries


def _is_pe_x64(artifact: Path) -> bool:
    try:
        with artifact.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            stream.seek(pe_offset)
            pe_header = stream.read(6)
    except (OSError, struct.error):
        return False
    return (
        len(pe_header) == 6
        and pe_header[:4] == b"PE\0\0"
        and struct.unpack_from("<H", pe_header, 4)[0] == PE_X64_MACHINE
    )


def _assert_safe_inventory(entries: Iterable[str]) -> None:
    forbidden: list[str] = []
    for raw_entry in entries:
        normalized = raw_entry.replace("\\", "/").strip("/")
        path = PurePosixPath(normalized)
        components = {component.casefold() for component in path.parts}
        root_component = path.parts[0].casefold() if path.parts else ""
        name = path.name.casefold()
        normalized_casefold = normalized.casefold()
        if (
            root_component in FORBIDDEN_ROOT_COMPONENTS
            or bool(components & FORBIDDEN_ANY_COMPONENTS)
            or any(
                normalized.casefold() == module
                or normalized.casefold().startswith(f"{module}.")
                for module in FORBIDDEN_RUNTIME_MODULES
            )
            or (
                root_component == "stock_desk"
                and name.endswith(".py")
                and not normalized_casefold.startswith("stock_desk/migrations/")
            )
            or (len(path.parts) == 1 and name in FORBIDDEN_DEV_FILES)
            or name.endswith(FORBIDDEN_DEV_SUFFIXES)
        ):
            forbidden.append(raw_entry)
    if forbidden:
        preview = ", ".join(sorted(forbidden)[:5])
        raise RuntimeError(f"sidecar contains forbidden development content: {preview}")


def validate_sidecar_artifact(
    binaries: Path,
    *,
    inventory_reader: InventoryReader = _default_inventory_reader,
) -> Path:
    executables = sorted(binaries.glob("*.exe"))
    expected = binaries / SIDECAR_EXE
    if executables != [expected]:
        raise RuntimeError(
            "PyInstaller must produce exactly one sidecar executable with the "
            f"name {SIDECAR_EXE}"
        )
    if not _is_pe_x64(expected):
        raise RuntimeError("sidecar executable is not a valid PE x64 binary")
    _assert_safe_inventory(inventory_reader(expected))
    return expected


def _invoke(
    arguments: list[str],
    *,
    root: Path,
    environment: dict[str, str],
    runner: Runner | None,
    dry_run: bool,
) -> None:
    if runner is not None:
        runner(arguments, cwd=root, env=environment)
    elif dry_run:
        print(shlex.join(arguments))
    else:
        _default_runner(arguments, cwd=root, env=environment)


def _build_with_work_dir(
    *,
    root: Path,
    identity: SourceIdentity,
    executable: str,
    pnpm: str,
    work_dir: Path,
    runner: Runner | None,
    inventory_reader: InventoryReader,
    dry_run: bool,
) -> Path | None:
    binaries = root / "src-tauri" / "binaries"
    pyinstaller_work = work_dir / "pyinstaller"
    environment = os.environ.copy()
    environment.update(
        {
            "CARGO_ENCODED_RUSTFLAGS": "-C\x1flink-arg=/Brepro",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(identity.epoch),
            "STOCK_DESK_SOURCE_REVISION": identity.revision,
        }
    )
    if not dry_run:
        binaries.mkdir(parents=True, exist_ok=True)
        for old_executable in binaries.glob("*.exe"):
            old_executable.unlink()
        pyinstaller_work.mkdir(parents=True, exist_ok=True)
    _invoke(
        [
            executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            os.fspath(binaries),
            "--workpath",
            os.fspath(pyinstaller_work),
            os.fspath(root / "packaging" / "stock-desk-sidecar.spec"),
        ],
        root=root,
        environment=environment,
        runner=runner,
        dry_run=dry_run,
    )
    artifact = None
    if not dry_run:
        artifact = validate_sidecar_artifact(
            binaries, inventory_reader=inventory_reader
        )
    _invoke(
        [
            pnpm,
            "exec",
            "tauri",
            "build",
            "--config",
            "src-tauri/tauri.conf.json",
            "--bundles",
            "nsis",
            "--target",
            WINDOWS_TARGET,
        ],
        root=root,
        environment=environment,
        runner=runner,
        dry_run=dry_run,
    )
    return artifact


def build_windows_desktop(
    *,
    root: Path = ROOT,
    source_identity: SourceIdentity | None = None,
    system: str | None = None,
    machine: str | None = None,
    executable: str | None = None,
    pnpm: str | None = None,
    work_dir: Path | None = None,
    runner: Runner | None = None,
    inventory_reader: InventoryReader = _default_inventory_reader,
    dry_run: bool = False,
) -> Path | None:
    root = root.resolve()
    _require_windows_x64(system or platform.system(), machine or platform.machine())
    identity = source_identity or git_source_identity(root)
    python = executable or sys.executable
    pnpm_command = pnpm or _resolve_pnpm()
    if work_dir is not None:
        return _build_with_work_dir(
            root=root,
            identity=identity,
            executable=python,
            pnpm=pnpm_command,
            work_dir=work_dir.resolve(),
            runner=runner,
            inventory_reader=inventory_reader,
            dry_run=dry_run,
        )
    with tempfile.TemporaryDirectory(prefix="stock-desk-desktop-") as temporary:
        return _build_with_work_dir(
            root=root,
            identity=identity,
            executable=python,
            pnpm=pnpm_command,
            work_dir=Path(temporary),
            runner=runner,
            inventory_reader=inventory_reader,
            dry_run=dry_run,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the Windows x64 Stock Desk Tauri desktop bundle"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the host/source and print commands without building",
    )
    arguments = parser.parse_args(argv)
    artifact = build_windows_desktop(dry_run=arguments.dry_run)
    if artifact is not None:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
