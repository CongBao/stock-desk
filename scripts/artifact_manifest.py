from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 2
PAYLOAD_LIST_SCHEMA_VERSION = 1
MAX_PAYLOAD_LIST_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_LIST_ENTRIES = 8192
PAYLOAD_KINDS = frozenset(
    {"web", "python", "sidecar", "oci", "sbom", "provenance", "tauri-unsigned"}
)
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PRODUCER_FIELDS = ("workflow", "run_id", "run_attempt", "job_id", "job_name")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:/")


class ManifestError(ValueError):
    """A manifest is malformed or does not bind the requested artifact identity."""


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"payload must be a regular non-symlink file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _relative_path(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ManifestError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or _WINDOWS_ABSOLUTE_PATH.match(raw)
        or ".." in path.parts
        or path.as_posix() != raw
    ):
        raise ManifestError(f"{field} must be a normalized POSIX relative path")
    return raw


def _payload_path_key(path: str) -> str:
    """Approximate Windows path identity for cross-platform collision checks."""
    return unicodedata.normalize("NFKC", path).casefold()


def _validate_payload_specs(
    payloads: Sequence[tuple[str, str]], *, field: str
) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for index, (raw_path, kind) in enumerate(payloads):
        path = _relative_path(raw_path, field=f"{field}[{index}].path")
        collision_key = _payload_path_key(path)
        if collision_key in seen:
            raise ManifestError(
                f"colliding payload path: {path} conflicts with {seen[collision_key]}"
            )
        seen[collision_key] = path
        if kind not in PAYLOAD_KINDS:
            raise ManifestError(f"unsupported payload kind: {kind}")
        normalized.append((path, kind))
    return normalized


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_payload_list(path: Path) -> list[tuple[str, str]]:
    """Read a canonical response file whose expanded entries bind the manifest."""
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"payload list must be a regular non-symlink file: {path}")
    try:
        size = path.stat().st_size
        if size > MAX_PAYLOAD_LIST_BYTES:
            raise ManifestError(f"payload list exceeds {MAX_PAYLOAD_LIST_BYTES} bytes")
        encoded = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"cannot read payload list: {error}") from error
    if len(encoded) > MAX_PAYLOAD_LIST_BYTES:
        raise ManifestError(f"payload list exceeds {MAX_PAYLOAD_LIST_BYTES} bytes")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise ManifestError("payload list must be UTF-8 without BOM")
    try:
        text = encoded.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(f"invalid JSON constant: {value}")
            ),
        )
    except UnicodeDecodeError as error:
        raise ManifestError("payload list must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"payload list must be valid JSON: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "payloads"}:
        raise ManifestError(
            "payload list must contain exactly schema_version and payloads"
        )
    if (
        isinstance(raw["schema_version"], bool)
        or raw["schema_version"] != PAYLOAD_LIST_SCHEMA_VERSION
    ):
        raise ManifestError(
            f"payload list schema_version must be {PAYLOAD_LIST_SCHEMA_VERSION}"
        )
    payload_values = raw["payloads"]
    if not isinstance(payload_values, list) or not payload_values:
        raise ManifestError("payload list payloads must be a non-empty array")
    if len(payload_values) > MAX_PAYLOAD_LIST_ENTRIES:
        raise ManifestError(f"payload list exceeds {MAX_PAYLOAD_LIST_ENTRIES} entries")
    payloads: list[tuple[str, str]] = []
    for index, item in enumerate(payload_values):
        if not isinstance(item, dict) or set(item) != {"path", "kind"}:
            raise ManifestError(
                f"payload list payloads[{index}] must contain exactly path and kind"
            )
        raw_path = item["path"]
        kind = item["kind"]
        if not isinstance(raw_path, str) or not isinstance(kind, str):
            raise ManifestError(
                f"payload list payloads[{index}] path and kind must be strings"
            )
        payloads.append((raw_path, kind))
    normalized = _validate_payload_specs(payloads, field="payload list payloads")
    canonical = _canonical_bytes(
        {
            "schema_version": PAYLOAD_LIST_SCHEMA_VERSION,
            "payloads": [
                {"path": payload_path, "kind": kind}
                for payload_path, kind in normalized
            ],
        }
    )
    if encoded != canonical:
        raise ManifestError("payload list must use canonical UTF-8 JSON")
    return normalized


def _string_map(
    value: object,
    *,
    field: str,
    digest_values: bool,
    allow_empty: bool = False,
) -> dict[str, str]:
    if not isinstance(value, dict) or (not value and not allow_empty):
        raise ManifestError(f"{field} must be a non-empty object")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise ManifestError(f"{field} keys must be non-empty strings")
        if not isinstance(raw_value, str) or not raw_value:
            raise ManifestError(f"{field}.{raw_key} must be a non-empty string")
        if digest_values and _SHA256.fullmatch(raw_value) is None:
            raise ManifestError(f"{field}.{raw_key} must be a lowercase SHA-256")
        result[raw_key] = raw_value
    return dict(sorted(result.items()))


def validate_manifest(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be an object")
    allowed = {
        "schema_version",
        "manifest_sha256",
        "source_sha",
        "source_tree",
        "producer",
        "critical_inputs",
        "toolchain",
        "lockfiles",
        "payloads",
        "image_digest",
        "tauri",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ManifestError(f"unknown manifest fields: {', '.join(unknown)}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("source_sha", "source_tree"):
        value = raw.get(field)
        if not isinstance(value, str) or _HEX_40.fullmatch(value) is None:
            raise ManifestError(
                f"{field} must be a lowercase 40-character git object id"
            )

    producer = raw.get("producer")
    if not isinstance(producer, dict) or set(producer) != set(_PRODUCER_FIELDS):
        raise ManifestError(
            "producer must contain exactly workflow, run_id, run_attempt, job_id, job_name"
        )
    normalized_producer: dict[str, str | int] = {}
    for field in ("workflow", "job_id", "job_name"):
        value = producer[field]
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f"producer.{field} must be a non-empty string")
        normalized_producer[field] = value
    for field in ("run_id", "run_attempt"):
        value = producer[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ManifestError(f"producer.{field} must be a positive integer")
        normalized_producer[field] = value

    payloads = raw.get("payloads")
    if not isinstance(payloads, list) or not payloads:
        raise ManifestError("payloads must be a non-empty array")
    normalized_payloads: list[dict[str, object]] = []
    seen_paths: dict[str, str] = {}
    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict) or set(payload) != {
            "path",
            "kind",
            "size",
            "sha256",
        }:
            raise ManifestError(
                f"payloads[{index}] must contain exactly path, kind, size, sha256"
            )
        path = _relative_path(payload["path"], field=f"payloads[{index}].path")
        collision_key = _payload_path_key(path)
        if collision_key in seen_paths:
            raise ManifestError(
                f"colliding payload path: {path} conflicts with {seen_paths[collision_key]}"
            )
        seen_paths[collision_key] = path
        kind = payload["kind"]
        if kind not in PAYLOAD_KINDS:
            raise ManifestError(f"unsupported payload kind: {kind}")
        size = payload["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ManifestError(
                f"payloads[{index}].size must be a non-negative integer"
            )
        digest = payload["sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ManifestError(f"payloads[{index}].sha256 must be a lowercase SHA-256")
        normalized_payloads.append(
            {"path": path, "kind": kind, "size": size, "sha256": digest}
        )

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": raw["source_sha"],
        "source_tree": raw["source_tree"],
        "producer": normalized_producer,
        "critical_inputs": _string_map(
            raw.get("critical_inputs"), field="critical_inputs", digest_values=True
        ),
        "toolchain": _string_map(
            raw.get("toolchain"), field="toolchain", digest_values=False
        ),
        "lockfiles": _string_map(
            raw.get("lockfiles"), field="lockfiles", digest_values=True
        ),
        "payloads": sorted(
            normalized_payloads, key=lambda payload: str(payload["path"])
        ),
    }
    image_digest = raw.get("image_digest")
    has_oci_payload = any(payload["kind"] == "oci" for payload in normalized_payloads)
    if image_digest is not None:
        if (
            not isinstance(image_digest, str)
            or _OCI_DIGEST.fullmatch(image_digest) is None
        ):
            raise ManifestError("image_digest must be a sha256 OCI digest")
        if not has_oci_payload:
            raise ManifestError("image_digest is only valid with an OCI payload")
        normalized["image_digest"] = image_digest
    elif has_oci_payload:
        raise ManifestError("an OCI payload requires image_digest")

    tauri = raw.get("tauri")
    has_tauri_payload = any(
        payload["kind"] == "tauri-unsigned" for payload in normalized_payloads
    )
    if tauri is not None:
        if not isinstance(tauri, dict) or set(tauri) != {"cargo_lock_sha256"}:
            raise ManifestError("tauri must contain exactly cargo_lock_sha256")
        cargo_digest = tauri["cargo_lock_sha256"]
        if not isinstance(cargo_digest, str) or _SHA256.fullmatch(cargo_digest) is None:
            raise ManifestError("tauri.cargo_lock_sha256 must be a lowercase SHA-256")
        if not has_tauri_payload:
            raise ManifestError("tauri metadata requires a real tauri-unsigned payload")
        normalized["tauri"] = {"cargo_lock_sha256": cargo_digest}
    elif has_tauri_payload:
        raise ManifestError("a tauri-unsigned payload requires Cargo lock metadata")

    expected_digest = manifest_digest(normalized)
    supplied_digest = raw.get("manifest_sha256")
    if supplied_digest is not None and supplied_digest != expected_digest:
        raise ManifestError("manifest_sha256 does not match canonical manifest content")
    normalized["manifest_sha256"] = expected_digest
    return normalized


def build_manifest(
    *,
    root: Path,
    source_sha: str,
    source_tree: str,
    producer: Mapping[str, object],
    payloads: Sequence[tuple[str, str]],
    critical_inputs: Mapping[str, str],
    toolchain: Mapping[str, str],
    lockfiles: Mapping[str, str],
    image_digest: str | None = None,
    cargo_lock_sha256: str | None = None,
) -> dict[str, Any]:
    payload_records: list[dict[str, object]] = []
    resolved_root = root.resolve(strict=True)
    for normalized, kind in _validate_payload_specs(payloads, field="payloads"):
        absolute = root / normalized
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as error:
            raise ManifestError(f"missing payload: {normalized}") from error
        if resolved.parent != resolved_root and resolved_root not in resolved.parents:
            raise ManifestError(f"payload escapes artifact root: {normalized}")
        payload_records.append(
            {
                "path": normalized,
                "kind": kind,
                "size": absolute.stat().st_size,
                "sha256": sha256_file(absolute),
            }
        )
    candidate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "producer": dict(producer),
        "critical_inputs": dict(critical_inputs),
        "toolchain": dict(toolchain),
        "lockfiles": dict(lockfiles),
        "payloads": payload_records,
    }
    if image_digest is not None:
        candidate["image_digest"] = image_digest
    if cargo_lock_sha256 is not None:
        candidate["tauri"] = {"cargo_lock_sha256": cargo_lock_sha256}
    return validate_manifest(candidate)


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    normalized = validate_manifest(dict(manifest))
    path.write_bytes(_canonical_bytes(normalized))


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest: {error}") from error
    return validate_manifest(raw)


def verify_payloads(manifest: Mapping[str, Any], root: Path) -> None:
    normalized = validate_manifest(dict(manifest))
    resolved_root = root.resolve(strict=True)
    for payload in normalized["payloads"]:
        relative = str(payload["path"])
        absolute = root / relative
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as error:
            raise ManifestError(f"missing payload: {relative}") from error
        if resolved.parent != resolved_root and resolved_root not in resolved.parents:
            raise ManifestError(f"payload escapes artifact root: {relative}")
        if absolute.is_symlink() or not absolute.is_file():
            raise ManifestError(
                f"payload must be a regular non-symlink file: {relative}"
            )
        if absolute.stat().st_size != payload["size"]:
            raise ManifestError(f"payload size mismatch: {relative}")
        if sha256_file(absolute) != payload["sha256"]:
            raise ManifestError(f"payload SHA-256 mismatch: {relative}")


def verify_artifact_root_closure(
    manifest: Mapping[str, Any], *, root: Path, artifact_name: str
) -> None:
    """Reject files not bound by the manifest or its exact local bindings."""
    normalized = validate_manifest(dict(manifest))
    manifest_name = _relative_path(
        f"{artifact_name}.json", field="artifact manifest path"
    )
    expected = {
        *(str(payload["path"]) for payload in normalized["payloads"]),
        manifest_name,
        "manifest-binding.json",
    }
    root_path = root.resolve(strict=True)
    if root.is_symlink() or not root_path.is_dir():
        raise ManifestError("artifact root must be a regular directory")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ManifestError("artifact root contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    extras = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extras or missing:
        raise ManifestError(
            f"artifact root is not manifest-closed; extra={extras}, missing={missing}"
        )
    if read_manifest(root / manifest_name) != normalized:
        raise ManifestError("artifact manifest file differs from the proved manifest")
    try:
        binding = json.loads(
            (root / "manifest-binding.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(
            f"cannot read artifact manifest binding: {error}"
        ) from error
    if binding != create_attestation_binding(normalized):
        raise ManifestError(
            "artifact manifest binding differs from the proved manifest"
        )


def verify_for_consumption(
    manifest: Mapping[str, Any],
    *,
    root: Path,
    expected_source_sha: str,
    expected_source_tree: str,
    attestation: Mapping[str, Any],
) -> None:
    normalized = validate_manifest(dict(manifest))
    if normalized["source_sha"] != expected_source_sha:
        raise ManifestError("manifest source_sha does not match the requested commit")
    if normalized["source_tree"] != expected_source_tree:
        raise ManifestError("manifest source_tree does not match the requested tree")
    verify_payloads(normalized, root)

    expected_attestation = {
        "schema_version": 1,
        "manifest_sha256": normalized["manifest_sha256"],
        "source_sha": expected_source_sha,
        "source_tree": expected_source_tree,
        "payloads": {
            payload["path"]: payload["sha256"] for payload in normalized["payloads"]
        },
    }
    if dict(attestation) != expected_attestation:
        raise ManifestError("attestation does not bind the exact manifest and payloads")


def create_attestation_binding(manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_manifest(dict(manifest))
    return {
        "schema_version": 1,
        "manifest_sha256": normalized["manifest_sha256"],
        "source_sha": normalized["source_sha"],
        "source_tree": normalized["source_tree"],
        "payloads": {
            payload["path"]: payload["sha256"] for payload in normalized["payloads"]
        },
    }


def _pairs(values: Sequence[str], *, field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item:
            raise ManifestError(f"{field} values must use NAME=VALUE")
        result[name] = item
    return result


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or verify exact-SHA artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--source-tree", required=True)
    create.add_argument("--workflow", required=True)
    create.add_argument("--run-id", type=int, required=True)
    create.add_argument("--run-attempt", type=int, required=True)
    create.add_argument("--job-id", required=True)
    create.add_argument("--job-name", required=True)
    create.add_argument("--payload", action="append", default=[])
    create.add_argument("--payload-list", type=Path)
    create.add_argument("--critical-input", action="append", default=[])
    create.add_argument("--toolchain", action="append", default=[])
    create.add_argument("--lockfile", action="append", default=[])
    create.add_argument("--image-digest")
    create.add_argument("--cargo-lock-sha256")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--source-tree", required=True)
    verify.add_argument("--attestation", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "create":
            payloads: list[tuple[str, str]] = []
            for raw in args.payload:
                path, separator, kind = raw.rpartition(":")
                if not separator:
                    raise ManifestError("payload values must use PATH:KIND")
                payloads.append((path, kind))
            if args.payload_list is not None:
                payloads.extend(read_payload_list(args.payload_list))
            if not payloads:
                raise ManifestError(
                    "at least one --payload or --payload-list is required"
                )
            manifest = build_manifest(
                root=args.root,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                producer={
                    "workflow": args.workflow,
                    "run_id": args.run_id,
                    "run_attempt": args.run_attempt,
                    "job_id": args.job_id,
                    "job_name": args.job_name,
                },
                payloads=payloads,
                critical_inputs=_pairs(args.critical_input, field="critical-input"),
                toolchain=_pairs(args.toolchain, field="toolchain"),
                lockfiles=_pairs(args.lockfile, field="lockfile"),
                image_digest=args.image_digest,
                cargo_lock_sha256=args.cargo_lock_sha256,
            )
            write_manifest(args.output, manifest)
            print(manifest["manifest_sha256"])
        else:
            manifest = read_manifest(args.manifest)
            try:
                attestation = json.loads(args.attestation.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ManifestError(
                    f"cannot read attestation binding: {error}"
                ) from error
            verify_for_consumption(
                manifest,
                root=args.root,
                expected_source_sha=args.source_sha,
                expected_source_tree=args.source_tree,
                attestation=attestation,
            )
    except (ManifestError, OSError) as error:
        print(f"artifact manifest error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
