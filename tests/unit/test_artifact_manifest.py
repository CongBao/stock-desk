from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.artifact_manifest import (
    ManifestError,
    build_manifest,
    create_attestation_binding,
    main,
    manifest_digest,
    read_payload_list,
    read_manifest,
    validate_manifest,
    verify_for_consumption,
    verify_payloads,
    write_manifest,
)


SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
INPUT_SHA = "3" * 64
LOCK_SHA = "4" * 64


def _write_payload_list(path: Path, payloads: list[dict[str, str]]) -> None:
    path.write_text(
        json.dumps(
            {"schema_version": 1, "payloads": payloads},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="",
    )


def _manifest(root: Path, *, payload_kind: str = "web") -> dict[str, object]:
    payload = root / "payload.bin"
    payload.write_bytes(b"stock-desk-artifact\n")
    return build_manifest(
        root=root,
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
        producer={
            "workflow": "CI",
            "run_id": 123,
            "run_attempt": 1,
            "job_id": "web-build",
            "job_name": "Web build",
        },
        payloads=[("payload.bin", payload_kind)],
        critical_inputs={"Dockerfile": INPUT_SHA},
        toolchain={"node": "22.17.0", "pnpm": "10.13.1"},
        lockfiles={"pnpm-lock.yaml": LOCK_SHA},
    )


def test_manifest_round_trip_binds_source_producer_inputs_and_payload(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "manifest.json"

    write_manifest(path, manifest)
    loaded = read_manifest(path)
    verify_for_consumption(
        loaded,
        root=tmp_path,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        attestation=create_attestation_binding(loaded),
    )

    assert loaded["schema_version"] == 2
    assert loaded["manifest_sha256"] == manifest_digest(loaded)
    assert loaded["producer"] == {
        "workflow": "CI",
        "run_id": 123,
        "run_attempt": 1,
        "job_id": "web-build",
        "job_name": "Web build",
    }
    assert loaded["payloads"][0]["size"] == len(b"stock-desk-artifact\n")
    assert path.read_bytes().endswith(b"\n")


def test_canonical_digest_is_independent_of_input_key_order(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    reversed_manifest = dict(reversed(list(manifest.items())))

    assert manifest_digest(manifest) == manifest_digest(reversed_manifest)
    assert validate_manifest(reversed_manifest) == manifest


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_sha", "main", "source_sha"),
        ("source_sha", "latest", "source_sha"),
        ("source_tree", "a" * 39, "source_tree"),
        ("manifest_sha256", "0" * 64, "manifest_sha256"),
        ("schema_version", 1, "schema_version"),
    ],
)
def test_branch_latest_missing_digest_and_old_schema_are_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    manifest = _manifest(tmp_path)
    manifest[field] = value

    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest)


def test_payload_substitution_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "payload.bin").write_bytes(b"substituted\n")

    with pytest.raises(ManifestError, match="payload (size|SHA-256) mismatch"):
        verify_payloads(manifest, tmp_path)


def test_cross_sha_and_cross_tree_consumption_are_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    attestation = create_attestation_binding(manifest)

    with pytest.raises(ManifestError, match="source_sha"):
        verify_for_consumption(
            manifest,
            root=tmp_path,
            expected_source_sha="9" * 40,
            expected_source_tree=SOURCE_TREE,
            attestation=attestation,
        )
    with pytest.raises(ManifestError, match="source_tree"):
        verify_for_consumption(
            manifest,
            root=tmp_path,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree="9" * 40,
            attestation=attestation,
        )


@pytest.mark.parametrize(
    "tampered_field", ["manifest_sha256", "source_sha", "payloads"]
)
def test_attestation_must_bind_exact_manifest_and_payloads(
    tmp_path: Path, tampered_field: str
) -> None:
    manifest = _manifest(tmp_path)
    attestation = create_attestation_binding(manifest)
    if tampered_field == "payloads":
        attestation[tampered_field] = {"payload.bin": "f" * 64}
    elif tampered_field == "source_sha":
        attestation[tampered_field] = "f" * 40
    else:
        attestation[tampered_field] = "f" * 64

    with pytest.raises(ManifestError, match="attestation"):
        verify_for_consumption(
            manifest,
            root=tmp_path,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            attestation=attestation,
        )


def test_payload_paths_cannot_escape_artifact_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"outside")

    with pytest.raises(ManifestError, match="normalized POSIX relative path"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "job",
                "job_name": "Job",
            },
            payloads=[("../outside.bin", "web")],
            critical_inputs={"input": INPUT_SHA},
            toolchain={"node": "22"},
            lockfiles={"pnpm": LOCK_SHA},
        )


def test_symlink_payload_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"real")
    (tmp_path / "link.bin").symlink_to(real)

    with pytest.raises(ManifestError, match="non-symlink"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "job",
                "job_name": "Job",
            },
            payloads=[("link.bin", "web")],
            critical_inputs={"input": INPUT_SHA},
            toolchain={"node": "22"},
            lockfiles={"pnpm": LOCK_SHA},
        )


def test_symlinked_parent_cannot_escape_artifact_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-directory"
    outside.mkdir(exist_ok=True)
    (outside / "payload.bin").write_bytes(b"outside")
    (tmp_path / "linked-directory").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManifestError, match="escapes artifact root"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "job",
                "job_name": "Job",
            },
            payloads=[("linked-directory/payload.bin", "web")],
            critical_inputs={"input": INPUT_SHA},
            toolchain={"node": "22"},
            lockfiles={"pnpm": LOCK_SHA},
        )


def test_tauri_fields_are_reserved_but_cannot_fake_an_absent_payload(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest["tauri"] = {"cargo_lock_sha256": "5" * 64}

    with pytest.raises(ManifestError, match="real tauri-unsigned payload"):
        validate_manifest(manifest)


def test_real_unsigned_tauri_payload_requires_and_records_cargo_lock(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "stock-desk.exe"
    payload.write_bytes(b"unsigned-test-payload")
    manifest = build_manifest(
        root=tmp_path,
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
        producer={
            "workflow": "CI",
            "run_id": 123,
            "run_attempt": 2,
            "job_id": "tauri",
            "job_name": "Tauri unsigned",
        },
        payloads=[("stock-desk.exe", "tauri-unsigned")],
        critical_inputs={"tauri.conf.json": INPUT_SHA},
        toolchain={"rustc": "1.88.0"},
        lockfiles={"Cargo.lock": LOCK_SHA},
        cargo_lock_sha256=LOCK_SHA,
    )

    assert manifest["tauri"] == {"cargo_lock_sha256": LOCK_SHA}


def test_unsigned_tauri_payload_without_cargo_identity_is_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "stock-desk.exe").write_bytes(b"unsigned-test-payload")

    with pytest.raises(ManifestError, match="requires Cargo lock metadata"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "tauri",
                "job_name": "Tauri",
            },
            payloads=[("stock-desk.exe", "tauri-unsigned")],
            critical_inputs={"tauri.conf.json": INPUT_SHA},
            toolchain={"rustc": "1.88.0"},
            lockfiles={"Cargo.lock": LOCK_SHA},
        )


def test_oci_payload_requires_image_digest(tmp_path: Path) -> None:
    (tmp_path / "image.tar").write_bytes(b"oci-image")

    with pytest.raises(ManifestError, match="requires image_digest"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "oci",
                "job_name": "OCI",
            },
            payloads=[("image.tar", "oci")],
            critical_inputs={"Dockerfile": INPUT_SHA},
            toolchain={"docker": "28"},
            lockfiles={"uv.lock": LOCK_SHA},
        )


def test_cli_create_then_verify_requires_attestation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "web.tar").write_bytes(b"web")
    manifest_path = tmp_path / "manifest.json"
    create_result = main(
        [
            "create",
            "--root",
            str(tmp_path),
            "--output",
            str(manifest_path),
            "--source-sha",
            SOURCE_SHA,
            "--source-tree",
            SOURCE_TREE,
            "--workflow",
            "CI",
            "--run-id",
            "123",
            "--run-attempt",
            "1",
            "--job-id",
            "web",
            "--job-name",
            "Web",
            "--payload",
            "web.tar:web",
            "--critical-input",
            f"vite={INPUT_SHA}",
            "--toolchain",
            "node=22",
            "--lockfile",
            f"pnpm-lock.yaml={LOCK_SHA}",
        ]
    )
    assert create_result == 0
    assert len(capsys.readouterr().out.strip()) == 64

    manifest = read_manifest(manifest_path)
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(
        json.dumps(create_attestation_binding(manifest)), encoding="utf-8"
    )
    assert (
        main(
            [
                "verify",
                "--manifest",
                str(manifest_path),
                "--root",
                str(tmp_path),
                "--source-sha",
                SOURCE_SHA,
                "--source-tree",
                SOURCE_TREE,
                "--attestation",
                str(attestation_path),
            ]
        )
        == 0
    )


def test_cli_create_accepts_canonical_payload_list_without_per_file_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.bin").write_bytes(b"b")
    payload_list = tmp_path / "payload-list.json"
    _write_payload_list(
        payload_list,
        [
            {"kind": "provenance", "path": "a.bin"},
            {"kind": "provenance", "path": "nested/b.bin"},
        ],
    )
    manifest_path = tmp_path / "manifest.json"

    result = main(
        [
            "create",
            "--root",
            str(tmp_path),
            "--output",
            str(manifest_path),
            "--source-sha",
            SOURCE_SHA,
            "--source-tree",
            SOURCE_TREE,
            "--workflow",
            "CI",
            "--run-id",
            "123",
            "--run-attempt",
            "1",
            "--job-id",
            "windows",
            "--job-name",
            "Windows",
            "--payload-list",
            str(payload_list),
            "--critical-input",
            f"ci={INPUT_SHA}",
            "--toolchain",
            "python=3.12",
            "--lockfile",
            f"Cargo.lock={LOCK_SHA}",
        ]
    )

    assert result == 0
    assert len(capsys.readouterr().out.strip()) == 64
    assert [
        (payload["path"], payload["kind"])
        for payload in read_manifest(manifest_path)["payloads"]
    ] == [("a.bin", "provenance"), ("nested/b.bin", "provenance")]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            b'{"payloads":[{"kind":"web","path":"a.bin"}],"schema_version":1}',
            "canonical UTF-8 JSON",
        ),
        (b"\xef\xbb\xbf{}\n", "UTF-8 without BOM"),
        (b"\xff\n", "UTF-8"),
        (
            b'{"payloads":[],"payloads":[],"schema_version":1}\n',
            "duplicate JSON key",
        ),
    ],
)
def test_payload_list_requires_strict_canonical_utf8_json(
    tmp_path: Path, content: bytes, message: str
) -> None:
    payload_list = tmp_path / "payload-list.json"
    payload_list.write_bytes(content)

    with pytest.raises(ManifestError, match=message):
        read_payload_list(payload_list)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schema_version": True, "payloads": []}, "schema_version"),
        ({"schema_version": 2, "payloads": []}, "schema_version"),
        ({"schema_version": 1, "payloads": []}, "non-empty array"),
        (
            {"schema_version": 1, "payloads": [], "extra": True},
            "exactly schema_version and payloads",
        ),
        (
            {
                "schema_version": 1,
                "payloads": [{"path": "a.bin", "kind": "unknown"}],
            },
            "unsupported payload kind",
        ),
        (
            {
                "schema_version": 1,
                "payloads": [{"path": "../a.bin", "kind": "web"}],
            },
            "normalized POSIX relative path",
        ),
        (
            {
                "schema_version": 1,
                "payloads": [{"path": "C:/a.bin", "kind": "web"}],
            },
            "relative path",
        ),
        (
            {
                "schema_version": 1,
                "payloads": [{"path": "a\\b.bin", "kind": "web"}],
            },
            "relative path",
        ),
    ],
)
def test_payload_list_rejects_invalid_contract(
    tmp_path: Path, document: dict[str, object], message: str
) -> None:
    payload_list = tmp_path / "payload-list.json"
    payload_list.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(ManifestError, match=message):
        read_payload_list(payload_list)


@pytest.mark.parametrize(
    "paths",
    [
        ["a.bin", "a.bin"],
        ["A.bin", "a.bin"],
        ["\uff21.bin", "A.bin"],
    ],
)
def test_payload_list_rejects_duplicate_or_windows_colliding_paths(
    tmp_path: Path, paths: list[str]
) -> None:
    payload_list = tmp_path / "payload-list.json"
    _write_payload_list(
        payload_list,
        [{"path": path, "kind": "web"} for path in paths],
    )

    with pytest.raises(ManifestError, match="colliding payload path"):
        read_payload_list(payload_list)


def test_cli_rejects_duplicate_path_across_inline_and_payload_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"a")
    payload_list = tmp_path / "payload-list.json"
    _write_payload_list(
        payload_list,
        [{"path": "A.bin", "kind": "provenance"}],
    )

    result = main(
        [
            "create",
            "--root",
            str(tmp_path),
            "--output",
            str(tmp_path / "manifest.json"),
            "--source-sha",
            SOURCE_SHA,
            "--source-tree",
            SOURCE_TREE,
            "--workflow",
            "CI",
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--job-id",
            "windows",
            "--job-name",
            "Windows",
            "--payload",
            "a.bin:provenance",
            "--payload-list",
            str(payload_list),
            "--critical-input",
            f"ci={INPUT_SHA}",
            "--toolchain",
            "python=3.12",
            "--lockfile",
            f"Cargo.lock={LOCK_SHA}",
        ]
    )

    assert result == 2
    assert "colliding payload path" in capsys.readouterr().err
