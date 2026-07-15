from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
from types import SimpleNamespace

import pytest

from scripts import build_windows_desktop as builder
from scripts import nsis_repack_contract as nsis_contract


_ORIGINAL_INSTALLER = b"tauri-original-unsigned-installer"
_CANONICAL_INSTALLER = b"canonical-makensis-output"
_DEFAULT_LOCAL_APP_DATA = object()


@dataclass(frozen=True)
class _NsisLayout:
    render: Path
    rendered_script: Path
    bundle: Path
    installer: Path
    toolchain: Path
    compiler: Path
    compiler_output: Path
    external_plugin: Path


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
        external_plugin=(
            local_app_data / "tauri" / "WixTools" / "nsis_tauri_utils.dll"
        ),
    )


def _write_file(path: Path, payload: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_complete_toolchain(root: Path) -> None:
    for directory in ("Include", "Plugins", "Stubs", "Bin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    _write_file(root / "makensis.exe")
    _write_file(root / "Bin" / "makensis.exe")


def _write_external_plugin(layout: _NsisLayout) -> None:
    _write_file(layout.external_plugin, b"locked-nsis-tauri-utils")


def _write_tauri_nsis_outputs(layout: _NsisLayout) -> None:
    _write_file(
        layout.rendered_script,
        (
            f'!addplugindir "{layout.external_plugin.parent}"\n'
            "nsis_tauri_utils::SemverCompare\n"
        ).encode(),
    )
    for name in ("FileAssociation.nsh", "utils.nsh"):
        _write_file(layout.render / name)
    _write_file(layout.installer, _ORIGINAL_INSTALLER)
    _write_complete_toolchain(layout.toolchain)
    _write_external_plugin(layout)

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
    compiler_writer: Callable[[Path], None] | None = None,
    toolchain_verifier: Callable[[Path, Path], object] | None = None,
    verification_log: list[tuple[Path, Path]] | None = None,
    event_log: list[str] | None = None,
    call_log: list[tuple[list[str], Path, dict[str, str]]] | None = None,
    local_app_data_env: str | None | object = _DEFAULT_LOCAL_APP_DATA,
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
    verifications = [] if verification_log is None else verification_log
    events = [] if event_log is None else event_log

    if local_app_data_env is _DEFAULT_LOCAL_APP_DATA:
        monkeypatch.setenv("LOCALAPPDATA", os.fspath(local_app_data))
    elif local_app_data_env is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", str(local_app_data_env))
    monkeypatch.setenv("NSISDIR", r"C:\inherited-nsis")
    monkeypatch.setenv("NSISCONFDIR", r"C:\inherited-nsis-config")
    monkeypatch.setenv("APPDATA", r"C:\inherited-appdata")

    def verify_toolchain(nsis_root: Path, rendered_script: Path) -> object:
        verifications.append((nsis_root, rendered_script))
        events.append("verify-toolchain")
        if toolchain_verifier is not None:
            return toolchain_verifier(nsis_root, rendered_script)
        return object()

    monkeypatch.setattr(
        nsis_contract,
        "verify_extracted_nsis_toolchain",
        verify_toolchain,
        raising=False,
    )

    def run(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        calls.append((arguments, cwd, env))
        if arguments[0] == python:
            events.append("pyinstaller")
            _write_file(
                root / "src-tauri" / "binaries" / builder.SIDECAR_EXE, _pe_x64()
            )
        elif arguments[0] == pnpm:
            events.append("tauri")
            _write_tauri_nsis_outputs(layout)
            if mutate_outputs is not None:
                mutate_outputs(layout)
        elif Path(arguments[0]) == layout.compiler:
            events.append("compiler")
            if compiler_writer is not None:
                compiler_writer(layout.compiler_output)
            elif compiler_payload is not None:
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


def _compiler_calls(
    calls: list[tuple[list[str], Path, dict[str, str]]],
    layout: _NsisLayout,
) -> list[tuple[list[str], Path, dict[str, str]]]:
    return [
        call
        for call in calls
        if Path(call[0][0]) == layout.compiler
        or Path(call[0][0]).name.casefold() == "makensis.exe"
    ]


def _metadata_with(
    metadata: os.stat_result,
    **overrides: int,
) -> SimpleNamespace:
    values = {
        name: getattr(metadata, name)
        for name in dir(metadata)
        if name.startswith("st_")
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
    inherited_environment = os.environ.copy()
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
    assert len(calls) == 2
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
    assert list(root.iterdir()) == []
    assert not work.exists()
    assert os.environ == inherited_environment


def test_build_canonicalizes_the_exact_tauri_nsis_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []
    verifications: list[tuple[Path, Path]] = []
    events: list[str] = []
    inherited_app_data = r"C:\inherited-appdata"

    def audited_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", audited_replace)

    result, layout, calls = _run_nsis_build(
        tmp_path,
        monkeypatch,
        verification_log=verifications,
        event_log=events,
    )

    compiler_calls = _compiler_calls(calls, layout)
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
    assert compiler_environment is not calls[0][2]
    assert compiler_environment is not calls[1][2]
    for build_environment in (calls[0][2], calls[1][2]):
        assert build_environment["APPDATA"] == inherited_app_data
        assert build_environment["NSISDIR"] == r"C:\inherited-nsis"
        assert build_environment["NSISCONFDIR"] == r"C:\inherited-nsis-config"
    private_app_data = Path(compiler_environment["APPDATA"])
    assert private_app_data != Path(inherited_app_data)
    assert private_app_data.is_absolute()
    assert private_app_data.is_relative_to(tmp_path / "work")
    assert private_app_data.is_dir()
    assert list(private_app_data.iterdir()) == []
    assert not private_app_data.is_symlink()
    assert not (
        getattr(os.lstat(private_app_data), "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    assert os.environ["APPDATA"] == inherited_app_data
    assert os.environ["NSISDIR"] == r"C:\inherited-nsis"
    assert os.environ["NSISCONFDIR"] == r"C:\inherited-nsis-config"
    assert verifications == [(layout.toolchain, layout.rendered_script)]
    assert events.index("tauri") < events.index("verify-toolchain")
    assert events.index("verify-toolchain") < events.index("compiler")
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
    with pytest.raises(RuntimeError):
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
    "object_kind",
    ["broken-symlink", "file-symlink", "hardlink", "directory"],
)
def test_build_uses_lstat_ownership_for_every_preexisting_output_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_kind: str,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    before: list[os.stat_result] = []

    def precreate_object(actual: _NsisLayout) -> None:
        external = tmp_path / "unknown-output-owner.exe"
        if object_kind == "broken-symlink":
            try:
                actual.compiler_output.symlink_to(tmp_path / "missing-owner")
            except OSError:
                pytest.skip("file symlink creation is unavailable")
        elif object_kind == "file-symlink":
            _write_file(external, b"unknown-owner")
            try:
                actual.compiler_output.symlink_to(external)
            except OSError:
                pytest.skip("file symlink creation is unavailable")
        elif object_kind == "hardlink":
            _write_file(external, b"unknown-owner")
            os.link(external, actual.compiler_output)
        elif object_kind == "directory":
            actual.compiler_output.mkdir()
        else:  # pragma: no cover - parametrization is exhaustive
            raise AssertionError(object_kind)
        before.append(os.lstat(actual.compiler_output))

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            mutate_outputs=precreate_object,
            call_log=calls,
        )

    after = os.lstat(layout.compiler_output)
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) == (
        before[0].st_dev,
        before[0].st_ino,
        before[0].st_mode,
        before[0].st_nlink,
    )
    assert os.path.lexists(layout.compiler_output)
    assert _compiler_calls(calls, layout) == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


def test_build_treats_only_file_not_found_as_absent_compiler_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    real_lstat = os.lstat

    def denied_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object):
        if Path(path) == layout.compiler_output:
            raise PermissionError("fixture lstat denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(builder.os, "lstat", denied_lstat)

    with pytest.raises(RuntimeError):
        _run_nsis_build(tmp_path, monkeypatch, call_log=calls)

    assert _compiler_calls(calls, layout) == []
    assert not os.path.lexists(layout.compiler_output)
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize("tamper", ["compiler", "external-plugin", "tree"])
def test_build_rejects_shared_toolchain_verifier_failure_before_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    events: list[str] = []
    replacements: list[tuple[Path, Path]] = []

    def tamper_toolchain(actual: _NsisLayout) -> None:
        if tamper == "compiler":
            actual.compiler.write_bytes(b"tampered-compiler")
        elif tamper == "external-plugin":
            actual.external_plugin.write_bytes(b"tampered-plugin")
        elif tamper == "tree":
            _write_file(actual.toolchain / "Include" / "unlocked.nsh")
        else:  # pragma: no cover - parametrization is exhaustive
            raise AssertionError(tamper)

    def reject_toolchain(nsis_root: Path, rendered_script: Path) -> object:
        assert (nsis_root, rendered_script) == (
            layout.toolchain,
            layout.rendered_script,
        )
        raise nsis_contract.NsisRepackContractError("locked tree mismatch")

    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            mutate_outputs=tamper_toolchain,
            toolchain_verifier=reject_toolchain,
            event_log=events,
            call_log=calls,
        )

    assert "verify-toolchain" in events
    assert "compiler" not in events
    assert _compiler_calls(calls, layout) == []
    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    "critical_path",
    [
        "render-root",
        "rendered-script",
        "render-ancestor",
        "bundle-root",
        "bundle-installer",
        "bundle-ancestor",
        "toolchain-root",
        "compiler",
        "plugin-directory",
        "external-plugin",
        "toolchain-ancestor",
    ],
)
def test_build_rejects_reparse_metadata_for_every_critical_path_and_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    critical_path: str,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    paths = {
        "render-root": layout.render,
        "rendered-script": layout.rendered_script,
        "render-ancestor": layout.render.parent,
        "bundle-root": layout.bundle,
        "bundle-installer": layout.installer,
        "bundle-ancestor": layout.bundle.parent,
        "toolchain-root": layout.toolchain,
        "compiler": layout.compiler,
        "plugin-directory": layout.toolchain / "Plugins",
        "external-plugin": layout.external_plugin,
        "toolchain-ancestor": layout.toolchain.parents[2],
    }
    marked_path = paths[critical_path]
    real_lstat = os.lstat
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    replacements: list[tuple[Path, Path]] = []

    def reparse_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object):
        metadata = real_lstat(path, *args, **kwargs)
        if Path(path) == marked_path:
            return _metadata_with(
                metadata,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return metadata

    monkeypatch.setattr(builder.os, "lstat", reparse_lstat)
    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    toolchain_owned_paths = {
        "toolchain-root",
        "compiler",
        "plugin-directory",
        "external-plugin",
        "toolchain-ancestor",
    }

    def verifier_guard(_nsis_root: Path, _rendered_script: Path) -> object:
        metadata = os.lstat(marked_path)
        if getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise nsis_contract.NsisRepackContractError("reparse path")
        return object()

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            call_log=calls,
            toolchain_verifier=(
                verifier_guard if critical_path in toolchain_owned_paths else None
            ),
        )

    assert _compiler_calls(calls, layout) == []
    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.skipif(os.name != "nt", reason="requires a native Windows junction")
def test_windows_build_rejects_a_real_render_root_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    external_render = tmp_path / "external-render"
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    junction_created = False

    def replace_render_with_junction(actual: _NsisLayout) -> None:
        nonlocal junction_created
        actual.render.rename(external_render)
        completed = subprocess.run(
            [
                "cmd",
                "/d",
                "/c",
                "mklink",
                "/J",
                os.fspath(actual.render),
                os.fspath(external_render),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            external_render.rename(actual.render)
            pytest.skip("Windows junction creation is unavailable")
        junction_created = True

    try:
        with pytest.raises(RuntimeError):
            _run_nsis_build(
                tmp_path,
                monkeypatch,
                mutate_outputs=replace_render_with_junction,
                call_log=calls,
            )
        assert _compiler_calls(calls, layout) == []
        assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER
    finally:
        if junction_created:
            subprocess.run(
                ["cmd", "/d", "/c", "rmdir", os.fspath(layout.render)],
                check=False,
                capture_output=True,
            )
            external_render.rename(layout.render)


@pytest.mark.parametrize(
    "case",
    [
        "missing-render",
        "missing-bundle",
        "ambiguous-bundle",
        "missing-toolchain",
        "ambiguous-toolchain",
    ],
)
def test_build_fails_closed_when_nsis_discovery_is_missing_or_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
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

    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    replacements: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            mutate_outputs=mutate,
            call_log=calls,
        )

    assert _compiler_calls(calls, layout) == []
    assert replacements == []
    if layout.installer.exists():
        assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "relative",
        "nonexistent",
        "unreadable",
        "case-collision",
    ],
)
def test_build_rejects_unsafe_local_app_data_before_candidate_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    actual_local_app_data = tmp_path / "Local App Data"
    layout = _nsis_layout(tmp_path / "repo", actual_local_app_data)
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    replacements: list[tuple[Path, Path]] = []
    local_app_data_env: str | None | object = os.fspath(actual_local_app_data)
    mutate_outputs: Callable[[_NsisLayout], None] | None = None

    if case == "missing":
        local_app_data_env = None
    elif case == "empty":
        local_app_data_env = ""
    elif case == "relative":
        local_app_data_env = "relative-cache"
    elif case == "nonexistent":
        local_app_data_env = os.fspath(tmp_path / "does-not-exist")
    elif case == "unreadable":
        real_scandir = os.scandir

        def unreadable_scandir(path: os.PathLike[str] | str):
            if (
                not isinstance(path, int)
                and Path(path) == actual_local_app_data / "tauri"
            ):
                raise PermissionError("fixture enumeration denied")
            return real_scandir(path)

        monkeypatch.setattr(builder.os, "scandir", unreadable_scandir)
    elif case == "case-collision":

        def create_collision(actual: _NsisLayout) -> None:
            shutil.rmtree(actual.toolchain)
            for name in ("Straße", "STRASSE"):
                _write_complete_toolchain(
                    actual_local_app_data / "tauri" / name / "v3.11"
                )

        mutate_outputs = create_collision
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            local_app_data_env=local_app_data_env,
            mutate_outputs=mutate_outputs,
            call_log=calls,
        )

    assert _compiler_calls(calls, layout) == []
    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


def test_build_rejects_missing_canonical_nsis_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            compiler_payload=None,
            call_log=calls,
        )

    assert any(Path(call[0][0]) == layout.compiler for call in calls)
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    "object_kind",
    ["empty", "directory", "file-symlink", "hardlink", "reparse"],
)
def test_build_rejects_unowned_or_nonregular_generated_nsis_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_kind: str,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    replacements: list[tuple[Path, Path]] = []

    def write_generated(output: Path) -> None:
        external = tmp_path / "compiler-external-output.exe"
        if object_kind == "empty":
            _write_file(output, b"")
        elif object_kind == "directory":
            output.mkdir()
        elif object_kind == "file-symlink":
            _write_file(external, _CANONICAL_INSTALLER)
            try:
                output.symlink_to(external)
            except OSError:
                pytest.skip("file symlink creation is unavailable")
        elif object_kind == "hardlink":
            _write_file(external, _CANONICAL_INSTALLER)
            os.link(external, output)
        elif object_kind == "reparse":
            _write_file(output, _CANONICAL_INSTALLER)
        else:  # pragma: no cover - parametrization is exhaustive
            raise AssertionError(object_kind)

    real_lstat = os.lstat

    def reparse_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object):
        metadata = real_lstat(path, *args, **kwargs)
        if object_kind == "reparse" and Path(path) == layout.compiler_output:
            return _metadata_with(
                metadata,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return metadata

    monkeypatch.setattr(builder.os, "lstat", reparse_lstat)
    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(
            tmp_path,
            monkeypatch,
            compiler_writer=write_generated,
        )

    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


def test_build_rejects_generated_output_replaced_between_lstat_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    moved_output = tmp_path / "moved-generated-output.exe"
    real_os_open = os.open
    real_path_open = Path.open
    replacements: list[tuple[Path, Path]] = []
    swapped: list[bool] = []

    def swap_output_once() -> None:
        if swapped or not layout.compiler_output.exists():
            return
        layout.compiler_output.rename(moved_output)
        _write_file(layout.compiler_output, b"raced-generated-object")
        swapped.append(True)

    def swapping_os_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if Path(path) == layout.compiler_output and flags & os.O_ACCMODE == os.O_RDONLY:
            swap_output_once()
        return real_os_open(path, flags, *args, **kwargs)

    def swapping_path_open(
        path: Path,
        mode: str = "r",
        *args: object,
        **kwargs: object,
    ):
        if path == layout.compiler_output and "r" in mode:
            swap_output_once()
        return real_path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builder.os, "open", swapping_os_open)
    monkeypatch.setattr(Path, "open", swapping_path_open)
    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(tmp_path, monkeypatch)

    assert swapped == [True]
    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


def test_build_rejects_cross_volume_output_before_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    real_lstat = os.lstat
    replacements: list[tuple[Path, Path]] = []

    def cross_volume_lstat(
        path: os.PathLike[str] | str, *args: object, **kwargs: object
    ):
        metadata = real_lstat(path, *args, **kwargs)
        if Path(path) == layout.compiler_output:
            return _metadata_with(metadata, st_dev=metadata.st_dev + 1)
        return metadata

    monkeypatch.setattr(builder.os, "lstat", cross_volume_lstat)
    monkeypatch.setattr(
        builder.os,
        "replace",
        lambda source, destination: replacements.append(
            (Path(source), Path(destination))
        ),
    )

    with pytest.raises(RuntimeError):
        _run_nsis_build(tmp_path, monkeypatch)

    assert replacements == []
    assert layout.installer.read_bytes() == _ORIGINAL_INSTALLER


@pytest.mark.parametrize(
    "replace_error",
    [
        pytest.param(OSError(errno.EXDEV, "cross-device"), id="cross-device"),
        pytest.param(PermissionError(errno.EACCES, "denied"), id="permission"),
    ],
)
def test_build_never_falls_back_to_copy_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_error: OSError,
) -> None:
    layout = _nsis_layout(tmp_path / "repo", tmp_path / "Local App Data")
    replace_attempts: list[tuple[Path, Path]] = []
    fallback_attempts: list[str] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        replace_attempts.append((Path(source), Path(destination)))
        raise replace_error

    def reject_fallback(*_args: object, **_kwargs: object) -> None:
        fallback_attempts.append("copy-or-move")
        raise AssertionError("copy fallback is forbidden")

    monkeypatch.setattr(builder.os, "replace", fail_replace)
    for name in ("move", "copy", "copy2", "copyfile"):
        monkeypatch.setattr(builder.shutil, name, reject_fallback)

    with pytest.raises(RuntimeError):
        _run_nsis_build(tmp_path, monkeypatch)

    assert replace_attempts == [(layout.compiler_output, layout.installer)]
    assert fallback_attempts == []
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

    with pytest.raises(RuntimeError):
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
