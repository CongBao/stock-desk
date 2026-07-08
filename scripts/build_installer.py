from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Final


ROOT: Final = Path(__file__).resolve().parent.parent
VERSION_PATTERN: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.-]+)?")


def _host_target() -> tuple[str, str]:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "windows", "x86_64"
    if system == "Darwin" and machine in {"x86_64", "amd64"}:
        return "macos", "x86_64"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "macos", "arm64"
    raise RuntimeError(f"unsupported native installer host: {system}/{machine}")


def _run(arguments: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)  # noqa: S603


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_checksum(artifact: Path) -> Path:
    checksum = artifact.with_name(f"{artifact.name}.sha256")
    checksum.write_text(f"{_sha256(artifact)}  {artifact.name}\n", encoding="ascii")
    return checksum


def _find_inno_compiler() -> Path:
    configured = os.environ.get("INNO_SETUP_COMPILER")
    candidates = [
        Path(configured) if configured else None,
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    discovered = shutil.which("ISCC.exe")
    if discovered:
        return Path(discovered)
    raise RuntimeError("Inno Setup 6 compiler was not found")


def _sign_windows(artifact: Path) -> None:
    encoded_certificate = os.environ.get("STOCK_DESK_WINDOWS_CERTIFICATE_BASE64")
    if not encoded_certificate:
        return
    password = os.environ.get("STOCK_DESK_WINDOWS_CERTIFICATE_PASSWORD")
    signtool = os.environ.get("STOCK_DESK_SIGNTOOL") or shutil.which("signtool.exe")
    if not password or not signtool:
        raise RuntimeError("Windows signing certificate configuration is incomplete")
    with tempfile.TemporaryDirectory(prefix="stock-desk-sign-") as directory:
        certificate = Path(directory) / "certificate.pfx"
        certificate.write_bytes(base64.b64decode(encoded_certificate, validate=True))
        _run(
            [
                signtool,
                "sign",
                "/fd",
                "SHA256",
                "/td",
                "SHA256",
                "/tr",
                "http://timestamp.digicert.com",
                "/f",
                os.fspath(certificate),
                "/p",
                password,
                os.fspath(artifact),
            ]
        )


def _build_windows(version: str, bundle_dir: Path, output_dir: Path) -> Path:
    compiler = _find_inno_compiler()
    _run(
        [
            os.fspath(compiler),
            f"/DAppVersion={version}",
            f"/DBundleDir={bundle_dir}",
            f"/DOutputDir={output_dir}",
            os.fspath(ROOT / "packaging" / "windows" / "stock-desk.iss"),
        ]
    )
    artifact = output_dir / f"stock-desk-{version}-windows-x86_64.exe"
    if not artifact.is_file():
        raise RuntimeError(f"Inno Setup did not produce {artifact}")
    _sign_windows(artifact)
    return artifact


def _sign_and_notarize_macos(application: Path, artifact: Path | None = None) -> None:
    identity = os.environ.get("STOCK_DESK_MACOS_SIGNING_IDENTITY")
    if identity:
        target = application if artifact is None else artifact
        arguments = ["codesign", "--force", "--options", "runtime", "--sign", identity]
        if artifact is None:
            arguments += [
                "--deep",
                "--entitlements",
                os.fspath(ROOT / "packaging" / "macos" / "entitlements.plist"),
            ]
        arguments.append(os.fspath(target))
        _run(arguments)
    if artifact is None:
        return
    notary_profile = os.environ.get("STOCK_DESK_MACOS_NOTARY_PROFILE")
    if notary_profile:
        _run(
            [
                "xcrun",
                "notarytool",
                "submit",
                os.fspath(artifact),
                "--keychain-profile",
                notary_profile,
                "--wait",
            ]
        )
        _run(["xcrun", "stapler", "staple", os.fspath(artifact)])


def _build_macos(
    version: str,
    architecture: str,
    pyinstaller_dist: Path,
    output_dir: Path,
) -> Path:
    application = pyinstaller_dist / "stock-desk.app"
    if not application.is_dir():
        raise RuntimeError(f"PyInstaller did not produce {application}")
    _sign_and_notarize_macos(application)
    artifact = output_dir / f"stock-desk-{version}-macos-{architecture}.dmg"
    artifact.unlink(missing_ok=True)
    _run(
        [
            "hdiutil",
            "create",
            "-volname",
            "Stock Desk",
            "-srcfolder",
            os.fspath(application),
            "-format",
            "UDZO",
            os.fspath(artifact),
        ]
    )
    _sign_and_notarize_macos(application, artifact)
    return artifact


def build_installer(version: str, *, output_dir: Path) -> tuple[Path, Path]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("installer version is invalid")
    os_name, architecture = _host_target()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pyinstaller_dist = ROOT / "dist" / "pyinstaller"
    pyinstaller_work = ROOT / "build" / "pyinstaller"
    shutil.rmtree(pyinstaller_dist, ignore_errors=True)
    shutil.rmtree(pyinstaller_work, ignore_errors=True)
    _run(["pnpm", "build"])
    environment = os.environ.copy()
    environment["STOCK_DESK_BUILD_VERSION"] = version
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            os.fspath(pyinstaller_dist),
            "--workpath",
            os.fspath(pyinstaller_work),
            os.fspath(ROOT / "packaging" / "stock-desk.spec"),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    if os_name == "windows":
        artifact = _build_windows(version, pyinstaller_dist / "stock-desk", output_dir)
    else:
        artifact = _build_macos(version, architecture, pyinstaller_dist, output_dir)
    checksum = _write_checksum(artifact)
    manifest = output_dir / f"stock-desk-{version}-{os_name}-{architecture}.json"
    manifest.write_text(
        json.dumps(
            {
                "architecture": architecture,
                "artifact": artifact.name,
                "os": os_name,
                "sha256": _sha256(artifact),
                "signed": bool(
                    os.environ.get("STOCK_DESK_WINDOWS_CERTIFICATE_BASE64")
                    or os.environ.get("STOCK_DESK_MACOS_SIGNING_IDENTITY")
                ),
                "version": version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the native Stock Desk installer"
    )
    parser.add_argument("version")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist" / "installers",
    )
    arguments = parser.parse_args(argv)
    artifact, checksum = build_installer(
        arguments.version, output_dir=arguments.output_dir
    )
    print(artifact)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
