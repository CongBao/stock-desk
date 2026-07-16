"""Content-addressed, fail-closed NSIS repack contract.

The kit created by this module is a complete immutable snapshot of every input
needed to reproduce one unsigned NSIS installer.  It deliberately separates
snapshot creation from execution so a later signing workflow never needs to
read an untrusted build workspace.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Final, Iterator
import unicodedata

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts.secure_artifact_snapshot import (
    private_directory_lease,
    SecureArtifactSnapshotError,
    SnapshotLimits,
    snapshot_artifacts,
    verify_private_directory,
)


KIT_ARTIFACT: Final = "stock-desk-nsis-repack-kit"
RECEIPT_ARTIFACT: Final = "stock-desk-nsis-repack-receipt-v1"
DIAGNOSTIC_REPACK_ARTIFACT: Final = "stock-desk-nsis-diagnostic-repack-v1"
PROVENANCE_SET_ARTIFACT: Final = "stock-desk-nsis-repack-provenance-set-v1"
PROVENANCE_SUMMARY_FIELDS: Final = frozenset(
    {
        "schema_version",
        "artifact",
        "source_ref",
        "source_sha",
        "source_tree",
        "source_epoch",
        "kit",
        "transformation",
        "transformation_sha256",
        "receipts",
        "installer",
    }
)
KIT_MANIFEST: Final = "nsis-repack-kit.json"
SCHEMA_VERSION: Final = 1
MAX_JSON_BYTES: Final = 2 * 1024 * 1024
MAX_FILE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_FILES: Final = 4096
MAX_TAURI_HOST_BYTES: Final = MAX_FILE_BYTES
MAX_SOURCE_EPOCH: Final = 2**63 - 1
NSIS_DIAGNOSTIC_TAIL_BYTES: Final = 32 * 1024
_NSIS_DIAGNOSTIC_MAX_LINES: Final = 40
TOOLCHAIN_LOCK_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "nsis-toolchain-lock.json"
)

_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REF: Final = re.compile(r"^(?:refs/heads/main|refs/pull/[1-9][0-9]*/merge)$")
_PLUGIN_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_WINDOWS_ABSOLUTE: Final = re.compile(r"^(?:[A-Za-z]:|[/\\]{2})")
_WINDOWS_DRIVE_PATH: Final = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z]:[/\\]")
_PLUGIN_CALL: Final = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)::")
_WINDOWS_RESERVED: Final = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_OFFICIAL_ARGV: Final = (
    "-INPUTCHARSET",
    "UTF8",
    "-OUTPUTCHARSET",
    "UTF8",
    "-V3",
    "installer.nsi",
)
_PRIVATE_WORK_PLACEHOLDER: Final = "@STOCK_DESK_PRIVATE_WORK@"
TAURI_TRANSFORMATION_ALGORITHM: Final = "tauri-bundle-type-unk-to-nss-v1"
TAURI_SOURCE_TAG: Final = "tauri-cli-v2.11.4"
TAURI_SOURCE_COMMIT: Final = "8909f221d1515955fc843808032bdc5d62209c96"
TAURI_SOURCE_PATH: Final = "crates/tauri-bundler/src/bundle.rs"
TAURI_TRANSFORMED_PAYLOAD_PATH: Final = "payload/stock-desk-desktop.exe"
TAURI_BUNDLE_MARKER_UNKNOWN: Final = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
TAURI_BUNDLE_MARKER_NSIS: Final = b"__TAURI_BUNDLE_TYPE_VAR_NSS"
_TAURI_BUNDLE_MARKER_UNKNOWN: Final = TAURI_BUNDLE_MARKER_UNKNOWN
_TAURI_BUNDLE_MARKER_NSIS: Final = TAURI_BUNDLE_MARKER_NSIS
_ALLOWED_ENVIRONMENT: Final = frozenset(
    {"SOURCE_DATE_EPOCH", "LANG", "LC_ALL", "TZ", "TEMP", "TMP"}
)
_REQUIRED_TOOLCHAIN_PATHS: Final = frozenset(
    {
        "toolchain/makensis.exe",
        "toolchain/Bin/makensis.exe",
        "toolchain/Stubs/lzma-x86-unicode",
        "toolchain/Stubs/lzma_solid-x86-unicode",
        "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll",
        "toolchain/Include/MUI2.nsh",
        "toolchain/Include/FileFunc.nsh",
        "toolchain/Include/x64.nsh",
        "toolchain/Include/WordFunc.nsh",
        "toolchain/Include/Win/COM.nsh",
        "toolchain/Include/Win/Propkey.nsh",
        "toolchain/Include/StrFunc.nsh",
        "toolchain/Include/MultiUser.nsh",
        "toolchain/Include/nsDialogs.nsh",
        "toolchain/Include/WinMessages.nsh",
        "toolchain/Include/Win/RestartManager.nsh",
    }
)
FILE_ROLES: Final = frozenset(
    {
        "tauri-config",
        "nsis-toolchain",
        "nsis-plugin",
        "nsis-template",
        "nsis-rendered-script",
        "nsis-include",
        "nsis-hook",
        "nsis-language",
        "icon",
        "webview2",
        "payload",
    }
)
_REQUIRED_ROLES: Final = frozenset(
    {
        "tauri-config",
        "nsis-toolchain",
        "nsis-plugin",
        "nsis-template",
        "nsis-rendered-script",
        "nsis-include",
        "nsis-language",
    }
)
_SCRIPT_ROLES: Final = frozenset(
    {"nsis-rendered-script", "nsis-include", "nsis-hook", "nsis-language"}
)
_FORBIDDEN_EXTERNAL_COMPILE_CONTROLS: Final = frozenset(
    {
        "!execute",
        "!makensis",
        "!packhdr",
        "!finalize",
        "!uninstfinalize",
        "!system",
    }
)


class NsisRepackContractError(ValueError):
    """The repack snapshot or execution does not close its trusted inputs."""


@dataclass(frozen=True)
class NsisToolchainTreeIdentity:
    """Canonical identity of the complete extracted NSIS compiler tree."""

    algorithm: str
    file_count: int
    total_size: int
    sha256: str


@dataclass(frozen=True)
class VerifiedNsisToolchain:
    """Immutable identity of every executable input used by makensis."""

    compiler: Path
    compiler_size: int
    compiler_sha256: str
    additional_plugins_root: Path
    nsis_tauri_utils: Path
    nsis_tauri_utils_size: int
    nsis_tauri_utils_sha256: str
    lock_sha256: str
    tree: NsisToolchainTreeIdentity


def _reject_constant(value: str) -> object:
    raise NsisRepackContractError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NsisRepackContractError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _transformation_digest(transformation: Mapping[str, object]) -> str:
    unsigned = dict(transformation)
    unsigned.pop("transformation_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _read_json(path: Path, field: str) -> object:
    payload = _read_regular_file(path, field, limit=MAX_JSON_BYTES)
    return _parse_json(payload, field)


def _parse_json(payload: bytes, field: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except NsisRepackContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise NsisRepackContractError(f"{field} must be strict UTF-8 JSON") from error


def _read_descriptor_secure(path: Path) -> object:
    if not path.name or path.name in {".", ".."}:
        raise NsisRepackContractError("descriptor path is invalid")
    with tempfile.TemporaryDirectory(prefix="stock-desk-nsis-descriptor-") as temporary:
        snapshot = (Path(temporary) / "snapshot").resolve(strict=False)
        try:
            snapshot_artifacts(
                path.parent.absolute(),
                [path.name],
                snapshot.absolute(),
                limits=SnapshotLimits(
                    max_files=1,
                    max_file_size=MAX_JSON_BYTES,
                    max_total_size=MAX_JSON_BYTES,
                    max_depth=1,
                ),
                allow_windows_hardlinks=False,
            )
        except SecureArtifactSnapshotError as error:
            raise NsisRepackContractError("could not secure the descriptor") from error
        return _read_json(snapshot / path.name, "descriptor")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise NsisRepackContractError(f"{field} must be an object")
    return value


def _array(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise NsisRepackContractError(f"{field} must be an array")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {','.join(missing)}")
        if unknown:
            details.append(f"unknown {','.join(unknown)}")
        raise NsisRepackContractError(
            f"{field} fields are invalid: {'; '.join(details)}"
        )


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 for character in value)
    ):
        raise NsisRepackContractError(f"{field} is invalid")
    return value


def _digest(value: object, field: str) -> str:
    text = _text(value, field, limit=64)
    if _DIGEST.fullmatch(text) is None:
        raise NsisRepackContractError(f"{field} must be a lowercase SHA-256")
    return text


def _git_id(value: object, field: str) -> str:
    text = _text(value, field, limit=40)
    if _SHA.fullmatch(text) is None:
        raise NsisRepackContractError(f"{field} must be a lowercase Git object id")
    return text


def _source_ref(value: object, field: str) -> str:
    text = _text(value, field, limit=128)
    if _SOURCE_REF.fullmatch(text) is None:
        raise NsisRepackContractError(
            f"{field} must be refs/heads/main or a canonical refs/pull/N/merge"
        )
    return text


def _repack_slot(value: object, field: str) -> str:
    slot = _text(value, field, limit=1)
    if slot not in {"a", "b"}:
        raise NsisRepackContractError(f"{field} must be a or b")
    return slot


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise NsisRepackContractError(f"{field} must be a {qualifier} integer")
    return value


def _source_epoch(value: object, field: str) -> int:
    epoch = _positive_int(value, field)
    if epoch > MAX_SOURCE_EPOCH:
        raise NsisRepackContractError(f"{field} exceeds signed 64-bit range")
    return epoch


def _relative_path(value: object, field: str) -> str:
    raw = _text(value, field, limit=1024)
    if (
        "\\" in raw
        or ":" in raw
        or _WINDOWS_ABSOLUTE.match(raw)
        or unicodedata.normalize("NFKC", raw) != raw
    ):
        raise NsisRepackContractError(f"{field} must be a normalized POSIX path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or raw in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise NsisRepackContractError(f"{field} must be a normalized POSIX path")
    for part in path.parts:
        if part.endswith((".", " ")) or _WINDOWS_RESERVED.fullmatch(part) is not None:
            raise NsisRepackContractError(
                f"{field} is not portable to the Windows filesystem"
            )
    return raw


def _mapping_target(value: object, field: str) -> str:
    """Canonicalize only benign separator artifacts at the descriptor boundary."""

    raw = _text(value, field, limit=1024)
    if (
        "\\" in raw
        or ":" in raw
        or raw.startswith("/")
        or _WINDOWS_ABSOLUTE.match(raw)
        or unicodedata.normalize("NFKC", raw) != raw
    ):
        raise NsisRepackContractError(f"{field} must be a portable relative path")
    parts = raw.split("/")
    if ".." in parts:
        raise NsisRepackContractError(f"{field} must not traverse its root")
    normalized = "/".join(part for part in parts if part not in {"", "."})
    return _relative_path(normalized, field)


def _assert_case_unique(paths: Sequence[str], field: str) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        folded = unicodedata.normalize("NFKC", path).casefold()
        previous = seen.get(folded)
        if previous is not None:
            raise NsisRepackContractError(
                f"{field} contains a case-insensitive collision: {previous}, {path}"
            )
        seen[folded] = path


@contextmanager
def _open_regular_file(path: Path, field: str) -> Iterator[BinaryIO]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None and os.name != "nt":
        raise NsisRepackContractError(f"{field} cannot be read without link safety")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if nofollow is not None:
            flags |= nofollow
        descriptor = os.open(path, flags)
    except OSError as error:
        raise NsisRepackContractError(f"{field} is missing or unsafe") from error
    stream: BinaryIO | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise NsisRepackContractError(f"{field} must be a regular file")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
        after = os.fstat(stream.fileno())
        path_after = os.lstat(path)
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
        if (
            stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
            or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
            or before_identity != after_identity
        ):
            raise NsisRepackContractError(f"{field} changed while being read")
    except OSError as error:
        raise NsisRepackContractError(f"{field} could not be read safely") from error
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _read_regular_file(path: Path, field: str, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with _open_regular_file(path, field) as stream:
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise NsisRepackContractError(f"{field} exceeds the size limit")
            chunks.append(block)
    return b"".join(chunks)


def _hash_regular_file(path: Path, field: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular_file(path, field) as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > MAX_FILE_BYTES:
                raise NsisRepackContractError(f"{field} exceeds the size limit")
            digest.update(block)
    return size, digest.hexdigest()


def _load_toolchain_lock(
    lock_path: Path | None = None,
) -> tuple[dict[str, object], str]:
    """Load the repository-pinned official Tauri/NSIS trust root."""

    path = TOOLCHAIN_LOCK_PATH if lock_path is None else lock_path
    payload = _read_regular_file(path, "NSIS toolchain lock", limit=MAX_JSON_BYTES)
    raw = _object(_parse_json(payload, "NSIS toolchain lock"), "NSIS toolchain lock")
    _exact_fields(
        raw,
        {
            "schema_version",
            "tauri_cli",
            "nsis",
            "nsis_tauri_utils",
            "extracted_tree",
        },
        "NSIS toolchain lock",
    )
    if raw["schema_version"] != 1:
        raise NsisRepackContractError("NSIS toolchain lock schema_version must be 1")

    tauri = _object(raw["tauri_cli"], "NSIS toolchain lock.tauri_cli")
    _exact_fields(
        tauri,
        {"version", "source_tag", "source_path"},
        "NSIS toolchain lock.tauri_cli",
    )
    if dict(tauri) != {
        "version": "2.11.4",
        "source_tag": "tauri-cli-v2.11.4",
        "source_path": "crates/tauri-bundler/src/bundle/windows/nsis/mod.rs",
    }:
        raise NsisRepackContractError("NSIS toolchain lock has an unknown Tauri source")

    def trusted_download(
        value: object,
        field: str,
        *,
        version: str,
        url: str,
        sha1: str,
        sha256: str,
    ) -> dict[str, str]:
        record = _object(value, field)
        _exact_fields(record, {"version", "url", "sha1", "sha256"}, field)
        expected = {
            "version": version,
            "url": url,
            "sha1": sha1,
            "sha256": sha256,
        }
        if dict(record) != expected:
            raise NsisRepackContractError(f"{field} is not the audited official asset")
        return expected

    nsis = trusted_download(
        raw["nsis"],
        "NSIS toolchain lock.nsis",
        version="3.11",
        url=(
            "https://github.com/tauri-apps/binary-releases/releases/download/"
            "nsis-3.11/nsis-3.11.zip"
        ),
        sha1="ef7ff767e5cbd9edd22add3a32c9b8f4500bb10d",
        sha256="c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1",
    )
    utilities = trusted_download(
        raw["nsis_tauri_utils"],
        "NSIS toolchain lock.nsis_tauri_utils",
        version="0.5.3",
        url=(
            "https://github.com/tauri-apps/nsis-tauri-utils/releases/download/"
            "nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll"
        ),
        sha1="75197fee3c6a814fe035788d1c34ead39349b860",
        sha256="5ba143b5db4a87d32d6e7802e033330aae56cbceabe0d1e3ba41948385ad4709",
    )
    tree = _object(raw["extracted_tree"], "NSIS toolchain lock.extracted_tree")
    _exact_fields(
        tree,
        {"algorithm", "file_count", "total_size", "sha256"},
        "NSIS toolchain lock.extracted_tree",
    )
    if tree["algorithm"] != "stock-desk-nsis-toolchain-tree-v1":
        raise NsisRepackContractError("NSIS toolchain lock tree algorithm is unknown")
    normalized_tree: dict[str, object] = {
        "algorithm": "stock-desk-nsis-toolchain-tree-v1",
        "file_count": _positive_int(
            tree["file_count"], "NSIS toolchain lock.extracted_tree.file_count"
        ),
        "total_size": _positive_int(
            tree["total_size"], "NSIS toolchain lock.extracted_tree.total_size"
        ),
        "sha256": _digest(tree["sha256"], "NSIS toolchain lock.extracted_tree.sha256"),
    }
    normalized: dict[str, object] = {
        "schema_version": 1,
        "tauri_cli": dict(tauri),
        "nsis": nsis,
        "nsis_tauri_utils": utilities,
        "extracted_tree": normalized_tree,
    }
    if (
        _parse_json(_canonical_json(normalized), "normalized NSIS toolchain lock")
        != normalized
    ):
        raise NsisRepackContractError("NSIS toolchain lock cannot be canonicalized")
    return normalized, hashlib.sha256(payload).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_extracted_tree_records(
    nsis_root: Path,
) -> list[dict[str, object]]:
    """Read the complete extracted tree without following filesystem aliases."""

    try:
        root_metadata = os.lstat(nsis_root)
    except OSError as error:
        raise NsisRepackContractError("extracted NSIS root is missing") from error
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or _is_reparse(root_metadata)
    ):
        raise NsisRepackContractError("extracted NSIS root is unsafe")

    records: list[dict[str, object]] = []
    seen_paths: list[str] = []
    pending: list[tuple[Path, PurePosixPath, int]] = [
        (nsis_root, PurePosixPath("."), 0)
    ]
    directory_count = 0
    while pending:
        directory, relative_directory, depth = pending.pop()
        if depth > 32:
            raise NsisRepackContractError("extracted NSIS tree exceeds depth limit")
        directory_count += 1
        if directory_count > MAX_FILES:
            raise NsisRepackContractError("extracted NSIS tree exceeds directory limit")
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise NsisRepackContractError(
                "extracted NSIS tree cannot be enumerated"
            ) from error
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if relative_directory == PurePosixPath(".")
                else relative_directory / entry.name
            )
            portable = relative.as_posix()
            if (
                not entry.name
                or entry.name in {".", ".."}
                or unicodedata.normalize("NFKC", portable) != portable
            ):
                raise NsisRepackContractError("extracted NSIS tree path is unsafe")
            seen_paths.append(portable)
            if len(seen_paths) > MAX_FILES:
                raise NsisRepackContractError("extracted NSIS tree exceeds file limit")
            path = directory / entry.name
            try:
                metadata = os.lstat(path)
            except OSError as error:
                raise NsisRepackContractError(
                    "extracted NSIS tree contains an unreadable object"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise NsisRepackContractError(
                    "extracted NSIS tree contains a link or reparse point"
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((path, relative, depth + 1))
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise NsisRepackContractError(
                    "extracted NSIS tree contains an unowned object"
                )
            size, digest = _hash_regular_file(path, f"extracted NSIS file {portable}")
            records.append(
                {
                    "path": f"toolchain/{portable}",
                    "role": (
                        "nsis-plugin"
                        if portable
                        == "Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
                        else "nsis-toolchain"
                    ),
                    "size": size,
                    "sha256": digest,
                    "executable": portable == "makensis.exe",
                }
            )
    _assert_case_unique(seen_paths, "extracted NSIS tree")
    _assert_case_unique(
        [str(record["path"]) for record in records], "verified NSIS toolchain"
    )
    return sorted(records, key=lambda record: str(record["path"]).encode("utf-8"))


def _safe_regular_metadata(path: Path, field: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise NsisRepackContractError(f"{field} is missing") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse(metadata)
        or metadata.st_size <= 0
    ):
        raise NsisRepackContractError(f"{field} is unsafe")
    return metadata


def verify_extracted_nsis_toolchain(
    *,
    nsis_root: Path,
    additional_plugins_root: Path,
    lock_path: Path | None = None,
) -> VerifiedNsisToolchain:
    """Verify the exact extracted Tauri NSIS compiler and external plugin."""

    nsis_root = nsis_root.absolute()
    additional_plugins_root = additional_plugins_root.absolute()
    expected_plugins_root = nsis_root / "Plugins" / "x86-unicode" / "additional"
    if additional_plugins_root != expected_plugins_root:
        raise NsisRepackContractError(
            "additional NSIS plugin root must equal the compiler tree plugin root"
        )
    try:
        plugins_root_metadata = os.lstat(additional_plugins_root)
    except OSError as error:
        raise NsisRepackContractError(
            "additional NSIS plugin root is missing"
        ) from error
    if (
        stat.S_ISLNK(plugins_root_metadata.st_mode)
        or not stat.S_ISDIR(plugins_root_metadata.st_mode)
        or _is_reparse(plugins_root_metadata)
    ):
        raise NsisRepackContractError("additional NSIS plugin root is unsafe")
    compiler = nsis_root / "makensis.exe"
    external_plugin = additional_plugins_root / "nsis_tauri_utils.dll"
    compiler_metadata = _safe_regular_metadata(compiler, "top-level makensis.exe")
    plugin_metadata = _safe_regular_metadata(
        external_plugin, "external nsis_tauri_utils plugin"
    )
    records = _safe_extracted_tree_records(nsis_root)
    lock, lock_digest = _load_toolchain_lock(lock_path)
    actual_tree = _canonical_toolchain_tree(records)
    trusted_tree = _object(lock["extracted_tree"], "trusted toolchain tree")
    if actual_tree != dict(trusted_tree):
        raise NsisRepackContractError(
            "extracted NSIS tree does not equal the repository-pinned lock"
        )
    verified_records = _safe_extracted_tree_records(nsis_root)
    if verified_records != records:
        raise NsisRepackContractError("verified NSIS toolchain changed during review")
    records_by_path = {str(record["path"]): record for record in verified_records}
    compiler_record = records_by_path.get("toolchain/makensis.exe")
    plugin_record = records_by_path.get(
        "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
    )
    if compiler_record is None or plugin_record is None:
        raise NsisRepackContractError("verified NSIS compiler or plugin is missing")
    compiler_size, compiler_digest = _hash_regular_file(
        compiler, "top-level makensis.exe"
    )
    plugin_size, plugin_digest = _hash_regular_file(
        external_plugin, "external nsis_tauri_utils plugin"
    )
    compiler_metadata_after = _safe_regular_metadata(compiler, "top-level makensis.exe")
    plugin_metadata_after = _safe_regular_metadata(
        external_plugin, "external nsis_tauri_utils plugin"
    )
    if (
        compiler_size != compiler_metadata.st_size
        or plugin_size != plugin_metadata.st_size
        or _stable_file_identity(compiler_metadata_after)
        != _stable_file_identity(compiler_metadata)
        or _stable_file_identity(plugin_metadata_after)
        != _stable_file_identity(plugin_metadata)
        or compiler_size != compiler_record["size"]
        or compiler_digest != compiler_record["sha256"]
        or plugin_size != plugin_record["size"]
        or plugin_digest != plugin_record["sha256"]
    ):
        raise NsisRepackContractError("verified NSIS input identity changed")
    return VerifiedNsisToolchain(
        compiler=compiler,
        compiler_size=compiler_size,
        compiler_sha256=compiler_digest,
        additional_plugins_root=additional_plugins_root,
        nsis_tauri_utils=external_plugin,
        nsis_tauri_utils_size=plugin_size,
        nsis_tauri_utils_sha256=plugin_digest,
        lock_sha256=lock_digest,
        tree=NsisToolchainTreeIdentity(
            algorithm=str(actual_tree["algorithm"]),
            file_count=_positive_int(
                actual_tree["file_count"], "verified NSIS tree file_count"
            ),
            total_size=_positive_int(
                actual_tree["total_size"], "verified NSIS tree total_size"
            ),
            sha256=str(actual_tree["sha256"]),
        ),
    )


def _marker_scan(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[int, str, int, int, int]:
    """Stream one host binary and locate the two equal-length Tauri markers."""

    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
        or max_bytes > MAX_FILE_BYTES
    ):
        raise NsisRepackContractError("Tauri host size limit is invalid")
    if len(_TAURI_BUNDLE_MARKER_UNKNOWN) != len(_TAURI_BUNDLE_MARKER_NSIS):
        raise NsisRepackContractError("Tauri bundle markers are not equal length")
    marker_length = len(_TAURI_BUNDLE_MARKER_UNKNOWN)
    digest = hashlib.sha256()
    total = 0
    tail = b""
    unknown_count = 0
    nsis_count = 0
    marker_offset = -1
    with _open_regular_file(path, "private Tauri host payload") as stream:
        while block := stream.read(1024 * 1024):
            previous_total = total
            total += len(block)
            if total > max_bytes:
                raise NsisRepackContractError("private Tauri host exceeds size limit")
            digest.update(block)
            window = tail + block
            window_base = previous_total - len(tail)
            for marker, marker_name in (
                (_TAURI_BUNDLE_MARKER_UNKNOWN, "unknown"),
                (_TAURI_BUNDLE_MARKER_NSIS, "nsis"),
            ):
                search_from = 0
                while True:
                    index = window.find(marker, search_from)
                    if index < 0:
                        break
                    absolute = window_base + index
                    if absolute + marker_length > previous_total:
                        if marker_name == "unknown":
                            unknown_count += 1
                        else:
                            nsis_count += 1
                        if marker_offset < 0:
                            marker_offset = absolute
                    search_from = index + 1
            tail = window[-(marker_length - 1) :]
    return total, digest.hexdigest(), unknown_count, nsis_count, marker_offset


def _write_all(stream: BinaryIO, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("payload patch write made no progress")
        remaining = remaining[written:]


def _copy_patch_range(
    source: BinaryIO,
    destination: BinaryIO,
    count: int | None,
    before_digest: Any,
    after_digest: Any,
) -> int:
    copied = 0
    while count is None or copied < count:
        request = 1024 * 1024 if count is None else min(1024 * 1024, count - copied)
        if request == 0:
            break
        block = source.read(request)
        if not block:
            break
        before_digest.update(block)
        after_digest.update(block)
        _write_all(destination, block)
        copied += len(block)
    return copied


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def patch_tauri_bundle_payload(
    *,
    private_root: Path,
    payload: Path,
    max_bytes: int = MAX_TAURI_HOST_BYTES,
) -> dict[str, object]:
    """Patch one private restored Tauri host copy from UNK to NSS, in place."""

    private_root = private_root.absolute()
    payload = payload.absolute()
    try:
        verify_private_directory(private_root)
    except SecureArtifactSnapshotError as error:
        raise NsisRepackContractError("Tauri payload root is not private") from error
    try:
        relative = payload.relative_to(private_root)
    except ValueError as error:
        raise NsisRepackContractError(
            "Tauri payload is outside the private root"
        ) from error
    portable_relative = PurePosixPath(*relative.parts).as_posix()
    if portable_relative != TAURI_TRANSFORMED_PAYLOAD_PATH:
        raise NsisRepackContractError(
            f"Tauri payload path must be {TAURI_TRANSFORMED_PAYLOAD_PATH}"
        )
    secured_payload = _safe_child(
        private_root, portable_relative, "private Tauri host payload"
    )
    if secured_payload != payload:
        raise NsisRepackContractError("Tauri payload path is not canonical")
    before_metadata = _safe_regular_metadata(payload, "private Tauri host payload")
    before_size, before_sha256, unknown_count, nsis_count, marker_offset = _marker_scan(
        payload, max_bytes=max_bytes
    )
    if unknown_count != 1 or nsis_count != 0 or marker_offset < 0:
        raise NsisRepackContractError(
            "restored Tauri host must contain exactly one UNK and zero NSS markers"
        )
    if before_size != before_metadata.st_size:
        raise NsisRepackContractError("private Tauri host changed during scan")

    temporary_path: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{payload.name}.nss-patch-", dir=payload.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w+b", closefd=True) as destination:
            descriptor = -1
            before_copy_digest = hashlib.sha256()
            after_digest = hashlib.sha256()
            copied_prefix = 0
            copied_suffix = 0
            with _open_regular_file(payload, "private Tauri host payload") as source:
                copied_prefix = _copy_patch_range(
                    source,
                    destination,
                    marker_offset,
                    before_copy_digest,
                    after_digest,
                )
                marker = source.read(len(_TAURI_BUNDLE_MARKER_UNKNOWN))
                before_copy_digest.update(marker)
                if marker != _TAURI_BUNDLE_MARKER_UNKNOWN:
                    raise NsisRepackContractError(
                        "private Tauri host marker changed before patch"
                    )
                _write_all(destination, _TAURI_BUNDLE_MARKER_NSIS)
                after_digest.update(_TAURI_BUNDLE_MARKER_NSIS)
                copied_suffix = _copy_patch_range(
                    source,
                    destination,
                    None,
                    before_copy_digest,
                    after_digest,
                )
            patched_size = (
                copied_prefix + len(_TAURI_BUNDLE_MARKER_NSIS) + copied_suffix
            )
            if (
                copied_prefix != marker_offset
                or patched_size != before_size
                or before_copy_digest.hexdigest() != before_sha256
            ):
                raise NsisRepackContractError(
                    "private Tauri host changed while creating patched copy"
                )
            destination.flush()
            os.fsync(destination.fileno())
            patched_sha256 = after_digest.hexdigest()

        temporary_metadata = _safe_regular_metadata(
            temporary_path, "private NSS-patched Tauri host"
        )
        if temporary_metadata.st_size != before_size:
            raise NsisRepackContractError("private NSS-patched host size changed")
        current_metadata = _safe_regular_metadata(payload, "private Tauri host payload")
        if _stable_file_identity(current_metadata) != _stable_file_identity(
            before_metadata
        ):
            raise NsisRepackContractError(
                "private Tauri host was replaced before promotion"
            )
        current_size, current_sha256 = _hash_regular_file(
            payload, "private Tauri host payload"
        )
        if current_size != before_size or current_sha256 != before_sha256:
            raise NsisRepackContractError("private Tauri host changed before promotion")
        try:
            os.replace(temporary_path, payload)
        except OSError as error:
            raise NsisRepackContractError(
                "private NSS-patched host could not be promoted atomically"
            ) from error
        temporary_path = None
        final_metadata = _safe_regular_metadata(payload, "private NSS-patched host")
        final_size, final_sha256 = _hash_regular_file(
            payload, "private NSS-patched host"
        )
        if (
            final_metadata.st_size != before_size
            or final_size != before_size
            or final_sha256 != patched_sha256
        ):
            raise NsisRepackContractError(
                "private NSS-patched host identity changed after promotion"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass

    result: dict[str, object] = {
        "algorithm": TAURI_TRANSFORMATION_ALGORITHM,
        "source": {
            "tag": TAURI_SOURCE_TAG,
            "commit": TAURI_SOURCE_COMMIT,
            "path": TAURI_SOURCE_PATH,
        },
        "payload_path": TAURI_TRANSFORMED_PAYLOAD_PATH,
        "before_token": TAURI_BUNDLE_MARKER_UNKNOWN.decode("ascii"),
        "after_token": TAURI_BUNDLE_MARKER_NSIS.decode("ascii"),
        "marker_offset": marker_offset,
        "before": {
            "size": before_size,
            "sha256": before_sha256,
            "before_token_count": 1,
            "after_token_count": 0,
        },
        "after": {
            "size": before_size,
            "sha256": patched_sha256,
            "before_token_count": 0,
            "after_token_count": 1,
        },
    }
    result["transformation_sha256"] = _transformation_digest(result)
    return result


def _canonical_toolchain_tree(
    files: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Hash UTF-8 path-sorted ``{path,size,sha256}`` records with toolchain/ prefix."""

    records: list[dict[str, object]] = []
    total_size = 0
    for index, record in enumerate(files):
        path = str(record["path"])
        role = str(record["role"])
        is_toolchain_path = path.startswith("toolchain/")
        is_toolchain_role = role in {"nsis-toolchain", "nsis-plugin"}
        if is_toolchain_path != is_toolchain_role:
            raise NsisRepackContractError(
                f"files[{index}] toolchain path and role must agree"
            )
        if is_toolchain_path:
            size = record["size"]
            if not isinstance(size, int) or isinstance(size, bool):
                raise NsisRepackContractError(f"files[{index}].size must be an integer")
            total_size += size
            records.append(
                {
                    "path": path,
                    "size": size,
                    "sha256": record["sha256"],
                }
            )
    records.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    return {
        "algorithm": "stock-desk-nsis-toolchain-tree-v1",
        "file_count": len(records),
        "total_size": total_size,
        "sha256": hashlib.sha256(_canonical_json(records)).hexdigest(),
    }


def _mapping_targets(
    files: Sequence[Mapping[str, object]],
    expected_output: Mapping[str, object],
) -> set[str]:
    targets = {
        str(record["path"])
        for record in files
        if record["role"]
        in {
            "payload",
            "webview2",
            "icon",
            "nsis-hook",
            "nsis-language",
            "nsis-include",
        }
    }
    for record in files:
        if record["role"] not in {"nsis-toolchain", "nsis-plugin"}:
            continue
        parent = PurePosixPath(str(record["path"])).parent
        while parent != PurePosixPath("."):
            targets.add(parent.as_posix())
            parent = parent.parent
    targets.add(str(expected_output["path"]))
    return targets


def _safe_child(root: Path, relative: str, field: str) -> Path:
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                metadata = os.lstat(current)
            except OSError as error:
                raise NsisRepackContractError(f"{field} is unsafe") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise NsisRepackContractError(f"{field} traverses a link")
    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise NsisRepackContractError(f"{field} is missing") from error
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise NsisRepackContractError(f"{field} escapes its root")
    return current


def _remove_created_file(path: Path, identity: tuple[int, int] | None) -> None:
    """Remove only the exact regular file this process created."""

    if identity is None:
        return
    try:
        metadata = os.lstat(path)
        if (
            stat.S_ISREG(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            path.unlink()
    except OSError:
        return


def _write_new_file(destination: Path, payload: bytes, field: str) -> tuple[int, int]:
    """Create, completely write, and fsync one new regular file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise NsisRepackContractError(f"{field} destination is unsafe") from error
    identity: tuple[int, int] | None = None
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise OSError("destination is not a regular file")
        identity = (created.st_dev, created.st_ino)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("destination write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException as error:
        os.close(descriptor)
        _remove_created_file(destination, identity)
        if isinstance(error, NsisRepackContractError):
            raise
        raise NsisRepackContractError(f"{field} could not be written") from error
    os.close(descriptor)
    if identity is None:
        raise NsisRepackContractError(f"{field} destination identity is unavailable")
    return identity


def _copy_regular_file(source: Path, destination: Path, field: str) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise NsisRepackContractError(f"{field} destination is unsafe") from error
    identity: tuple[int, int] | None = None
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise OSError("destination is not a regular file")
        identity = (created.st_dev, created.st_ino)
        with _open_regular_file(source, field) as stream:
            while block := stream.read(1024 * 1024):
                remaining = memoryview(block)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("destination write made no progress")
                    remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _remove_created_file(destination, identity)
        raise
    os.close(descriptor)
    if identity is None:
        raise NsisRepackContractError(f"{field} destination identity is unavailable")
    return identity


def _snapshot_source_files(
    source_root: Path,
    records: Sequence[Mapping[str, object]],
    destination: Path,
) -> None:
    """Snapshot inputs only through the shared race-resistant public API."""
    entries = [str(record["path"]) for record in records]
    try:
        result = snapshot_artifacts(
            source_root.absolute(),
            entries,
            destination.absolute(),
            limits=SnapshotLimits(
                max_files=MAX_FILES,
                max_file_size=MAX_FILE_BYTES,
                max_total_size=8 * 1024 * 1024 * 1024,
                max_depth=32,
            ),
            allow_windows_hardlinks=False,
        )
    except SecureArtifactSnapshotError as error:
        raise NsisRepackContractError("secure artifact snapshot failed") from error
    expected = {
        str(record["path"]): (record["size"], record["sha256"]) for record in records
    }
    actual = {item.path: (item.size, item.sha256) for item in result.files}
    if actual != expected:
        raise NsisRepackContractError(
            "secure snapshot identity differs from descriptor"
        )


def _normalize_file_records(value: object) -> list[dict[str, object]]:
    records = _array(value, "files")
    if not records or len(records) > MAX_FILES:
        raise NsisRepackContractError("files must be a bounded non-empty array")
    normalized: list[dict[str, object]] = []
    paths: list[str] = []
    roles: set[str] = set()
    for index, raw_record in enumerate(records):
        record = _object(raw_record, f"files[{index}]")
        _exact_fields(
            record, {"path", "role", "size", "sha256", "executable"}, f"files[{index}]"
        )
        path = _relative_path(record["path"], f"files[{index}].path")
        role = _text(record["role"], f"files[{index}].role", limit=64)
        if role not in FILE_ROLES:
            raise NsisRepackContractError(f"files[{index}].role is unknown")
        size = _positive_int(record["size"], f"files[{index}].size", allow_zero=True)
        if size > MAX_FILE_BYTES:
            raise NsisRepackContractError(f"files[{index}].size exceeds the limit")
        executable = record["executable"]
        if not isinstance(executable, bool):
            raise NsisRepackContractError(f"files[{index}].executable must be boolean")
        if executable and role != "nsis-toolchain":
            raise NsisRepackContractError(
                "only the NSIS toolchain may be marked executable"
            )
        paths.append(path)
        roles.add(role)
        normalized.append(
            {
                "path": path,
                "role": role,
                "size": size,
                "sha256": _digest(record["sha256"], f"files[{index}].sha256"),
                "executable": executable,
            }
        )
    _assert_case_unique(paths, "files")
    missing_roles = sorted(_REQUIRED_ROLES - roles)
    if missing_roles:
        raise NsisRepackContractError(
            f"files do not bind required roles: {','.join(missing_roles)}"
        )
    executable_tools = [
        record
        for record in normalized
        if record["role"] == "nsis-toolchain" and record["executable"]
    ]
    if len(executable_tools) != 1:
        raise NsisRepackContractError(
            "files must bind exactly one executable NSIS tool"
        )
    if sum(record["role"] == "nsis-rendered-script" for record in normalized) != 1:
        raise NsisRepackContractError("files must bind exactly one rendered script")
    return sorted(normalized, key=lambda record: str(record["path"]))


def _normalize_toolchain(
    value: object, files: Sequence[Mapping[str, object]], *, manifest: bool
) -> dict[str, object]:
    toolchain = _object(value, "toolchain")
    expected_fields = {
        "path",
        "sha256",
        "tauri_cli_version",
        "nsis_version",
        "nsis_tauri_utils_version",
        "plugins",
    }
    if manifest:
        expected_fields.add("trust")
    _exact_fields(
        toolchain,
        expected_fields,
        "toolchain",
    )
    path = _relative_path(toolchain["path"], "toolchain.path")
    if path != "toolchain/makensis.exe":
        raise NsisRepackContractError(
            "toolchain.path must be the audited top-level makensis.exe"
        )
    digest = _digest(toolchain["sha256"], "toolchain.sha256")
    versions = {
        "tauri_cli_version": "2.11.4",
        "nsis_version": "3.11",
        "nsis_tauri_utils_version": "0.5.3",
    }
    for field, required in versions.items():
        if toolchain[field] != required:
            raise NsisRepackContractError(f"toolchain.{field} must be {required}")
    tool_records = [
        record
        for record in files
        if record["role"] == "nsis-toolchain"
        and record["path"] == path
        and record["executable"]
    ]
    if len(tool_records) != 1 or tool_records[0]["sha256"] != digest:
        raise NsisRepackContractError("toolchain identity is not bound by files")
    tool_paths = {str(record["path"]) for record in files}
    missing_tool_paths = sorted(_REQUIRED_TOOLCHAIN_PATHS - tool_paths)
    if missing_tool_paths:
        raise NsisRepackContractError(
            "toolchain files are incomplete: " + ",".join(missing_tool_paths)
        )
    lock, lock_digest = _load_toolchain_lock()
    trusted_tree = _object(lock["extracted_tree"], "trusted toolchain tree")
    actual_tree = _canonical_toolchain_tree(files)
    if actual_tree != dict(trusted_tree):
        raise NsisRepackContractError(
            "toolchain records do not equal the pinned official extracted tree"
        )
    normalized_trust = {
        "lock_sha256": lock_digest,
        "tree": actual_tree,
    }
    if manifest:
        trust = _object(toolchain["trust"], "toolchain.trust")
        _exact_fields(trust, {"lock_sha256", "tree"}, "toolchain.trust")
        supplied_tree = _object(trust["tree"], "toolchain.trust.tree")
        _exact_fields(
            supplied_tree,
            {"algorithm", "file_count", "total_size", "sha256"},
            "toolchain.trust.tree",
        )
        supplied_trust = {
            "lock_sha256": _digest(trust["lock_sha256"], "toolchain.trust.lock_sha256"),
            "tree": {
                "algorithm": _text(
                    supplied_tree["algorithm"],
                    "toolchain.trust.tree.algorithm",
                    limit=64,
                ),
                "file_count": _positive_int(
                    supplied_tree["file_count"], "toolchain.trust.tree.file_count"
                ),
                "total_size": _positive_int(
                    supplied_tree["total_size"], "toolchain.trust.tree.total_size"
                ),
                "sha256": _digest(
                    supplied_tree["sha256"], "toolchain.trust.tree.sha256"
                ),
            },
        }
        if supplied_trust != normalized_trust:
            raise NsisRepackContractError(
                "toolchain trust does not equal the repository-pinned lock"
            )

    plugins_raw = _array(toolchain["plugins"], "toolchain.plugins")
    if not plugins_raw or len(plugins_raw) > 128:
        raise NsisRepackContractError(
            "toolchain.plugins must be a bounded non-empty array"
        )
    plugins: list[dict[str, str]] = []
    names: list[str] = []
    paths: list[str] = []
    for index, raw_plugin in enumerate(plugins_raw):
        plugin = _object(raw_plugin, f"toolchain.plugins[{index}]")
        _exact_fields(plugin, {"name", "path", "sha256"}, f"toolchain.plugins[{index}]")
        name = _text(plugin["name"], f"toolchain.plugins[{index}].name", limit=64)
        if _PLUGIN_NAME.fullmatch(name) is None:
            raise NsisRepackContractError(f"unknown NSIS plugin: {name}")
        plugin_path = _relative_path(plugin["path"], f"toolchain.plugins[{index}].path")
        plugin_digest = _digest(plugin["sha256"], f"toolchain.plugins[{index}].sha256")
        matching = [
            record
            for record in files
            if record["role"] == "nsis-plugin" and record["path"] == plugin_path
        ]
        if len(matching) != 1 or matching[0]["sha256"] != plugin_digest:
            raise NsisRepackContractError(
                f"plugin identity is not bound by files: {name}"
            )
        names.append(name)
        paths.append(plugin_path)
        plugins.append({"name": name, "path": plugin_path, "sha256": plugin_digest})
    _assert_case_unique(names, "toolchain.plugins names")
    _assert_case_unique(paths, "toolchain.plugins paths")
    bound_plugin_paths = {
        str(record["path"]) for record in files if record["role"] == "nsis-plugin"
    }
    if set(paths) != bound_plugin_paths:
        raise NsisRepackContractError("every NSIS plugin file must have known metadata")
    nsis_utils = [plugin for plugin in plugins if plugin["name"] == "nsis_tauri_utils"]
    if len(nsis_utils) != 1 or nsis_utils[0]["path"] != (
        "toolchain/Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
    ):
        raise NsisRepackContractError(
            "nsis_tauri_utils 0.5.3 must be bound at the audited plugin path"
        )
    return {
        "path": path,
        "sha256": digest,
        **versions,
        "plugins": sorted(plugins, key=lambda plugin: plugin["name"]),
        "trust": normalized_trust,
    }


def _normalize_argv(value: object, files: Sequence[Mapping[str, object]]) -> list[str]:
    raw_arguments = _array(value, "argv")
    if not raw_arguments or len(raw_arguments) > 64:
        raise NsisRepackContractError("argv must be a bounded non-empty array")
    arguments: list[str] = []
    for index, raw_argument in enumerate(raw_arguments):
        argument = _text(raw_argument, f"argv[{index}]", limit=1024)
        arguments.append(argument)
    if tuple(arguments) != _OFFICIAL_ARGV:
        raise NsisRepackContractError(
            "argv must equal the audited Tauri 2.11.4 makensis invocation"
        )
    scripts = {
        str(record["path"])
        for record in files
        if record["role"] == "nsis-rendered-script"
    }
    if scripts != {"installer.nsi"}:
        raise NsisRepackContractError("rendered script must be installer.nsi")
    local_includes = {
        str(record["path"]) for record in files if record["role"] == "nsis-include"
    }
    if not {"FileAssociation.nsh", "utils.nsh"}.issubset(local_includes):
        raise NsisRepackContractError(
            "rendered output must bind FileAssociation.nsh and utils.nsh"
        )
    return arguments


def _normalize_environment(value: object, source_epoch: int) -> dict[str, str]:
    environment = _object(value, "environment")
    if not environment:
        raise NsisRepackContractError("environment must not be empty")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in environment.items():
        name = _text(raw_name, "environment key", limit=64)
        if _ENV_NAME.fullmatch(name) is None or name not in _ALLOWED_ENVIRONMENT:
            raise NsisRepackContractError(
                f"environment variable is not allowed: {name}"
            )
        normalized[name] = _text(raw_value, f"environment.{name}", limit=256)
    if normalized.get("SOURCE_DATE_EPOCH") != str(source_epoch):
        raise NsisRepackContractError(
            "environment.SOURCE_DATE_EPOCH must equal source_epoch"
        )
    for name in ("TEMP", "TMP"):
        supplied = normalized.get(name)
        if supplied is not None and supplied != _PRIVATE_WORK_PLACEHOLDER:
            raise NsisRepackContractError(
                f"environment.{name} must use the private-work placeholder"
            )
        normalized[name] = _PRIVATE_WORK_PLACEHOLDER
    return dict(sorted(normalized.items()))


def _normalize_cleared_environment(value: object) -> list[str]:
    values = _array(value, "cleared_environment")
    normalized = [
        _text(item, f"cleared_environment[{index}]", limit=64)
        for index, item in enumerate(values)
    ]
    if normalized != ["NSISCONFDIR", "NSISDIR"]:
        raise NsisRepackContractError(
            "cleared_environment must exactly remove NSISCONFDIR and NSISDIR"
        )
    return normalized


def _normalize_expected_output(value: object) -> dict[str, object]:
    output = _object(value, "expected_unsigned_installer")
    _exact_fields(output, {"path", "size", "sha256"}, "expected_unsigned_installer")
    size = _positive_int(output["size"], "expected_unsigned_installer.size")
    if size > MAX_FILE_BYTES:
        raise NsisRepackContractError("expected unsigned installer exceeds the limit")
    return {
        "path": _relative_path(output["path"], "expected_unsigned_installer.path"),
        "size": size,
        "sha256": _digest(output["sha256"], "expected_unsigned_installer.sha256"),
    }


def _normalize_path_mappings(
    value: object,
    files: Sequence[Mapping[str, object]],
    expected_output: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_mappings = _array(value, "path_mappings")
    if not raw_mappings or len(raw_mappings) > 128:
        raise NsisRepackContractError("path_mappings must be a bounded non-empty array")
    allowed_targets = _mapping_targets(files, expected_output)
    mappings: list[dict[str, object]] = []
    sources: list[str] = []
    targets: list[str] = []
    for index, raw_mapping in enumerate(raw_mappings):
        mapping = _object(raw_mapping, f"path_mappings[{index}]")
        _exact_fields(
            mapping,
            {"source_absolute", "target", "occurrences"},
            f"path_mappings[{index}]",
        )
        source = _text(
            mapping["source_absolute"],
            f"path_mappings[{index}].source_absolute",
            limit=4096,
        )
        if not (source.startswith("/") or _WINDOWS_ABSOLUTE.match(source)):
            raise NsisRepackContractError(
                f"path_mappings[{index}].source_absolute must be absolute"
            )
        target = _mapping_target(mapping["target"], f"path_mappings[{index}].target")
        if target not in allowed_targets:
            raise NsisRepackContractError(
                f"path_mappings[{index}].target is not a bound payload"
            )
        occurrences = _positive_int(
            mapping["occurrences"], f"path_mappings[{index}].occurrences"
        )
        sources.append(source)
        targets.append(target)
        mappings.append(
            {
                "source_absolute": source,
                "target": target,
                "occurrences": occurrences,
            }
        )
    _assert_case_unique(sources, "path_mappings sources")
    _assert_case_unique(targets, "path_mappings targets")
    return sorted(
        mappings,
        key=lambda mapping: (
            -len(str(mapping["source_absolute"])),
            str(mapping["source_absolute"]),
        ),
    )


def _normalize_normalization(
    value: object,
    files: Sequence[Mapping[str, object]],
    expected_output: Mapping[str, object],
) -> dict[str, object]:
    normalization = _object(value, "normalization")
    _exact_fields(
        normalization,
        {
            "algorithm",
            "raw_source_sha256",
            "structural_sha256",
            "normalized_sha256",
            "mapped_targets",
        },
        "normalization",
    )
    if normalization["algorithm"] != "tauri-rendered-nsis-exact-path-map-v1":
        raise NsisRepackContractError("normalization.algorithm is unknown")
    raw_source_digest = _digest(
        normalization["raw_source_sha256"], "normalization.raw_source_sha256"
    )
    structural_digest = _digest(
        normalization["structural_sha256"], "normalization.structural_sha256"
    )
    normalized_digest = _digest(
        normalization["normalized_sha256"], "normalization.normalized_sha256"
    )
    scripts = [record for record in files if record["role"] == "nsis-rendered-script"]
    if len(scripts) != 1 or scripts[0]["sha256"] != normalized_digest:
        raise NsisRepackContractError(
            "normalization target is not the rendered script identity"
        )
    raw_targets = _array(
        normalization["mapped_targets"], "normalization.mapped_targets"
    )
    if not raw_targets or len(raw_targets) > 128:
        raise NsisRepackContractError(
            "normalization.mapped_targets must be a bounded non-empty array"
        )
    targets: list[dict[str, object]] = []
    paths: list[str] = []
    bound_paths = {str(record["path"]) for record in files}
    bound_paths.update(_mapping_targets(files, expected_output))
    for index, raw_target in enumerate(raw_targets):
        target = _object(raw_target, f"normalization.mapped_targets[{index}]")
        _exact_fields(
            target,
            {"target", "occurrence_count"},
            f"normalization.mapped_targets[{index}]",
        )
        path = _relative_path(
            target["target"], f"normalization.mapped_targets[{index}].target"
        )
        if path not in bound_paths:
            raise NsisRepackContractError("normalization target is not bound by files")
        paths.append(path)
        targets.append(
            {
                "target": path,
                "occurrence_count": _positive_int(
                    target["occurrence_count"],
                    f"normalization.mapped_targets[{index}].occurrence_count",
                ),
            }
        )
    _assert_case_unique(paths, "normalization targets")
    return {
        "algorithm": "tauri-rendered-nsis-exact-path-map-v1",
        "raw_source_sha256": raw_source_digest,
        "structural_sha256": structural_digest,
        "normalized_sha256": normalized_digest,
        "mapped_targets": sorted(targets, key=lambda item: str(item["target"])),
    }


def _normalize_transformation_identity(value: object, field: str) -> dict[str, object]:
    identity = _object(value, field)
    _exact_fields(
        identity,
        {"size", "sha256", "before_token_count", "after_token_count"},
        field,
    )
    size = _positive_int(identity["size"], f"{field}.size")
    if size > MAX_TAURI_HOST_BYTES:
        raise NsisRepackContractError(f"{field}.size exceeds the Tauri host limit")
    return {
        "size": size,
        "sha256": _digest(identity["sha256"], f"{field}.sha256"),
        "before_token_count": _positive_int(
            identity["before_token_count"],
            f"{field}.before_token_count",
            allow_zero=True,
        ),
        "after_token_count": _positive_int(
            identity["after_token_count"],
            f"{field}.after_token_count",
            allow_zero=True,
        ),
    }


def _normalize_transformation(
    value: object, files: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    transformation = _object(value, "transformation")
    _exact_fields(
        transformation,
        {
            "algorithm",
            "transformation_sha256",
            "source",
            "payload_path",
            "before_token",
            "after_token",
            "marker_offset",
            "before",
            "after",
        },
        "transformation",
    )
    if transformation["algorithm"] != TAURI_TRANSFORMATION_ALGORITHM:
        raise NsisRepackContractError(
            "transformation.algorithm is not the fixed Tauri transformation"
        )
    source = _object(transformation["source"], "transformation.source")
    _exact_fields(source, {"tag", "commit", "path"}, "transformation.source")
    fixed_source = {
        "tag": TAURI_SOURCE_TAG,
        "commit": TAURI_SOURCE_COMMIT,
        "path": TAURI_SOURCE_PATH,
    }
    if dict(source) != fixed_source:
        raise NsisRepackContractError(
            "transformation.source is not the fixed audited Tauri source"
        )
    payload_path = _relative_path(
        transformation["payload_path"], "transformation.payload_path"
    )
    if payload_path != TAURI_TRANSFORMED_PAYLOAD_PATH:
        raise NsisRepackContractError(
            f"transformation.payload_path must be {TAURI_TRANSFORMED_PAYLOAD_PATH}"
        )
    before_token = _text(
        transformation["before_token"], "transformation.before_token", limit=64
    )
    after_token = _text(
        transformation["after_token"], "transformation.after_token", limit=64
    )
    if before_token != TAURI_BUNDLE_MARKER_UNKNOWN.decode("ascii"):
        raise NsisRepackContractError("transformation.before_token is not fixed")
    if after_token != TAURI_BUNDLE_MARKER_NSIS.decode("ascii"):
        raise NsisRepackContractError("transformation.after_token is not fixed")
    if len(before_token.encode("ascii")) != len(after_token.encode("ascii")):
        raise NsisRepackContractError("transformation tokens must have equal length")
    before = _normalize_transformation_identity(
        transformation["before"], "transformation.before"
    )
    after = _normalize_transformation_identity(
        transformation["after"], "transformation.after"
    )
    if before["size"] != after["size"]:
        raise NsisRepackContractError("transformation must preserve payload size")
    if (
        before["before_token_count"] != 1
        or before["after_token_count"] != 0
        or after["before_token_count"] != 0
        or after["after_token_count"] != 1
    ):
        raise NsisRepackContractError("transformation token counts are not reversible")
    marker_offset = _positive_int(
        transformation["marker_offset"],
        "transformation.marker_offset",
        allow_zero=True,
    )
    after_size = _positive_int(after["size"], "transformation.after.size")
    if marker_offset + len(TAURI_BUNDLE_MARKER_NSIS) > after_size:
        raise NsisRepackContractError("transformation.marker_offset is outside payload")
    records = [
        record
        for record in files
        if record["path"] == payload_path and record["role"] == "payload"
    ]
    if len(records) != 1:
        raise NsisRepackContractError(
            "transformation payload must be exactly one bound payload record"
        )
    if records[0]["size"] != after["size"] or records[0]["sha256"] != after["sha256"]:
        raise NsisRepackContractError(
            "transformation after identity is not bound by its payload record"
        )
    normalized: dict[str, object] = {
        "algorithm": TAURI_TRANSFORMATION_ALGORITHM,
        "source": fixed_source,
        "payload_path": payload_path,
        "before_token": before_token,
        "after_token": after_token,
        "marker_offset": marker_offset,
        "before": before,
        "after": after,
    }
    supplied_digest = _digest(
        transformation["transformation_sha256"],
        "transformation.transformation_sha256",
    )
    expected_digest = _transformation_digest(normalized)
    if supplied_digest != expected_digest:
        raise NsisRepackContractError(
            "transformation_sha256 does not match canonical transformation"
        )
    normalized["transformation_sha256"] = supplied_digest
    return normalized


def _verify_transformation_payload(
    content: Path, manifest: Mapping[str, object]
) -> None:
    transformation = _object(manifest["transformation"], "transformation")
    payload = _safe_child(
        content, str(transformation["payload_path"]), "transformation payload"
    )
    after = _object(transformation["after"], "transformation.after")
    size, digest, unknown_count, nsis_count, marker_offset = _marker_scan(
        payload, max_bytes=MAX_TAURI_HOST_BYTES
    )
    if (
        size != after["size"]
        or digest != after["sha256"]
        or unknown_count != after["before_token_count"]
        or nsis_count != after["after_token_count"]
        or marker_offset != transformation["marker_offset"]
    ):
        raise NsisRepackContractError(
            "transformation post payload identity or marker position does not match"
        )
    reconstructed = hashlib.sha256()
    remaining = int(transformation["marker_offset"])
    reconstructed_size = 0
    reconstructed_tail = b""
    reconstructed_unknown_count = 0
    reconstructed_nsis_count = 0
    reconstructed_marker_offset = -1

    def consume_reconstructed(block: bytes) -> None:
        nonlocal reconstructed_size
        nonlocal reconstructed_tail
        nonlocal reconstructed_unknown_count
        nonlocal reconstructed_nsis_count
        nonlocal reconstructed_marker_offset
        previous_total = reconstructed_size
        reconstructed.update(block)
        reconstructed_size += len(block)
        window = reconstructed_tail + block
        window_base = previous_total - len(reconstructed_tail)
        for marker, marker_name in (
            (TAURI_BUNDLE_MARKER_UNKNOWN, "unknown"),
            (TAURI_BUNDLE_MARKER_NSIS, "nsis"),
        ):
            search_from = 0
            while True:
                index = window.find(marker, search_from)
                if index < 0:
                    break
                absolute = window_base + index
                if absolute + len(marker) > previous_total:
                    if marker_name == "unknown":
                        reconstructed_unknown_count += 1
                    else:
                        reconstructed_nsis_count += 1
                    if reconstructed_marker_offset < 0:
                        reconstructed_marker_offset = absolute
                search_from = index + 1
        reconstructed_tail = window[-(len(TAURI_BUNDLE_MARKER_UNKNOWN) - 1) :]

    with _open_regular_file(payload, "transformation payload") as stream:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise NsisRepackContractError(
                    "transformation payload ended before marker"
                )
            consume_reconstructed(block)
            remaining -= len(block)
        marker = stream.read(len(TAURI_BUNDLE_MARKER_NSIS))
        if marker != TAURI_BUNDLE_MARKER_NSIS:
            raise NsisRepackContractError(
                "transformation NSS marker is not at its offset"
            )
        consume_reconstructed(TAURI_BUNDLE_MARKER_UNKNOWN)
        while block := stream.read(1024 * 1024):
            consume_reconstructed(block)
    before = _object(transformation["before"], "transformation.before")
    if (
        reconstructed_size != before["size"]
        or reconstructed.hexdigest() != before["sha256"]
        or reconstructed_unknown_count != before["before_token_count"]
        or reconstructed_nsis_count != before["after_token_count"]
        or reconstructed_marker_offset != transformation["marker_offset"]
    ):
        raise NsisRepackContractError(
            "transformation reversal does not reconstruct the exact preimage"
        )


def _kit_digest(manifest: Mapping[str, object]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("kit_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _normalize_contract(raw: object, *, manifest: bool) -> dict[str, object]:
    value = _object(raw, "NSIS repack contract")
    expected = {
        "schema_version",
        "source_ref",
        "source_sha",
        "source_tree",
        "source_epoch",
        "transformation",
        "toolchain",
        "argv",
        "environment",
        "cleared_environment",
        "files",
        "expected_unsigned_installer",
    }
    if manifest:
        expected |= {"artifact", "kit_sha256", "normalization"}
    else:
        expected |= {"path_mappings"}
    _exact_fields(value, expected, "NSIS repack contract")
    if value["schema_version"] != SCHEMA_VERSION:
        raise NsisRepackContractError("schema_version must be 1")
    if manifest and value["artifact"] != KIT_ARTIFACT:
        raise NsisRepackContractError(f"artifact must be {KIT_ARTIFACT}")
    source_ref = _source_ref(value["source_ref"], "source_ref")
    source_sha = _git_id(value["source_sha"], "source_sha")
    source_tree = _git_id(value["source_tree"], "source_tree")
    source_epoch = _source_epoch(value["source_epoch"], "source_epoch")
    cleared_environment = _normalize_cleared_environment(value["cleared_environment"])
    files = _normalize_file_records(value["files"])
    transformation = _normalize_transformation(value["transformation"], files)
    toolchain = _normalize_toolchain(value["toolchain"], files, manifest=manifest)
    argv = _normalize_argv(value["argv"], files)
    environment = _normalize_environment(value["environment"], source_epoch)
    expected_output = _normalize_expected_output(value["expected_unsigned_installer"])
    all_paths = [str(record["path"]) for record in files] + [
        str(expected_output["path"]),
        KIT_MANIFEST,
    ]
    _assert_case_unique(all_paths, "kit paths")
    normalized: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "artifact": KIT_ARTIFACT,
        "source_ref": source_ref,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "source_epoch": source_epoch,
        "transformation": transformation,
        "toolchain": toolchain,
        "argv": argv,
        "environment": environment,
        "cleared_environment": cleared_environment,
        "files": files,
        "expected_unsigned_installer": expected_output,
    }
    if manifest:
        normalized["normalization"] = _normalize_normalization(
            value["normalization"], files, expected_output
        )
    else:
        normalized["path_mappings"] = _normalize_path_mappings(
            value["path_mappings"], files, expected_output
        )
        return normalized
    expected_digest = _kit_digest(normalized)
    supplied = _digest(value["kit_sha256"], "kit_sha256")
    if supplied != expected_digest:
        raise NsisRepackContractError("kit_sha256 does not match canonical content")
    normalized["kit_sha256"] = expected_digest
    return normalized


def _strip_nsis_comment(line: str) -> str:
    in_quote = False
    for index, character in enumerate(line):
        if character == '"':
            in_quote = not in_quote
        elif character == ";" and not in_quote:
            return line[:index]
    return line


def _normalize_rendered_script(
    content: Path,
    files: list[dict[str, object]],
    mappings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    scripts = [record for record in files if record["role"] == "nsis-rendered-script"]
    if len(scripts) != 1:
        raise NsisRepackContractError("exactly one rendered script is required")
    script = scripts[0]
    path = _safe_child(content, str(script["path"]), "rendered script")
    payload = _read_regular_file(path, "rendered script", limit=4 * 1024 * 1024)
    raw_source_digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != script["size"] or raw_source_digest != script["sha256"]:
        raise NsisRepackContractError("rendered script source identity mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise NsisRepackContractError("rendered script must be UTF-8") from error
    if "@STOCK_DESK_PATH_MAP[" in text:
        raise NsisRepackContractError(
            "rendered script contains a reserved normalization marker"
        )
    structural_text = text
    mapped_targets: list[dict[str, object]] = []
    for mapping in mappings:
        source = str(mapping["source_absolute"])
        target = str(mapping["target"])
        expected_count = _positive_int(
            mapping["occurrences"], "normalized path mapping occurrences"
        )
        actual_count = text.count(source)
        if actual_count != expected_count:
            raise NsisRepackContractError(
                f"rendered path mapping occurrence mismatch for {target}"
            )
        structural_text = structural_text.replace(
            source, f"@STOCK_DESK_PATH_MAP[{target}]@"
        )
        text = text.replace(source, target.replace("/", "\\"))
        mapped_targets.append({"target": target, "occurrence_count": expected_count})
    for mapping in mappings:
        if str(mapping["source_absolute"]) in text:
            raise NsisRepackContractError("rendered script retains an absolute source")
    if _WINDOWS_DRIVE_PATH.search(text) or re.search(r"\\\\[^\\\s]+\\", text):
        raise NsisRepackContractError(
            "rendered script contains an unmapped absolute path"
        )
    normalized_payload = text.encode("utf-8")
    source_digest = hashlib.sha256(structural_text.encode("utf-8")).hexdigest()
    target_digest = hashlib.sha256(normalized_payload).hexdigest()
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    try:
        with path.open("wb") as stream:
            stream.write(normalized_payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise NsisRepackContractError(
            "could not write normalized rendered script"
        ) from error
    os.chmod(path, 0o400)
    os.chmod(path.parent, 0o500)
    script["size"] = len(normalized_payload)
    script["sha256"] = target_digest
    return {
        "algorithm": "tauri-rendered-nsis-exact-path-map-v1",
        "raw_source_sha256": raw_source_digest,
        "structural_sha256": source_digest,
        "normalized_sha256": target_digest,
        "mapped_targets": sorted(mapped_targets, key=lambda item: str(item["target"])),
    }


def _nsis_tokens(line: str, field: str) -> list[str]:
    try:
        import shlex

        return shlex.split(line, posix=True, comments=False)
    except ValueError as error:
        raise NsisRepackContractError(f"{field} contains malformed quoting") from error


def _dead_unsigned_uninstaller_finalize_lines(
    text: str, relative: str
) -> frozenset[int]:
    """Allow only Tauri's one provably dead unsigned-uninstaller finalize branch."""

    statements: list[tuple[int, list[str]]] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_nsis_comment(raw_line).strip()
        if line:
            statements.append((number, _nsis_tokens(line, f"{relative}:{number}")))
    finalize_positions = [
        index
        for index, (_number, tokens) in enumerate(statements)
        if tokens and tokens[0].casefold() == "!uninstfinalize"
    ]
    if not finalize_positions:
        return frozenset()
    matching_defines = [
        (index, tokens)
        for index, (_number, tokens) in enumerate(statements)
        if len(tokens) >= 2
        and tokens[0].casefold() in {"!define", "!undef"}
        and tokens[1].casefold() == "uninstallersigncommand"
    ]
    if (
        len(finalize_positions) != 1
        or len(matching_defines) != 1
        or matching_defines[0][1] != ["!define", "UNINSTALLERSIGNCOMMAND", ""]
    ):
        raise NsisRepackContractError(
            f"{relative} contains a forbidden unproven uninstaller finalize command"
        )
    finalize_index = finalize_positions[0]
    define_index = matching_defines[0][0]
    expected_if = ["!if", "${UNINSTALLERSIGNCOMMAND}", "!=", ""]
    expected_finalize = ["!uninstfinalize", "${UNINSTALLERSIGNCOMMAND}"]
    if (
        define_index >= finalize_index - 1
        or finalize_index < 1
        or finalize_index + 1 >= len(statements)
        or statements[finalize_index - 1][1] != expected_if
        or statements[finalize_index][1] != expected_finalize
        or statements[finalize_index + 1][1] != ["!endif"]
    ):
        raise NsisRepackContractError(
            f"{relative} has an unsafe uninstaller finalize branch"
        )
    return frozenset({statements[finalize_index][0]})


def _audit_scripts(kit_content: Path, manifest: Mapping[str, object]) -> None:
    records = manifest["files"]
    assert isinstance(records, list)
    bound_paths = {str(record["path"]) for record in records}
    include_paths = {
        str(record["path"])
        for record in records
        if record["role"] in {"nsis-include", "nsis-hook", "nsis-language"}
        or (
            record["role"] == "nsis-toolchain"
            and str(record["path"]).startswith("toolchain/Include/")
        )
    }
    plugin_names = {
        str(plugin["name"])
        for plugin in manifest["toolchain"]["plugins"]  # type: ignore[index]
    }
    plugin_directories = {
        str(PurePosixPath(str(plugin["path"])).parent)
        for plugin in manifest["toolchain"]["plugins"]  # type: ignore[index]
    }
    environment = manifest["environment"]
    assert isinstance(environment, Mapping)
    declared_environment = set(environment)
    environment_reference = re.compile(r"\$%([^%\r\n]+)%")
    defines: dict[str, str] = {}
    script_texts: dict[str, str] = {}
    preprocessor_definitions: dict[str, set[str]] = {}

    def parse_define(tokens: Sequence[str], *, field: str) -> tuple[str, Sequence[str]]:
        if len(tokens) < 2:
            raise NsisRepackContractError(f"{field} has no symbol name")
        name_index = 1
        if tokens[1].startswith("/"):
            if tokens[1].casefold() != "/ifndef":
                raise NsisRepackContractError(
                    f"{field} uses an unsupported !define option"
                )
            name_index = 2
        if len(tokens) <= name_index:
            raise NsisRepackContractError(f"{field} has no symbol name")
        name = tokens[name_index]
        if re.fullmatch(r"[A-Za-z0-9_]+", name) is None:
            raise NsisRepackContractError(f"{field} has an invalid symbol name")
        return name, tokens[name_index + 1 :]

    for record in records:
        if record["role"] not in _SCRIPT_ROLES:
            continue
        relative = str(record["path"])
        payload = _read_regular_file(
            _safe_child(kit_content, relative, relative),
            relative,
            limit=4 * 1024 * 1024,
        )
        try:
            text = payload.decode("utf-8")
        except UnicodeError as error:
            raise NsisRepackContractError(f"{relative} must be UTF-8") from error
        if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
            raise NsisRepackContractError(f"{relative} contains a control character")
        for match in environment_reference.finditer(text):
            name = match.group(1)
            if _ENV_NAME.fullmatch(name) is None or name not in declared_environment:
                raise NsisRepackContractError(
                    f"{relative} reads an undeclared environment variable"
                )
        if "$%" in environment_reference.sub("", text):
            raise NsisRepackContractError(
                f"{relative} contains a dynamic undeclared environment reference"
            )
        script_texts[relative] = text
        for number, raw_line in enumerate(text.splitlines(), start=1):
            line = _strip_nsis_comment(raw_line).strip()
            if not line:
                continue
            tokens = _nsis_tokens(line, f"{relative}:{number}")
            if tokens and tokens[0].casefold() == "!define":
                name, values = parse_define(tokens, field=f"{relative}:{number}")
                preprocessor_definitions.setdefault(name, set()).add(" ".join(values))

    macro_reference = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

    def expand_instruction(value: str) -> set[str]:
        pending = {value}
        completed: set[str] = set()
        for _depth in range(16):
            next_pending: set[str] = set()
            for candidate in pending:
                match = macro_reference.search(candidate)
                if match is None:
                    completed.add(candidate)
                    continue
                replacements = preprocessor_definitions.get(match.group(1))
                if not replacements:
                    completed.add(candidate)
                    continue
                for replacement in replacements:
                    next_pending.add(
                        candidate[: match.start()]
                        + replacement
                        + candidate[match.end() :]
                    )
            if len(completed) + len(next_pending) > 256:
                raise NsisRepackContractError(
                    "dynamic preprocessor instruction expansion is not bounded"
                )
            if not next_pending:
                return completed
            pending = next_pending
        raise NsisRepackContractError(
            "dynamic preprocessor instruction expansion is recursive"
        )

    def resolve_path(raw: str, *, field: str) -> str:
        macro = re.fullmatch(r"\$\{([A-Za-z0-9_]+)\}", raw)
        if macro is not None:
            try:
                return defines[macro.group(1)]
            except KeyError as error:
                raise NsisRepackContractError(
                    f"{field} uses an unbound path definition"
                ) from error
        return _relative_path(raw.replace("\\", "/"), field)

    for relative, text in script_texts.items():
        allowed_dead_finalize_lines = _dead_unsigned_uninstaller_finalize_lines(
            text, relative
        )
        for number, raw_line in enumerate(text.splitlines(), start=1):
            line = _strip_nsis_comment(raw_line).strip()
            if not line:
                continue
            if _WINDOWS_ABSOLUTE.search(line) or _WINDOWS_DRIVE_PATH.search(line):
                raise NsisRepackContractError(
                    f"{relative}:{number} contains an absolute path"
                )
            tokens = _nsis_tokens(line, f"{relative}:{number}")
            if not tokens:
                continue
            instruction = tokens[0].casefold()
            for expanded in expand_instruction(tokens[0]):
                if expanded == tokens[0]:
                    continue
                expanded_tokens = _nsis_tokens(
                    expanded, f"{relative}:{number} expanded instruction"
                )
                expanded_instruction = (
                    expanded_tokens[0].casefold() if expanded_tokens else ""
                )
                if expanded_instruction in {
                    "file",
                    "outfile",
                    "!include",
                    "!addplugindir",
                }:
                    raise NsisRepackContractError(
                        f"{relative}:{number} contains a dynamic preprocessor instruction"
                    )
                if (
                    expanded_instruction in _FORBIDDEN_EXTERNAL_COMPILE_CONTROLS
                    or expanded_instruction.startswith("nsexec::")
                ):
                    raise NsisRepackContractError(
                        f"{relative}:{number} contains a dynamic preprocessor instruction"
                    )
            if instruction.startswith("!${"):
                raise NsisRepackContractError(
                    f"{relative}:{number} contains a dynamic preprocessor instruction"
                )
            if instruction in _FORBIDDEN_EXTERNAL_COMPILE_CONTROLS and not (
                instruction == "!uninstfinalize"
                and number in allowed_dead_finalize_lines
            ):
                raise NsisRepackContractError(
                    f"{relative}:{number} contains a forbidden compile-time execution"
                )
            if instruction.startswith("nsexec::"):
                raise NsisRepackContractError(
                    f"{relative}:{number} contains a forbidden control instruction"
                )
            if instruction == "!define":
                name, values = parse_define(tokens, field=f"{relative}:{number}")
                if len(values) != 1:
                    continue
                candidate = values[0]
                try:
                    normalized_candidate = _relative_path(
                        candidate.replace("\\", "/"),
                        f"{relative}:{number} definition",
                    )
                except NsisRepackContractError:
                    continue
                if (
                    normalized_candidate in bound_paths
                    or normalized_candidate in plugin_directories
                    or normalized_candidate
                    == str(manifest["expected_unsigned_installer"]["path"])  # type: ignore[index]
                ):
                    if name in defines:
                        raise NsisRepackContractError(
                            f"{relative}:{number} redefines a bound path"
                        )
                    defines[name] = normalized_candidate
                continue
            if instruction == "file":
                sources = []
                for token in tokens[1:]:
                    option = token.casefold()
                    if option == "/a":
                        raise NsisRepackContractError(
                            f"{relative}:{number} cannot preserve source attributes"
                        )
                    if option in {"/r", "/nonfatal"} or option.startswith(
                        ("/oname=", "/x=")
                    ):
                        continue
                    sources.append(token)
                if len(sources) != 1:
                    raise NsisRepackContractError(
                        f"{relative}:{number} has an unknown File source"
                    )
                source = sources[0]
                if any(marker in source for marker in ("*", "?")):
                    raise NsisRepackContractError(
                        f"{relative}:{number} has a dynamic File source"
                    )
                normalized = resolve_path(
                    source, field=f"{relative}:{number} File source"
                )
                if normalized not in bound_paths:
                    raise NsisRepackContractError(
                        f"{relative}:{number} File source is not in the kit"
                    )
            elif instruction == "outfile":
                if len(tokens) != 2:
                    raise NsisRepackContractError(
                        f"{relative}:{number} has an unknown OutFile target"
                    )
                output = resolve_path(
                    tokens[1], field=f"{relative}:{number} OutFile target"
                )
                if output != str(manifest["expected_unsigned_installer"]["path"]):  # type: ignore[index]
                    raise NsisRepackContractError(
                        f"{relative}:{number} OutFile target is not the expected output"
                    )
            elif instruction == "!include":
                if len(tokens) != 2:
                    raise NsisRepackContractError(
                        f"{relative}:{number} has an unknown include source"
                    )
                included = resolve_path(tokens[1], field=f"{relative}:{number} include")
                matching_includes = {
                    path
                    for path in include_paths
                    if path == included or path.endswith(f"/{included}")
                }
                if len(matching_includes) != 1:
                    raise NsisRepackContractError(
                        f"{relative}:{number} include is not in the kit"
                    )
            elif instruction == "!addplugindir":
                if len(tokens) != 2:
                    raise NsisRepackContractError(
                        f"{relative}:{number} has an unknown plugin directory"
                    )
                plugin_directory = resolve_path(
                    tokens[1], field=f"{relative}:{number} plugin directory"
                )
                if plugin_directory not in plugin_directories:
                    raise NsisRepackContractError(
                        f"{relative}:{number} plugin directory is not bound"
                    )
            else:
                plugin_match = _PLUGIN_CALL.match(tokens[0])
                if (
                    plugin_match is not None
                    and plugin_match.group(1) not in plugin_names
                ):
                    raise NsisRepackContractError(
                        f"{relative}:{number} invokes an unknown plugin"
                    )


def _verify_snapshot_files(kit: Path, manifest: Mapping[str, object]) -> None:
    content = kit / "content"
    if content.is_symlink() or not content.is_dir():
        raise NsisRepackContractError("kit content directory is missing or unsafe")
    records = manifest["files"]
    assert isinstance(records, list)
    expected: set[str] = set()
    for index, record in enumerate(records):
        relative = str(record["path"])
        path = _safe_child(content, relative, f"files[{index}]")
        size, digest = _hash_regular_file(path, f"files[{index}]")
        if size != record["size"] or digest != record["sha256"]:
            raise NsisRepackContractError(f"files[{index}] content identity mismatch")
        expected.add(relative)
    actual: set[str] = set()
    actual_directories: set[str] = set()
    for root, directories, filenames in os.walk(content, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *filenames]:
            candidate = root_path / name
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode):
                raise NsisRepackContractError("kit content contains a link")
        for name in directories:
            actual_directories.add((root_path / name).relative_to(content).as_posix())
        for name in filenames:
            actual.add((root_path / name).relative_to(content).as_posix())
    _assert_case_unique(sorted(actual), "snapshot files")
    if actual != expected:
        raise NsisRepackContractError("kit content has missing or unbound files")
    expected_directories: set[str] = set()
    for relative in expected:
        for parent in PurePosixPath(relative).parents:
            if parent != PurePosixPath("."):
                expected_directories.add(parent.as_posix())
    if actual_directories != expected_directories:
        raise NsisRepackContractError("kit content has unbound directories")
    _verify_transformation_payload(content, manifest)
    _audit_scripts(content, manifest)


def create_kit(
    *,
    descriptor: Path,
    source_root: Path,
    output: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise NsisRepackContractError("output kit must not already exist")
    if not descriptor.name or descriptor.name in {".", ".."}:
        raise NsisRepackContractError("descriptor path is invalid")
    try:
        source_root.resolve(strict=True)
    except OSError as error:
        raise NsisRepackContractError("source root does not exist") from error
    descriptor_contract = _normalize_contract(
        _read_descriptor_secure(descriptor), manifest=False
    )
    if descriptor_contract["source_ref"] != _source_ref(
        expected_source_ref, "expected_source_ref"
    ):
        raise NsisRepackContractError(
            "descriptor source_ref does not match expected_source_ref"
        )
    if descriptor_contract["source_sha"] != _git_id(
        expected_source_sha, "expected_source_sha"
    ):
        raise NsisRepackContractError(
            "descriptor source_sha does not match expected_source_sha"
        )
    if descriptor_contract["source_tree"] != _git_id(
        expected_source_tree, "expected_source_tree"
    ):
        raise NsisRepackContractError(
            "descriptor source_tree does not match expected_source_tree"
        )
    if descriptor_contract["source_epoch"] != _source_epoch(
        expected_source_epoch, "expected_source_epoch"
    ):
        raise NsisRepackContractError(
            "descriptor source_epoch does not match expected_source_epoch"
        )
    try:
        output_parent = output.parent.absolute()
        output_parent_metadata = output_parent.stat(follow_symlinks=False)
    except OSError as error:
        raise NsisRepackContractError(
            "output parent must already exist safely"
        ) from error
    if (
        not stat.S_ISDIR(output_parent_metadata.st_mode)
        or stat.S_ISLNK(output_parent_metadata.st_mode)
        or int(getattr(output_parent_metadata, "st_file_attributes", 0)) & 0x400
    ):
        raise NsisRepackContractError("output parent must be a non-link directory")
    private_stage = "lease creation"
    try:
        with private_directory_lease(output.absolute()):
            private_stage = "source snapshot"
            records = descriptor_contract["files"]
            assert isinstance(records, list)
            _snapshot_source_files(source_root, records, output / "content")
            private_stage = "render normalization"
            mappings = descriptor_contract.pop("path_mappings")
            assert isinstance(mappings, list)
            normalization = _normalize_rendered_script(
                output / "content", records, mappings
            )
            normalized = {
                "schema_version": SCHEMA_VERSION,
                "artifact": KIT_ARTIFACT,
                **descriptor_contract,
                "normalization": normalization,
            }
            normalized["kit_sha256"] = _kit_digest(normalized)
            private_stage = "snapshot verification"
            _verify_snapshot_files(output, normalized)
            private_stage = "manifest write"
            _write_new_file(
                output / KIT_MANIFEST,
                _canonical_json(normalized),
                "kit manifest",
            )
            private_stage = "lease verification"
    except SecureArtifactSnapshotError as error:
        raise NsisRepackContractError(
            f"private kit root failed during {private_stage}: {error}"
        ) from error
    return normalized


def _verify_private_kit(
    *,
    kit: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    if kit.is_symlink() or not kit.is_dir():
        raise NsisRepackContractError("kit must be a non-link directory")
    manifest_bytes = _read_regular_file(
        kit / KIT_MANIFEST, "kit manifest", limit=MAX_JSON_BYTES
    )
    manifest = _normalize_contract(
        _parse_json(manifest_bytes, "kit manifest"), manifest=True
    )
    if manifest_bytes != _canonical_json(manifest):
        raise NsisRepackContractError("kit manifest must use canonical JSON encoding")
    if manifest["source_ref"] != _source_ref(
        expected_source_ref, "expected_source_ref"
    ):
        raise NsisRepackContractError("kit source_ref does not match expectation")
    if manifest["source_sha"] != _git_id(expected_source_sha, "expected_source_sha"):
        raise NsisRepackContractError("kit source_sha does not match expectation")
    if manifest["source_tree"] != _git_id(expected_source_tree, "expected_source_tree"):
        raise NsisRepackContractError("kit source_tree does not match expectation")
    if manifest["source_epoch"] != _source_epoch(
        expected_source_epoch, "expected_source_epoch"
    ):
        raise NsisRepackContractError("kit source_epoch does not match expectation")
    if manifest["kit_sha256"] != _digest(expected_kit_sha256, "expected_kit_sha256"):
        raise NsisRepackContractError("kit_sha256 does not match expectation")
    top_level = {entry.name for entry in kit.iterdir()}
    if top_level != {KIT_MANIFEST, "content"}:
        raise NsisRepackContractError("kit root contains unknown entries")
    _verify_snapshot_files(kit, manifest)
    return manifest


@contextmanager
def _verified_kit_snapshot(
    *,
    kit: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> Iterator[tuple[dict[str, object], Path]]:
    """Keep the exact verified private snapshot alive for its consumer."""
    if kit.is_symlink() or not kit.is_dir():
        raise NsisRepackContractError("kit must be a non-link directory")
    with tempfile.TemporaryDirectory(prefix="stock-desk-verify-nsis-kit-") as temporary:
        snapshot = (Path(temporary) / "snapshot").resolve(strict=False)
        try:
            snapshot_artifacts(
                kit.absolute(),
                [KIT_MANIFEST, "content"],
                snapshot.absolute(),
                limits=SnapshotLimits(
                    max_files=MAX_FILES + 1,
                    max_file_size=MAX_FILE_BYTES,
                    max_total_size=8 * 1024 * 1024 * 1024,
                    max_depth=33,
                ),
                allow_windows_hardlinks=False,
                reject_empty_directories=True,
                closed_source_root=True,
            )
        except SecureArtifactSnapshotError as error:
            raise NsisRepackContractError(
                "could not secure the kit for verification"
            ) from error
        manifest = _verify_private_kit(
            kit=snapshot,
            expected_source_ref=expected_source_ref,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_source_epoch=expected_source_epoch,
            expected_kit_sha256=expected_kit_sha256,
        )
        yield manifest, snapshot


def verify_kit(
    *,
    kit: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    """Verify only a private secure snapshot, never the caller's mutable tree."""
    with _verified_kit_snapshot(
        kit=kit,
        expected_source_ref=expected_source_ref,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_source_epoch=expected_source_epoch,
        expected_kit_sha256=expected_kit_sha256,
    ) as (manifest, _snapshot):
        return dict(manifest)


def _receipt_digest(receipt: Mapping[str, object]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _normalize_receipt(raw: object) -> dict[str, object]:
    receipt = _object(raw, "NSIS repack receipt")
    _exact_fields(
        receipt,
        {
            "schema_version",
            "artifact",
            "receipt_sha256",
            "kit_sha256",
            "transformation_sha256",
            "repack_slot",
            "source_ref",
            "source_sha",
            "source_tree",
            "source_epoch",
            "argv",
            "environment",
            "cleared_environment",
            "output",
        },
        "NSIS repack receipt",
    )
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise NsisRepackContractError("receipt schema_version must be 1")
    if receipt["artifact"] != RECEIPT_ARTIFACT:
        raise NsisRepackContractError(f"receipt artifact must be {RECEIPT_ARTIFACT}")
    source_epoch = _source_epoch(receipt["source_epoch"], "receipt.source_epoch")
    argv = [
        _text(argument, f"receipt.argv[{index}]", limit=1024)
        for index, argument in enumerate(_array(receipt["argv"], "receipt.argv"))
    ]
    if tuple(argv) != _OFFICIAL_ARGV:
        raise NsisRepackContractError("receipt.argv is not the audited invocation")
    output = _object(receipt["output"], "receipt.output")
    _exact_fields(output, {"path", "size", "sha256"}, "receipt.output")
    output_size = _positive_int(output["size"], "receipt.output.size")
    if output_size > MAX_FILE_BYTES:
        raise NsisRepackContractError("receipt.output.size exceeds size limit")
    normalized: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "artifact": RECEIPT_ARTIFACT,
        "kit_sha256": _digest(receipt["kit_sha256"], "receipt.kit_sha256"),
        "transformation_sha256": _digest(
            receipt["transformation_sha256"], "receipt.transformation_sha256"
        ),
        "repack_slot": _repack_slot(receipt["repack_slot"], "receipt.repack_slot"),
        "source_ref": _source_ref(receipt["source_ref"], "receipt.source_ref"),
        "source_sha": _git_id(receipt["source_sha"], "receipt.source_sha"),
        "source_tree": _git_id(receipt["source_tree"], "receipt.source_tree"),
        "source_epoch": source_epoch,
        "argv": argv,
        "environment": _normalize_environment(receipt["environment"], source_epoch),
        "cleared_environment": _normalize_cleared_environment(
            receipt["cleared_environment"]
        ),
        "output": {
            "path": _relative_path(output["path"], "receipt.output.path"),
            "size": output_size,
            "sha256": _digest(output["sha256"], "receipt.output.sha256"),
        },
    }
    supplied = _digest(receipt["receipt_sha256"], "receipt.receipt_sha256")
    expected = _receipt_digest(normalized)
    if supplied != expected:
        raise NsisRepackContractError("receipt_sha256 does not match canonical receipt")
    normalized["receipt_sha256"] = supplied
    return normalized


def verify_receipt(
    *,
    receipt: Path,
    kit: Path,
    output: Path,
    expected_repack_slot: str,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    """Close a receipt over the exact kit and installer bytes using private snapshots."""
    with tempfile.TemporaryDirectory(
        prefix="stock-desk-verify-nsis-receipt-"
    ) as temporary:
        private_root = Path(temporary).resolve(strict=True)
        try:
            receipt_snapshot = snapshot_artifacts(
                receipt.parent.absolute(),
                [receipt.name],
                (private_root / "receipt").absolute(),
                limits=SnapshotLimits(
                    max_files=1,
                    max_file_size=MAX_JSON_BYTES,
                    max_total_size=MAX_JSON_BYTES,
                    max_depth=1,
                ),
                allow_windows_hardlinks=False,
            )
            output_snapshot = snapshot_artifacts(
                output.parent.absolute(),
                [output.name],
                (private_root / "output").absolute(),
                limits=SnapshotLimits(
                    max_files=1,
                    max_file_size=MAX_FILE_BYTES,
                    max_total_size=MAX_FILE_BYTES,
                    max_depth=1,
                ),
                allow_windows_hardlinks=False,
            )
        except SecureArtifactSnapshotError as error:
            raise NsisRepackContractError(
                "could not secure receipt or installer for verification"
            ) from error
        receipt_bytes = _read_regular_file(
            receipt_snapshot.root / receipt.name,
            "receipt",
            limit=MAX_JSON_BYTES,
        )
        normalized = _normalize_receipt(_parse_json(receipt_bytes, "receipt"))
        if receipt_bytes != _canonical_json(normalized):
            raise NsisRepackContractError("receipt must use canonical JSON encoding")
        if normalized["repack_slot"] != _repack_slot(
            expected_repack_slot, "expected_repack_slot"
        ):
            raise NsisRepackContractError(
                "receipt repack_slot does not match expected_repack_slot"
            )
        with _verified_kit_snapshot(
            kit=kit,
            expected_source_ref=expected_source_ref,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_source_epoch=expected_source_epoch,
            expected_kit_sha256=expected_kit_sha256,
        ) as (manifest, _kit_snapshot):
            for field in (
                "kit_sha256",
                "source_ref",
                "source_sha",
                "source_tree",
                "source_epoch",
                "argv",
                "environment",
                "cleared_environment",
            ):
                if normalized[field] != manifest[field]:
                    raise NsisRepackContractError(
                        f"receipt.{field} does not match the verified kit"
                    )
            transformation = _object(manifest["transformation"], "transformation")
            if (
                normalized["transformation_sha256"]
                != transformation["transformation_sha256"]
            ):
                raise NsisRepackContractError(
                    "receipt.transformation_sha256 does not match the verified kit"
                )
            expected_output = _object(
                manifest["expected_unsigned_installer"],
                "expected_unsigned_installer",
            )
        output_record = normalized["output"]
        assert isinstance(output_record, Mapping)
        if dict(output_record) != dict(expected_output):
            raise NsisRepackContractError(
                "receipt output does not match the verified kit output"
            )
        captured = output_snapshot.files[0]
        if (
            output_record["size"] != captured.size
            or output_record["sha256"] != captured.sha256
        ):
            raise NsisRepackContractError(
                "receipt output does not match the installer bytes"
            )
        return normalized


def normalize_provenance_summary(value: object) -> dict[str, object]:
    """Strictly normalize the public eleven-field NSIS provenance summary."""

    summary = _object(value, "NSIS provenance summary")
    _exact_fields(summary, set(PROVENANCE_SUMMARY_FIELDS), "NSIS provenance summary")
    schema_version = _positive_int(
        summary["schema_version"], "NSIS provenance summary.schema_version"
    )
    if schema_version != SCHEMA_VERSION:
        raise NsisRepackContractError("provenance summary schema_version must be 1")
    if summary["artifact"] != PROVENANCE_SET_ARTIFACT:
        raise NsisRepackContractError(
            f"provenance summary artifact must be {PROVENANCE_SET_ARTIFACT}"
        )

    kit = _object(summary["kit"], "NSIS provenance summary.kit")
    _exact_fields(kit, {"path", "sha256", "kit_sha256"}, "NSIS provenance summary.kit")
    kit_path = _relative_path(kit["path"], "NSIS provenance summary.kit.path")
    expected_kit_path = f"nsis-repack-kit/{KIT_MANIFEST}"
    if kit_path != expected_kit_path:
        raise NsisRepackContractError(
            f"provenance summary kit path must be {expected_kit_path}"
        )

    transformation_raw = _object(
        summary["transformation"], "NSIS provenance summary.transformation"
    )
    after_raw = _object(
        transformation_raw.get("after"),
        "NSIS provenance summary.transformation.after",
    )
    transformation = _normalize_transformation(
        transformation_raw,
        [
            {
                "path": TAURI_TRANSFORMED_PAYLOAD_PATH,
                "role": "payload",
                "size": after_raw.get("size"),
                "sha256": after_raw.get("sha256"),
            }
        ],
    )
    transformation_sha256 = _digest(
        summary["transformation_sha256"],
        "NSIS provenance summary.transformation_sha256",
    )
    if transformation_sha256 != transformation["transformation_sha256"]:
        raise NsisRepackContractError(
            "provenance summary transformation digest is inconsistent"
        )

    receipt_values = _array(summary["receipts"], "NSIS provenance summary.receipts")
    expected_receipts = (
        ("a", "nsis-repack-verification/repack-a-receipt.json"),
        ("b", "nsis-repack-verification/repack-b-receipt.json"),
    )
    if len(receipt_values) != len(expected_receipts):
        raise NsisRepackContractError(
            "provenance summary must contain exactly two receipts"
        )
    receipts: list[dict[str, object]] = []
    for index, ((expected_slot, expected_path), raw) in enumerate(
        zip(expected_receipts, receipt_values, strict=True)
    ):
        receipt = _object(raw, f"NSIS provenance summary.receipts[{index}]")
        _exact_fields(
            receipt,
            {"path", "repack_slot", "sha256", "receipt_sha256"},
            f"NSIS provenance summary.receipts[{index}]",
        )
        path = _relative_path(
            receipt["path"], f"NSIS provenance summary.receipts[{index}].path"
        )
        slot = _repack_slot(
            receipt["repack_slot"],
            f"NSIS provenance summary.receipts[{index}].repack_slot",
        )
        if slot != expected_slot or path != expected_path:
            raise NsisRepackContractError(
                "provenance summary receipt order, slot, or path is invalid"
            )
        receipts.append(
            {
                "path": path,
                "repack_slot": slot,
                "sha256": _digest(
                    receipt["sha256"],
                    f"NSIS provenance summary.receipts[{index}].sha256",
                ),
                "receipt_sha256": _digest(
                    receipt["receipt_sha256"],
                    f"NSIS provenance summary.receipts[{index}].receipt_sha256",
                ),
            }
        )

    installer = _object(summary["installer"], "NSIS provenance summary.installer")
    _exact_fields(installer, {"size", "sha256"}, "NSIS provenance summary.installer")
    installer_size = _positive_int(
        installer["size"], "NSIS provenance summary.installer.size"
    )
    if installer_size > MAX_FILE_BYTES:
        raise NsisRepackContractError(
            "provenance summary installer exceeds the file-size limit"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": PROVENANCE_SET_ARTIFACT,
        "source_ref": _source_ref(
            summary["source_ref"], "NSIS provenance summary.source_ref"
        ),
        "source_sha": _git_id(
            summary["source_sha"], "NSIS provenance summary.source_sha"
        ),
        "source_tree": _git_id(
            summary["source_tree"], "NSIS provenance summary.source_tree"
        ),
        "source_epoch": _source_epoch(
            summary["source_epoch"], "NSIS provenance summary.source_epoch"
        ),
        "kit": {
            "path": kit_path,
            "sha256": _digest(kit["sha256"], "NSIS provenance summary.kit.sha256"),
            "kit_sha256": _digest(
                kit["kit_sha256"], "NSIS provenance summary.kit.kit_sha256"
            ),
        },
        "transformation": transformation,
        "transformation_sha256": transformation_sha256,
        "receipts": receipts,
        "installer": {
            "size": installer_size,
            "sha256": _digest(
                installer["sha256"], "NSIS provenance summary.installer.sha256"
            ),
        },
    }


def verify_provenance_set(
    *,
    candidate_root: Path,
    installer: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    """Secure and semantically close one kit, two slotted receipts, and installer."""

    candidate_root = candidate_root.absolute()
    installer = installer.absolute()
    try:
        installer_relative_path = installer.relative_to(candidate_root)
    except ValueError as error:
        raise NsisRepackContractError(
            "promoted installer must be inside the candidate root"
        ) from error
    if len(installer_relative_path.parts) != 1:
        raise NsisRepackContractError(
            "promoted installer must be a direct candidate-root file"
        )
    installer_relative = _relative_path(
        PurePosixPath(*installer_relative_path.parts).as_posix(),
        "promoted installer path",
    )
    if installer_relative.casefold() in {
        "nsis-repack-kit",
        "nsis-repack-verification",
    }:
        raise NsisRepackContractError("promoted installer path collides with evidence")
    expected_entries = [
        "nsis-repack-kit",
        "nsis-repack-verification",
        installer_relative,
    ]
    try:
        root_metadata = os.lstat(candidate_root)
    except OSError as error:
        raise NsisRepackContractError("candidate root is missing or unsafe") from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or _is_reparse(root_metadata)
    ):
        raise NsisRepackContractError("candidate root must be a non-link directory")
    source_entries = sorted(entry.name for entry in os.scandir(candidate_root))
    _assert_case_unique(source_entries, "candidate root entries")
    if not set(expected_entries).issubset(source_entries):
        raise NsisRepackContractError(
            "candidate root is missing NSIS provenance evidence"
        )
    with tempfile.TemporaryDirectory(
        prefix="stock-desk-verify-nsis-provenance-"
    ) as temporary:
        snapshot_root = (Path(temporary) / "candidate").resolve(strict=False)
        try:
            snapshot_artifacts(
                candidate_root,
                source_entries,
                snapshot_root,
                limits=SnapshotLimits(
                    max_files=MAX_FILES + 4,
                    max_file_size=MAX_FILE_BYTES,
                    max_total_size=10 * 1024 * 1024 * 1024,
                    max_depth=35,
                ),
                allow_windows_hardlinks=False,
                reject_empty_directories=True,
                closed_source_root=True,
            )
        except SecureArtifactSnapshotError as error:
            raise NsisRepackContractError(
                "could not secure the unsafe NSIS provenance set"
            ) from error
        verification_root = snapshot_root / "nsis-repack-verification"
        if verification_root.is_symlink() or not verification_root.is_dir():
            raise NsisRepackContractError(
                "NSIS repack verification evidence is missing or unsafe"
            )
        verification_entries = {entry.name for entry in verification_root.iterdir()}
        expected_receipts = {
            "repack-a-receipt.json": "a",
            "repack-b-receipt.json": "b",
        }
        _assert_case_unique(
            sorted(verification_entries), "NSIS repack verification evidence"
        )
        if verification_entries != set(expected_receipts):
            raise NsisRepackContractError(
                "NSIS repack verification evidence must contain exactly two receipts"
            )
        kit = snapshot_root / "nsis-repack-kit"
        promoted_installer = snapshot_root / installer_relative
        manifest = verify_kit(
            kit=kit,
            expected_source_ref=expected_source_ref,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_source_epoch=expected_source_epoch,
            expected_kit_sha256=expected_kit_sha256,
        )
        verified_receipts: list[dict[str, object]] = []
        receipt_summaries: list[dict[str, object]] = []
        for receipt_name, slot in expected_receipts.items():
            receipt_path = verification_root / receipt_name
            receipt = verify_receipt(
                receipt=receipt_path,
                kit=kit,
                output=promoted_installer,
                expected_repack_slot=slot,
                expected_source_ref=expected_source_ref,
                expected_source_sha=expected_source_sha,
                expected_source_tree=expected_source_tree,
                expected_source_epoch=expected_source_epoch,
                expected_kit_sha256=expected_kit_sha256,
            )
            if receipt["repack_slot"] != slot:
                raise NsisRepackContractError(
                    f"{receipt_name} does not bind repack slot {slot}"
                )
            verified_receipts.append(receipt)
            _receipt_size, receipt_raw_sha256 = _hash_regular_file(
                receipt_path, receipt_name
            )
            receipt_summaries.append(
                {
                    "path": f"nsis-repack-verification/{receipt_name}",
                    "repack_slot": slot,
                    "sha256": receipt_raw_sha256,
                    "receipt_sha256": receipt["receipt_sha256"],
                }
            )
        if {str(receipt["repack_slot"]) for receipt in verified_receipts} != {"a", "b"}:
            raise NsisRepackContractError(
                "NSIS provenance receipts do not have distinct slots"
            )
        installer_size, installer_sha256 = _hash_regular_file(
            promoted_installer, "promoted installer"
        )
        expected_output = _object(
            manifest["expected_unsigned_installer"], "expected_unsigned_installer"
        )
        if (
            installer_size != expected_output["size"]
            or installer_sha256 != expected_output["sha256"]
        ):
            raise NsisRepackContractError(
                "promoted installer does not match the verified kit output"
            )
        _kit_manifest_size, kit_manifest_sha256 = _hash_regular_file(
            kit / KIT_MANIFEST, "kit manifest"
        )
        transformation = dict(_object(manifest["transformation"], "transformation"))
        summary = {
            "schema_version": SCHEMA_VERSION,
            "artifact": PROVENANCE_SET_ARTIFACT,
            "source_ref": manifest["source_ref"],
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "source_epoch": manifest["source_epoch"],
            "kit": {
                "path": f"nsis-repack-kit/{KIT_MANIFEST}",
                "sha256": kit_manifest_sha256,
                "kit_sha256": manifest["kit_sha256"],
            },
            "transformation": transformation,
            "transformation_sha256": transformation["transformation_sha256"],
            "receipts": receipt_summaries,
            "installer": {"size": installer_size, "sha256": installer_sha256},
        }
        if set(summary) != PROVENANCE_SUMMARY_FIELDS:
            raise NsisRepackContractError("provenance summary field set is invalid")
        return summary


def repack(
    *,
    kit: Path,
    output: Path,
    receipt: Path,
    repack_slot: str,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise NsisRepackContractError("installer output must not already exist")
    if receipt.exists() or receipt.is_symlink():
        raise NsisRepackContractError("receipt output must not already exist")
    with _verified_kit_snapshot(
        kit=kit,
        expected_source_ref=expected_source_ref,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_source_epoch=expected_source_epoch,
        expected_kit_sha256=expected_kit_sha256,
    ) as (manifest, verified_snapshot):
        return _repack_verified_snapshot(
            verified_snapshot=verified_snapshot,
            manifest=manifest,
            output=output,
            receipt=receipt,
            repack_slot=_repack_slot(repack_slot, "repack_slot"),
            diagnostic_mode=False,
        )


def diagnose_repack_mismatch(
    *,
    kit: Path,
    output: Path,
    expected_source_ref: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_source_epoch: int,
    expected_kit_sha256: str,
) -> dict[str, object]:
    """Compile one private, non-promotable output for mismatch diagnosis."""

    if output.exists() or output.is_symlink():
        raise NsisRepackContractError("diagnostic output must not already exist")
    try:
        verify_private_directory(output.parent.absolute())
    except SecureArtifactSnapshotError as error:
        raise NsisRepackContractError(
            "diagnostic output parent must be a private directory"
        ) from error
    with _verified_kit_snapshot(
        kit=kit,
        expected_source_ref=expected_source_ref,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_source_epoch=expected_source_epoch,
        expected_kit_sha256=expected_kit_sha256,
    ) as (manifest, verified_snapshot):
        result = _repack_verified_snapshot(
            verified_snapshot=verified_snapshot,
            manifest=manifest,
            output=output,
            receipt=None,
            repack_slot=None,
            diagnostic_mode=True,
        )
    try:
        verify_private_directory(output.parent.absolute())
    except SecureArtifactSnapshotError as error:
        raise NsisRepackContractError(
            "diagnostic output parent lost its private boundary"
        ) from error
    return result


def _drain_bounded_output_tail(
    read_fd: int,
    stop: threading.Event,
    tail: bytearray,
    failures: list[OSError],
) -> None:
    empty_polls_after_stop = 0
    try:
        while True:
            try:
                chunk = os.read(read_fd, 8192)
            except BlockingIOError:
                if stop.is_set():
                    empty_polls_after_stop += 1
                    if empty_polls_after_stop >= 10:
                        return
                    time.sleep(0.01)
                else:
                    stop.wait(0.01)
                continue
            if not chunk:
                return
            empty_polls_after_stop = 0
            tail.extend(chunk)
            if len(tail) > NSIS_DIAGNOSTIC_TAIL_BYTES:
                del tail[:-NSIS_DIAGNOSTIC_TAIL_BYTES]
    except OSError as error:
        failures.append(error)
    finally:
        try:
            os.close(read_fd)
        except OSError as error:
            failures.append(error)


def _format_nsis_diagnostic(payload: bytes, work: Path) -> str:
    text = payload.decode("utf-8", errors="replace").replace("\r\n", "\n")
    for private_path in {str(work), str(work).replace("\\", "/")}:
        text = re.sub(
            re.escape(private_path),
            "@STOCK_DESK_PRIVATE_WORK@",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(
        r"(?im)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|[\\/]{2}[^\\/\s]+[\\/])[^\r\n]*",
        "@ABSOLUTE_PATH@",
        text,
    )
    text = "".join(
        character
        if character in "\t\n" or not unicodedata.category(character).startswith("C")
        else "?"
        for character in text
    )
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(f"NSIS> {line}" for line in lines[-_NSIS_DIAGNOSTIC_MAX_LINES:])


def _repack_verified_snapshot(
    *,
    verified_snapshot: Path,
    manifest: Mapping[str, object],
    output: Path,
    receipt: Path | None,
    repack_slot: str | None,
    diagnostic_mode: bool,
) -> dict[str, object]:
    records = manifest["files"]
    assert isinstance(records, list)
    output_identity: tuple[int, int] | None = None
    with tempfile.TemporaryDirectory(prefix="stock-desk-nsis-repack-") as temporary_raw:
        temporary_root = Path(temporary_raw).resolve(strict=True)
        work = (temporary_root / "content").resolve(strict=False)
        _snapshot_source_files(verified_snapshot / "content", records, work)
        toolchain = manifest["toolchain"]
        assert isinstance(toolchain, Mapping)
        executable = _safe_child(work, str(toolchain["path"]), "NSIS toolchain")
        os.chmod(executable, 0o500)
        argv = manifest["argv"]
        assert isinstance(argv, list)
        rendered_script = _safe_child(work, str(argv[-1]), "rendered NSIS script")
        environment = manifest["environment"]
        assert isinstance(environment, dict)
        cleared_environment = manifest["cleared_environment"]
        assert isinstance(cleared_environment, list)
        os.chmod(work, 0o700)
        private_temp = work / ".private-temp"
        private_temp.mkdir(mode=0o700)
        execution_environment = {
            str(key): (
                str(private_temp)
                if str(value) == _PRIVATE_WORK_PLACEHOLDER
                else str(value)
            )
            for key, value in environment.items()
        }
        if any(str(name) in execution_environment for name in cleared_environment):
            raise NsisRepackContractError(
                "cleared NSIS environment leaked into the explicit environment"
            )
        expected = manifest["expected_unsigned_installer"]
        assert isinstance(expected, Mapping)
        generated_parent = work / PurePosixPath(str(expected["path"])).parent
        generated_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(generated_parent, 0o700)
        diagnostic_tail = bytearray()
        diagnostic_failures: list[OSError] = []
        diagnostic_read_fd, diagnostic_write_fd = os.pipe()
        try:
            os.set_blocking(diagnostic_read_fd, False)
        except OSError as error:
            os.close(diagnostic_read_fd)
            os.close(diagnostic_write_fd)
            raise NsisRepackContractError(
                "NSIS output capture could not be configured"
            ) from error
        diagnostic_stop = threading.Event()
        diagnostic_reader = threading.Thread(
            target=_drain_bounded_output_tail,
            args=(
                diagnostic_read_fd,
                diagnostic_stop,
                diagnostic_tail,
                diagnostic_failures,
            ),
            name="stock-desk-nsis-output",
            daemon=True,
        )
        try:
            diagnostic_reader.start()
        except (OSError, RuntimeError) as error:
            os.close(diagnostic_read_fd)
            os.close(diagnostic_write_fd)
            raise NsisRepackContractError(
                "NSIS output capture could not start"
            ) from error
        execution_error: OSError | subprocess.TimeoutExpired | None = None
        completed: subprocess.CompletedProcess[bytes] | None = None
        try:
            completed = subprocess.run(
                [
                    str(executable),
                    *[str(argument) for argument in argv[:-1]],
                    os.fspath(rendered_script),
                ],
                cwd=work,
                env=execution_environment,
                stdin=subprocess.DEVNULL,
                stdout=diagnostic_write_fd,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            execution_error = error
        finally:
            try:
                os.close(diagnostic_write_fd)
            except OSError as error:
                diagnostic_failures.append(error)
            diagnostic_stop.set()
            diagnostic_reader.join(timeout=5)
        if execution_error is not None:
            raise NsisRepackContractError(
                "NSIS toolchain execution failed"
            ) from execution_error
        if diagnostic_reader.is_alive():
            raise NsisRepackContractError("NSIS output capture did not terminate")
        if diagnostic_failures:
            raise NsisRepackContractError(
                "NSIS output capture failed"
            ) from diagnostic_failures[0]
        assert completed is not None
        if completed.returncode != 0:
            diagnostic = _format_nsis_diagnostic(bytes(diagnostic_tail), work)
            suffix = f"\n{diagnostic}" if diagnostic else ""
            raise NsisRepackContractError(
                f"NSIS toolchain returned {completed.returncode}{suffix}"
            )
        generated = _safe_child(work, str(expected["path"]), "generated installer")
        size, digest = _hash_regular_file(generated, "generated installer")
        matches_expected = size == expected["size"] and digest == expected["sha256"]
        if not matches_expected and not diagnostic_mode:
            diagnostic = _format_nsis_diagnostic(bytes(diagnostic_tail), work)
            suffix = f"\n{diagnostic}" if diagnostic else ""
            raise NsisRepackContractError(
                "generated installer does not match the expected unsigned identity; "
                f"expected size={expected['size']} sha256={expected['sha256']}; "
                f"actual size={size} sha256={digest}{suffix}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output_identity = _copy_regular_file(generated, output, "generated installer")
        try:
            output_snapshot = snapshot_artifacts(
                output.parent.absolute(),
                [output.name],
                temporary_root / "final-output-snapshot",
                limits=SnapshotLimits(
                    max_files=1,
                    max_file_size=MAX_FILE_BYTES,
                    max_total_size=MAX_FILE_BYTES,
                    max_depth=1,
                ),
                allow_windows_hardlinks=False,
            )
            captured = output_snapshot.files[0]
            if captured.size != size or captured.sha256 != digest:
                raise NsisRepackContractError(
                    "copied installer does not match the verified generated bytes"
                )
        except BaseException as error:
            _remove_created_file(output, output_identity)
            if isinstance(error, SecureArtifactSnapshotError):
                raise NsisRepackContractError(
                    "generated installer output could not be secured"
                ) from error
            raise
        size = captured.size
        digest = captured.sha256

    if diagnostic_mode:
        expected = _object(
            manifest["expected_unsigned_installer"], "expected_unsigned_installer"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact": DIAGNOSTIC_REPACK_ARTIFACT,
            "kit_sha256": manifest["kit_sha256"],
            "source_ref": manifest["source_ref"],
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "source_epoch": manifest["source_epoch"],
            "matches_expected": matches_expected,
            "expected": {
                "size": expected["size"],
                "sha256": expected["sha256"],
            },
            "actual": {"size": size, "sha256": digest},
        }

    assert receipt is not None
    assert repack_slot is not None

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "artifact": RECEIPT_ARTIFACT,
        "kit_sha256": manifest["kit_sha256"],
        "transformation_sha256": _object(manifest["transformation"], "transformation")[
            "transformation_sha256"
        ],
        "repack_slot": repack_slot,
        "source_ref": manifest["source_ref"],
        "source_sha": manifest["source_sha"],
        "source_tree": manifest["source_tree"],
        "source_epoch": manifest["source_epoch"],
        "argv": manifest["argv"],
        "environment": manifest["environment"],
        "cleared_environment": manifest["cleared_environment"],
        "output": {
            "path": _object(
                manifest["expected_unsigned_installer"],
                "expected_unsigned_installer",
            )["path"],
            "size": size,
            "sha256": digest,
        },
    }
    result["receipt_sha256"] = _receipt_digest(result)
    try:
        _write_new_file(receipt, _canonical_json(result), "repack receipt")
    except NsisRepackContractError as error:
        _remove_created_file(output, output_identity)
        raise NsisRepackContractError("could not write the repack receipt") from error
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create and verify immutable NSIS repack kits"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-kit")
    create.add_argument("--descriptor", type=Path, required=True)
    create.add_argument("--source-root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--expected-source-ref", required=True)
    create.add_argument("--expected-source-sha", required=True)
    create.add_argument("--expected-source-tree", required=True)
    create.add_argument("--expected-source-epoch", type=int, required=True)
    verify = commands.add_parser("verify-kit")
    verify.add_argument("--kit", type=Path, required=True)
    verify.add_argument("--expected-source-ref", required=True)
    verify.add_argument("--expected-source-sha", required=True)
    verify.add_argument("--expected-source-tree", required=True)
    verify.add_argument("--expected-source-epoch", type=int, required=True)
    verify.add_argument("--expected-kit-sha256", required=True)
    run = commands.add_parser("repack")
    run.add_argument("--kit", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    run.add_argument("--repack-slot", required=True, choices=("a", "b"))
    run.add_argument("--expected-source-ref", required=True)
    run.add_argument("--expected-source-sha", required=True)
    run.add_argument("--expected-source-tree", required=True)
    run.add_argument("--expected-source-epoch", type=int, required=True)
    run.add_argument("--expected-kit-sha256", required=True)
    diagnose = commands.add_parser("diagnose-repack-mismatch")
    diagnose.add_argument("--kit", type=Path, required=True)
    diagnose.add_argument("--output", type=Path, required=True)
    diagnose.add_argument("--expected-source-ref", required=True)
    diagnose.add_argument("--expected-source-sha", required=True)
    diagnose.add_argument("--expected-source-tree", required=True)
    diagnose.add_argument("--expected-source-epoch", type=int, required=True)
    diagnose.add_argument("--expected-kit-sha256", required=True)
    verify_receipt_command = commands.add_parser("verify-receipt")
    verify_receipt_command.add_argument("--receipt", type=Path, required=True)
    verify_receipt_command.add_argument("--kit", type=Path, required=True)
    verify_receipt_command.add_argument("--output", type=Path, required=True)
    verify_receipt_command.add_argument(
        "--expected-repack-slot", required=True, choices=("a", "b")
    )
    verify_receipt_command.add_argument("--expected-source-ref", required=True)
    verify_receipt_command.add_argument("--expected-source-sha", required=True)
    verify_receipt_command.add_argument("--expected-source-tree", required=True)
    verify_receipt_command.add_argument(
        "--expected-source-epoch", type=int, required=True
    )
    verify_receipt_command.add_argument("--expected-kit-sha256", required=True)
    verify_set_command = commands.add_parser("verify-provenance-set")
    verify_set_command.add_argument("--candidate-root", type=Path, required=True)
    verify_set_command.add_argument("--installer", type=Path, required=True)
    verify_set_command.add_argument("--expected-source-ref", required=True)
    verify_set_command.add_argument("--expected-source-sha", required=True)
    verify_set_command.add_argument("--expected-source-tree", required=True)
    verify_set_command.add_argument("--expected-source-epoch", type=int, required=True)
    verify_set_command.add_argument("--expected-kit-sha256", required=True)
    verify_toolchain_command = commands.add_parser("verify-extracted-toolchain")
    verify_toolchain_command.add_argument("--nsis-root", type=Path, required=True)
    verify_toolchain_command.add_argument(
        "--additional-plugins-root", type=Path, required=True
    )
    patch_payload_command = commands.add_parser("patch-tauri-bundle-payload")
    patch_payload_command.add_argument("--private-root", type=Path, required=True)
    patch_payload_command.add_argument("--payload", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create-kit":
            result = create_kit(
                descriptor=arguments.descriptor,
                source_root=arguments.source_root,
                output=arguments.output,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
            )
        elif arguments.command == "verify-kit":
            result = verify_kit(
                kit=arguments.kit,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
                expected_kit_sha256=arguments.expected_kit_sha256,
            )
        elif arguments.command == "repack":
            result = repack(
                kit=arguments.kit,
                output=arguments.output,
                receipt=arguments.receipt,
                repack_slot=arguments.repack_slot,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
                expected_kit_sha256=arguments.expected_kit_sha256,
            )
        elif arguments.command == "diagnose-repack-mismatch":
            result = diagnose_repack_mismatch(
                kit=arguments.kit,
                output=arguments.output,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
                expected_kit_sha256=arguments.expected_kit_sha256,
            )
        elif arguments.command == "verify-receipt":
            result = verify_receipt(
                receipt=arguments.receipt,
                kit=arguments.kit,
                output=arguments.output,
                expected_repack_slot=arguments.expected_repack_slot,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
                expected_kit_sha256=arguments.expected_kit_sha256,
            )
        elif arguments.command == "verify-provenance-set":
            result = verify_provenance_set(
                candidate_root=arguments.candidate_root,
                installer=arguments.installer,
                expected_source_ref=arguments.expected_source_ref,
                expected_source_sha=arguments.expected_source_sha,
                expected_source_tree=arguments.expected_source_tree,
                expected_source_epoch=arguments.expected_source_epoch,
                expected_kit_sha256=arguments.expected_kit_sha256,
            )
        elif arguments.command == "verify-extracted-toolchain":
            identity = verify_extracted_nsis_toolchain(
                nsis_root=arguments.nsis_root,
                additional_plugins_root=arguments.additional_plugins_root,
            )
            result = {
                "compiler": os.fspath(identity.compiler),
                "compiler_size": identity.compiler_size,
                "compiler_sha256": identity.compiler_sha256,
                "additional_plugins_root": os.fspath(identity.additional_plugins_root),
                "nsis_tauri_utils": os.fspath(identity.nsis_tauri_utils),
                "nsis_tauri_utils_size": identity.nsis_tauri_utils_size,
                "nsis_tauri_utils_sha256": identity.nsis_tauri_utils_sha256,
                "lock_sha256": identity.lock_sha256,
                "tree": {
                    "algorithm": identity.tree.algorithm,
                    "file_count": identity.tree.file_count,
                    "total_size": identity.tree.total_size,
                    "sha256": identity.tree.sha256,
                },
            }
        else:
            result = patch_tauri_bundle_payload(
                private_root=arguments.private_root,
                payload=arguments.payload,
            )
        sys.stdout.buffer.write(_canonical_json(result))
    except (NsisRepackContractError, OSError) as error:
        print(f"NSIS repack contract failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
