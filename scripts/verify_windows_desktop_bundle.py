from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
from typing import Final, Protocol


SCHEMA_VERSION: Final = 1
ARTIFACT_KIND: Final = "windows-desktop-bundle"
WINDOWS_TARGET: Final = "x86_64-pc-windows-msvc"
HOST_EXE: Final = "stock-desk-desktop.exe"
# Tauri consumes the target-triple-suffixed externalBin source but strips that
# suffix from the installed payload name.  Verify the payload users actually
# receive, not the build-only source filename.
SIDECAR_EXE: Final = "stock-desk-sidecar.exe"
UNINSTALL_EXE: Final = "uninstall.exe"
WEBVIEW2_INSTALLERS: Final = frozenset(
    {
        "microsoftedgewebview2runtimeinstaller.exe",
        "microsoftedgewebview2runtimeinstallerx64.exe",
        "microsoftedgewebview2setup.exe",
    }
)
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
PE_X64_MACHINE: Final = 0x8664
HEX_40: Final = re.compile(r"[0-9a-f]{40}")
HEX_64: Final = re.compile(r"[0-9a-f]{64}")
PUBLIC_KEY: Final = re.compile(r"[A-Za-z0-9_.-]+")
FORBIDDEN_COMPONENTS: Final = frozenset(
    {
        ".git",
        ".github",
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
FORBIDDEN_NAMES: Final = frozenset(
    {
        ".gitignore",
        "cargo.lock",
        "cargo.toml",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "rust-toolchain.toml",
        "uv.lock",
    }
)
FORBIDDEN_SUFFIXES: Final = (
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".js.map",
    ".map",
    ".py",
    ".pyc",
    ".pyo",
    ".rs",
    ".spec.js",
    ".spec.ts",
    ".test.js",
    ".test.ts",
    ".toml",
    ".ts",
    ".tsx",
)


class BundleVerificationError(ValueError):
    """The Windows desktop payload cannot be trusted for publication."""


@dataclass(frozen=True)
class Limits:
    max_files: int = 50_000
    max_file_size: int = 512 * 1024 * 1024
    max_total_size: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_file_size < 1 or self.max_total_size < 1:
            raise ValueError("bundle limits must be positive")


@dataclass(frozen=True)
class PeMetadata:
    timestamp_offset: int
    checksum_offset: int
    timestamp: int
    checksum: int
    signed: bool


@dataclass(frozen=True)
class SignatureIdentity:
    valid: bool
    subject: str


class SignatureVerifier(Protocol):
    def __call__(self, path: Path) -> SignatureIdentity: ...


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def manifest_digest(manifest: Mapping[str, object]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def is_reparse_point(metadata: object) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def hash_regular_file(path: Path, *, expected_lstat: os.stat_result) -> tuple[str, int]:
    if (
        stat.S_ISLNK(expected_lstat.st_mode)
        or is_reparse_point(expected_lstat)
        or not stat.S_ISREG(expected_lstat.st_mode)
    ):
        raise BundleVerificationError(
            f"symlink or reparse payload is forbidden: {path.name}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BundleVerificationError(
            f"cannot safely open payload file: {path.name}"
        ) from error
    try:
        before = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(expected_lstat):
            raise BundleVerificationError(
                f"payload changed before hashing: {path.name}"
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        if _stat_identity(after) != _stat_identity(before):
            raise BundleVerificationError(f"payload changed while hashing: {path.name}")
        return digest.hexdigest(), before.st_size
    finally:
        os.close(descriptor)


def parse_pe_x64(payload: bytes, *, label: str) -> PeMetadata:
    try:
        if len(payload) < 64 or payload[:2] != b"MZ":
            raise BundleVerificationError(f"{label} is not a valid PE x64 binary")
        pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
        if pe_offset < 64 or pe_offset + 24 > len(payload):
            raise BundleVerificationError(f"{label} is not a valid PE x64 binary")
        if payload[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise BundleVerificationError(f"{label} is not a valid PE x64 binary")
        machine = struct.unpack_from("<H", payload, pe_offset + 4)[0]
        optional_size = struct.unpack_from("<H", payload, pe_offset + 20)[0]
        optional = pe_offset + 24
        if machine != PE_X64_MACHINE or optional_size < 152:
            raise BundleVerificationError(f"{label} is not a valid PE x64 binary")
        if optional + optional_size > len(payload):
            raise BundleVerificationError(f"{label} has a truncated PE optional header")
        if struct.unpack_from("<H", payload, optional)[0] != 0x20B:
            raise BundleVerificationError(f"{label} is not a valid PE x64 binary")
        timestamp_offset = pe_offset + 8
        checksum_offset = optional + 64
        security_directory = optional + 112 + (4 * 8)
        certificate_offset, certificate_size = struct.unpack_from(
            "<II", payload, security_directory
        )
        signed = _validate_certificate_table(
            payload,
            offset=certificate_offset,
            size=certificate_size,
            label=label,
            minimum_offset=optional + optional_size,
        )
        return PeMetadata(
            timestamp_offset=timestamp_offset,
            checksum_offset=checksum_offset,
            timestamp=struct.unpack_from("<I", payload, timestamp_offset)[0],
            checksum=struct.unpack_from("<I", payload, checksum_offset)[0],
            signed=signed,
        )
    except struct.error as error:
        raise BundleVerificationError(
            f"{label} is not a valid PE x64 binary"
        ) from error


def _validate_certificate_table(
    payload: bytes, *, offset: int, size: int, label: str, minimum_offset: int
) -> bool:
    if offset == 0 and size == 0:
        return False
    if (
        offset == 0
        or size == 0
        or offset % 8 != 0
        or offset < minimum_offset
        or size < 8
    ):
        raise BundleVerificationError(f"{label} has an invalid PE certificate table")
    end = offset + size
    if end < offset or end > len(payload):
        raise BundleVerificationError(
            f"{label} has an out-of-bounds PE certificate table"
        )
    cursor = offset
    while cursor < end:
        if cursor + 8 > end:
            raise BundleVerificationError(
                f"{label} has a truncated PE certificate entry"
            )
        length, revision, certificate_type = struct.unpack_from("<IHH", payload, cursor)
        if (
            length < 8
            or cursor + length > end
            or revision not in {0x0100, 0x0200}
            or certificate_type != 0x0002
        ):
            raise BundleVerificationError(
                f"{label} has an invalid PE certificate entry"
            )
        cursor += (length + 7) & ~7
    if cursor != end:
        raise BundleVerificationError(f"{label} has inconsistent PE certificate bounds")
    return True


def verify_windows_authenticode(path: Path) -> SignatureIdentity:
    if os.name != "nt":
        raise BundleVerificationError(
            "Authenticode verification requires Windows or an injected verifier"
        )
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        raise BundleVerificationError("Windows Authenticode verifier is unavailable")
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $args[0];"
        "[pscustomobject]@{status=[string]$s.Status;"
        "subject=[string]$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                os.fspath(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        raise BundleVerificationError(
            "Windows Authenticode verification failed"
        ) from error
    if not isinstance(result, dict):
        raise BundleVerificationError("Windows Authenticode returned an invalid result")
    status = result.get("status")
    subject = result.get("subject")
    if not isinstance(status, str) or not isinstance(subject, str):
        raise BundleVerificationError("Windows Authenticode returned an invalid result")
    return SignatureIdentity(valid=status == "Valid", subject=subject)


def is_microsoft_signer_subject(subject: str) -> bool:
    for relative_name in subject.split(","):
        key, separator, value = relative_name.partition("=")
        if not separator or key.strip().upper() not in {"CN", "O"}:
            continue
        normalized = " ".join(value.split()).casefold()
        if normalized == "microsoft corporation":
            return True
    return False


def read_pe_x64(
    path: Path, *, label: str, expected_sha256: str
) -> tuple[bytes, PeMetadata]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise BundleVerificationError(f"cannot read {label}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise BundleVerificationError(f"payload changed after hashing: {path.name}")
    return payload, parse_pe_x64(payload, label=label)


def _relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as error:
        raise BundleVerificationError(
            "optional artifact must be inside payload directory"
        ) from error
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != relative
    ):
        raise BundleVerificationError("payload path traversal is forbidden")
    return relative


def _assert_public_map(
    raw: Mapping[str, str], *, field: str, digest_values: bool
) -> dict[str, str]:
    if not raw:
        raise BundleVerificationError(f"{field} must not be empty")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if PUBLIC_KEY.fullmatch(key) is None or not value:
            raise BundleVerificationError(f"{field} contains an invalid identity")
        if digest_values:
            if HEX_64.fullmatch(value) is None:
                raise BundleVerificationError(f"{field}.{key} must be a SHA-256")
        elif (
            "\\" in value
            or "/Users/" in value
            or "/home/" in value
            or re.search(r"[A-Za-z]:[/\\]", value) is not None
        ):
            raise BundleVerificationError(f"{field}.{key} contains a private path")
        normalized[key] = value
    return dict(sorted(normalized.items()))


def _assert_safe_relative(relative: str) -> None:
    path = PurePosixPath(relative)
    components = {component.casefold() for component in path.parts}
    name = path.name.casefold()
    if (
        components & FORBIDDEN_COMPONENTS
        or name in FORBIDDEN_NAMES
        or name.endswith(FORBIDDEN_SUFFIXES)
    ):
        raise BundleVerificationError(
            f"forbidden source or development file: {relative}"
        )


def _role(relative: str, *, installer_relative: str | None) -> str:
    name = PurePosixPath(relative).name
    folded = name.casefold()
    if name == HOST_EXE:
        return "desktop-host"
    if name == SIDECAR_EXE:
        return "sidecar"
    if folded in WEBVIEW2_INSTALLERS:
        return "webview2-offline-installer"
    if folded == UNINSTALL_EXE:
        return "nsis-uninstaller"
    if relative == installer_relative:
        return "nsis-installer"
    if folded.endswith(".exe") or folded.endswith(
        (".com", ".bat", ".cmd", ".ps1", ".msi", ".scr", ".cpl")
    ):
        raise BundleVerificationError(f"unexpected executable in payload: {relative}")
    if folded.endswith(".dll"):
        return "runtime-library"
    return "runtime-resource"


def _walk_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    try:
        root_metadata = os.lstat(root)
    except OSError as error:
        raise BundleVerificationError("payload directory is unavailable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or is_reparse_point(root_metadata):
        raise BundleVerificationError(
            "payload directory cannot be a symlink or reparse point"
        )
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise BundleVerificationError("payload root must be a directory")
    discovered: list[tuple[Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise BundleVerificationError(
                "cannot enumerate payload directory"
            ) from error
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = os.lstat(path)
            except OSError as error:
                raise BundleVerificationError(
                    f"cannot inspect payload entry: {entry.name}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or is_reparse_point(metadata):
                raise BundleVerificationError(
                    f"symlink or reparse payload is forbidden: {entry.name}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                discovered.append((path, metadata))
            else:
                raise BundleVerificationError(
                    f"non-regular payload entry: {entry.name}"
                )
    return sorted(discovered, key=lambda item: item[0].relative_to(root).as_posix())


def verify_bundle(
    payload_root: Path,
    *,
    version: str,
    source_sha: str,
    toolchain: Mapping[str, str],
    locks: Mapping[str, str],
    installer: Path | None = None,
    sidecar: Path | None = None,
    limits: Limits = Limits(),
    signature_verifier: SignatureVerifier = verify_windows_authenticode,
) -> dict[str, object]:
    if (
        not version
        or "-" not in version
        or any(character in version for character in "\\/\x00")
    ):
        raise BundleVerificationError("release version is invalid")
    if HEX_40.fullmatch(source_sha) is None:
        raise BundleVerificationError("source_sha must be a lowercase 40-character SHA")
    safe_toolchain = _assert_public_map(
        toolchain, field="toolchain", digest_values=False
    )
    safe_locks = _assert_public_map(locks, field="locks", digest_values=True)
    root = payload_root.absolute()
    installer_relative = (
        _relative_path(root, installer.absolute()) if installer else None
    )
    sidecar_relative = _relative_path(root, sidecar.absolute()) if sidecar else None
    if (
        sidecar_relative is not None
        and PurePosixPath(sidecar_relative).name != SIDECAR_EXE
    ):
        raise BundleVerificationError(
            f"sidecar must use the exact target name {SIDECAR_EXE}"
        )

    files = _walk_files(root)
    if len(files) > limits.max_files:
        raise BundleVerificationError("payload exceeds file-count limit")
    records: list[dict[str, object]] = []
    total_size = 0
    roles: list[str] = []
    for path, metadata in files:
        relative = _relative_path(root, path)
        _assert_safe_relative(relative)
        role = _role(relative, installer_relative=installer_relative)
        if metadata.st_size > limits.max_file_size:
            raise BundleVerificationError(
                f"payload exceeds single-file limit: {relative}"
            )
        total_size += metadata.st_size
        if total_size > limits.max_total_size:
            raise BundleVerificationError("payload exceeds total-size limit")
        digest, size = hash_regular_file(path, expected_lstat=metadata)
        if role in {
            "desktop-host",
            "sidecar",
            "webview2-offline-installer",
            "nsis-installer",
        }:
            pe_payload, pe = read_pe_x64(path, label=role, expected_sha256=digest)
            if role == "webview2-offline-installer":
                if not pe.signed:
                    raise BundleVerificationError(
                        "WebView2 offline installer must carry an Authenticode signature"
                    )
                signature = signature_verifier(path)
                if not signature.valid or not is_microsoft_signer_subject(
                    signature.subject
                ):
                    raise BundleVerificationError(
                        "WebView2 offline installer Authenticode signer is not Microsoft"
                    )
            elif pe.signed:
                raise BundleVerificationError(
                    f"unsigned prerelease contains signed PE: {relative}"
                )
            if len(pe_payload) != size:
                raise BundleVerificationError(
                    f"payload changed while hashing: {relative}"
                )
        records.append({"path": relative, "size": size, "sha256": digest, "role": role})
        roles.append(role)

    for required in (
        "desktop-host",
        "sidecar",
        "webview2-offline-installer",
        "nsis-uninstaller",
    ):
        if roles.count(required) != 1:
            label = (
                "WebView2 offline payload"
                if required.startswith("webview2")
                else required
            )
            raise BundleVerificationError(f"payload must contain exactly one {label}")
    if sidecar_relative is not None and not any(
        record["path"] == sidecar_relative and record["role"] == "sidecar"
        for record in records
    ):
        raise BundleVerificationError("selected sidecar is absent from payload")
    if installer_relative is not None and roles.count("nsis-installer") != 1:
        raise BundleVerificationError("selected installer is absent from payload")

    installer_record = next(
        (record for record in records if record["role"] == "nsis-installer"), None
    )
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "artifact": ARTIFACT_KIND,
        "release": {
            "version": version,
            "channel": "prerelease",
            "signature": "unsigned",
        },
        "source_sha": source_sha,
        "toolchain": safe_toolchain,
        "locks": safe_locks,
        "files": records,
        "installer": installer_record,
        "sbom": {"status": "not-produced", "hook": "cyclonedx-reserved"},
    }
    manifest["manifest_sha256"] = manifest_digest(manifest)
    return validate_manifest(manifest)


def validate_manifest(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise BundleVerificationError("manifest must be an object")
    fields = {
        "schema_version",
        "artifact",
        "release",
        "source_sha",
        "toolchain",
        "locks",
        "files",
        "installer",
        "sbom",
        "manifest_sha256",
    }
    if set(raw) != fields:
        raise BundleVerificationError("manifest has unknown or missing fields")
    if raw["schema_version"] != SCHEMA_VERSION or raw["artifact"] != ARTIFACT_KIND:
        raise BundleVerificationError("manifest identity is invalid")
    release_raw = raw["release"]
    if (
        not isinstance(release_raw, dict)
        or set(release_raw) != {"version", "channel", "signature"}
        or release_raw.get("channel") != "prerelease"
        or release_raw.get("signature") != "unsigned"
    ):
        raise BundleVerificationError("release must be an unsigned prerelease")
    version = release_raw["version"]
    if not isinstance(version, str) or not version or "-" not in version:
        raise BundleVerificationError("release version is invalid")
    source_sha = raw["source_sha"]
    if not isinstance(source_sha, str) or HEX_40.fullmatch(source_sha) is None:
        raise BundleVerificationError("source_sha is invalid")
    for field, digest_values in (("toolchain", False), ("locks", True)):
        value = raw[field]
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise BundleVerificationError(f"{field} is invalid")
        if _assert_public_map(value, field=field, digest_values=digest_values) != value:
            raise BundleVerificationError(f"{field} is not canonical")
    files = raw["files"]
    if not isinstance(files, list) or not files:
        raise BundleVerificationError("files must be a non-empty array")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    valid_roles = {
        "desktop-host",
        "sidecar",
        "webview2-offline-installer",
        "nsis-uninstaller",
        "nsis-installer",
        "runtime-library",
        "runtime-resource",
    }
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "size",
            "sha256",
            "role",
        }:
            raise BundleVerificationError("file record has unknown or missing fields")
        path, size, digest, role = (
            record["path"],
            record["size"],
            record["sha256"],
            record["role"],
        )
        if not isinstance(path, str) or path in seen:
            raise BundleVerificationError("file path is invalid or duplicated")
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or pure.as_posix() != path
        ):
            raise BundleVerificationError("file path is not a normalized relative path")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BundleVerificationError("file size is invalid")
        if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
            raise BundleVerificationError("file digest is invalid")
        if role not in valid_roles:
            raise BundleVerificationError("file role is invalid")
        seen.add(path)
        normalized.append(dict(record))
    if normalized != sorted(normalized, key=lambda record: str(record["path"])):
        raise BundleVerificationError("files are not in canonical path order")
    roles = [record["role"] for record in normalized]
    for required in (
        "desktop-host",
        "sidecar",
        "webview2-offline-installer",
        "nsis-uninstaller",
    ):
        if roles.count(required) != 1:
            raise BundleVerificationError(f"manifest must bind exactly one {required}")
    host_record = next(
        record for record in normalized if record["role"] == "desktop-host"
    )
    sidecar_record = next(
        record for record in normalized if record["role"] == "sidecar"
    )
    webview_record = next(
        record
        for record in normalized
        if record["role"] == "webview2-offline-installer"
    )
    if PurePosixPath(str(host_record["path"])).name != HOST_EXE:
        raise BundleVerificationError("desktop host record has an invalid target name")
    if PurePosixPath(str(sidecar_record["path"])).name != SIDECAR_EXE:
        raise BundleVerificationError("sidecar record has an invalid target name")
    if (
        PurePosixPath(str(webview_record["path"])).name.casefold()
        not in WEBVIEW2_INSTALLERS
    ):
        raise BundleVerificationError("WebView2 record has an invalid target name")
    uninstaller_record = next(
        record for record in normalized if record["role"] == "nsis-uninstaller"
    )
    if PurePosixPath(str(uninstaller_record["path"])).name.casefold() != UNINSTALL_EXE:
        raise BundleVerificationError("NSIS uninstaller has an invalid target name")
    installer = raw["installer"]
    matching = [record for record in normalized if record["role"] == "nsis-installer"]
    if (
        installer is None
        and matching
        or installer is not None
        and matching != [installer]
    ):
        raise BundleVerificationError("installer record does not bind the NSIS payload")
    if raw["sbom"] != {"status": "not-produced", "hook": "cyclonedx-reserved"}:
        raise BundleVerificationError("SBOM hook must not claim a fabricated SBOM")
    digest = raw["manifest_sha256"]
    if not isinstance(digest, str) or digest != manifest_digest(raw):
        raise BundleVerificationError("manifest SHA-256 is invalid")
    return raw


def _key_value(raw: str) -> tuple[str, str]:
    key, separator, value = raw.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    return key, value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an unpacked Windows desktop bundle"
    )
    parser.add_argument("payload", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--toolchain", action="append", type=_key_value, required=True)
    parser.add_argument("--lock", action="append", type=_key_value, required=True)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        manifest = verify_bundle(
            arguments.payload,
            version=arguments.version,
            source_sha=arguments.source_sha,
            toolchain=dict(arguments.toolchain),
            locks=dict(arguments.lock),
            installer=arguments.installer,
            sidecar=arguments.sidecar,
        )
        output = canonical_json(manifest)
        if arguments.output is None:
            sys.stdout.buffer.write(output)
        else:
            arguments.output.write_bytes(output)
    except (BundleVerificationError, OSError) as error:
        print(f"windows bundle verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
