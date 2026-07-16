from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.artifact_manifest import (
    ManifestError,
    build_manifest,
    create_attestation_binding,
    main as manifest_main,
    read_manifest,
    validate_manifest,
    verify_for_consumption,
    verify_payloads,
)
from scripts.verify_ci_cache_policy import (
    CachePolicyError,
    compare_clean_and_warm_runs,
    main as cache_main,
    validate_cache_policy,
    validate_cache_run_evidence,
    verify_workflow_cache_policy,
)


SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
DIGEST = "3" * 64


def _manifest(root: Path, *, kind: str = "web") -> dict[str, object]:
    (root / "payload.bin").write_bytes(b"payload")
    return build_manifest(
        root=root,
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
        producer={
            "workflow": "CI",
            "run_id": 1,
            "run_attempt": 1,
            "job_id": "build",
            "job_name": "Build",
        },
        payloads=[("payload.bin", kind)],
        critical_inputs={"Dockerfile": DIGEST},
        toolchain={"python": "3.12"},
        lockfiles={"uv.lock": DIGEST},
        image_digest=f"sha256:{DIGEST}" if kind == "oci" else None,
        cargo_lock_sha256=DIGEST if kind == "tauri-unsigned" else None,
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "unknown manifest fields"),
        (lambda value: value.update(source_sha=1), "source_sha"),
        (lambda value: value.update(source_tree="A" * 40), "source_tree"),
        (lambda value: value.update(producer=[]), "producer must contain exactly"),
        (
            lambda value: value["producer"].update(workflow=" "),
            "producer.workflow",
        ),
        (lambda value: value["producer"].update(run_id=True), "producer.run_id"),
        (lambda value: value["producer"].update(run_attempt=0), "producer.run_attempt"),
        (lambda value: value.update(payloads=[]), "payloads must be a non-empty"),
        (lambda value: value.update(payloads=[{"path": "x"}]), "contain exactly"),
        (
            lambda value: value["payloads"][0].update(path=""),
            "non-empty POSIX relative path",
        ),
        (
            lambda value: value["payloads"][0].update(path="dir\\payload"),
            "non-empty POSIX relative path",
        ),
        (
            lambda value: value["payloads"][0].update(path="/payload"),
            "normalized POSIX relative path",
        ),
        (
            lambda value: value["payloads"].append(deepcopy(value["payloads"][0])),
            "colliding payload path",
        ),
        (
            lambda value: value["payloads"][0].update(kind="installer"),
            "unsupported payload kind",
        ),
        (
            lambda value: value["payloads"][0].update(size=True),
            "non-negative integer",
        ),
        (
            lambda value: value["payloads"][0].update(size=-1),
            "non-negative integer",
        ),
        (
            lambda value: value["payloads"][0].update(sha256="ABC"),
            "lowercase SHA-256",
        ),
        (lambda value: value.update(critical_inputs=[]), "critical_inputs"),
        (lambda value: value.update(critical_inputs={}), "critical_inputs"),
        (lambda value: value.update(critical_inputs={1: DIGEST}), "keys"),
        (
            lambda value: value.update(critical_inputs={"Dockerfile": ""}),
            "non-empty string",
        ),
        (
            lambda value: value.update(lockfiles={"uv.lock": "not-a-digest"}),
            "lowercase SHA-256",
        ),
        (lambda value: value.update(toolchain={"python": 312}), "non-empty string"),
    ],
)
def test_manifest_validation_fails_closed_for_malformed_identity(
    tmp_path: Path, mutate: object, message: str
) -> None:
    manifest = _manifest(tmp_path)
    manifest.pop("manifest_sha256")
    assert callable(mutate)
    mutate(manifest)

    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    ("kind", "metadata", "message"),
    [
        ("web", {"image_digest": f"sha256:{DIGEST}"}, "only valid with an OCI"),
        ("oci", {"image_digest": "sha256:BAD"}, "sha256 OCI digest"),
        ("web", {"tauri": []}, "tauri must contain exactly"),
        (
            "web",
            {"tauri": {"cargo_lock_sha256": "bad"}},
            "lowercase SHA-256",
        ),
    ],
)
def test_optional_artifact_metadata_cannot_claim_unbound_identity(
    tmp_path: Path, kind: str, metadata: dict[str, object], message: str
) -> None:
    manifest = _manifest(tmp_path, kind=kind)
    manifest.pop("manifest_sha256")
    manifest.update(metadata)
    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest)


def test_manifest_io_and_payload_verification_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ManifestError, match="cannot read manifest"):
        read_manifest(missing)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ManifestError, match="cannot read manifest"):
        read_manifest(invalid)

    manifest = _manifest(tmp_path)
    (tmp_path / "payload.bin").unlink()
    with pytest.raises(ManifestError, match="missing payload"):
        verify_payloads(manifest, tmp_path)

    (tmp_path / "payload.bin").mkdir()
    with pytest.raises(ManifestError, match="regular non-symlink"):
        verify_payloads(manifest, tmp_path)


def test_payload_digest_and_attestation_are_both_checked(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"PAYLOAD")  # Same size, different digest.
    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_payloads(manifest, tmp_path)

    payload.write_bytes(b"payload")
    attestation = create_attestation_binding(manifest)
    attestation["unexpected"] = True
    with pytest.raises(ManifestError, match="attestation"):
        verify_for_consumption(
            manifest,
            root=tmp_path,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            attestation=attestation,
        )


def test_build_and_manifest_cli_report_bad_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ManifestError, match="missing payload"):
        build_manifest(
            root=tmp_path,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            producer={
                "workflow": "CI",
                "run_id": 1,
                "run_attempt": 1,
                "job_id": "build",
                "job_name": "Build",
            },
            payloads=[("missing.bin", "web")],
            critical_inputs={"Dockerfile": DIGEST},
            toolchain={"python": "3.12"},
            lockfiles={"uv.lock": DIGEST},
        )

    common = [
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
        "build",
        "--job-name",
        "Build",
    ]
    assert manifest_main([*common, "--payload", "no-kind"]) == 2
    assert "PATH:KIND" in capsys.readouterr().err

    assert (
        manifest_main(
            [
                *common,
                "--payload",
                "missing.bin:web",
                "--critical-input",
                "malformed",
            ]
        )
        == 2
    )
    assert "NAME=VALUE" in capsys.readouterr().err

    bad_attestation = tmp_path / "attestation.json"
    bad_attestation.write_text("{", encoding="utf-8")
    manifest = _manifest(tmp_path)
    manifest_path = tmp_path / "valid-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        manifest_main(
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
                str(bad_attestation),
            ]
        )
        == 2
    )
    assert "cannot read attestation binding" in capsys.readouterr().err


def _cache_entry() -> dict[str, object]:
    dimensions = {
        "os": "${{ runner.os }}",
        "architecture": "${{ runner.arch }}",
        "toolchain": "${{ matrix.python-version }}",
        "lockfile": "${{ hashFiles('uv.lock') }}",
    }
    return {
        "name": "uv-cache",
        "ecosystem": "uv",
        "paths": ["~/.cache/uv"],
        "key": "-".join(dimensions.values()),
        "dimensions": dimensions,
        "content_classes": ["dependency-downloads"],
    }


def _policy() -> dict[str, object]:
    return {"schema_version": 1, "entries": [_cache_entry()]}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda policy: policy.update(extra=True), "exactly schema_version"),
        (lambda policy: policy.update(schema_version=2), "schema_version"),
        (lambda policy: policy.update(entries=[]), "non-empty array"),
        (lambda policy: policy.update(entries=[{}]), "contain exactly"),
        (lambda policy: policy["entries"][0].update(name=" "), "name"),
        (
            lambda policy: policy["entries"].append(deepcopy(policy["entries"][0])),
            "duplicate cache entry",
        ),
        (
            lambda policy: policy["entries"][0].update(ecosystem="npm"),
            "unsupported ecosystem",
        ),
        (lambda policy: policy["entries"][0].update(paths=[]), "paths"),
        (
            lambda policy: policy["entries"][0].update(paths=["../.cache/uv"]),
            "cannot contain",
        ),
        (
            lambda policy: policy["entries"][0].update(paths=[1]),
            "non-empty POSIX paths",
        ),
        (
            lambda policy: policy["entries"][0].update(content_classes=[]),
            "content_classes must be non-empty",
        ),
        (
            lambda policy: policy["entries"][0].update(content_classes=[1]),
            "must contain strings",
        ),
        (
            lambda policy: policy["entries"][0].update(
                ecosystem="playwright", paths=["~/.cache/ms-playwright"]
            ),
            "not valid for playwright",
        ),
        (lambda policy: policy["entries"][0].update(key=" "), "key"),
        (
            lambda policy: policy["entries"][0].update(dimensions=[]),
            "dimensions must contain exactly",
        ),
        (
            lambda policy: policy["entries"][0]["dimensions"].update(os=""),
            "dimension os is empty",
        ),
        (
            lambda policy: (
                policy["entries"][0]["dimensions"].update(os="linux"),
                policy["entries"][0].update(
                    key=policy["entries"][0]["key"].replace("${{ runner.os }}", "linux")
                ),
            ),
            "OS dimension",
        ),
        (
            lambda policy: (
                policy["entries"][0]["dimensions"].update(architecture="amd64"),
                policy["entries"][0].update(
                    key=policy["entries"][0]["key"].replace(
                        "${{ runner.arch }}", "amd64"
                    )
                ),
            ),
            "architecture dimension",
        ),
    ],
)
def test_cache_policy_rejects_malformed_or_under_bound_entries(
    mutate: object, message: str
) -> None:
    policy = _policy()
    assert callable(mutate)
    mutate(policy)
    with pytest.raises(CachePolicyError, match=message):
        validate_cache_policy(policy)


def _workflow(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ci.yml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("{", "cannot read workflow"),
        ("name: CI\n", "no jobs object"),
        ("jobs:\n  ? [bad]\n  : {}\n", "cannot read workflow"),
        ("jobs:\n  verify: []\n", "invalid workflow job"),
        ("jobs:\n  verify:\n    steps: {}\n", "steps must be an array"),
        ("jobs:\n  verify:\n    steps:\n      - bad\n", "must be an object"),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n",
            "requires a with object",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: [bad]\n          key: bad\n",
            "path must be a string",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: '  '\n          key: bad\n",
            "path is empty",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: |\n            ~/.cache/uv\n            ~/.cache/ms-playwright\n          key: bad\n",
            "exactly one supported ecosystem",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: ~/.cache/uv\n          key: 1\n",
            "key must be a string",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: ~/.cache/uv\n          key: ${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('uv.lock') }}\n",
            "toolchain version expression",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/cache@v4\n        with:\n          path: ~/.cache/uv\n          key: ${{ runner.os }}-${{ runner.arch }}-${{ matrix.python-version }}\n",
            "lockfile hashFiles",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - uses: actions/checkout@v4\n",
            "no explicit cache entries",
        ),
    ],
)
def test_workflow_cache_inventory_rejects_ambiguous_or_implicit_inputs(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(CachePolicyError, match=message):
        verify_workflow_cache_policy([_workflow(tmp_path, body)])


def _run(state: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "cache_state": state,
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "install_completed": True,
        "required_gates": {"unit": "success"},
        "artifact_manifests": {"python": DIGEST},
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda run: run.update(extra=True), "contain exactly"),
        (lambda run: run.update(schema_version=2), "schema_version"),
        (lambda run: run.update(cache_state="warm"), "expected cache_state"),
        (lambda run: run.update(source_sha="main"), "source_sha"),
        (lambda run: run.update(source_tree=1), "source_tree"),
        (lambda run: run.update(required_gates={}), "required_gates"),
        (lambda run: run.update(required_gates={1: "success"}), "required gate"),
        (lambda run: run.update(artifact_manifests={}), "artifact_manifests"),
        (
            lambda run: run.update(artifact_manifests={"python": "BAD"}),
            "map names to lowercase",
        ),
        (
            lambda run: run.update(artifact_manifests={1: DIGEST}),
            "map names to lowercase",
        ),
    ],
)
def test_cache_run_evidence_requires_exact_successful_identity(
    mutate: object, message: str
) -> None:
    run = _run("clean-miss")
    assert callable(mutate)
    mutate(run)
    with pytest.raises(CachePolicyError, match=message):
        validate_cache_run_evidence(run, expected_state="clean-miss")


def test_cache_compare_ignores_digest_value_but_not_manifest_set() -> None:
    clean = _run("clean-miss")
    warm = _run("warm")
    warm["artifact_manifests"] = {"python": "4" * 64}
    compare_clean_and_warm_runs(clean, warm)


def test_cache_cli_reports_invalid_json_and_invalid_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    assert cache_main(["policy", str(invalid)]) == 2
    assert "cannot read cache policy" in capsys.readouterr().err

    wrong = tmp_path / "wrong.json"
    wrong.write_text("{}", encoding="utf-8")
    assert cache_main(["policy", str(wrong)]) == 2
    assert "policy must contain exactly" in capsys.readouterr().err
