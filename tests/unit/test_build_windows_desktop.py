from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from scripts import build_windows_desktop as builder


_ORIGINAL_INSTALLER = b"tauri-original-unsigned-installer"
_CANONICAL_INSTALLER = b"canonical-makensis-output"


@dataclass(frozen=True)
class _NsisLayout:
    render: Path
    rendered_script: Path
    bundle: Path
    installer: Path
    toolchain: Path
    compiler: Path
    compiler_output: Path


def _nsis_layout(root: Path, local_app_data: Path) -> _NsisLayout:
    release = root / "src-tauri" / "target" / builder.WINDOWS_TARGET / "release"
    render = release / "nsis" / "x64"
    bundle = release / "bundle" / "nsis"
    toolchain = local_app_data / "tauri" / "NSIS" / "v3.11"
    return _NsisLayout(
        render=render,
        rendered_script=render / "installer.nsi",
        bundle=bundle,
        installer=bundle / "Stock Desk_1.1.0_x64-setup.exe",
        toolchain=toolchain,
        compiler=toolchain / "makensis.exe",
        compiler_output=render / "nsis-output.exe",
    )


def _write_file(path: Path, payload: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_complete_toolchain(root: Path) -> None:
    for directory in ("Include", "Plugins", "Stubs", "Bin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    _write_file(root / "makensis.exe")
    _write_file(root / "Bin" / "makensis.exe")


def _write_tauri_nsis_outputs(layout: _NsisLayout) -> None:
    for name in ("installer.nsi", "FileAssociation.nsh", "utils.nsh"):
        _write_file(layout.render / name)
    _write_file(layout.installer, _ORIGINAL_INSTALLER)
    _write_complete_toolchain(layout.toolchain)

    # Discovery is deliberately bounded: these plausible decoys are not inputs.
    _write_file(layout.render.parent / "arm64" / "installer.nsi")
    _write_file(layout.bundle / "nested" / "decoy.exe")
    _write_file(layout.toolchain.parent / "incomplete-decoy" / "makensis.exe")


def _run_nsis_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_outputs: Callable[[_NsisLayout], None] | None = None,
    compiler_payload: bytes | None = _CANONICAL_INSTALLER,
    call_log: list[tuple[list[str], Path, dict[str, str]]] | None = None,
) -> tuple[
    Path | None,
    _NsisLayout,
    list[tuple[list[str], Path, dict[str, str]]],
]:
    root = tmp_path / "repo"
    root.mkdir()
    work = tmp_path / "work"
    local_app_data = tmp_path / "Local App Data"
    layout = _nsis_layout(root, local_app_data)
    python = r"C:\Python312\python.exe"
    pnpm = r"C:\pnpm\pnpm.cmd"
    calls = [] if call_log is None else call_log

    monkeypatch.setenv("LOCALAPPDATA", os.fspath(local_app_data))
    monkeypatch.setenv("NSISDIR", r"C:\inherited-nsis")
    monkeypatch.setenv("NSISCONFDIR", r"C:\inherited-nsis-config")

    def run(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        calls.append((arguments, cwd, env))
        if arguments[0] == python:
            _write_file(
                root / "src-tauri" / "binaries" / builder.SIDECAR_EXE, _pe_x64()
            )
        elif arguments[0] == pnpm:
            _write_tauri_nsis_outputs(layout)
            if mutate_outputs is not None:
                mutate_outputs(layout)
        elif Path(arguments[0]) == layout.compiler and compiler_payload is not None:
            _write_file(layout.compiler_output, compiler_payload)

    result = builder.build_windows_desktop(
        root=root,
        source_identity=builder.SourceIdentity("c" * 40, 1_700_000_456),
        system="Windows",
        machine="AMD64",
        executable=python,
        pnpm=pnpm,
        work_dir=work,
        runner=run,
        inventory_reader=lambda _path: [],
    )
    return result, layout, calls


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


def test_build_canonicalizes_the_exact_tauri_nsis_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def audited_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", audited_replace)

    result, layout, calls = _run_nsis_build(tmp_path, monkeypatch)

    compiler_calls = [call for call in calls if Path(call[0][0]) == layout.compiler]
    assert len(compiler_calls) == 1
    compiler_arguments, compiler_cwd, compiler_environment = compiler_calls[0]
    assert compiler_arguments == [
        os.fspath(layout.compiler),
        "-INPUTCHARSET",
        "UTF8",
        "-OUTPUTCHARSET",
        "UTF8",
        "-V3",
        os.fspath(layout.rendered_script),
    ]
    assert compiler_cwd == layout.render
    assert "NSISDIR" not in compiler_environment
    assert "NSISCONFDIR" not in compiler_environment
    assert compiler_environment["SOURCE_DATE_EPOCH"] == "1700000456"
    assert compiler_environment["STOCK_DESK_SOURCE_REVISION"] == "c" * 40
    assert replacements == [(layout.compiler_output, layout.installer)]
    assert not layout.compiler_output.exists()
    assert layout.installer.read_bytes() == _CANONICAL_INSTALLER
    assert result == layout.render.parents[4] / "binaries" / builder.SIDECAR_EXE


def test_build_rejects_preexisting_canonical_nsis_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def precreate_output(layout: _NsisLayout) -> None:
        _write_file(layout.compiler_output, b"unowned-output")

    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    with pytest.raises(RuntimeError, match="NSIS output.*already exists"):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            mutate_outputs=precreate_output,
            call_log=calls,
        )

    assert not any(Path(call[0][0]) == layout.compiler for call in calls)
    assert layout.compiler_output.read_bytes() == b"unowned-output"
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-render", "rendered NSIS input is missing"),
        ("missing-bundle", "exactly one unsigned NSIS installer"),
        ("ambiguous-bundle", "exactly one unsigned NSIS installer"),
        ("missing-toolchain", "exactly one Tauri NSIS toolchain"),
        ("ambiguous-toolchain", "exactly one Tauri NSIS toolchain"),
    ],
)
def test_build_fails_closed_when_nsis_discovery_is_missing_or_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    def mutate(layout: _NsisLayout) -> None:
        if case == "missing-render":
            layout.rendered_script.unlink()
        elif case == "missing-bundle":
            layout.installer.unlink()
        elif case == "ambiguous-bundle":
            _write_file(layout.bundle / "second-installer.exe")
        elif case == "missing-toolchain":
            layout.compiler.unlink()
        elif case == "ambiguous-toolchain":
            _write_complete_toolchain(
                layout.toolchain.parent / "second-complete-toolchain"
            )
        else:  # pragma: no cover - the parametrization is exhaustive
            raise AssertionError(case)

    with pytest.raises(RuntimeError, match=message):
        _run_nsis_build(tmp_path, monkeypatch, mutate_outputs=mutate)


def test_build_rejects_missing_canonical_nsis_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    with pytest.raises(RuntimeError, match="canonical NSIS output is missing"):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            compiler_payload=None,
            call_log=calls,
        )

    assert any(Path(call[0][0]) == layout.compiler for call in calls)
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    "tampered_payload",
    [
        pytest.param(b"wrong-size", id="destination-size"),
        pytest.param(
            b"x" * len(_CANONICAL_INSTALLER),
            id="destination-sha256",
        ),
    ],
)
def test_build_rereads_destination_identity_after_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_payload: bytes,
) -> None:
    real_replace = os.replace

    def tampering_replace(source: str | Path, destination: str | Path) -> None:
        real_replace(source, destination)
        Path(destination).write_bytes(tampered_payload)

    monkeypatch.setattr(builder.os, "replace", tampering_replace)

    with pytest.raises(RuntimeError, match="NSIS destination identity mismatch"):
        _run_nsis_build(tmp_path, monkeypatch)


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


def test_sidecar_spec_bundles_the_public_synthetic_demo_snapshot() -> None:
    spec = (
        Path(__file__).resolve().parents[2] / "packaging" / "stock-desk-sidecar.spec"
    ).read_text(encoding="utf-8")

    assert '"src" / "stock_desk" / "demo" / "market_snapshot.json"' in spec
    assert '"stock_desk/demo"' in spec
