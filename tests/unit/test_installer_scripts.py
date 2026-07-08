from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_installer
from scripts import verify_installed_app as verifier


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", ("windows", "x86_64")),
        ("Darwin", "x86_64", ("macos", "x86_64")),
        ("Darwin", "arm64", ("macos", "arm64")),
    ],
)
def test_host_target_requires_a_matching_native_builder(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: tuple[str, str],
) -> None:
    monkeypatch.setattr(build_installer.platform, "system", lambda: system)
    monkeypatch.setattr(build_installer.platform, "machine", lambda: machine)

    assert build_installer._host_target() == expected


def test_host_target_rejects_cross_compilation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_installer.platform, "machine", lambda: "x86_64")

    with pytest.raises(RuntimeError, match="unsupported native installer host"):
        build_installer._host_target()


def test_checksum_manifest_is_flat_and_reproducible(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.dmg"
    artifact.write_bytes(b"stock-desk")

    checksum = build_installer._write_checksum(artifact)

    assert build_installer._sha256(artifact) in checksum.read_text(encoding="ascii")
    assert checksum.read_text(encoding="ascii").endswith("  artifact.dmg\n")


def test_inno_compiler_prefers_explicit_verified_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compiler = tmp_path / "ISCC.exe"
    compiler.touch()
    monkeypatch.setenv("INNO_SETUP_COMPILER", os.fspath(compiler))

    assert build_installer._find_inno_compiler() == compiler


def test_inno_compiler_reports_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INNO_SETUP_COMPILER", raising=False)
    monkeypatch.setenv("ProgramFiles(x86)", "/missing-x86")
    monkeypatch.setenv("ProgramFiles", "/missing")
    monkeypatch.setattr(build_installer.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="Inno Setup 6"):
        build_installer._find_inno_compiler()


def test_optional_windows_signing_requires_complete_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STOCK_DESK_WINDOWS_CERTIFICATE_BASE64", "Y2VydA==")
    monkeypatch.delenv("STOCK_DESK_WINDOWS_CERTIFICATE_PASSWORD", raising=False)
    monkeypatch.setattr(build_installer.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="incomplete"):
        build_installer._sign_windows(tmp_path / "installer.exe")


def test_optional_windows_signing_uses_temporary_certificate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("STOCK_DESK_WINDOWS_CERTIFICATE_BASE64", "Y2VydA==")
    monkeypatch.setenv("STOCK_DESK_WINDOWS_CERTIFICATE_PASSWORD", "secret")
    monkeypatch.setenv("STOCK_DESK_SIGNTOOL", "signtool.exe")
    monkeypatch.setattr(build_installer, "_run", lambda args: calls.append(args))

    build_installer._sign_windows(tmp_path / "installer.exe")

    assert calls[0][0:3] == ["signtool.exe", "sign", "/fd"]
    assert calls[0][-1] == os.fspath(tmp_path / "installer.exe")


def test_windows_builder_requires_and_returns_inno_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compiler = tmp_path / "ISCC.exe"
    compiler.touch()
    output = tmp_path / "output"
    output.mkdir()
    artifact = output / "stock-desk-1.2.3-windows-x86_64.exe"
    artifact.touch()
    calls: list[list[str]] = []
    monkeypatch.setattr(build_installer, "_find_inno_compiler", lambda: compiler)
    monkeypatch.setattr(build_installer, "_run", lambda args: calls.append(args))
    monkeypatch.setattr(
        build_installer, "_sign_windows", lambda path: calls.append([str(path)])
    )

    assert (
        build_installer._build_windows("1.2.3", tmp_path / "bundle", output) == artifact
    )
    assert calls[-1] == [str(artifact)]


def test_macos_signing_and_notarization_are_optional_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = tmp_path / "stock-desk.app"
    artifact = tmp_path / "stock-desk.dmg"
    calls: list[list[str]] = []
    monkeypatch.setenv("STOCK_DESK_MACOS_SIGNING_IDENTITY", "Developer ID")
    monkeypatch.setenv("STOCK_DESK_MACOS_NOTARY_PROFILE", "stock-desk-ci")
    monkeypatch.setattr(build_installer, "_run", lambda args: calls.append(args))

    build_installer._sign_and_notarize_macos(application)
    build_installer._sign_and_notarize_macos(application, artifact)

    assert calls[0][0] == "codesign"
    assert "--entitlements" in calls[0]
    assert calls[1][-1] == os.fspath(artifact)
    assert calls[2][0:3] == ["xcrun", "notarytool", "submit"]
    assert calls[3][0:3] == ["xcrun", "stapler", "staple"]


def test_macos_builder_creates_architecture_named_dmg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = tmp_path / "pyinstaller" / "stock-desk.app"
    application.mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(build_installer, "_run", lambda args: calls.append(args))
    monkeypatch.setattr(
        build_installer,
        "_sign_and_notarize_macos",
        lambda app, artifact=None: calls.append([str(app), str(artifact)]),
    )

    artifact = build_installer._build_macos(
        "1.2.3", "arm64", application.parent, output
    )

    assert artifact.name == "stock-desk-1.2.3-macos-arm64.dmg"
    assert any(call[:2] == ["hdiutil", "create"] for call in calls)


@pytest.mark.parametrize("target", [("windows", "x86_64"), ("macos", "arm64")])
def test_build_installer_drives_native_bundle_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: tuple[str, str],
) -> None:
    root = tmp_path / "repo"
    output = tmp_path / "output"
    calls: list[object] = []
    monkeypatch.setattr(build_installer, "ROOT", root)
    monkeypatch.setattr(build_installer, "_host_target", lambda: target)
    monkeypatch.setattr(build_installer, "_run", lambda args: calls.append(args))
    monkeypatch.setattr(
        build_installer.shutil, "rmtree", lambda path, **kwargs: calls.append(path)
    )
    monkeypatch.setattr(
        build_installer.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    def create_artifact(*args: object) -> Path:
        artifact = output / f"stock-desk-1.2.3-{target[0]}-{target[1]}.dmg"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"artifact")
        return artifact

    monkeypatch.setattr(build_installer, "_build_windows", create_artifact)
    monkeypatch.setattr(build_installer, "_build_macos", create_artifact)

    artifact, checksum = build_installer.build_installer("1.2.3", output_dir=output)

    assert artifact.is_file()
    assert checksum.is_file()
    manifest = json.loads(
        (output / f"stock-desk-1.2.3-{target[0]}-{target[1]}.json").read_text()
    )
    assert manifest["os"] == target[0]
    assert manifest["architecture"] == target[1]
    assert calls


def test_build_installer_rejects_invalid_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        build_installer.build_installer("version/latest", output_dir=tmp_path)


def test_build_installer_main_prints_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "artifact.dmg"
    checksum = tmp_path / "artifact.dmg.sha256"
    monkeypatch.setattr(
        build_installer,
        "build_installer",
        lambda version, output_dir: (artifact, checksum),
    )

    assert build_installer.main(["1.2.3", "--output-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == [str(artifact), str(checksum)]


class _Response(BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_verifier_reads_health_and_browser_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"host":"127.0.0.1","port":43210}', encoding="utf-8")
    responses = iter(
        [
            _Response(b'{"status":"ok"}'),
            _Response(b"<title>stock-desk</title>"),
        ]
    )
    monkeypatch.setattr(verifier, "urlopen", lambda *_args, **_kwargs: next(responses))

    record = verifier._wait_for_health(runtime)
    verifier._assert_browser_document(record)

    assert record["port"] == 43210


def test_verifier_rejects_non_loopback_runtime_record(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"host":"0.0.0.0","port":80}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="not private loopback"):
        verifier._wait_for_health(runtime)


def test_frozen_dispatch_checks_akshare_and_formula(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if "--internal-akshare-worker" in arguments:
            Path(arguments[-1]).write_text(
                '{"status":"invalid_response"}', encoding="utf-8"
            )
            return SimpleNamespace(returncode=2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(verifier.subprocess, "run", run)

    verifier._verify_frozen_internal_dispatch(
        tmp_path / "stock-desk", {"PATH": "/system"}, tmp_path
    )

    assert calls[1][-1] == "--internal-formula-smoke"


def test_verifier_lifecycle_preserves_fixture_and_user_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = (tmp_path / "stock-desk").resolve()
    command.touch()
    data_dir = tmp_path / "data"
    runtime = data_dir / "runtime" / "runtime.json"
    fixture = tmp_path / "fixture.sql"
    fixture.write_text(
        "CREATE TABLE distribution_fixture (release_version TEXT);"
        "INSERT INTO distribution_fixture VALUES ('0.5.0');"
        "CREATE TABLE alembic_version (version_num TEXT);"
        "INSERT INTO alembic_version VALUES ('head');",
        encoding="utf-8",
    )
    processes = [SimpleNamespace(name="first"), SimpleNamespace(name="second")]
    records = iter(
        [
            {"data_dir": str(data_dir), "host": "127.0.0.1", "port": 1},
            {"data_dir": str(data_dir), "host": "127.0.0.1", "port": 2},
        ]
    )
    monkeypatch.setattr(
        verifier, "_verify_frozen_internal_dispatch", lambda *args: None
    )
    monkeypatch.setattr(verifier, "_start", lambda *args, **kwargs: processes.pop(0))
    monkeypatch.setattr(verifier, "_wait_for_health", lambda _path: next(records))
    monkeypatch.setattr(verifier, "_assert_browser_document", lambda _record: None)
    monkeypatch.setattr(verifier, "_stop_and_wait", lambda *args: None)

    verifier.verify_installed_app(
        command,
        runtime,
        sanitized_path="/system",
        fixture_sql=fixture,
    )

    assert (data_dir / "installer-persistence.txt").read_text() == "persistent\n"


def test_verifier_rejects_missing_command(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="command is missing"):
        verifier.verify_installed_app(
            tmp_path / "relative-command",
            tmp_path / "runtime.json",
            sanitized_path="/system",
        )


def test_verifier_main_delegates_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        verifier,
        "verify_installed_app",
        lambda *args, **kwargs: captured.append((*args, kwargs)),
    )

    assert (
        verifier.main(
            [
                "--command",
                str(tmp_path / "app"),
                "--runtime-record",
                str(tmp_path / "runtime.json"),
                "--sanitized-path",
                "/system",
            ]
        )
        == 0
    )
    assert captured
