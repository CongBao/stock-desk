from __future__ import annotations

import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from scripts import build_windows_desktop as builder


def _pe_x64() -> bytes:
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, 0x8664)
    return bytes(payload)


def test_source_identity_requires_a_clean_sha_and_uses_commit_epoch(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def capture(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if "status" in arguments:
            return SimpleNamespace(stdout="")
        if "show" in arguments:
            return SimpleNamespace(stdout="1700000000\n")
        return SimpleNamespace(stdout=f"{'a' * 40}\n")

    identity = builder.git_source_identity(tmp_path, capture=capture)

    assert identity == builder.SourceIdentity("a" * 40, 1_700_000_000)
    assert any("--untracked-files=all" in call for call in calls)


def test_source_identity_rejects_dirty_or_invalid_sources(tmp_path: Path) -> None:
    def dirty(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        output = " M src/app.py\n" if "status" in arguments else f"{'a' * 40}\n"
        return SimpleNamespace(stdout=output)

    with pytest.raises(RuntimeError, match="source tree is not clean"):
        builder.git_source_identity(tmp_path, capture=dirty)

    def invalid(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        if "status" in arguments:
            return SimpleNamespace(stdout="")
        if "show" in arguments:
            return SimpleNamespace(stdout="1700000000\n")
        return SimpleNamespace(stdout="not-a-sha\n")

    with pytest.raises(RuntimeError, match="source revision is invalid"):
        builder.git_source_identity(tmp_path, capture=invalid)


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "x86_64"), ("Darwin", "arm64"), ("Windows", "ARM64")],
)
def test_builder_rejects_non_windows_x64_hosts(
    tmp_path: Path, system: str, machine: str
) -> None:
    with pytest.raises(RuntimeError, match="Windows x64"):
        builder.build_windows_desktop(
            root=tmp_path,
            source_identity=builder.SourceIdentity("a" * 40, 1_700_000_000),
            system=system,
            machine=machine,
            dry_run=True,
        )


def test_dry_run_binds_reproducible_environment_and_exact_commands(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    work = tmp_path / "work"
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def run(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        calls.append((arguments, cwd, env))

    result = builder.build_windows_desktop(
        root=root,
        source_identity=builder.SourceIdentity("b" * 40, 1_700_000_123),
        system="Windows",
        machine="AMD64",
        executable=r"C:\Python312\python.exe",
        pnpm=r"C:\pnpm\pnpm.cmd",
        work_dir=work,
        runner=run,
        dry_run=True,
    )

    assert result is None
    assert calls[0][0] == [
        r"C:\Python312\python.exe",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        os.fspath(root / "src-tauri" / "binaries"),
        "--workpath",
        os.fspath(work / "pyinstaller"),
        os.fspath(root / "packaging" / "stock-desk-sidecar.spec"),
    ]
    assert calls[1][0] == [
        r"C:\pnpm\pnpm.cmd",
        "exec",
        "tauri",
        "build",
        "--config",
        "src-tauri/tauri.conf.json",
        "--bundles",
        "nsis",
        "--target",
        "x86_64-pc-windows-msvc",
    ]
    for _arguments, cwd, environment in calls:
        assert cwd == root
        assert environment["STOCK_DESK_SOURCE_REVISION"] == "b" * 40
        assert environment["SOURCE_DATE_EPOCH"] == "1700000123"
        assert environment["PYTHONHASHSEED"] == "0"
        assert environment["CARGO_ENCODED_RUSTFLAGS"] == "-C\x1flink-arg=/Brepro"


def test_artifact_validation_requires_one_exact_pe_x64_binary(
    tmp_path: Path,
) -> None:
    expected = tmp_path / builder.SIDECAR_EXE
    expected.write_bytes(_pe_x64())

    assert (
        builder.validate_sidecar_artifact(
            tmp_path, inventory_reader=lambda _path: ["stock_desk/api.pyc", "LICENSE"]
        )
        == expected
    )

    (tmp_path / "unexpected.exe").write_bytes(_pe_x64())
    with pytest.raises(RuntimeError, match="exactly one sidecar executable"):
        builder.validate_sidecar_artifact(tmp_path, inventory_reader=lambda _path: [])


@pytest.mark.parametrize(
    "payload",
    [b"not-pe", b"MZ" + (b"\0" * 254)],
)
def test_artifact_validation_rejects_non_x64_pe(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / builder.SIDECAR_EXE).write_bytes(payload)

    with pytest.raises(RuntimeError, match="PE x64"):
        builder.validate_sidecar_artifact(tmp_path, inventory_reader=lambda _path: [])


@pytest.mark.parametrize(
    "entry",
    [
        "web/dist/index.html",
        "browser/chromium.exe",
        "provider/.git/config",
        "src/stock_desk/sidecar.py",
        "stock_desk/sidecar.py",
        "stock_desk.web",
        "stock_desk.web.routes",
        "stock_desk.desktop",
        "stock_desk.desktop.launcher",
        "tests/unit/test_sidecar.py",
        "pyproject.toml",
        "pnpm-lock.yaml",
    ],
)
def test_artifact_inventory_rejects_web_browser_source_and_dev_files(
    tmp_path: Path, entry: str
) -> None:
    (tmp_path / builder.SIDECAR_EXE).write_bytes(_pe_x64())

    with pytest.raises(RuntimeError, match="forbidden development content"):
        builder.validate_sidecar_artifact(
            tmp_path, inventory_reader=lambda _path: [entry]
        )


def test_artifact_inventory_allows_locked_provider_runtime_python_data(
    tmp_path: Path,
) -> None:
    expected = tmp_path / builder.SIDECAR_EXE
    expected.write_bytes(_pe_x64())

    assert (
        builder.validate_sidecar_artifact(
            tmp_path,
            inventory_reader=lambda _path: [
                "akshare/__init__.py",
                "akshare/air/air_hebei.py",
                "tushare/subs/runtime.py",
                "numpy-2.5.1.dist-info/licenses/numpy/_core/src/common/COPYING",
                "provider/runtime/package.json",
                "stock_desk/migrations/env.py",
                "stock_desk/migrations/versions/0001_core_tables.py",
            ],
        )
        == expected
    )
