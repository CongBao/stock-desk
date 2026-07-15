"""Fail-closed SignPath request, approval, and receipt contracts.

This module never approves a signing request.  It only proves that a request is
bound to protected ``main`` identities and that the result closes the exact
desktop-host, sidecar, and NSIS identities expected by the release pipeline.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any, BinaryIO, Final, Iterator, TypedDict


_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_THUMBPRINT: Final = re.compile(r"^[0-9A-F]{40}$")
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_KNOWN_STATES: Final = frozenset(
    {
        "application-submitted",
        "pending-review",
        "approved",
        "integrated",
        "SmartScreen-verified",
    }
)
_REQUIRED_SECRETS: Final = (
    "SIGNPATH_API_TOKEN",
    "SIGNPATH_ORGANIZATION_ID",
    "SIGNPATH_PROJECT_SLUG",
    "SIGNPATH_SIGNING_POLICY_SLUG",
    "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG",
    "SIGNPATH_POLICY_TOKEN",
)
REQUIRED_ROLES: Final = frozenset({"desktop-host", "sidecar", "nsis-installer"})
_ROLE_FILENAMES: Final = {
    "desktop-host": "stock-desk-desktop.exe",
    "sidecar": "stock-desk-sidecar.exe",
    "nsis-installer": "stock-desk-unsigned-nsis.exe",
}
_ENVIRONMENT: Final = "release-signing"


class SignPathContractError(ValueError):
    """The signing boundary is incomplete, mutable, or not approved."""


class SigningDecision(TypedDict):
    enabled: bool
    reason: str
    status: str


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SignPathContractError(f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SignPathContractError(f"{field} must be an array")
    return value


def _safe_text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise SignPathContractError(f"{field} is invalid")
    return value


def _require_sha(value: str, field: str) -> str:
    if _SHA.fullmatch(value) is None:
        raise SignPathContractError(f"{field} must be an exact lowercase Git SHA")
    return value


def _require_digest(value: str, field: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise SignPathContractError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _read_json(path: Path, field: str) -> object:
    try:
        return json.loads(_read_regular_file(path, field).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SignPathContractError(
            f"{field} is not readable canonical JSON"
        ) from error


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))


def _open_windows_non_reparse(path: Path, field: str) -> int:
    """Open the final Windows path component without following a reparse point."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(get_last_error(), f"could not open {field}")
    info = FileAttributeTagInfo()
    # FileAttributeTagInfo is FILE_INFO_BY_HANDLE_CLASS value 9.
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = get_last_error()
        close_handle(handle)
        raise OSError(error, f"could not inspect {field}")
    if info.file_attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
        close_handle(handle)
        raise SignPathContractError(f"{field} must not be a Windows reparse point")
    if info.file_attributes & 0x00000010:  # FILE_ATTRIBUTE_DIRECTORY
        close_handle(handle)
        raise SignPathContractError(f"{field} must be a regular non-link file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
        return int(open_osfhandle(int(handle), flags))
    except OSError:
        close_handle(handle)
        raise


@contextmanager
def _open_regular_file(path: Path, field: str) -> Iterator[BinaryIO]:
    """Yield one stable descriptor and reject links/reparse points atomically."""
    try:
        if os.name == "nt":
            descriptor = _open_windows_non_reparse(path, field)
        else:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise SignPathContractError(
                    f"{field} cannot be opened without link traversal"
                )
            descriptor = os.open(
                path,
                os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            )
    except SignPathContractError:
        raise
    except OSError as error:
        raise SignPathContractError(f"{field} is missing or unsafe") from error

    stream: BinaryIO | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SignPathContractError(f"{field} must be a regular non-link file")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
        after = os.fstat(stream.fileno())
        try:
            path_after = os.lstat(path)
        except OSError as error:
            raise SignPathContractError(
                f"{field} path changed while being read"
            ) from error
        if (
            stat.S_ISLNK(path_after.st_mode)
            or _is_reparse(path_after)
            or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise SignPathContractError(f"{field} path changed while being read")
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise SignPathContractError(f"{field} changed while being read")
    except OSError as error:
        raise SignPathContractError(f"{field} could not be read safely") from error
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _hash_regular_file(path: Path, field: str) -> str:
    digest = hashlib.sha256()
    with _open_regular_file(path, field) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_regular_file(path: Path, field: str) -> bytes:
    with _open_regular_file(path, field) as stream:
        return stream.read()


def _pe_authenticode_identity(
    path: Path, field: str, *, require_signature: bool
) -> tuple[str, str, str]:
    """Return raw and Authenticode-normalized SHA-256 identities for one PE.

    The normalization follows the PE Authenticode exclusions: checksum, security
    directory entry, and the terminal WIN_CERTIFICATE table.  No other byte may
    change.  Requiring a well-formed terminal table also prevents an attacker from
    hiding a substituted payload behind a security-directory pointer.
    """
    data = _read_regular_file(path, field)
    raw_digest = hashlib.sha256(data).hexdigest()
    try:
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise SignPathContractError(f"{field} is not a PE executable")
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise SignPathContractError(f"{field} has an invalid PE header")
        section_count = struct.unpack_from("<H", data, pe_offset + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
        optional = pe_offset + 24
        optional_end = optional + optional_size
        if optional_end > len(data):
            raise SignPathContractError(f"{field} has a truncated optional header")
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic == 0x10B:
            number_offset, directory_offset = optional + 92, optional + 96
        elif magic == 0x20B:
            number_offset, directory_offset = optional + 108, optional + 112
        else:
            raise SignPathContractError(f"{field} has an unsupported PE format")
        checksum_offset = optional + 64
        security_entry = directory_offset + (4 * 8)
        if security_entry + 8 > optional_end or number_offset + 4 > optional_end:
            raise SignPathContractError(f"{field} lacks the PE security directory")
        directory_count = struct.unpack_from("<I", data, number_offset)[0]
        if directory_count < 5:
            raise SignPathContractError(f"{field} lacks the PE security directory")
        certificate_offset, certificate_size = struct.unpack_from(
            "<II", data, security_entry
        )
        section_table_end = optional_end + (section_count * 40)
        if section_count == 0 or section_table_end > len(data):
            raise SignPathContractError(f"{field} has an invalid PE section table")
        image_end = section_table_end
        for index in range(section_count):
            section = optional_end + (index * 40)
            raw_size, raw_offset = struct.unpack_from("<II", data, section + 16)
            section_end = raw_offset + raw_size
            if section_end > len(data):
                raise SignPathContractError(f"{field} has a truncated PE section")
            image_end = max(image_end, section_end)
    except struct.error as error:
        raise SignPathContractError(f"{field} has a truncated PE header") from error

    has_signature = certificate_offset != 0 or certificate_size != 0
    if require_signature and not has_signature:
        raise SignPathContractError(f"{field} lacks an Authenticode certificate table")
    if not require_signature and has_signature:
        raise SignPathContractError(f"{field} is not an unsigned PE executable")

    certificate_end = certificate_offset + certificate_size
    if has_signature:
        if (
            certificate_offset % 8 != 0
            or certificate_size < 8
            or certificate_offset < image_end
            or certificate_end != len(data)
        ):
            raise SignPathContractError(
                f"{field} has an unsafe Authenticode certificate table"
            )
        cursor = certificate_offset
        while cursor < certificate_end:
            if cursor + 8 > certificate_end:
                raise SignPathContractError(
                    f"{field} has a truncated WIN_CERTIFICATE entry"
                )
            length, revision, certificate_type = struct.unpack_from(
                "<IHH", data, cursor
            )
            if (
                length < 8
                or revision != 0x0200
                or certificate_type != 0x0002
                or cursor + length > certificate_end
            ):
                raise SignPathContractError(
                    f"{field} has an invalid WIN_CERTIFICATE entry"
                )
            next_cursor = (cursor + length + 7) & ~7
            if next_cursor > certificate_end or any(
                data[cursor + length : next_cursor]
            ):
                raise SignPathContractError(
                    f"{field} has nonzero WIN_CERTIFICATE padding"
                )
            cursor = next_cursor
        if cursor != certificate_end:
            raise SignPathContractError(f"{field} certificate table is ambiguous")

    normalized = bytearray(data[:certificate_offset] if has_signature else data)
    normalized[checksum_offset : checksum_offset + 4] = b"\0" * 4
    normalized[security_entry : security_entry + 8] = b"\0" * 8
    return (
        raw_digest,
        hashlib.sha256(normalized).hexdigest(),
        hashlib.sha256(normalized[:image_end]).hexdigest(),
    )


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _extracted_files(root: Path, field: str) -> dict[str, Path]:
    """Enumerate an extracted installer tree without traversing links/reparse points."""
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as error:
        raise SignPathContractError(f"{field} is missing") from error
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise SignPathContractError(f"{field} must be a non-reparse directory")

    files: dict[str, Path] = {}
    casefolded: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise SignPathContractError(f"{field} could not be enumerated") from error
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SignPathContractError(
                    f"{field} contains an unsafe entry"
                ) from error
            if entry.is_symlink() or _is_reparse(entry_stat):
                raise SignPathContractError(f"{field} contains a link or reparse point")
            path = Path(entry.path)
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise SignPathContractError(f"{field} contains a non-regular entry")
            relative = path.relative_to(root).as_posix()
            folded = relative.casefold()
            if folded in casefolded:
                raise SignPathContractError(
                    f"{field} contains a case-insensitive path collision"
                )
            casefolded.add(folded)
            files[relative] = path
    if not files:
        raise SignPathContractError(f"{field} is empty")
    return files


def _one_role_path(
    files: Mapping[str, Path], role: str, field: str
) -> tuple[str, Path]:
    expected = _ROLE_FILENAMES[role]
    matches = [
        (relative, path) for relative, path in files.items() if path.name == expected
    ]
    if len(matches) != 1:
        raise SignPathContractError(f"{field} must contain exactly one {role}")
    return matches[0]


def verify_signing_equivalence(
    *,
    expected_unsigned: Mapping[str, Mapping[str, str]],
    unsigned_paths: Mapping[str, Path],
    signed_paths: Mapping[str, Path],
    unsigned_extract_root: Path,
    signed_extract_root: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Prove the SignPath output is only the authorized nested signing transform.

    PE host and sidecar bytes are compared after removing only Authenticode's
    specified regions.  Because nested signing necessarily rewrites the compressed
    NSIS data, installer equivalence is the canonical full extraction tree: the two
    normalized nested PEs plus every other file byte-for-byte.
    """
    normalized_expected = _normalize_identities(expected_unsigned, field="unsigned")
    if set(unsigned_paths) != REQUIRED_ROLES or set(signed_paths) != REQUIRED_ROLES:
        raise SignPathContractError("equivalence paths must contain the exact roles")

    unsigned_tree = _extracted_files(unsigned_extract_root, "unsigned NSIS extraction")
    signed_tree = _extracted_files(signed_extract_root, "signed NSIS extraction")
    if set(unsigned_tree) != set(signed_tree):
        raise SignPathContractError("signed NSIS extraction changes the payload paths")

    unsigned_nested: dict[str, tuple[str, Path]] = {}
    signed_nested: dict[str, tuple[str, Path]] = {}
    for role in ("desktop-host", "sidecar"):
        unsigned_nested[role] = _one_role_path(
            unsigned_tree, role, "unsigned extraction"
        )
        signed_nested[role] = _one_role_path(signed_tree, role, "signed extraction")
        if unsigned_nested[role][0] != signed_nested[role][0]:
            raise SignPathContractError(f"signed NSIS moves the {role} payload")
        if os.path.abspath(unsigned_paths[role]) != os.path.abspath(
            unsigned_nested[role][1]
        ):
            raise SignPathContractError(f"unsigned {role} path is not extraction-bound")
        if os.path.abspath(signed_paths[role]) != os.path.abspath(
            signed_nested[role][1]
        ):
            raise SignPathContractError(f"signed {role} path is not extraction-bound")

    signed_identities: dict[str, dict[str, str]] = {}
    equivalence: dict[str, dict[str, str]] = {}
    normalized_nested: dict[str, str] = {}
    for role in ("desktop-host", "sidecar"):
        unsigned_raw, unsigned_normalized, _ = _pe_authenticode_identity(
            unsigned_paths[role], f"unsigned {role}", require_signature=False
        )
        signed_raw, signed_normalized, _ = _pe_authenticode_identity(
            signed_paths[role], f"signed {role}", require_signature=True
        )
        if unsigned_raw != normalized_expected[role]["sha256"]:
            raise SignPathContractError(f"unsigned {role} mismatches the request")
        if unsigned_normalized != signed_normalized:
            raise SignPathContractError(
                f"signed {role} changes bytes outside Authenticode regions"
            )
        signed_identities[role] = {
            "path": signed_paths[role].name,
            "sha256": signed_raw,
        }
        normalized_nested[unsigned_nested[role][0]] = unsigned_normalized
        equivalence[role] = {
            "algorithm": "pe-authenticode-normalized-sha256-v1",
            "content_sha256": unsigned_normalized,
        }

    unsigned_installer_raw, _, unsigned_installer_stub = _pe_authenticode_identity(
        unsigned_paths["nsis-installer"],
        "unsigned nsis-installer",
        require_signature=False,
    )
    signed_installer_raw, _, signed_installer_stub = _pe_authenticode_identity(
        signed_paths["nsis-installer"],
        "signed nsis-installer",
        require_signature=True,
    )
    if unsigned_installer_raw != normalized_expected["nsis-installer"]["sha256"]:
        raise SignPathContractError("unsigned nsis-installer mismatches the request")
    if unsigned_installer_stub != signed_installer_stub:
        raise SignPathContractError(
            "signed nsis-installer changes the PE stub outside Authenticode regions"
        )
    signed_identities["nsis-installer"] = {
        "path": signed_paths["nsis-installer"].name,
        "sha256": signed_installer_raw,
    }

    unsigned_entries: list[dict[str, str]] = []
    signed_entries: list[dict[str, str]] = []
    for relative in sorted(unsigned_tree):
        if relative in normalized_nested:
            unsigned_digest = normalized_nested[relative]
            signed_digest = normalized_nested[relative]
            algorithm = "pe-authenticode-normalized-sha256-v1"
        else:
            unsigned_digest = _hash_regular_file(
                unsigned_tree[relative], f"unsigned extracted {relative}"
            )
            signed_digest = _hash_regular_file(
                signed_tree[relative], f"signed extracted {relative}"
            )
            algorithm = "sha256"
        unsigned_entries.append(
            {"path": relative, "algorithm": algorithm, "sha256": unsigned_digest}
        )
        signed_entries.append(
            {"path": relative, "algorithm": algorithm, "sha256": signed_digest}
        )
    if unsigned_entries != signed_entries:
        raise SignPathContractError(
            "signed NSIS changes extracted bytes outside Authenticode regions"
        )
    payload_digest = hashlib.sha256(
        _canonical_json(
            {
                "pe_stub_sha256": unsigned_installer_stub,
                "extracted_payload": unsigned_entries,
            }
        )
    ).hexdigest()
    equivalence["nsis-installer"] = {
        "algorithm": "nsis-pe-stub-and-extracted-payload-sha256-v1",
        "content_sha256": payload_digest,
    }
    return signed_identities, equivalence


def evaluate_signing_contract(
    *,
    status: str,
    enabled: bool,
    source_sha: str,
    source_tree: str,
    proof_digest: str,
    candidate_digest: str,
    secrets: Mapping[str, str],
) -> SigningDecision:
    """Require explicit integration and all immutable identities and secrets."""
    if status not in _KNOWN_STATES:
        if enabled:
            raise SignPathContractError(
                "SignPath signing requires the explicit integrated status"
            )
        raise SignPathContractError("unknown SignPath application status")
    if not enabled:
        return SigningDecision(
            enabled=False,
            reason="signpath-application-not-integrated",
            status=status,
        )
    if status != "integrated":
        raise SignPathContractError(
            "SignPath signing requires the explicit integrated status"
        )
    _require_sha(source_sha, "source_sha")
    _require_sha(source_tree, "source_tree")
    _require_digest(proof_digest, "proof_digest")
    _require_digest(candidate_digest, "candidate_digest")
    missing = [name for name in _REQUIRED_SECRETS if not secrets.get(name)]
    if missing:
        raise SignPathContractError(
            "missing required SignPath secret names: " + ", ".join(missing)
        )
    return SigningDecision(
        enabled=True,
        reason="protected-main-identities-eligible-for-manual-approval",
        status=status,
    )


def verify_manual_approval_environment(
    value: object,
    *,
    branch_policies: object,
    repository: str,
) -> None:
    """Verify that release-signing cannot bypass exact-main second review."""
    environment = _object(value, "environment")
    if environment.get("name") != _ENVIRONMENT:
        raise SignPathContractError("environment name mismatch")
    expected_url = (
        f"https://api.github.com/repos/{repository}/environments/{_ENVIRONMENT}"
    )
    if environment.get("url") != expected_url:
        raise SignPathContractError("environment repository binding mismatch")
    if environment.get("can_admins_bypass") is not False:
        raise SignPathContractError(
            "environment administrators must not bypass approval"
        )
    deployment = _object(
        environment.get("deployment_branch_policy"), "deployment_branch_policy"
    )
    if deployment != {
        "protected_branches": False,
        "custom_branch_policies": True,
    }:
        raise SignPathContractError("environment must use an exact main branch policy")

    rules = _sequence(environment.get("protection_rules"), "protection_rules")
    branch_rules = [
        _object(rule, "protection rule")
        for rule in rules
        if _object(rule, "protection rule").get("type") == "branch_policy"
    ]
    reviewer_rules = [
        _object(rule, "protection rule")
        for rule in rules
        if _object(rule, "protection rule").get("type") == "required_reviewers"
    ]
    if len(branch_rules) != 1 or len(reviewer_rules) != 1:
        raise SignPathContractError(
            "environment requires one exact-main rule and one reviewer rule"
        )
    reviewer_rule = reviewer_rules[0]
    if reviewer_rule.get("prevent_self_review") is not True:
        raise SignPathContractError("environment must prevent self review")
    reviewers = _sequence(reviewer_rule.get("reviewers"), "reviewers")
    if not reviewers:
        raise SignPathContractError("environment requires a second reviewer")
    for raw_reviewer in reviewers:
        reviewer = _object(raw_reviewer, "reviewer")
        if reviewer.get("type") not in {"User", "Team"}:
            raise SignPathContractError("reviewer type is invalid")
        identity = _object(reviewer.get("reviewer"), "reviewer identity")
        reviewer_id = identity.get("id")
        if not isinstance(reviewer_id, int) or isinstance(reviewer_id, bool):
            raise SignPathContractError("reviewer identity is invalid")

    policy = _object(branch_policies, "branch policies")
    branches = _sequence(policy.get("branch_policies"), "branch policies")
    if policy.get("total_count") != 1 or len(branches) != 1:
        raise SignPathContractError(
            "environment must have one exact main branch policy"
        )
    if _object(branches[0], "branch policy") != {"name": "main", "type": "branch"}:
        raise SignPathContractError("environment branch policy must be exact main")


def _normalize_identities(
    value: Mapping[str, Mapping[str, str]], *, field: str
) -> dict[str, dict[str, str]]:
    if set(value) != REQUIRED_ROLES:
        raise SignPathContractError(f"{field} must contain the exact roles")
    normalized: dict[str, dict[str, str]] = {}
    for role in sorted(REQUIRED_ROLES):
        record = _object(value[role], f"{field}.{role}")
        if set(record) != {"path", "sha256"}:
            raise SignPathContractError(f"{field}.{role} has unknown or missing fields")
        path = _safe_text(record.get("path"), f"{field}.{role}.path", limit=260)
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in path
            or ":" in path
            or pure.as_posix() != path
        ):
            raise SignPathContractError(f"{field}.{role}.path is unsafe")
        expected_name = _ROLE_FILENAMES[role]
        if role == "nsis-installer" and field == "signed":
            expected_name = "stock-desk-signed-nsis.exe"
        if pure.name != expected_name:
            raise SignPathContractError(f"{field}.{role}.path has an invalid filename")
        digest = _require_digest(
            _safe_text(record.get("sha256"), f"{field}.{role}.sha256", limit=64),
            f"{field}.{role}.sha256",
        )
        normalized[role] = {"path": path, "sha256": digest}
    return normalized


def build_signing_request(
    *,
    source_sha: str,
    source_tree: str,
    proof_digest: str,
    candidate_digest: str,
    unsigned: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Build the public, canonical request envelope before submission."""
    return {
        "schema": "stock-desk-signpath-request-v1",
        "status": "awaiting-manual-approval",
        "source": {
            "ref": "refs/heads/main",
            "sha": _require_sha(source_sha, "source_sha"),
            "tree": _require_sha(source_tree, "source_tree"),
        },
        "proof_sha256": _require_digest(proof_digest, "proof_digest"),
        "candidate_sha256": _require_digest(candidate_digest, "candidate_digest"),
        "approval": {
            "github_environment": _ENVIRONMENT,
            "prevent_self_review": True,
            "signpath_policy": "manual",
        },
        "unsigned": _normalize_identities(unsigned, field="unsigned"),
    }


def _validate_signing_request(request: Mapping[str, object]) -> dict[str, object]:
    expected_fields = {
        "schema",
        "status",
        "source",
        "proof_sha256",
        "candidate_sha256",
        "approval",
        "unsigned",
    }
    if set(request) != expected_fields:
        raise SignPathContractError("request has unknown or missing fields")
    if request.get("schema") != "stock-desk-signpath-request-v1":
        raise SignPathContractError("request schema is invalid")
    if request.get("status") != "awaiting-manual-approval":
        raise SignPathContractError("request status is invalid")
    source = _object(request.get("source"), "request source")
    if set(source) != {"ref", "sha", "tree"} or source.get("ref") != "refs/heads/main":
        raise SignPathContractError("request source is not exact main")
    approval = _object(request.get("approval"), "request approval")
    if approval != {
        "github_environment": _ENVIRONMENT,
        "prevent_self_review": True,
        "signpath_policy": "manual",
    }:
        raise SignPathContractError("request manual approval identity is invalid")
    unsigned_raw = _object(request.get("unsigned"), "request unsigned identities")
    normalized_unsigned: dict[str, Mapping[str, str]] = {}
    for role, value in unsigned_raw.items():
        normalized_unsigned[role] = _object(value, f"request unsigned {role}")
    rebuilt = build_signing_request(
        source_sha=_safe_text(source.get("sha"), "request source sha", limit=40),
        source_tree=_safe_text(source.get("tree"), "request source tree", limit=40),
        proof_digest=_safe_text(
            request.get("proof_sha256"), "request proof digest", limit=64
        ),
        candidate_digest=_safe_text(
            request.get("candidate_sha256"), "request candidate digest", limit=64
        ),
        unsigned=normalized_unsigned,
    )
    if dict(request) != rebuilt:
        raise SignPathContractError("request is not canonical")
    return rebuilt


def build_identity_closure(
    *,
    request: Mapping[str, object],
    request_id: str,
    signed: Mapping[str, Mapping[str, str]],
    equivalence: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Close all three signed identities against the canonical request."""
    normalized_request = _validate_signing_request(request)
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise SignPathContractError("request_id is invalid")
    unsigned = _normalize_identities(
        _object(normalized_request.get("unsigned"), "request unsigned identities"),
        field="unsigned",
    )
    signed_identities = _normalize_identities(signed, field="signed")
    if set(equivalence) != REQUIRED_ROLES:
        raise SignPathContractError("equivalence must contain the exact roles")
    artifacts: dict[str, object] = {}
    for role in sorted(REQUIRED_ROLES):
        if signed_identities[role]["sha256"] == unsigned[role]["sha256"]:
            raise SignPathContractError(
                f"{role} signed digest equals its unsigned digest"
            )
        evidence = _object(equivalence[role], f"equivalence.{role}")
        if set(evidence) != {"algorithm", "content_sha256"}:
            raise SignPathContractError(f"equivalence.{role} has invalid fields")
        expected_algorithm = "pe-authenticode-normalized-sha256-v1"
        if role == "nsis-installer":
            expected_algorithm = "nsis-pe-stub-and-extracted-payload-sha256-v1"
        if evidence.get("algorithm") != expected_algorithm:
            raise SignPathContractError(f"equivalence.{role} algorithm is invalid")
        content_digest = _require_digest(
            _safe_text(
                evidence.get("content_sha256"),
                f"equivalence.{role}.content_sha256",
                limit=64,
            ),
            f"equivalence.{role}.content_sha256",
        )
        artifacts[role] = {
            "unsigned_path": unsigned[role]["path"],
            "unsigned_sha256": unsigned[role]["sha256"],
            "signed_path": signed_identities[role]["path"],
            "signed_sha256": signed_identities[role]["sha256"],
            "equivalence_algorithm": expected_algorithm,
            "equivalent_content_sha256": content_digest,
        }
    return {
        "schema": "stock-desk-signpath-identity-closure-v1",
        "status": "signed",
        "request_id": request_id,
        "request_sha256": hashlib.sha256(
            _canonical_json(normalized_request)
        ).hexdigest(),
        "source": normalized_request.get("source"),
        "proof_sha256": normalized_request.get("proof_sha256"),
        "candidate_sha256": normalized_request.get("candidate_sha256"),
        "artifacts": artifacts,
    }


def build_signing_receipt(
    *,
    source_sha: str,
    payload_digest: str,
    request_id: str,
    signer_subject: str,
    certificate_thumbprint: str,
    timestamp_subject: str,
) -> dict[str, str]:
    """Build the exact legacy receipt consumed by trusted_updater_release.py."""
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise SignPathContractError("request_id is invalid")
    if _THUMBPRINT.fullmatch(certificate_thumbprint) is None:
        raise SignPathContractError("certificate_thumbprint is invalid")
    return {
        "schema": "stock-desk-signpath-receipt-v1",
        "status": "signed",
        "source_sha": _require_sha(source_sha, "source_sha"),
        "payload_sha256": _require_digest(payload_digest, "payload_digest"),
        "request_id": request_id,
        "signer_subject": _safe_text(signer_subject, "signer_subject"),
        "certificate_thumbprint": certificate_thumbprint,
        "timestamp_subject": _safe_text(timestamp_subject, "timestamp_subject"),
    }


def _manifest_unsigned_identities(
    manifest_path: Path, installer_path: Path, source_sha: str
) -> dict[str, dict[str, str]]:
    from scripts.verify_windows_desktop_bundle import validate_manifest

    raw_manifest = _object(
        _read_json(manifest_path, "bundle manifest"), "bundle manifest"
    )
    try:
        manifest = validate_manifest(dict(raw_manifest))
    except ValueError as error:
        raise SignPathContractError("bundle manifest is invalid") from error
    if manifest.get("source_sha") != source_sha:
        raise SignPathContractError("bundle manifest source_sha mismatch")
    files = _sequence(manifest.get("files"), "bundle manifest files")
    matches: dict[str, dict[str, str]] = {}
    for raw_record in files:
        record = _object(raw_record, "bundle file")
        role = record.get("role")
        if role not in REQUIRED_ROLES:
            continue
        if role in matches:
            raise SignPathContractError(f"bundle manifest duplicates {role}")
        matches[str(role)] = {
            "path": _safe_text(record.get("path"), f"bundle {role} path", limit=260),
            "sha256": _safe_text(
                record.get("sha256"), f"bundle {role} digest", limit=64
            ),
        }
    identities = _normalize_identities(matches, field="unsigned")
    actual_installer = _hash_regular_file(installer_path, "unsigned installer")
    if actual_installer != identities["nsis-installer"]["sha256"]:
        raise SignPathContractError(
            "unsigned installer digest mismatches bundle manifest"
        )
    return identities


def _verify_candidate_manifest(
    path: Path,
    *,
    source_sha: str,
    source_tree: str,
    candidate_digest: str,
    installer_path: Path,
    bundle_manifest_path: Path,
) -> None:
    from scripts.artifact_manifest import read_manifest

    if _hash_regular_file(path, "candidate manifest") != candidate_digest:
        raise SignPathContractError("candidate manifest digest mismatch")
    try:
        manifest = read_manifest(path)
    except ValueError as error:
        raise SignPathContractError("candidate manifest is invalid") from error
    if manifest["source_sha"] != source_sha or manifest["source_tree"] != source_tree:
        raise SignPathContractError("candidate manifest source identity mismatch")
    installer_digest = _hash_regular_file(installer_path, "unsigned installer")
    bundle_digest = _hash_regular_file(bundle_manifest_path, "bundle manifest")
    installer_records = [
        record for record in manifest["payloads"] if record["kind"] == "tauri-unsigned"
    ]
    bundle_records = [
        record
        for record in manifest["payloads"]
        if record["path"] == "windows-desktop-bundle.json"
    ]
    if (
        len(installer_records) != 1
        or installer_records[0]["sha256"] != installer_digest
    ):
        raise SignPathContractError("candidate manifest does not bind the installer")
    if len(bundle_records) != 1 or bundle_records[0]["sha256"] != bundle_digest:
        raise SignPathContractError(
            "candidate manifest does not bind the bundle manifest"
        )


def _parse_identity(raw: str) -> tuple[str, Path]:
    role, separator, path = raw.partition("=")
    if not separator or role not in REQUIRED_ROLES or not path:
        raise argparse.ArgumentTypeError("expected ROLE=PATH for a required role")
    return role, Path(path)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--status", required=True)
    preflight.add_argument("--source-sha", required=True)
    preflight.add_argument("--source-tree", required=True)
    preflight.add_argument("--proof-digest", required=True)
    preflight.add_argument("--candidate-digest", required=True)

    environment = commands.add_parser("verify-environment")
    environment.add_argument("--environment", required=True, type=Path)
    environment.add_argument("--branch-policies", required=True, type=Path)
    environment.add_argument("--repository", required=True)

    request = commands.add_parser("create-request")
    request.add_argument("--source-sha", required=True)
    request.add_argument("--source-tree", required=True)
    request.add_argument("--proof-digest", required=True)
    request.add_argument("--candidate-digest", required=True)
    request.add_argument("--candidate-manifest", required=True, type=Path)
    request.add_argument("--bundle-manifest", required=True, type=Path)
    request.add_argument("--installer", required=True, type=Path)
    request.add_argument("--output", required=True, type=Path)

    receipt = commands.add_parser("create-receipt")
    receipt.add_argument("--request", required=True, type=Path)
    receipt.add_argument("--request-id", required=True)
    receipt.add_argument(
        "--unsigned", action="append", required=True, type=_parse_identity
    )
    receipt.add_argument(
        "--signed", action="append", required=True, type=_parse_identity
    )
    receipt.add_argument("--unsigned-extract-root", required=True, type=Path)
    receipt.add_argument("--signed-extract-root", required=True, type=Path)
    receipt.add_argument("--signer-subject", required=True)
    receipt.add_argument("--certificate-thumbprint", required=True)
    receipt.add_argument("--timestamp-subject", required=True)
    receipt.add_argument("--closure-output", required=True, type=Path)
    receipt.add_argument("--receipt-output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    if arguments.command == "preflight":
        # Reusable workflow_call marks every secret as required before this job
        # can start. Keep their values scoped only to the one step that consumes
        # each credential; this deterministic preflight checks state and immutable
        # identities without exposing credentials to a general Python process.
        evaluate_signing_contract(
            status=arguments.status,
            enabled=True,
            source_sha=arguments.source_sha,
            source_tree=arguments.source_tree,
            proof_digest=arguments.proof_digest,
            candidate_digest=arguments.candidate_digest,
            secrets={name: "workflow-call-required" for name in _REQUIRED_SECRETS},
        )
        return 0
    if arguments.command == "verify-environment":
        verify_manual_approval_environment(
            _read_json(arguments.environment, "environment"),
            branch_policies=_read_json(arguments.branch_policies, "branch policies"),
            repository=arguments.repository,
        )
        return 0
    if arguments.command == "create-request":
        _verify_candidate_manifest(
            arguments.candidate_manifest,
            source_sha=arguments.source_sha,
            source_tree=arguments.source_tree,
            candidate_digest=arguments.candidate_digest,
            installer_path=arguments.installer,
            bundle_manifest_path=arguments.bundle_manifest,
        )
        unsigned = _manifest_unsigned_identities(
            arguments.bundle_manifest, arguments.installer, arguments.source_sha
        )
        request = build_signing_request(
            source_sha=arguments.source_sha,
            source_tree=arguments.source_tree,
            proof_digest=arguments.proof_digest,
            candidate_digest=arguments.candidate_digest,
            unsigned=unsigned,
        )
        _write_json(arguments.output, request)
        return 0
    if arguments.command == "create-receipt":
        receipt_request = _object(_read_json(arguments.request, "request"), "request")
        unsigned_request = _object(
            receipt_request.get("unsigned"), "request unsigned identities"
        )
        expected_unsigned = {
            role: _object(record, f"request unsigned {role}")
            for role, record in unsigned_request.items()
        }
        signed, equivalence = verify_signing_equivalence(
            expected_unsigned=expected_unsigned,
            unsigned_paths=dict(arguments.unsigned),
            signed_paths=dict(arguments.signed),
            unsigned_extract_root=arguments.unsigned_extract_root,
            signed_extract_root=arguments.signed_extract_root,
        )
        closure = build_identity_closure(
            request=receipt_request,
            request_id=arguments.request_id,
            signed=signed,
            equivalence=equivalence,
        )
        source = _object(receipt_request.get("source"), "request source")
        receipt = build_signing_receipt(
            source_sha=_safe_text(source.get("sha"), "source sha", limit=40),
            payload_digest=signed["nsis-installer"]["sha256"],
            request_id=arguments.request_id,
            signer_subject=arguments.signer_subject,
            certificate_thumbprint=arguments.certificate_thumbprint,
            timestamp_subject=arguments.timestamp_subject,
        )
        _write_json(arguments.closure_output, closure)
        _write_json(arguments.receipt_output, receipt)
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
