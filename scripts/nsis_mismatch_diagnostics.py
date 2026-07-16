from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import BinaryIO, cast, Final

from scripts.verify_windows_desktop_bundle import (
    BundleVerificationError,
    canonical_json,
    parse_pe,
)


MAX_INSTALLER_BYTES: Final = 1024 * 1024 * 1024
MAX_EXTRACTED_FILES: Final = 8192
MAX_EXTRACTED_FILE_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_TOTAL_BYTES: Final = 4 * 1024 * 1024 * 1024
MAX_DIFFERENCES: Final = 64
CHUNK_SIZE: Final = 1024 * 1024


class NsisMismatchDiagnosticError(ValueError):
    """The bounded NSIS mismatch evidence could not be trusted."""


def _regular_metadata(path: Path, field: str) -> os.stat_result:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise NsisMismatchDiagnosticError(f"cannot inspect {field}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
    ):
        raise NsisMismatchDiagnosticError(f"{field} must be a regular non-link file")
    return metadata


def _hash_file(path: Path, field: str, *, limit: int) -> tuple[int, str]:
    metadata = _regular_metadata(path, field)
    if metadata.st_size > limit:
        raise NsisMismatchDiagnosticError(f"{field} exceeds the size limit")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(CHUNK_SIZE):
                size += len(block)
                if size > limit:
                    raise NsisMismatchDiagnosticError(
                        f"{field} exceeds the size limit"
                    )
                digest.update(block)
    except OSError as error:
        raise NsisMismatchDiagnosticError(f"cannot read {field}") from error
    current = _regular_metadata(path, field)
    if size != metadata.st_size or (
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
        current.st_ino,
    ) != (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_ino,
    ):
        raise NsisMismatchDiagnosticError(f"{field} changed while hashing")
    return size, digest.hexdigest()


def _read_pe_header(path: Path, field: str) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(4096)
    except OSError as error:
        raise NsisMismatchDiagnosticError(f"cannot read {field} PE header") from error
    try:
        parsed = parse_pe(payload, label=field, allow_x86=True)
    except BundleVerificationError as error:
        raise NsisMismatchDiagnosticError(str(error)) from error
    return {
        "timestamp": parsed.timestamp,
        "timestamp_offset": parsed.timestamp_offset,
        "checksum": parsed.checksum,
        "checksum_offset": parsed.checksum_offset,
        "signed": parsed.signed,
    }


def _common_prefix(left: BinaryIO, right: BinaryIO, common_size: int) -> int:
    matched = 0
    while matched < common_size:
        width = min(CHUNK_SIZE, common_size - matched)
        left_block = left.read(width)
        right_block = right.read(width)
        if len(left_block) != width or len(right_block) != width:
            raise NsisMismatchDiagnosticError("installer changed during prefix scan")
        if left_block == right_block:
            matched += width
            continue
        for left_byte, right_byte in zip(left_block, right_block, strict=True):
            if left_byte != right_byte:
                return matched
            matched += 1
        raise AssertionError("unequal blocks must contain a differing byte")
    return matched


def _common_suffix(
    left: BinaryIO, right: BinaryIO, left_size: int, right_size: int, maximum: int
) -> int:
    matched = 0
    while matched < maximum:
        width = min(CHUNK_SIZE, maximum - matched)
        left.seek(left_size - matched - width)
        right.seek(right_size - matched - width)
        left_block = left.read(width)
        right_block = right.read(width)
        if len(left_block) != width or len(right_block) != width:
            raise NsisMismatchDiagnosticError("installer changed during suffix scan")
        if left_block == right_block:
            matched += width
            continue
        for index in range(1, width + 1):
            if left_block[-index] != right_block[-index]:
                return matched
            matched += 1
        raise AssertionError("unequal blocks must contain a differing byte")
    return matched


def _byte_difference(
    expected: Path, actual: Path, expected_size: int, actual_size: int
) -> dict[str, int]:
    common_size = min(expected_size, actual_size)
    try:
        with expected.open("rb") as left, actual.open("rb") as right:
            prefix = _common_prefix(left, right, common_size)
            suffix = _common_suffix(
                left,
                right,
                expected_size,
                actual_size,
                max(0, common_size - prefix),
            )
    except OSError as error:
        raise NsisMismatchDiagnosticError("cannot compare installer bytes") from error
    return {
        "common_prefix_bytes": prefix,
        "common_suffix_bytes": suffix,
        "size_delta": actual_size - expected_size,
    }


def _portable_relative(root: Path, path: Path) -> str:
    relative = PurePosixPath(*path.relative_to(root).parts)
    portable = relative.as_posix()
    if (
        not portable
        or portable in {".", ".."}
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(ord(character) < 32 for character in portable)
        or len(portable.encode("utf-8")) > 1024
    ):
        raise NsisMismatchDiagnosticError("extracted payload has an unsafe path")
    return portable


def _tree_inventory(root: Path, field: str) -> tuple[list[dict[str, object]], str]:
    try:
        root_metadata = root.stat(follow_symlinks=False)
    except OSError as error:
        raise NsisMismatchDiagnosticError(f"cannot inspect {field}") from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or int(getattr(root_metadata, "st_file_attributes", 0)) & 0x400
    ):
        raise NsisMismatchDiagnosticError(
            f"{field} must be a regular non-link directory"
        )
    records: list[dict[str, object]] = []
    total_size = 0
    seen_casefold: set[str] = set()
    for walk_root, directories, filenames in os.walk(root, followlinks=False):
        directories.sort(key=lambda value: value.encode("utf-8"))
        filenames.sort(key=lambda value: value.encode("utf-8"))
        parent = Path(walk_root)
        for name in directories:
            child = parent / name
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise NsisMismatchDiagnosticError(
                    f"cannot inspect {field} directory"
                ) from error
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or int(getattr(metadata, "st_file_attributes", 0)) & 0x400
            ):
                raise NsisMismatchDiagnosticError(
                    f"{field} contains a link or non-directory entry"
                )
            _portable_relative(root, child)
        for name in filenames:
            child = parent / name
            relative = _portable_relative(root, child)
            folded = relative.casefold()
            if folded in seen_casefold:
                raise NsisMismatchDiagnosticError(
                    f"{field} contains case-colliding paths"
                )
            seen_casefold.add(folded)
            size, digest = _hash_file(
                child, f"{field} file", limit=MAX_EXTRACTED_FILE_BYTES
            )
            total_size += size
            if len(records) >= MAX_EXTRACTED_FILES:
                raise NsisMismatchDiagnosticError(f"{field} has too many files")
            if total_size > MAX_EXTRACTED_TOTAL_BYTES:
                raise NsisMismatchDiagnosticError(f"{field} is too large")
            records.append({"path": relative, "size": size, "sha256": digest})
    records.sort(key=lambda record: str(record["path"]).encode("utf-8"))
    tree_digest = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return records, tree_digest


def _tree_comparison(expected_root: Path, actual_root: Path) -> dict[str, object]:
    expected, expected_digest = _tree_inventory(expected_root, "expected extraction")
    actual, actual_digest = _tree_inventory(actual_root, "actual extraction")
    expected_by_path = {str(record["path"]): record for record in expected}
    actual_by_path = {str(record["path"]): record for record in actual}
    differences: list[dict[str, object]] = []
    for path in sorted(set(expected_by_path) | set(actual_by_path)):
        left = expected_by_path.get(path)
        right = actual_by_path.get(path)
        fields = (
            ["missing-expected"]
            if left is None
            else ["missing-actual"]
            if right is None
            else [field for field in ("size", "sha256") if left[field] != right[field]]
        )
        if fields and len(differences) < MAX_DIFFERENCES:
            differences.append({"path": path, "fields": fields})
    equivalent = expected == actual
    return {
        "equivalent": equivalent,
        "expected_tree_sha256": expected_digest,
        "actual_tree_sha256": actual_digest,
        "expected_file_count": len(expected),
        "actual_file_count": len(actual),
        "expected_total_size": sum(cast(int, record["size"]) for record in expected),
        "actual_total_size": sum(cast(int, record["size"]) for record in actual),
        "difference_count": sum(
            1
            for path in set(expected_by_path) | set(actual_by_path)
            if expected_by_path.get(path) != actual_by_path.get(path)
        ),
        "differences": differences,
    }


def _installer_identity(path: Path, field: str) -> dict[str, object]:
    size, digest = _hash_file(path, field, limit=MAX_INSTALLER_BYTES)
    return {"size": size, "sha256": digest, "pe": _read_pe_header(path, field)}


def build_diagnostic(
    *, expected: Path, actual: Path, expected_tree: Path, actual_tree: Path
) -> dict[str, object]:
    expected_identity = _installer_identity(expected, "expected installer")
    actual_identity = _installer_identity(actual, "actual installer")
    extracted = _tree_comparison(expected_tree, actual_tree)
    return {
        "schema_version": 1,
        "artifact": "stock-desk-nsis-mismatch-diagnostic-v1",
        "classification": (
            "wrapper-only" if extracted["equivalent"] else "payload-difference"
        ),
        "expected": expected_identity,
        "actual": actual_identity,
        "byte_difference": _byte_difference(
            expected,
            actual,
            cast(int, expected_identity["size"]),
            cast(int, actual_identity["size"]),
        ),
        "extracted": extracted,
    }


def _write_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise NsisMismatchDiagnosticError("cannot write diagnostic output") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create bounded diagnostics for two unequal unsigned NSIS installers"
    )
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--expected-tree", type=Path, required=True)
    parser.add_argument("--actual-tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = build_diagnostic(
            expected=arguments.expected,
            actual=arguments.actual,
            expected_tree=arguments.expected_tree,
            actual_tree=arguments.actual_tree,
        )
        _write_new(arguments.output, canonical_json(report))
    except NsisMismatchDiagnosticError as error:
        print(f"NSIS mismatch diagnostic failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
