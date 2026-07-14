"""Fail-closed verification of real stable-updater artifacts and attestations."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO, Final, Iterator, TypedDict
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT: Final = Path(__file__).resolve().parents[1]
TRUSTED_TAURI_PUBLIC_KEY: Final = ROOT / "config/tauri-updater-public-key.pub"
_SCHEMA: Final = "stock-desk-trusted-updater-v1"
_TARGET: Final = "windows-x86_64-nsis"
_ARCH: Final = "x86_64"
_REPOSITORY: Final = "CongBao/stock-desk"
_SOURCE_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_VERSION: Final = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
TRUSTED_UPDATER_METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "channel",
        "version",
        "target",
        "arch",
        "source_sha",
        "url",
        "sha256",
        "signature",
    }
)
TRUSTED_UPDATER_METADATA_MAX_BYTES: Final = 32 * 1024


class TrustedUpdaterReleaseError(ValueError):
    """Raised when any real artifact or trusted evidence is unavailable."""


class TrustedUpdaterDecision(TypedDict):
    eligible: bool
    channel: str
    version: str
    target: str
    payload_sha256: str
    verified_installer_path: str


class AuthenticodeEvidence(TypedDict):
    signer_subject: str
    certificate_thumbprint: str
    timestamp_subject: str


class EvidencePaths(TypedDict):
    installer_attestation: Path
    signpath_receipt: Path
    signpath_attestation: Path
    windows_10_receipt: Path
    windows_10_attestation: Path
    windows_11_receipt: Path
    windows_11_attestation: Path


@dataclass(frozen=True)
class StagedInstaller:
    path: Path
    stream: BinaryIO
    sha256: str
    initial_stat: os.stat_result


def evaluate_trusted_updater_release(
    *,
    metadata_path: Path,
    installer_path: Path,
    verified_installer_path: Path,
    signature_path: Path,
    evidence: EvidencePaths,
    expected_version: str,
    source_sha: str,
) -> TrustedUpdaterDecision:
    """Verify bytes, signatures, Windows trust, and exact-SHA attestations.

    Protected signing secrets are intentionally not inputs. Their presence can
    authorize a signing job, but can never prove that an output is trustworthy.
    """

    _require_version(expected_version)
    _require_source_sha(source_sha)
    metadata = _read_exact_json(
        metadata_path,
        "updater metadata",
        limit=TRUSTED_UPDATER_METADATA_MAX_BYTES,
    )
    _require_exact_keys(
        metadata,
        set(TRUSTED_UPDATER_METADATA_FIELDS),
        "updater metadata",
    )
    if metadata["schema_version"] != _SCHEMA or metadata["channel"] != "stable":
        raise TrustedUpdaterReleaseError("only the trusted stable schema is allowed")
    if metadata["version"] != expected_version:
        raise TrustedUpdaterReleaseError(
            "release version is not the exact stable version"
        )
    if metadata["target"] != _TARGET or metadata["arch"] != _ARCH:
        raise TrustedUpdaterReleaseError(
            "release architecture or target is unsupported"
        )
    if metadata["source_sha"] != source_sha:
        raise TrustedUpdaterReleaseError("metadata source is not the exact commit")

    expected_url = (
        "https://github.com/CongBao/stock-desk/releases/download/"
        f"v{expected_version}/stock-desk-{expected_version}-windows-x64-setup.exe"
    )
    if metadata["url"] != expected_url or not _is_strict_https_url(expected_url):
        raise TrustedUpdaterReleaseError("updater asset URL is not repository-confined")

    expected_name = f"stock-desk-{expected_version}-windows-x64-setup.exe"
    if verified_installer_path.name != expected_name:
        raise TrustedUpdaterReleaseError(
            "verified installer output must use the published asset filename"
        )
    with _stage_installer(installer_path, verified_installer_path) as staged:
        payload_digest = staged.sha256
        if metadata["sha256"] != payload_digest:
            raise TrustedUpdaterReleaseError(
                "metadata digest does not match installer bytes"
            )
        signature_text = _read_text(signature_path, "Tauri signature", 16_384)
        if metadata["signature"] != signature_text:
            raise TrustedUpdaterReleaseError(
                "metadata signature differs from signature artifact"
            )
        _verify_minisign(
            payload=staged.stream,
            signature_text=signature_text,
            public_key_path=TRUSTED_TAURI_PUBLIC_KEY,
        )

        authenticode = _verify_authenticode(staged.path)
        _verify_gh_attestation(
            staged.path,
            evidence["installer_attestation"],
            _REPOSITORY,
            source_sha,
            ".github/workflows/signpath.yml",
        )
        _verify_signpath_receipt(
            evidence["signpath_receipt"],
            source_sha,
            payload_digest,
            authenticode,
        )
        _verify_gh_attestation(
            evidence["signpath_receipt"],
            evidence["signpath_attestation"],
            _REPOSITORY,
            source_sha,
            ".github/workflows/signpath.yml",
        )
        for platform, receipt_key, attestation_key in (
            (
                "windows_10_22h2_x64",
                "windows_10_receipt",
                "windows_10_attestation",
            ),
            ("windows_11_x64", "windows_11_receipt", "windows_11_attestation"),
        ):
            receipt_path = evidence[receipt_key]  # type: ignore[literal-required]
            attestation_path = evidence[attestation_key]  # type: ignore[literal-required]
            _verify_windows_receipt(receipt_path, platform, source_sha, payload_digest)
            _verify_gh_attestation(
                receipt_path,
                attestation_path,
                _REPOSITORY,
                source_sha,
                ".github/workflows/windows-installed.yml",
            )

        return TrustedUpdaterDecision(
            eligible=True,
            channel="stable",
            version=expected_version,
            target=_TARGET,
            payload_sha256=payload_digest,
            verified_installer_path=str(verified_installer_path.resolve()),
        )


def _verify_minisign(
    *, payload: BinaryIO, signature_text: str, public_key_path: Path
) -> None:
    """Verify the exact Tauri/minisign artifact using one fixed public key."""

    public_key_lines = _read_text(
        public_key_path, "trusted Tauri public key", 4096
    ).splitlines()
    signature_lines = signature_text.splitlines()
    if len(public_key_lines) != 2 or len(signature_lines) != 4:
        raise TrustedUpdaterReleaseError("Tauri signing material has invalid framing")
    try:
        public_packet = base64.b64decode(public_key_lines[1], validate=True)
        signature_packet = base64.b64decode(signature_lines[1], validate=True)
        global_signature = base64.b64decode(signature_lines[3], validate=True)
    except (ValueError, binascii.Error) as error:
        raise TrustedUpdaterReleaseError(
            "Tauri signing material is not valid base64"
        ) from error
    if (
        len(public_packet) != 42
        or len(signature_packet) != 74
        or len(global_signature) != 64
        or public_packet[:2] not in {b"Ed", b"ED"}
        or signature_packet[:2] != b"ED"
        or public_packet[2:10] != signature_packet[2:10]
        or not signature_lines[2].startswith("trusted comment: ")
    ):
        raise TrustedUpdaterReleaseError("Tauri signature identity is invalid")
    message = _blake2b_stream(payload)
    key = Ed25519PublicKey.from_public_bytes(public_packet[10:])
    try:
        key.verify(signature_packet[10:], message)
        trusted_comment = signature_lines[2][len("trusted comment: ") :].encode()
        key.verify(global_signature, signature_packet[10:] + trusted_comment)
    except InvalidSignature as error:
        raise TrustedUpdaterReleaseError("Tauri signature does not verify") from error


def _verify_authenticode(installer_path: Path) -> AuthenticodeEvidence:
    """Invoke Windows trust verification over the actual installer bytes."""

    if sys.platform != "win32":
        raise TrustedUpdaterReleaseError(
            "WinVerifyTrust is unavailable on this platform; release remains blocked"
        )
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=Get-AuthenticodeSignature -LiteralPath $args[0];"
        "[ordered]@{verifier='WinVerifyTrust';status=[string]$s.Status;"
        "subject=[string]$s.SignerCertificate.Subject;"
        "thumbprint=[string]$s.SignerCertificate.Thumbprint;"
        "timestamp_subject=[string]$s.TimeStamperCertificate.Subject}"
        "|ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(installer_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise TrustedUpdaterReleaseError("WinVerifyTrust execution failed")
    try:
        record = json.loads(result.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise TrustedUpdaterReleaseError("WinVerifyTrust output is invalid") from error
    if (
        not isinstance(record, Mapping)
        or set(record)
        != {"verifier", "status", "subject", "thumbprint", "timestamp_subject"}
        or record["verifier"] != "WinVerifyTrust"
        or record["status"] != "Valid"
        or not _safe_nonempty(record["subject"], 512)
        or not isinstance(record["thumbprint"], str)
        or re.fullmatch(r"[0-9A-F]{40,64}", record["thumbprint"]) is None
        or not _safe_nonempty(record["timestamp_subject"], 512)
    ):
        raise TrustedUpdaterReleaseError("installer Authenticode trust is not valid")
    return AuthenticodeEvidence(
        signer_subject=record["subject"],
        certificate_thumbprint=record["thumbprint"],
        timestamp_subject=record["timestamp_subject"],
    )


def _verify_signpath_receipt(
    path: Path,
    source_sha: str,
    payload_digest: str,
    authenticode: AuthenticodeEvidence,
) -> None:
    record = _read_exact_json(path, "SignPath receipt")
    _require_exact_keys(
        record,
        {
            "schema",
            "status",
            "source_sha",
            "payload_sha256",
            "request_id",
            "signer_subject",
            "certificate_thumbprint",
            "timestamp_subject",
        },
        "SignPath receipt",
    )
    if record != {
        "schema": "stock-desk-signpath-receipt-v1",
        "status": "signed",
        "source_sha": source_sha,
        "payload_sha256": payload_digest,
        "request_id": record["request_id"],
        "signer_subject": authenticode["signer_subject"],
        "certificate_thumbprint": authenticode["certificate_thumbprint"],
        "timestamp_subject": authenticode["timestamp_subject"],
    } or not _safe_nonempty(record["request_id"], 256):
        raise TrustedUpdaterReleaseError("SignPath receipt is not exact-SHA bound")


def _verify_windows_receipt(
    path: Path, platform: str, source_sha: str, payload_digest: str
) -> None:
    record = _read_exact_json(path, "Windows receipt")
    expected = {
        "schema": "stock-desk-windows-trust-receipt-v1",
        "platform": platform,
        "source_sha": source_sha,
        "payload_sha256": payload_digest,
        "verifier": "WinVerifyTrust",
        "authenticode_status": "Valid",
        "standard_user_install": "passed",
    }
    if record != expected:
        raise TrustedUpdaterReleaseError(
            "Windows receipt is not exact-SHA trust evidence"
        )


def _verify_gh_attestation(
    subject: Path,
    bundle: Path,
    repository: str,
    source_sha: str,
    signer_workflow: str,
) -> None:
    _require_regular_file(bundle, "GitHub attestation bundle")
    command = [
        "gh",
        "attestation",
        "verify",
        str(subject),
        "--bundle",
        str(bundle),
        "--repo",
        repository,
        "--source-digest",
        source_sha,
        "--source-ref",
        "refs/heads/main",
        "--signer-digest",
        source_sha,
        "--signer-workflow",
        f"{repository}/{signer_workflow}",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=90
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TrustedUpdaterReleaseError(
            "GitHub attestation verification unavailable"
        ) from error
    if result.returncode != 0:
        raise TrustedUpdaterReleaseError("GitHub attestation verification failed")
    try:
        verified = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TrustedUpdaterReleaseError(
            "GitHub attestation output is invalid"
        ) from error
    if not isinstance(verified, list) or not verified:
        raise TrustedUpdaterReleaseError(
            "GitHub attestation returned no trusted result"
        )


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    try:
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        stream.seek(0)
    except OSError as error:
        raise TrustedUpdaterReleaseError("staged installer is unreadable") from error
    return digest.hexdigest()


def _blake2b_stream(stream: BinaryIO) -> bytes:
    digest = hashlib.blake2b(digest_size=64)
    try:
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        stream.seek(0)
    except OSError as error:
        raise TrustedUpdaterReleaseError("staged installer is unreadable") from error
    return digest.digest()


def _open_locked_staged_installer(path: Path) -> BinaryIO:
    """Open and cooperatively lock the POSIX verifier object."""

    if sys.platform == "win32":
        raise TrustedUpdaterReleaseError(
            "Windows staged ownership cannot be reacquired by path"
        )
    stream = path.open("rb")
    try:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError) as error:
        stream.close()
        raise TrustedUpdaterReleaseError(
            "staged installer could not be locked"
        ) from error
    return stream


def _create_staged_installer_file(parent: Path) -> tuple[Path, BinaryIO, bool]:
    if sys.platform != "win32":
        descriptor, name = tempfile.mkstemp(
            prefix=".stock-desk-verified-", suffix=".exe", dir=parent
        )
        return Path(name), os.fdopen(descriptor, "w+b"), False

    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    for _attempt in range(64):
        path = parent / f".stock-desk-verified-{secrets.token_hex(16)}.exe"
        handle = create_file(
            str(path),
            # GENERIC_READ | GENERIC_WRITE | DELETE | FILE_WRITE_ATTRIBUTES.
            0x80000000 | 0x40000000 | 0x00010000 | 0x00000100,
            0x00000001 | 0x00000004,  # share read/delete, deny external writes
            None,
            1,  # CREATE_NEW: never adopt an attacker-created path
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() in {80, 183}:  # file/already exists
                continue
            raise TrustedUpdaterReleaseError("staged installer could not be created")
        try:
            descriptor = msvcrt.open_osfhandle(
                handle, os.O_RDWR | getattr(os, "O_BINARY", 0)
            )
        except OSError as error:
            close_handle(handle)
            raise TrustedUpdaterReleaseError(
                "staged installer handle could not be created"
            ) from error
        return path, os.fdopen(descriptor, "w+b"), True
    raise TrustedUpdaterReleaseError("unique staged installer path unavailable")


def _duplicate_windows_verifier_stream(stream: BinaryIO) -> BinaryIO:
    """Derive a read stream for the same owned object without reopening a path."""

    if sys.platform != "win32":
        raise OSError("Windows handle duplication is unavailable")

    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_process = kernel32.GetCurrentProcess
    get_process.argtypes = []
    get_process.restype = ctypes.c_void_p
    duplicate = kernel32.DuplicateHandle
    duplicate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    duplicate.restype = ctypes.c_int
    process = get_process()
    duplicated = ctypes.c_void_p()
    source = msvcrt.get_osfhandle(stream.fileno())
    if (
        duplicate(
            process,
            source,
            process,
            ctypes.byref(duplicated),
            0x80000000 | 0x00010000 | 0x00000100,
            0,
            0,
        )
        == 0
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "staged installer handle could not be duplicated")
    if duplicated.value is None:
        raise OSError("staged installer duplicate handle is unavailable")
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    try:
        descriptor = msvcrt.open_osfhandle(
            duplicated.value, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError:
        close_handle(duplicated)
        raise
    return os.fdopen(descriptor, "rb")


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
    )


def _sha256_descriptor(descriptor: int) -> str:
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb") as stream:
            duplicate = -1
            return _sha256_stream(stream)
    finally:
        if duplicate >= 0:
            os.close(duplicate)


def _source_matches(
    descriptor: int,
    initial: os.stat_result,
    path_identity: os.stat_result,
    expected_sha256: str,
) -> bool:
    return (
        (initial.st_dev, initial.st_ino) != (0, 0)
        and _same_file_object(initial, os.fstat(descriptor))
        and _same_file_object(initial, path_identity)
        and _sha256_descriptor(descriptor) == expected_sha256
    )


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        raise OSError("directory fsync is not a supported Windows publication path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_staged_installer(temporary: Path, output: Path) -> None:
    """Publish without replacement, using the platform's durable primitive."""

    if sys.platform != "win32":
        os.link(temporary, output, follow_symlinks=False)
        return

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    # The paths share a parent and therefore a volume. Omitting
    # MOVEFILE_REPLACE_EXISTING makes an attacker-created destination fail
    # closed. MOVEFILE_WRITE_THROUGH makes the supported Win32 move complete
    # on disk before this call returns.
    if move_file(str(temporary), str(output), 0x00000008) == 0:
        error = ctypes.get_last_error()
        raise OSError(error, "write-through installer publication failed")


def _unlink_readonly(path: Path) -> None:
    for attempt in range(2):
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except FileNotFoundError:
            return
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 1:
                raise
            _clear_windows_readonly_attribute(path)


def _revoke_open_windows_file(stream: BinaryIO) -> None:
    """Clear readonly and delete the staged object through its locked handle."""

    if sys.platform != "win32":
        raise OSError("open-handle revocation is only supported on Windows")
    _set_open_windows_file_attributes(stream, 0x00000080)  # FILE_ATTRIBUTE_NORMAL

    import ctypes
    import msvcrt

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    handle = msvcrt.get_osfhandle(stream.fileno())
    disposition = FileDispositionInfo(delete_file=1)
    if (
        set_information(
            handle,
            4,  # FileDispositionInfo
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
        == 0
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "staged installer could not be revoked by handle")


def _set_open_windows_file_attributes(stream: BinaryIO, attributes: int) -> None:
    """Change attributes on the owned object, never through a mutable path."""

    if sys.platform != "win32":
        raise OSError("open-handle attributes are only supported on Windows")

    import ctypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_int64),
            ("last_access_time", ctypes.c_int64),
            ("last_write_time", ctypes.c_int64),
            ("change_time", ctypes.c_int64),
            ("file_attributes", ctypes.c_uint32),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    handle = msvcrt.get_osfhandle(stream.fileno())
    basic = FileBasicInfo(file_attributes=attributes)
    if set_information(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)) == 0:
        error = ctypes.get_last_error()
        raise OSError(error, "staged installer attributes could not be changed")


def _clear_windows_readonly_attribute(path: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL(  # type: ignore[attr-defined]
        "kernel32", use_last_error=True
    )
    set_attributes = kernel32.SetFileAttributesW
    set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    set_attributes.restype = ctypes.c_int
    if set_attributes(str(path), 0x00000080) == 0:  # FILE_ATTRIBUTE_NORMAL
        raise OSError("staged installer cleanup attributes could not be reset")


@contextmanager
def _stage_installer(source: Path, verified_output: Path) -> Iterator[StagedInstaller]:
    """Copy once into a verifier-owned object and publish that exact object."""

    if not verified_output.is_absolute():
        raise TrustedUpdaterReleaseError("verified installer output must be absolute")
    parent = verified_output.parent
    if not parent.is_dir() or parent.is_symlink() or verified_output.exists():
        raise TrustedUpdaterReleaseError("verified installer output is unsafe")
    _require_regular_file(source, "installer")

    source_fd = -1
    temporary_path: Path | None = None
    staged_stream: BinaryIO | None = None
    windows_owned_handle = False
    output_linked = False
    verified = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode):
            raise TrustedUpdaterReleaseError("installer is missing or unsafe")

        (
            temporary_path,
            staged_stream,
            windows_owned_handle,
        ) = _create_staged_installer_file(parent)
        created_stat = os.fstat(staged_stream.fileno())
        temporary_file_id = (created_stat.st_dev, created_stat.st_ino)
        if temporary_file_id == (0, 0):
            raise TrustedUpdaterReleaseError("staged installer identity is unavailable")
        source_stream = os.fdopen(source_fd, "rb", closefd=False)
        digest = hashlib.sha256()
        try:
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                staged_stream.write(chunk)
                digest.update(chunk)
        finally:
            source_stream.close()
        staged_stream.flush()
        os.fsync(staged_stream.fileno())

        source_digest = digest.hexdigest()
        try:
            source_path_after = source.stat(follow_symlinks=False)
            source_is_unchanged = _source_matches(
                source_fd,
                source_before,
                source_path_after,
                source_digest,
            )
        except OSError as error:
            raise TrustedUpdaterReleaseError(
                "installer source changed while it was staged"
            ) from error
        if not source_is_unchanged:
            raise TrustedUpdaterReleaseError(
                "installer source changed while it was staged"
            )

        if sys.platform == "win32":
            _set_open_windows_file_attributes(
                staged_stream, 0x00000001
            )  # FILE_ATTRIBUTE_READONLY
            verifier_stream = _duplicate_windows_verifier_stream(staged_stream)
            verifier_stat = os.fstat(verifier_stream.fileno())
            writer_stat = os.fstat(staged_stream.fileno())
            if (
                (writer_stat.st_dev, writer_stat.st_ino) != temporary_file_id
                or (verifier_stat.st_dev, verifier_stat.st_ino) != temporary_file_id
                or not _same_file_object(writer_stat, verifier_stat)
            ):
                verifier_stream.close()
                raise TrustedUpdaterReleaseError(
                    "staged installer handle identity changed"
                )
            writer_stream = staged_stream
            staged_stream = verifier_stream
            writer_stream.close()
        else:
            os.chmod(temporary_path, stat.S_IRUSR)
            staged_stream.close()
            staged_stream = _open_locked_staged_installer(temporary_path)
        initial_stat = os.fstat(staged_stream.fileno())
        staged_stream.seek(0)
        staged = StagedInstaller(
            path=temporary_path,
            stream=staged_stream,
            sha256=source_digest,
            initial_stat=initial_stat,
        )
        yield staged

        try:
            source_path_final = source.stat(follow_symlinks=False)
            source_is_unchanged = _source_matches(
                source_fd,
                source_before,
                source_path_final,
                source_digest,
            )
        except OSError as error:
            raise TrustedUpdaterReleaseError(
                "installer source changed during verification"
            ) from error
        if not source_is_unchanged:
            raise TrustedUpdaterReleaseError(
                "installer source changed during verification"
            )
        path_after = temporary_path.stat(follow_symlinks=False)
        handle_after = os.fstat(staged_stream.fileno())
        if (
            not _same_file_object(initial_stat, path_after)
            or not _same_file_object(initial_stat, handle_after)
            or _sha256_stream(staged_stream) != staged.sha256
        ):
            raise TrustedUpdaterReleaseError(
                "staged installer changed during verification"
            )
        if verified_output.exists():
            raise TrustedUpdaterReleaseError(
                "verified installer output appeared during verification"
            )
        _publish_staged_installer(temporary_path, verified_output)
        output_linked = True
        published_stat = verified_output.stat(follow_symlinks=False)
        locked_stat = os.fstat(staged_stream.fileno())
        if not _same_file_object(locked_stat, published_stat):
            raise TrustedUpdaterReleaseError(
                "published installer is not the verified staged object"
            )
        if sys.platform != "win32":
            _fsync_directory(parent)
        verified = True
    except TrustedUpdaterReleaseError:
        raise
    except OSError as error:
        raise TrustedUpdaterReleaseError(
            "installer could not be staged or published safely"
        ) from error
    finally:
        windows_cleanup_error: OSError | None = None
        if (
            sys.platform == "win32"
            and not verified
            and windows_owned_handle
            and staged_stream is not None
        ):
            try:
                _revoke_open_windows_file(staged_stream)
            except OSError as error:
                windows_cleanup_error = error
        if staged_stream is not None:
            staged_stream.close()
        if source_fd >= 0:
            os.close(source_fd)
        if sys.platform != "win32" and output_linked and not verified:
            _unlink_readonly(verified_output)
        if sys.platform != "win32" and temporary_path is not None:
            _unlink_readonly(temporary_path)
        if windows_cleanup_error is not None:
            raise TrustedUpdaterReleaseError(
                "installer publication could not be revoked safely"
            ) from windows_cleanup_error


def _read_text(path: Path, field: str, limit: int) -> str:
    _require_regular_file(path, field)
    try:
        if path.stat().st_size > limit:
            raise TrustedUpdaterReleaseError(f"{field} exceeds its size limit")
        return path.read_text(encoding="utf-8", errors="strict").rstrip("\r\n")
    except (OSError, UnicodeError) as error:
        raise TrustedUpdaterReleaseError(f"{field} is unreadable") from error


def _read_exact_json(
    path: Path, field: str, *, limit: int = 1024 * 1024
) -> Mapping[str, object]:
    payload = _read_text(path, field, limit)
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise TrustedUpdaterReleaseError(f"{field} is invalid JSON") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TrustedUpdaterReleaseError(f"{field} must be an object")
    return value


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_regular_file(path: Path, field: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise TrustedUpdaterReleaseError(f"{field} is missing or unsafe")


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], field: str
) -> None:
    if set(value) != expected:
        raise TrustedUpdaterReleaseError(f"{field} fields are incomplete or expanded")


def _require_version(value: str) -> None:
    if _VERSION.fullmatch(value) is None:
        raise TrustedUpdaterReleaseError("version must be exact X.Y.Z")


def _require_source_sha(value: str) -> None:
    if _SOURCE_SHA.fullmatch(value) is None:
        raise TrustedUpdaterReleaseError("source SHA must be an exact Git commit")


def _safe_nonempty(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.strip() == value
        and all(ord(character) >= 32 for character in value)
    )


def _is_strict_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify real Stock Desk stable updater artifacts and evidence."
    )
    parser.add_argument("metadata", type=Path)
    parser.add_argument("installer", type=Path)
    parser.add_argument("signature", type=Path)
    parser.add_argument("--verified-installer", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-sha", required=True)
    for name in EvidencePaths.__required_keys__:
        parser.add_argument(f"--{name.replace('_', '-')}", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        evidence = EvidencePaths(
            installer_attestation=options.installer_attestation,
            signpath_receipt=options.signpath_receipt,
            signpath_attestation=options.signpath_attestation,
            windows_10_receipt=options.windows_10_receipt,
            windows_10_attestation=options.windows_10_attestation,
            windows_11_receipt=options.windows_11_receipt,
            windows_11_attestation=options.windows_11_attestation,
        )
        evaluate_trusted_updater_release(
            metadata_path=options.metadata,
            installer_path=options.installer,
            verified_installer_path=options.verified_installer,
            signature_path=options.signature,
            evidence=evidence,
            expected_version=options.expected_version,
            source_sha=options.source_sha,
        )
    except TrustedUpdaterReleaseError as error:
        print(f"trusted updater release blocked: {error}")
        return 1
    # The in-process decision deliberately contains the verified installer path
    # for the release controller. Never echo that path (or any caller-derived
    # value) into shared CI logs; the exit code is the authoritative gate and
    # this closed public summary is sufficient for operators.
    print(
        json.dumps(
            {"channel": "stable", "eligible": True, "target": _TARGET},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
