from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_TARGETS = frozenset({"aarch64-apple-darwin", "x86_64-apple-darwin"})


class MacOSSidecarError(RuntimeError):
    """The native macOS sidecar could not be built safely."""


def host_target_triple() -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["rustc", "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise MacOSSidecarError("unable to determine macOS host target") from error
    target_triple = result.stdout.strip()
    sidecar_filename(target_triple)
    return target_triple


def sidecar_filename(target_triple: str) -> str:
    if target_triple not in SUPPORTED_TARGETS:
        raise MacOSSidecarError("unsupported macOS target")
    return f"stock-desk-sidecar-{target_triple}"


def build_native_sidecar(
    root: Path,
    output_dir: Path,
    target_triple: str,
    *,
    work_dir: Path,
    timeout_seconds: int,
) -> Path:
    root = root.resolve()
    output_dir = output_dir.resolve()
    work_dir = work_dir.resolve()
    if timeout_seconds < 1:
        raise MacOSSidecarError("native sidecar build timeout is invalid")
    if work_dir == root or work_dir.is_relative_to(root):
        raise MacOSSidecarError(
            "native sidecar work path must be outside the repository"
        )
    if work_dir.exists():
        raise MacOSSidecarError("native sidecar work path must be unique")
    if output_dir == work_dir or output_dir.is_relative_to(work_dir):
        raise MacOSSidecarError("native sidecar output cannot be inside its work path")
    name = sidecar_filename(target_triple)
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["STOCK_DESK_PYINSTALLER_SIDECAR_NAME"] = name
    try:
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                os.fspath(output_dir),
                "--workpath",
                os.fspath(work_dir),
                os.fspath(root / "packaging" / "stock-desk-sidecar.spec"),
            ],
            cwd=root,
            env=environment,
            check=True,
            timeout=timeout_seconds,
        )
        expected = output_dir / name
        executables = sorted(
            path
            for path in output_dir.iterdir()
            if path.is_file() and os.access(path, os.X_OK)
        )
        if executables != [expected]:
            raise MacOSSidecarError(
                "PyInstaller must produce exactly one native sidecar executable"
            )
        return expected
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the native macOS sidecar")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="stock-desk-macos-sidecar-") as temporary:
        artifact = build_native_sidecar(
            ROOT,
            arguments.output.resolve(),
            host_target_triple(),
            work_dir=Path(temporary) / "pyinstaller-work",
            timeout_seconds=900,
        )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
