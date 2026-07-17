from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import macos_sidecar
from scripts.macos_sidecar import MacOSSidecarError, sidecar_filename


def test_sidecar_filename_accepts_only_supported_macos_host_targets() -> None:
    assert sidecar_filename("aarch64-apple-darwin") == (
        "stock-desk-sidecar-aarch64-apple-darwin"
    )
    assert sidecar_filename("x86_64-apple-darwin") == (
        "stock-desk-sidecar-x86_64-apple-darwin"
    )
    with pytest.raises(MacOSSidecarError, match="unsupported macOS target"):
        sidecar_filename("x86_64-pc-windows-msvc")


def test_generated_native_sidecars_are_ignored_without_ignoring_sources() -> None:
    ignore = (macos_sidecar.ROOT / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()

    assert "src-tauri/binaries/stock-desk-sidecar-*" in ignore
    assert "src-tauri/binaries/" not in ignore


def test_host_target_triple_comes_from_rustc_and_is_target_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        return SimpleNamespace(stdout="aarch64-apple-darwin\n")

    monkeypatch.setattr(macos_sidecar.subprocess, "run", run)

    assert macos_sidecar.host_target_triple() == "aarch64-apple-darwin"
    assert calls == [
        (
            ["rustc", "--print", "host-tuple"],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "timeout": 30,
            },
        )
    ]


def test_build_native_sidecar_uses_current_python_and_validated_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    output_dir = tmp_path / "dist"
    (root / "packaging").mkdir(parents=True)
    spec = root / "packaging" / "stock-desk-sidecar.spec"
    spec.write_text("# test spec\n", encoding="utf-8")
    target = "aarch64-apple-darwin"
    expected = output_dir / sidecar_filename(target)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        expected.write_bytes(b"native executable")
        expected.chmod(0o755)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_sidecar.sys, "executable", "/current/python")
    monkeypatch.setattr(macos_sidecar.subprocess, "run", run)
    monkeypatch.setenv(
        "STOCK_DESK_PYINSTALLER_SIDECAR_NAME", "untrusted-inherited-name"
    )

    assert (
        macos_sidecar.build_native_sidecar(root, output_dir, target) == expected
    )
    assert calls[0][0] == [
        "/current/python",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        os.fspath(output_dir),
        "--workpath",
        os.fspath(root / "build"),
        os.fspath(spec),
    ]
    invocation = calls[0][1]
    assert invocation["cwd"] == root
    assert invocation["check"] is True
    environment = invocation["env"]
    assert isinstance(environment, dict)
    assert environment["STOCK_DESK_PYINSTALLER_SIDECAR_NAME"] == expected.name


@pytest.mark.parametrize("artifact_state", ["missing", "extra"])
def test_build_native_sidecar_requires_exactly_one_expected_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_state: str,
) -> None:
    root = tmp_path / "repo"
    output_dir = tmp_path / "dist"
    (root / "packaging").mkdir(parents=True)
    target = "x86_64-apple-darwin"
    expected = output_dir / sidecar_filename(target)

    def run(_arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        if artifact_state == "extra":
            expected.write_bytes(b"expected")
            expected.chmod(0o755)
            extra = output_dir / "unexpected-executable"
            extra.write_bytes(b"extra")
            extra.chmod(0o755)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_sidecar.subprocess, "run", run)

    with pytest.raises(
        MacOSSidecarError, match="exactly one native sidecar executable"
    ):
        macos_sidecar.build_native_sidecar(root, output_dir, target)


@pytest.mark.parametrize("build_fails", [False, True])
def test_build_native_sidecar_always_deletes_pyinstaller_intermediates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fails: bool,
) -> None:
    root = tmp_path / "repo"
    output_dir = tmp_path / "dist"
    packaging = root / "packaging"
    packaging.mkdir(parents=True)
    source_spec = packaging / "stock-desk-sidecar.spec"
    source_spec.write_text("# source spec\n", encoding="utf-8")
    target = "aarch64-apple-darwin"
    name = sidecar_filename(target)
    expected = output_dir / name
    generated_spec = root / f"{name}.spec"

    def run(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        assert os.fspath(root / "build") in arguments
        (root / "build" / "nested").mkdir(parents=True)
        generated_spec.write_text("# generated\n", encoding="utf-8")
        if build_fails:
            raise macos_sidecar.subprocess.CalledProcessError(1, arguments)
        expected.write_bytes(b"native executable")
        expected.chmod(0o755)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_sidecar.subprocess, "run", run)

    if build_fails:
        with pytest.raises(macos_sidecar.subprocess.CalledProcessError):
            macos_sidecar.build_native_sidecar(root, output_dir, target)
    else:
        assert (
            macos_sidecar.build_native_sidecar(root, output_dir, target)
            == expected
        )

    assert not (root / "build").exists()
    assert not generated_spec.exists()
    assert source_spec.is_file()


def test_cli_builds_the_host_target_into_the_requested_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "native-sidecar"
    target = "aarch64-apple-darwin"
    expected = output_dir / sidecar_filename(target)
    calls: list[tuple[Path, Path, str]] = []

    monkeypatch.setattr(macos_sidecar, "host_target_triple", lambda: target)

    def build(root: Path, output: Path, target_triple: str) -> Path:
        calls.append((root, output, target_triple))
        return expected

    monkeypatch.setattr(macos_sidecar, "build_native_sidecar", build)

    assert macos_sidecar.main(["--output", os.fspath(output_dir)]) == 0
    assert calls == [(macos_sidecar.ROOT, output_dir.resolve(), target)]
    assert capsys.readouterr().out.strip() == os.fspath(expected)
