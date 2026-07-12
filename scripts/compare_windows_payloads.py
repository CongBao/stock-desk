from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from collections.abc import Mapping, Sequence

from scripts.verify_windows_desktop_bundle import (
    BundleVerificationError,
    canonical_json,
    parse_pe,
    validate_manifest,
)


class PayloadComparisonError(ValueError):
    """Two desktop builds do not have the same trusted inputs and payload."""


def _file_difference_summary(
    left: Sequence[Mapping[str, object]],
    right: Sequence[Mapping[str, object]],
) -> str:
    differences: list[str] = []
    if len(left) != len(right):
        differences.append("inventory.count")
    for left_record, right_record in zip(left, right):
        left_role = left_record.get("role")
        right_role = right_record.get("role")
        label = (
            left_role
            if left_role == right_role and isinstance(left_role, str)
            else "record"
        )
        for field in ("path", "size", "sha256", "role"):
            if left_record.get(field) != right_record.get(field):
                differences.append(f"{label}.{field}")
    bounded = sorted(set(differences))[:8]
    return ",".join(bounded) if bounded else "inventory.order"


def compare_nsis_installers(left: Path, right: Path) -> dict[str, object]:
    try:
        left_payload = left.read_bytes()
        right_payload = right.read_bytes()
    except OSError as error:
        raise PayloadComparisonError("cannot read NSIS installer") from error
    try:
        left_pe = parse_pe(left_payload, label="left NSIS installer", allow_x86=True)
        right_pe = parse_pe(right_payload, label="right NSIS installer", allow_x86=True)
    except BundleVerificationError as error:
        raise PayloadComparisonError(str(error)) from error
    if left_pe.signed or right_pe.signed:
        raise PayloadComparisonError(
            "NSIS comparison accepts only unsigned prerelease installers"
        )
    if len(left_payload) != len(right_payload):
        raise PayloadComparisonError(
            "NSIS installers differ beyond PE timestamp/checksum"
        )
    mutable_offsets = {
        "pe-timestamp": (left_pe.timestamp_offset, right_pe.timestamp_offset),
        "pe-checksum": (left_pe.checksum_offset, right_pe.checksum_offset),
    }
    allowed: list[str] = []
    left_canonical = bytearray(left_payload)
    right_canonical = bytearray(right_payload)
    for name, (left_offset, right_offset) in mutable_offsets.items():
        if left_offset != right_offset:
            raise PayloadComparisonError("NSIS PE layouts are not identical")
        if (
            left_payload[left_offset : left_offset + 4]
            != right_payload[right_offset : right_offset + 4]
        ):
            allowed.append(name)
        struct.pack_into("<I", left_canonical, left_offset, 0)
        struct.pack_into("<I", right_canonical, right_offset, 0)
    if left_canonical != right_canonical:
        raise PayloadComparisonError(
            "NSIS installers differ beyond PE timestamp/checksum"
        )
    return {
        "equivalent": True,
        "allowed_differences": sorted(allowed),
        "left_raw_sha256": hashlib.sha256(left_payload).hexdigest(),
        "right_raw_sha256": hashlib.sha256(right_payload).hexdigest(),
        "canonical_sha256": hashlib.sha256(left_canonical).hexdigest(),
    }


def compare_manifests(
    left_raw: object,
    right_raw: object,
    *,
    left_installer: Path | None = None,
    right_installer: Path | None = None,
) -> dict[str, object]:
    try:
        left = validate_manifest(left_raw)
        right = validate_manifest(right_raw)
    except BundleVerificationError as error:
        raise PayloadComparisonError(str(error)) from error
    for field in ("source_sha", "locks", "toolchain", "release", "sbom"):
        if left[field] != right[field]:
            raise PayloadComparisonError(f"desktop manifests differ in {field}")
    left_files = left["files"]
    right_files = right["files"]
    assert isinstance(left_files, list) and isinstance(right_files, list)
    left_payload = [
        record for record in left_files if record["role"] != "nsis-installer"
    ]
    right_payload = [
        record for record in right_files if record["role"] != "nsis-installer"
    ]
    if left_payload != right_payload:
        raise PayloadComparisonError(
            "desktop manifests differ in files: "
            f"{_file_difference_summary(left_payload, right_payload)}"
        )
    left_nsis = [record for record in left_files if record["role"] == "nsis-installer"]
    right_nsis = [
        record for record in right_files if record["role"] == "nsis-installer"
    ]
    if len(left_nsis) != len(right_nsis) or len(left_nsis) > 1:
        raise PayloadComparisonError("desktop manifests differ in installer")
    for left_record, right_record in zip(left_nsis, right_nsis, strict=True):
        for field in ("path", "size", "role"):
            if left_record[field] != right_record[field]:
                raise PayloadComparisonError(f"desktop installers differ in {field}")
    if (left_installer is None) != (right_installer is None):
        raise PayloadComparisonError(
            "both NSIS installers are required for canonical comparison"
        )
    nsis: Mapping[str, object] | None = None
    if left_installer is not None and right_installer is not None:
        nsis = compare_nsis_installers(left_installer, right_installer)
        if not left_nsis or not right_nsis:
            raise PayloadComparisonError(
                "installer bytes were provided without bound manifest records"
            )
        if nsis["left_raw_sha256"] != left_nsis[0]["sha256"]:
            raise PayloadComparisonError(
                "left NSIS bytes do not match the manifest installer digest"
            )
        if nsis["right_raw_sha256"] != right_nsis[0]["sha256"]:
            raise PayloadComparisonError(
                "right NSIS bytes do not match the manifest installer digest"
            )
    elif left_nsis and left_nsis[0]["sha256"] != right_nsis[0]["sha256"]:
        raise PayloadComparisonError(
            "differing NSIS digests require both installers for canonical comparison"
        )
    return {
        "schema_version": 1,
        "artifact": "windows-desktop-reproducibility-comparison",
        "reproducible": True,
        "source_sha": left["source_sha"],
        "left_manifest_sha256": left["manifest_sha256"],
        "right_manifest_sha256": right["manifest_sha256"],
        "left_installer_sha256": left_nsis[0]["sha256"] if left_nsis else None,
        "right_installer_sha256": right_nsis[0]["sha256"] if right_nsis else None,
        "nsis": nsis,
    }


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise PayloadComparisonError(f"cannot read manifest: {path.name}") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two verified Windows payloads"
    )
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--left-installer", type=Path)
    parser.add_argument("--right-installer", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = compare_manifests(
            _read_json(arguments.left),
            _read_json(arguments.right),
            left_installer=arguments.left_installer,
            right_installer=arguments.right_installer,
        )
        output = canonical_json(result)
        if arguments.output is None:
            sys.stdout.buffer.write(output)
        else:
            arguments.output.write_bytes(output)
    except (PayloadComparisonError, OSError) as error:
        print(f"windows payload comparison failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
