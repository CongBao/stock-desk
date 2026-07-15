from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

import scripts.stable_release_readiness as readiness
from scripts import deployment_latency
from scripts.stable_release_readiness import (
    StableReleaseReadinessError,
    evaluate_stable_release_readiness,
)


SHA = "a" * 40
TREE = "b" * 40
TAG_OBJECT = "e" * 40
MAIN_PROOF = "c" * 64
SIGNED_CANDIDATE_BYTES = b"stock-desk-signed-candidate-v1.1.0"
CANDIDATE = hashlib.sha256(SIGNED_CANDIDATE_BYTES).hexdigest()
CANDIDATE_MANIFEST = "f" * 64
SIGNER = "CN=Stock Desk"
THUMBPRINT = "E" * 40
TIMESTAMP_SUBJECT = "CN=Timestamp Authority"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "stable-release-readiness-v1.schema.json"
)


def _sample(run_id: int, category: str) -> dict[str, object]:
    return {
        "schema_version": "stock-desk-deployment-latency-sample-v1",
        "run_id": str(run_id),
        "run_attempt": 1,
        "run_url": (
            f"https://github.com/CongBao/stock-desk/actions/runs/{run_id}/attempts/1"
        ),
        "source_sha": SHA,
        "source_tree": TREE,
        "workflow": f"stable-readiness-{category}",
        "ref": "refs/heads/main",
        "category": category,
        "queued_at": "2026-07-15T00:00:00Z",
        "started_at": "2026-07-15T00:00:01Z",
        "completed_at": "2026-07-15T00:00:03Z",
        "queue_seconds": 1.0,
        "wall_seconds": 2.0,
        "cache_status": "hit",
        "outcome": "success",
        "environment_baseline": {
            "os": "windows-2022",
            "architecture": "x86_64",
            "runner_image": "windows-2022@20260701.1",
            "toolchain": "python-3.12,node-22,rust-1.88",
        },
        "invalidated": False,
        "invalidation_reason": None,
    }


def _latency_bundle() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    ledger = deployment_latency.empty_ledger()
    run_id = 1000
    for category in sorted(readiness.LATENCY_CATEGORIES):
        for _ in range(5):
            run_id += 1
            seal = (
                deployment_latency.ledger_seal(ledger)
                if ledger["record_count"]
                else None
            )
            ledger = deployment_latency.append_sample(
                ledger, _sample(run_id, category), expected_seal=seal
            )
    final_seal = deployment_latency.ledger_seal(ledger)
    return (
        ledger,
        final_seal,
        deployment_latency.aggregate_ledger(ledger, expected_seal=final_seal),
    )


def _case_receipts(*fields: str) -> list[dict[str, str]]:
    return [
        {
            "case_id": case_id,
            **{
                field: hashlib.sha256(f"{case_id}:{field}".encode()).hexdigest()
                for field in fields
            },
        }
        for case_id in sorted(readiness.WINDOWS_CASE_IDS)
    ]


def _key_material(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key_id = b"key-id01"
    packet = b"Ed" + key_id + public
    public_path = tmp_path / "tauri-updater-public-key.pub"
    public_path.write_text(
        "untrusted comment: Stock Desk updater public key\n"
        + base64.b64encode(packet).decode()
        + "\n",
        encoding="utf-8",
    )
    challenge = f"stock-desk-updater-key-ceremony-v1:{SHA}:{TREE}"
    receipt = {
        "schema": "stock-desk-updater-key-ceremony-v1",
        "evidence_kind": "offline-key-possession-ceremony",
        "source_sha": SHA,
        "source_tree": TREE,
        "public_key_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(),
        "key_id": key_id.hex(),
        "challenge": challenge,
        "challenge_signature": base64.b64encode(
            private.sign(challenge.encode())
        ).decode(),
        "ceremony_evidence_sha256": "1" * 64,
        "status": "witnessed",
    }
    return public_path, receipt


def _sign_private_audit_records(tmp_path: Path, *records: dict[str, object]) -> Path:
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path = tmp_path / "release-auditor-public-key.pem"
    public_path.write_bytes(public_bytes)
    public_digest = hashlib.sha256(public_bytes).hexdigest()
    for record in records:
        record["auditor_public_key_sha256"] = public_digest
        payload = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record["audit_signature"] = base64.b64encode(private.sign(payload)).decode()
    return public_path


def _evidence_documents(
    tmp_path: Path,
) -> tuple[dict[str, dict[str, object]], Path, Path]:
    public_key, ceremony = _key_material(tmp_path)
    ledger, seal, report = _latency_bundle()
    identity = {
        "source_sha": SHA,
        "source_tree": TREE,
        "main_proof_sha256": MAIN_PROOF,
        "candidate_sha256": CANDIDATE,
    }
    nsis = {
        "schema": "stock-desk-nsis-installation-control-proof-v1",
        "evidence_kind": "observed-windows-install-control",
        **identity,
        "candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "verifier": "external-protected-windows-controller",
        "run_id": 201,
        "run_attempt": 1,
        "cases": [
            {
                "case_id": case_id,
                "result": result,
                "observation_sha256": hashlib.sha256(case_id.encode()).hexdigest(),
            }
            for case_id, result in readiness.NSIS_CASE_RESULTS.items()
        ],
    }
    acceptance = {
        "schema": "stock-desk-windows-installed-acceptance-receipt-v2",
        "artifact": "windows-installed-acceptance-receipt",
        "evidence_kind": "observed-windows-vm",
        **identity,
        "webview_installer_sha256": "2" * 64,
        "snapshot_policy_sha256": "3" * 64,
        "adapter_sha256": "4" * 64,
        "broker_public_key_sha256": "5" * 64,
        "repository": readiness.REPOSITORY,
        "workflow": "Windows Installed Acceptance",
        "workflow_ref": (
            "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main"
        ),
        "workflow_sha256": "6" * 64,
        "run_id": 202,
        "run_attempt": 1,
        "case_receipts": _case_receipts("derived_sha256", "raw_package_sha256"),
        "status": "accepted",
    }
    ux = {
        "schema": "stock-desk-windows-ux-evidence-v1",
        "evidence_kind": "observed-windows-desktop-ux",
        **identity,
        "verifier": "external-protected-windows-controller",
        "run_id": 203,
        "run_attempt": 1,
        "first_kline_ready_seconds": 42.5,
        "first_kline_click_count": 4,
        "case_receipts": _case_receipts(
            "evidence_sha256",
            "screenshot_sha256",
            "video_sha256",
            "journey_event_sha256",
        ),
        "result": "passed",
    }
    wiki = {
        "schema": "stock-desk-final-wiki-evidence-v1",
        "evidence_kind": "published-bilingual-wiki-and-real-screenshots",
        "source_sha": SHA,
        "source_tree": TREE,
        "wiki_commit": "7" * 40,
        "readme_sha256": "8" * 64,
        "wiki_manifest_sha256": "9" * 64,
        "screenshot_manifest_sha256": "0" * 64,
        "screenshot_count": 40,
        "locales": ["zh-CN", "en"],
        "verifier": "stock-desk-docs-final-gate-v1",
        "result": "passed",
    }
    openspec = {
        "schema": "stock-desk-openspec-completion-evidence-v1",
        "evidence_kind": "redacted-private-pre-release-specification-audit",
        "source_sha": SHA,
        "source_tree": TREE,
        "release_tag": "v1.1.0",
        "tag_object_sha": TAG_OBJECT,
        "change_id": "build-windows-desktop-ux-v1-1",
        "tasks_sha256": "a" * 64,
        "completed_task_ids": sorted(readiness.PRE_RELEASE_OPENSPEC_TASK_IDS),
        "remaining_task_ids": sorted(readiness.POST_RELEASE_OPENSPEC_TASK_IDS),
        "completed_task_count": 43,
        "total_task_count": 44,
        "remaining_task_count": 1,
        "publication_boundary": "private-source-redacted-proof-only",
        "verifier": "openspec-strict-pre-release-audit-v1",
        "status": "pre-release-complete",
    }
    requirements = {
        "schema": "stock-desk-requirements-completion-evidence-v1",
        "evidence_kind": "redacted-private-requirements-audit",
        "source_sha": SHA,
        "source_tree": TREE,
        "release_tag": "v1.1.0",
        "tag_object_sha": TAG_OBJECT,
        "private_requirements_sha256": "b" * 64,
        "active_requirement_ids": sorted(readiness.REQUIREMENT_IDS),
        "non_goal_ids": sorted(readiness.NON_GOAL_IDS),
        "public_acceptance_sha256": hashlib.sha256(
            (readiness.ROOT / readiness.PUBLIC_REQUIREMENTS_AUTHORITY).read_bytes()
        ).hexdigest(),
        "public_requirement_ids": [f"R-{number:03d}" for number in range(1, 83)],
        "public_non_goal_ids": [f"N-{number:03d}" for number in range(1, 11)],
        "public_v11_acceptance_sha256": hashlib.sha256(
            (readiness.ROOT / readiness.V11_REQUIREMENTS_AUTHORITY).read_bytes()
        ).hexdigest(),
        "public_v11_requirement_ids": [
            f"V11-R-{number:03d}" for number in range(1, 22)
        ],
        "public_v11_non_goal_ids": [],
        "verified_requirement_count": 49,
        "verified_public_requirement_count": 82,
        "verified_public_v11_requirement_count": 21,
        "failed_requirement_count": 0,
        "xfail_requirement_count": 0,
        "stale_requirement_count": 0,
        "publication_boundary": "private-source-redacted-proof-only",
        "verifier": "requirements-ledger-final-audit-v1",
        "status": "complete",
    }
    auditor_public = _sign_private_audit_records(tmp_path, openspec, requirements)
    return (
        {
            "nsis_control_proof": nsis,
            "signpath_receipt": {},
            "windows_acceptance_receipt": acceptance,
            "windows_10_trust_receipt": {},
            "windows_11_trust_receipt": {},
            "windows_ux_evidence": ux,
            "updater_key_ceremony": ceremony,
            "latency_ledger": ledger,
            "latency_seal": seal,
            "latency_report": report,
            "final_wiki_evidence": wiki,
            "requirements_completion_evidence": requirements,
            "openspec_completion_evidence": openspec,
        },
        public_key,
        auditor_public,
    )


def _write_closure(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, dict[str, dict[str, object]]]:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    documents, public_key, auditor_public = _evidence_documents(tmp_path)
    refs: dict[str, dict[str, str]] = {}
    for name, document in documents.items():
        subject = evidence_root / f"{name}.json"
        subject.write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
        )
        bundle = evidence_root / f"{name}.attestation.json"
        bundle.write_text(json.dumps({"attestation": name}) + "\n", encoding="utf-8")
        refs[name] = {
            "path": subject.name,
            "sha256": hashlib.sha256(subject.read_bytes()).hexdigest(),
            "attestation_path": bundle.name,
            "attestation_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
    manifest = {
        "schema_version": readiness.SCHEMA,
        "release_version": "1.1.0",
        "source_sha": SHA,
        "source_tree": TREE,
        "release_tag": "v1.1.0",
        "tag_object_sha": TAG_OBJECT,
        "tag_target_sha": SHA,
        "main_proof_sha256": MAIN_PROOF,
        "candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "candidate_sha256": CANDIDATE,
        "signer": {
            "subject": SIGNER,
            "certificate_thumbprint": THUMBPRINT,
            "timestamp_subject": TIMESTAMP_SUBJECT,
        },
        "artifacts": refs,
    }
    manifest_path = tmp_path / "stable-readiness.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    (tmp_path / "signed-candidate.exe").write_bytes(SIGNED_CANDIDATE_BYTES)
    return manifest_path, evidence_root, public_key, auditor_public, documents


def _patch_external_verifiers(
    monkeypatch: pytest.MonkeyPatch, auditor_public: Path
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        readiness,
        "_verify_github_attestation",
        lambda subject, _bundle, _sha, workflow: calls.append((subject.name, workflow)),
    )
    monkeypatch.setattr(readiness, "_verify_signpath_receipt", lambda *_args: None)
    monkeypatch.setattr(readiness, "_verify_windows_receipt", lambda *_args: None)
    monkeypatch.setattr(readiness, "_verify_annotated_tag", lambda *_args: None)

    def tracked_source(_source_sha: str, relative_path: str) -> bytes:
        if relative_path == readiness.RELEASE_AUDITOR_PUBLIC_KEY_PATH:
            return auditor_public.read_bytes()
        return (readiness.ROOT / relative_path).read_bytes()

    monkeypatch.setattr(readiness, "_read_tracked_source_file", tracked_source)
    return calls


def _trusted_release_inputs(tmp_path: Path) -> dict[str, object]:
    return {
        "expected_main_proof_sha256": MAIN_PROOF,
        "expected_candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "expected_candidate_sha256": CANDIDATE,
        "signed_candidate_path": tmp_path / "signed-candidate.exe",
    }


def _evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], list[tuple[str, str]]]:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    calls = _patch_external_verifiers(monkeypatch, auditor_public)
    decision = evaluate_stable_release_readiness(
        manifest_path=manifest,
        evidence_root=evidence,
        expected_version="1.1.0",
        expected_source_sha=SHA,
        expected_source_tree=TREE,
        **_trusted_release_inputs(tmp_path),
        updater_public_key_path=public_key,
    )
    return dict(decision), calls


def _cli_arguments(tmp_path: Path) -> list[str]:
    return [
        "--manifest",
        str(tmp_path / "stable-readiness.json"),
        "--evidence-root",
        str(tmp_path / "evidence"),
        "--expected-version",
        "1.1.0",
        "--source-sha",
        SHA,
        "--source-tree",
        TREE,
        "--expected-main-proof-sha256",
        MAIN_PROOF,
        "--expected-candidate-manifest-sha256",
        CANDIDATE_MANIFEST,
        "--expected-candidate-sha256",
        CANDIDATE,
        "--signed-candidate",
        str(tmp_path / "signed-candidate.exe"),
        "--updater-public-key",
        str(tmp_path / "updater.pub"),
    ]


def test_cli_passes_every_trusted_input_and_prints_the_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}
    decision: readiness.StableReleaseDecision = {
        "eligible": True,
        "version": "1.1.0",
        "source_sha": SHA,
        "source_tree": TREE,
        "release_tag": "v1.1.0",
        "tag_object_sha": TAG_OBJECT,
        "candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "candidate_sha256": CANDIDATE,
        "evidence_count": 13,
    }

    def evaluate(**kwargs: object) -> readiness.StableReleaseDecision:
        observed.update(kwargs)
        return decision

    monkeypatch.setattr(readiness, "evaluate_stable_release_readiness", evaluate)
    assert readiness.main(_cli_arguments(tmp_path)) == 0
    assert observed == {
        "manifest_path": tmp_path / "stable-readiness.json",
        "evidence_root": tmp_path / "evidence",
        "expected_version": "1.1.0",
        "expected_source_sha": SHA,
        "expected_source_tree": TREE,
        "expected_main_proof_sha256": MAIN_PROOF,
        "expected_candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "expected_candidate_sha256": CANDIDATE,
        "signed_candidate_path": tmp_path / "signed-candidate.exe",
        "updater_public_key_path": tmp_path / "updater.pub",
    }
    assert json.loads(capsys.readouterr().out) == decision


def test_cli_converts_readiness_errors_to_a_fail_closed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(**_kwargs: object) -> readiness.StableReleaseDecision:
        raise StableReleaseReadinessError("evidence is incomplete")

    monkeypatch.setattr(readiness, "evaluate_stable_release_readiness", reject)
    with pytest.raises(SystemExit, match="evidence is incomplete"):
        readiness.main(_cli_arguments(tmp_path))


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: readiness._decode_json(b'{"a":1,"a":2}', "record"), "duplicate"),
        (lambda: readiness._decode_json(b"\xff", "record"), "UTF-8 JSON"),
        (lambda: readiness._decode_json(b"[]", "record"), "JSON object"),
        (lambda: readiness._exact({}, {"required"}, "record"), "missing required"),
        (lambda: readiness._git_id("A" * 40, "commit"), "lowercase 40-hex"),
        (lambda: readiness._digest("F" * 64, "artifact"), "lowercase SHA-256"),
        (lambda: readiness._safe_relative("bad\\path", "path"), "safe relative"),
        (lambda: readiness._safe_relative("../escape", "path"), "safe relative"),
    ],
)
def test_strict_json_identity_and_path_helpers_fail_closed(
    operation: Any, message: str
) -> None:
    with pytest.raises(StableReleaseReadinessError, match=message):
        operation()


def test_regular_file_reader_and_evidence_path_resolution_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x")
    with pytest.raises(StableReleaseReadinessError, match="bounded regular file"):
        readiness._read_regular(oversized, "oversized file", limit=0)

    subject = tmp_path / "subject.json"
    subject.write_text("{}", encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(
            readiness.os,
            "lstat",
            lambda _path: (_ for _ in ()).throw(FileNotFoundError()),
        )
        with pytest.raises(StableReleaseReadinessError, match="path changed"):
            readiness._read_regular(subject, "subject")

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(StableReleaseReadinessError, match="parent is unavailable"):
        readiness._resolve_under(root, Path("missing/record.json"), "record")


def test_external_command_failures_do_not_become_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("unavailable")

    monkeypatch.setattr(readiness.subprocess, "run", unavailable)
    with pytest.raises(StableReleaseReadinessError, match="attestation.*unavailable"):
        readiness._verify_github_attestation(
            tmp_path / "subject", tmp_path / "bundle", SHA, "workflow.yml"
        )
    with pytest.raises(StableReleaseReadinessError, match="trusted source blob"):
        readiness._read_tracked_source_file(SHA, "authority.yml")

    monkeypatch.setattr(
        readiness.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    with pytest.raises(StableReleaseReadinessError, match="attestation.*failed"):
        readiness._verify_github_attestation(
            tmp_path / "subject", tmp_path / "bundle", SHA, "workflow.yml"
        )
    with pytest.raises(StableReleaseReadinessError, match="missing or oversized"):
        readiness._read_tracked_source_file(SHA, "authority.yml")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.update(artifacts=[]), "artifacts must be an object"),
        (
            lambda manifest: manifest["artifacts"].update(nsis_control_proof=[]),
            "must be an object",
        ),
        (
            lambda manifest: manifest["artifacts"]["nsis_control_proof"].update(
                attestation_path=manifest["artifacts"]["nsis_control_proof"]["path"]
            ),
            "paths must be unique",
        ),
        (
            lambda manifest: manifest["artifacts"]["nsis_control_proof"].update(
                sha256="0" * 64
            ),
            "digest does not match",
        ),
        (
            lambda manifest: manifest["artifacts"]["nsis_control_proof"].update(
                attestation_sha256="0" * 64
            ),
            "attestation digest does not match",
        ),
    ],
)
def test_artifact_index_rejects_ambiguous_or_unbound_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
    message: str,
) -> None:
    manifest_path, evidence, _, auditor_public, _ = _write_closure(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match=message):
        readiness._load_artifacts(manifest, evidence, SHA)


def test_nsis_control_receipt_rejects_malformed_observations(tmp_path: Path) -> None:
    documents, _, _ = _evidence_documents(tmp_path)
    valid = documents["nsis_control_proof"]
    mutations: list[tuple[Any, str]] = [
        (
            lambda record: record.update(candidate_manifest_sha256="0" * 64),
            "candidate manifest",
        ),
        (lambda record: record.update(schema="unknown"), "authoritative observed"),
        (lambda record: record["cases"].__setitem__(0, "invalid"), "must be objects"),
        (
            lambda record: record["cases"][1].update(
                case_id=record["cases"][0]["case_id"]
            ),
            "invalid or duplicated",
        ),
        (lambda record: record["cases"][0].update(result=1), "result is invalid"),
    ]
    for mutate, message in mutations:
        record = copy.deepcopy(valid)
        mutate(record)
        with pytest.raises(StableReleaseReadinessError, match=message):
            readiness._verify_nsis(
                record,
                source_sha=SHA,
                source_tree=TREE,
                main_proof=MAIN_PROOF,
                candidate_manifest=CANDIDATE_MANIFEST,
                candidate=CANDIDATE,
            )


def test_windows_receipts_reject_malformed_case_collections(tmp_path: Path) -> None:
    documents, _, _ = _evidence_documents(tmp_path)
    acceptance = documents["windows_acceptance_receipt"]
    acceptance_mutations: list[tuple[Any, str]] = [
        (lambda record: record.update(status="skipped"), "authoritative first-attempt"),
        (lambda record: record.update(case_receipts={}), "must be a list"),
        (
            lambda record: record["case_receipts"].__setitem__(0, "invalid"),
            "must be an object",
        ),
        (
            lambda record: record["case_receipts"][1].update(
                case_id=record["case_receipts"][0]["case_id"]
            ),
            "case is duplicated",
        ),
    ]
    for mutate, message in acceptance_mutations:
        record = copy.deepcopy(acceptance)
        mutate(record)
        with pytest.raises(StableReleaseReadinessError, match=message):
            readiness._verify_windows_acceptance(
                record,
                source_sha=SHA,
                source_tree=TREE,
                main_proof=MAIN_PROOF,
                candidate=CANDIDATE,
            )

    ux = documents["windows_ux_evidence"]
    ux_mutations: list[tuple[Any, str]] = [
        (lambda record: record.update(case_receipts={}), "must be a list"),
        (
            lambda record: record["case_receipts"].__setitem__(0, "invalid"),
            "must be an object",
        ),
        (
            lambda record: record["case_receipts"][1].update(
                case_id=record["case_receipts"][0]["case_id"]
            ),
            "case is duplicated",
        ),
        (lambda record: record["case_receipts"].pop(), "cover all eleven"),
    ]
    for mutate, message in ux_mutations:
        record = copy.deepcopy(ux)
        mutate(record)
        with pytest.raises(StableReleaseReadinessError, match=message):
            readiness._verify_windows_ux(
                record,
                source_sha=SHA,
                source_tree=TREE,
                main_proof=MAIN_PROOF,
                candidate=CANDIDATE,
            )


def test_key_ceremony_rejects_malformed_identity_and_encoding(tmp_path: Path) -> None:
    documents, public_key, _ = _evidence_documents(tmp_path)
    valid = documents["updater_key_ceremony"]
    mutations: list[tuple[Any, str]] = [
        (lambda record: record.update(status="claimed"), "exact-SHA witnessed"),
        (
            lambda record: record.update(challenge_signature="%%%"),
            "signature is invalid",
        ),
        (lambda record: record.update(key_id="00" * 8), "identity is invalid"),
    ]
    for mutate, message in mutations:
        record = copy.deepcopy(valid)
        mutate(record)
        with pytest.raises(StableReleaseReadinessError, match=message):
            readiness._verify_key_ceremony(
                record,
                source_sha=SHA,
                source_tree=TREE,
                public_key_path=public_key,
            )


def test_schema_and_verifier_require_the_same_complete_evidence_closure() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "release_version",
        "source_sha",
        "source_tree",
        "release_tag",
        "tag_object_sha",
        "tag_target_sha",
        "main_proof_sha256",
        "candidate_manifest_sha256",
        "candidate_sha256",
        "signer",
        "artifacts",
    }
    artifacts = schema["properties"]["artifacts"]
    assert artifacts["additionalProperties"] is False
    assert set(artifacts["required"]) == set(readiness.ARTIFACT_WORKFLOWS)
    assert set(artifacts["properties"]) == set(readiness.ARTIFACT_WORKFLOWS)
    artifact_ref = schema["$defs"]["artifactRef"]
    assert artifact_ref["additionalProperties"] is False
    assert set(artifact_ref["required"]) == {
        "path",
        "sha256",
        "attestation_path",
        "attestation_sha256",
    }
    assert schema["$defs"]["relativePath"]["maxLength"] == (
        readiness.MAX_RELATIVE_PATH_LENGTH
    )
    serialized = json.dumps(schema)
    assert "claimed_verified" not in serialized
    assert not re.search(r'"(passed|skipped)"\s*:', serialized)


def test_runtime_rejects_paths_longer_than_the_schema_limit() -> None:
    with pytest.raises(StableReleaseReadinessError, match="at most 1024"):
        readiness._safe_relative(
            "x" * (readiness.MAX_RELATIVE_PATH_LENGTH + 1), "evidence path"
        )


def test_github_attestation_uses_hardened_json_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        return subprocess.CompletedProcess(command, 0, '[{"verified": true}]', "")

    monkeypatch.setattr(readiness.subprocess, "run", run)
    readiness._verify_github_attestation(
        tmp_path / "subject.json",
        tmp_path / "bundle.json",
        SHA,
        ".github/workflows/release.yml",
    )
    assert "--deny-self-hosted-runners" in observed
    assert observed[observed.index("--format") + 1] == "json"
    assert observed[observed.index("--source-digest") + 1] == SHA
    assert observed[observed.index("--signer-digest") + 1] == SHA


@pytest.mark.parametrize("stdout", ["", "{}", "[]", "not-json"])
def test_github_attestation_rejects_empty_or_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    monkeypatch.setattr(
        readiness.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout, ""),
    )
    with pytest.raises(StableReleaseReadinessError, match="JSON|verified subject"):
        readiness._verify_github_attestation(
            tmp_path / "subject.json",
            tmp_path / "bundle.json",
            SHA,
            ".github/workflows/release.yml",
        )


def test_complete_attested_exact_sha_closure_is_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision, calls = _evaluate(tmp_path, monkeypatch)
    assert decision == {
        "eligible": True,
        "version": "1.1.0",
        "source_sha": SHA,
        "source_tree": TREE,
        "release_tag": "v1.1.0",
        "tag_object_sha": TAG_OBJECT,
        "candidate_manifest_sha256": CANDIDATE_MANIFEST,
        "candidate_sha256": CANDIDATE,
        "evidence_count": 13,
    }
    assert len(calls) == 13
    assert {workflow for _, workflow in calls} == set(
        readiness.ARTIFACT_WORKFLOWS.values()
    )


def test_evidence_is_verified_from_one_immutable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    _patch_external_verifiers(monkeypatch, auditor_public)
    swapped = False

    def swap_original_after_snapshot(
        subject: Path, _bundle: Path, _sha: str, _workflow: str
    ) -> None:
        nonlocal swapped
        assert subject.parent != evidence
        if not swapped:
            (evidence / subject.name).write_text(
                '{"attacker":"replacement"}\n', encoding="utf-8"
            )
            swapped = True

    monkeypatch.setattr(
        readiness, "_verify_github_attestation", swap_original_after_snapshot
    )
    decision = evaluate_stable_release_readiness(
        manifest_path=manifest,
        evidence_root=evidence,
        expected_version="1.1.0",
        expected_source_sha=SHA,
        expected_source_tree=TREE,
        **_trusted_release_inputs(tmp_path),
        updater_public_key_path=public_key,
    )
    assert decision["eligible"] is True
    assert swapped is True


def test_wrong_version_and_conflated_candidate_identities_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    _patch_external_verifiers(monkeypatch, auditor_public)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["release_version"] = "1.1.1"
    value["release_tag"] = "v1.1.1"
    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(StableReleaseReadinessError, match="expected stable version"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )

    value["release_version"] = "1.1.0"
    value["release_tag"] = "v1.1.0"
    value["candidate_manifest_sha256"] = CANDIDATE
    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(StableReleaseReadinessError, match="must be distinct"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )

    value["candidate_manifest_sha256"] = CANDIDATE_MANIFEST
    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    (tmp_path / "signed-candidate.exe").write_bytes(b"different installer")
    with pytest.raises(StableReleaseReadinessError, match="actual installer bytes"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_annotated_tag_verifier_rejects_lightweight_or_wrong_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "ROOT", tmp_path)
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(
        ["git", "-C", tmp_path, "config", "user.name", "Stock Desk Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", tmp_path, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    signing_key = tmp_path / "test-release-signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", signing_key],
        check=True,
    )
    subprocess.run(["git", "-C", tmp_path, "config", "gpg.format", "ssh"], check=True)
    subprocess.run(
        ["git", "-C", tmp_path, "config", "user.signingkey", signing_key],
        check=True,
    )
    allowed_signers = tmp_path / readiness.RELEASE_TAG_ALLOWED_SIGNERS_PATH
    allowed_signers.parent.mkdir()
    public_key = signing_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    allowed_signers.write_text(f"test@example.invalid {public_key}\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", tmp_path, "add", "tracked.txt", allowed_signers], check=True
    )
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "one"], check=True)
    source_sha = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", tmp_path, "tag", "-sam", "release", "v1.1.0"], check=True
    )
    tag_object = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "refs/tags/v1.1.0"], text=True
    ).strip()
    readiness._verify_annotated_tag("v1.1.0", tag_object, source_sha)

    subprocess.run(
        ["git", "-C", tmp_path, "tag", "-am", "unsigned", "unsigned"], check=True
    )
    unsigned = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "refs/tags/unsigned"], text=True
    ).strip()
    with pytest.raises(StableReleaseReadinessError, match="signature"):
        readiness._verify_annotated_tag("unsigned", unsigned, source_sha)

    subprocess.run(["git", "-C", tmp_path, "tag", "lightweight"], check=True)
    lightweight = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "refs/tags/lightweight"], text=True
    ).strip()
    with pytest.raises(StableReleaseReadinessError, match="annotated tag"):
        readiness._verify_annotated_tag("lightweight", lightweight, source_sha)


def test_every_subject_requires_a_real_exact_sha_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    _patch_external_verifiers(monkeypatch, auditor_public)
    monkeypatch.setattr(readiness, "_verify_signpath_receipt", lambda *_args: None)
    monkeypatch.setattr(readiness, "_verify_windows_receipt", lambda *_args: None)
    attempts: list[tuple[str, str, str]] = []

    def reject(subject: Path, _bundle: Path, sha: str, workflow: str) -> None:
        attempts.append((subject.name, sha, workflow))
        raise StableReleaseReadinessError("attestation rejected")

    monkeypatch.setattr(readiness, "_verify_github_attestation", reject)
    with pytest.raises(StableReleaseReadinessError, match="attestation rejected"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )
    assert attempts == [
        (
            "nsis_control_proof.json",
            SHA,
            ".github/workflows/windows-installed.yml",
        )
    ]


@pytest.mark.parametrize(
    ("artifact", "mutate", "message"),
    [
        (
            "nsis_control_proof",
            lambda value: value["cases"][0].update(result="skipped"),
            "incomplete, skipped, or failed",
        ),
        (
            "windows_acceptance_receipt",
            lambda value: value["case_receipts"].pop(),
            "all eleven",
        ),
        (
            "windows_ux_evidence",
            lambda value: value.update(result="skipped"),
            "failed, skipped",
        ),
        (
            "final_wiki_evidence",
            lambda value: value.update(result="skipped"),
            "final-gate",
        ),
        (
            "openspec_completion_evidence",
            lambda value: value["completed_task_ids"].pop(),
            "exact non-circular task set",
        ),
        (
            "requirements_completion_evidence",
            lambda value: value["active_requirement_ids"].pop(),
            "every private and public requirement",
        ),
        (
            "nsis_control_proof",
            lambda value: value.update(run_attempt=True),
            "NSIS run_attempt",
        ),
        (
            "windows_acceptance_receipt",
            lambda value: value.update(run_attempt=True),
            "Windows acceptance run_attempt",
        ),
        (
            "windows_ux_evidence",
            lambda value: value.update(run_attempt=True),
            "Windows UX run_attempt",
        ),
        (
            "requirements_completion_evidence",
            lambda value: value.update(failed_requirement_count=False),
            "failed_requirement_count",
        ),
        (
            "requirements_completion_evidence",
            lambda value: value.update(xfail_requirement_count=False),
            "xfail_requirement_count",
        ),
        (
            "requirements_completion_evidence",
            lambda value: value.update(stale_requirement_count=False),
            "stale_requirement_count",
        ),
        (
            "openspec_completion_evidence",
            lambda value: value.update(remaining_task_count=True),
            "remaining_task_count",
        ),
    ],
)
def test_claimed_or_skipped_receipts_cannot_replace_observed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    mutate: Any,
    message: str,
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    ref = manifest_value["artifacts"][artifact]
    subject = evidence / ref["path"]
    value = json.loads(subject.read_text(encoding="utf-8"))
    mutate(value)
    subject.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    ref["sha256"] = hashlib.sha256(subject.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True) + "\n", encoding="utf-8"
    )
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match=message):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_private_audit_proofs_require_the_pinned_offline_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    ref = manifest_value["artifacts"]["requirements_completion_evidence"]
    subject = evidence / ref["path"]
    value = json.loads(subject.read_text(encoding="utf-8"))
    value["private_requirements_sha256"] = "0" * 64
    subject.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    ref["sha256"] = hashlib.sha256(subject.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True) + "\n", encoding="utf-8"
    )
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match="signature verification"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_public_acceptance_requirement_cannot_be_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    ref = manifest_value["artifacts"]["requirements_completion_evidence"]
    subject = evidence / ref["path"]
    value = json.loads(subject.read_text(encoding="utf-8"))
    value["public_requirement_ids"].pop()
    subject.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    ref["sha256"] = hashlib.sha256(subject.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True) + "\n", encoding="utf-8"
    )
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(
        StableReleaseReadinessError, match="every private and public requirement"
    ):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_fixture_marker_or_claimed_boolean_is_unknown_not_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    ref = manifest_value["artifacts"]["openspec_completion_evidence"]
    subject = evidence / ref["path"]
    value = json.loads(subject.read_text(encoding="utf-8"))
    value["fixture"] = True
    value["claimed_verified"] = True
    subject.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    ref["sha256"] = hashlib.sha256(subject.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True) + "\n", encoding="utf-8"
    )
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match="unknown field"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_latency_requires_five_successful_consecutive_runs_in_every_category() -> None:
    ledger, seal, report = _latency_bundle()
    broken = copy.deepcopy(ledger)
    records = broken["records"]
    assert isinstance(records, list)
    sample = records[-1]["sample"]
    sample["outcome"] = "skipped"
    records[-1]["record_hash"] = deployment_latency._digest(
        deployment_latency._record_payload(records[-1])
    )
    broken["head_hash"] = records[-1]["record_hash"]
    broken_seal = deployment_latency.ledger_seal(broken)
    broken_report = deployment_latency.aggregate_ledger(
        broken, expected_seal=broken_seal
    )
    with pytest.raises(StableReleaseReadinessError, match="skipped, failed"):
        readiness._verify_latency(broken, broken_seal, broken_report)


def test_key_ceremony_requires_private_key_possession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    ref = manifest_value["artifacts"]["updater_key_ceremony"]
    subject = evidence / ref["path"]
    value = json.loads(subject.read_text(encoding="utf-8"))
    value["challenge_signature"] = base64.b64encode(b"x" * 64).decode()
    subject.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    ref["sha256"] = hashlib.sha256(subject.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(manifest_value, sort_keys=True) + "\n", encoding="utf-8"
    )
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match="private-key possession"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha=SHA,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )


def test_manifest_and_receipts_are_bound_to_the_requested_exact_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, evidence, public_key, auditor_public, _ = _write_closure(tmp_path)
    _patch_external_verifiers(monkeypatch, auditor_public)
    with pytest.raises(StableReleaseReadinessError, match="exact-SHA"):
        evaluate_stable_release_readiness(
            manifest_path=manifest,
            evidence_root=evidence,
            expected_version="1.1.0",
            expected_source_sha="f" * 40,
            expected_source_tree=TREE,
            **_trusted_release_inputs(tmp_path),
            updater_public_key_path=public_key,
        )
