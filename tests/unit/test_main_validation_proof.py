from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import scripts.main_validation_proof as proof_module
from scripts.artifact_manifest import create_attestation_binding, manifest_digest
from scripts.main_validation_proof import MainValidationProofError
from scripts.source_fingerprint import ROOT_FILES, TREE_ROOTS


REPOSITORY = "CongBao/stock-desk"
REF = "refs/heads/main"
TIMESTAMP = "2026-07-10T04:00:00Z"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    required = set(ROOT_FILES) | set(proof_module.CRITICAL_INPUTS)
    for relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{relative}\n", encoding="utf-8")
    for tree_root in TREE_ROOTS:
        (tmp_path / tree_root).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "stock_desk").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "stock_desk" / "main.py").write_text(
        "APP = True\n", encoding="utf-8"
    )
    (tmp_path / "migrations" / "env.py").write_text(
        "MIGRATION = True\n", encoding="utf-8"
    )
    (tmp_path / "web" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "main.tsx").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "acceptance" / "requirements.yml").write_text(
        "requirements: []\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "CongBao")
    _git(tmp_path, "config", "user.email", "bao_cong@outlook.com")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "fixture")
    return tmp_path


def _job(
    *,
    name: str,
    run_id: int,
    commit_sha: str,
    job_id: int,
    conclusion: str = "success",
    status: str = "completed",
) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "head_sha": commit_sha,
        "name": name,
        "status": status,
        "conclusion": None if status == "in_progress" else conclusion,
        "started_at": TIMESTAMP,
        "completed_at": None if status == "in_progress" else "2026-07-10T04:01:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/actions/jobs/{job_id}",
    }


def _api_evidence(
    repo: Path,
    policies: dict[str, proof_module.WorkflowPolicy] | None = None,
) -> dict[str, object]:
    commit_sha = _git(repo, "rev-parse", "HEAD")
    tree_sha = _git(repo, "rev-parse", "HEAD^{tree}")
    evidence: dict[str, object] = {}
    for workflow_number, (workflow, policy) in enumerate(
        (policies or proof_module.WORKFLOW_POLICIES).items(), start=1
    ):
        run_id = 1000 + workflow_number
        jobs = [
            _job(
                name=name,
                run_id=run_id,
                commit_sha=commit_sha,
                job_id=(run_id * 100) + index,
            )
            for index, name in enumerate(sorted(policy.required_jobs), start=1)
        ]
        jobs.extend(
            _job(
                name=name,
                run_id=run_id,
                commit_sha=commit_sha,
                job_id=(run_id * 100) + len(jobs) + index,
                conclusion="skipped",
            )
            for index, name in enumerate(sorted(policy.allowed_skipped_jobs), start=1)
        )
        if policy.generation_job is not None:
            jobs.append(
                _job(
                    name=policy.generation_job,
                    run_id=run_id,
                    commit_sha=commit_sha,
                    job_id=(run_id * 100) + len(jobs) + 1,
                    status="in_progress",
                )
            )
        evidence[workflow] = {
            "run": {
                "id": run_id,
                "workflow_id": 2000 + workflow_number,
                "run_attempt": 1,
                "name": workflow,
                "path": policy.path,
                "event": "push",
                "status": "in_progress" if policy.generation_job else "completed",
                "conclusion": None if policy.generation_job else "success",
                "head_branch": "main",
                "head_sha": commit_sha,
                "head_commit": {"tree_id": tree_sha},
                "repository": {"full_name": REPOSITORY},
                "created_at": TIMESTAMP,
                "updated_at": "2026-07-10T04:02:00Z",
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
            },
            "jobs": {"total_count": len(jobs), "jobs": jobs},
        }
    return evidence


def _validation_evidence(
    repo: Path, api_evidence: dict[str, object]
) -> dict[str, object]:
    commit_sha = _git(repo, "rev-parse", "HEAD")
    tree_sha = _git(repo, "rev-parse", "HEAD^{tree}")
    manifests: dict[str, object] = {}
    for policy in proof_module.EVIDENCE_POLICIES.values():
        workflow = api_evidence[policy.workflow]
        assert isinstance(workflow, dict)
        run = workflow["run"]
        assert isinstance(run, dict)
        payload_kind = (
            "web"
            if policy.artifact_name == "web-build-manifest"
            else "oci"
            if policy.artifact_name == "oci-image-manifest"
            else "tauri-unsigned"
            if policy.artifact_name == "windows-desktop-alpha-candidate-manifest"
            else "provenance"
        )
        payload_name = (
            "stock-desk-1.1.0-alpha.2-unsigned-x64-setup.exe"
            if payload_kind == "tauri-unsigned"
            else f"{policy.artifact_name}.json"
        )
        manifests[policy.artifact_name] = {
            "schema_version": 2,
            "source_sha": commit_sha,
            "source_tree": tree_sha,
            "producer": {
                "workflow": policy.workflow,
                "run_id": run["id"],
                "run_attempt": run["run_attempt"],
                "job_id": policy.job_id,
                "job_name": policy.job_name,
            },
            "critical_inputs": {"fixture": "1" * 64},
            "toolchain": {"fixture": "1.0"},
            "lockfiles": {"fixture.lock": "2" * 64},
            "payloads": [
                {
                    "path": payload_name,
                    "kind": payload_kind,
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
            **({"image_digest": "sha256:" + "4" * 64} if payload_kind == "oci" else {}),
            **(
                {"tauri": {"cargo_lock_sha256": "5" * 64}}
                if payload_kind == "tauri-unsigned"
                else {}
            ),
        }
    return manifests


def _proof(repo: Path, evidence: dict[str, object] | None = None) -> dict[str, object]:
    api_evidence = evidence if evidence is not None else _api_evidence(repo)
    return proof_module.generate_proof(
        repo_root=repo,
        repository=REPOSITORY,
        ref=REF,
        api_evidence=api_evidence,
        validation_evidence=_validation_evidence(repo, api_evidence),
    )


def _resign(proof: dict[str, Any]) -> None:
    unsigned = dict(proof)
    del unsigned["proof_sha256"]
    proof["proof_sha256"] = proof_module._proof_digest(unsigned)


def test_generate_and_verify_complete_main_validation_proof(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)

    proof_module.verify_proof(
        proof,
        repo_root=repo,
        expected_repository=REPOSITORY,
        expected_ref=REF,
    )

    assert proof["schema"] == proof_module.SCHEMA
    assert set(proof["workflows"]) == {"CI", "CodeQL", "Security"}  # type: ignore[arg-type]
    assert set(proof["critical_inputs"]) == set(proof_module.CRITICAL_INPUTS)  # type: ignore[arg-type]
    assert "tests/acceptance/v1_1_requirements.yml" in proof["critical_inputs"]  # type: ignore[operator]
    assert "tests/acceptance/v1_1_requirements.yml" in proof["fixture_hashes"]  # type: ignore[operator]


def test_generation_rejects_failed_required_job(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    evidence = _api_evidence(repo)
    ci_jobs = evidence["CI"]["jobs"]["jobs"]  # type: ignore[index]
    ci_jobs[0]["conclusion"] = "failure"

    with pytest.raises(MainValidationProofError, match="did not succeed"):
        _proof(repo, evidence)


def test_generation_rejects_missing_or_duplicate_jobs(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    missing = _api_evidence(repo)
    jobs_response = missing["CodeQL"]["jobs"]  # type: ignore[index]
    jobs_response["jobs"].pop()  # type: ignore[union-attr]
    jobs_response["total_count"] -= 1  # type: ignore[operator]
    with pytest.raises(MainValidationProofError, match="job set is invalid"):
        _proof(repo, missing)

    duplicate = _api_evidence(repo)
    jobs_response = duplicate["CI"]["jobs"]  # type: ignore[index]
    jobs_response["jobs"].append(deepcopy(jobs_response["jobs"][0]))  # type: ignore[index,union-attr]
    jobs_response["total_count"] += 1  # type: ignore[operator]
    with pytest.raises(MainValidationProofError, match="duplicate job"):
        _proof(repo, duplicate)


def test_generation_rejects_unknown_or_skipped_required_job(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    unknown = _api_evidence(repo)
    jobs_response = unknown["CI"]["jobs"]  # type: ignore[index]
    jobs = jobs_response["jobs"]  # type: ignore[index]
    run_id = unknown["CI"]["run"]["id"]  # type: ignore[index]
    commit_sha = _git(repo, "rev-parse", "HEAD")
    jobs.append(  # type: ignore[union-attr]
        _job(
            name="Unreviewed extra validation",
            run_id=run_id,
            commit_sha=commit_sha,
            job_id=999999,
        )
    )
    jobs_response["total_count"] += 1  # type: ignore[operator]
    with pytest.raises(MainValidationProofError, match="job set is invalid"):
        _proof(repo, unknown)

    skipped = _api_evidence(repo)
    required = skipped["CI"]["jobs"]["jobs"][0]  # type: ignore[index]
    required["conclusion"] = "skipped"
    with pytest.raises(MainValidationProofError, match="did not succeed"):
        _proof(repo, skipped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "workflow_dispatch"),
        ("head_branch", "release"),
        ("head_sha", "1" * 40),
        ("conclusion", "failure"),
    ],
)
def test_generation_rejects_wrong_run_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    repo = _repository(tmp_path)
    evidence = _api_evidence(repo)
    evidence["CI"]["run"][field] = value  # type: ignore[index]

    with pytest.raises(MainValidationProofError, match="does not match"):
        _proof(repo, evidence)


def test_generation_rejects_partial_api_job_page(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    evidence = _api_evidence(repo)
    evidence["Security"]["jobs"]["total_count"] = 999  # type: ignore[index]

    with pytest.raises(MainValidationProofError, match="incomplete"):
        _proof(repo, evidence)


def test_verification_rejects_tampering_before_local_checks(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)
    proof["ref"] = "refs/heads/release"

    with pytest.raises(MainValidationProofError, match="repository or ref"):
        proof_module.verify_proof(
            proof,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )

    proof = _proof(repo)
    proof["generated_at"] = "2026-07-10T05:00:00Z"
    with pytest.raises(MainValidationProofError, match="digest"):
        proof_module.verify_proof(
            proof,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_verification_rejects_resigned_incomplete_workflow_proof(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)
    workflows = proof["workflows"]
    assert isinstance(workflows, dict)
    ci = workflows["CI"]
    assert isinstance(ci, dict)
    jobs = ci["required_jobs"]
    assert isinstance(jobs, list)
    jobs.pop()
    _resign(proof)

    with pytest.raises(MainValidationProofError, match="required jobs"):
        proof_module.verify_proof(
            proof,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_verification_rejects_changed_critical_input(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)
    (repo / "uv.lock").write_text("changed after proof\n", encoding="utf-8")

    with pytest.raises(MainValidationProofError, match="critical inputs"):
        proof_module.verify_proof(
            proof,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_offline_cli_generates_and_verifies_proof(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    api_data = tmp_path / "api-data.json"
    output = tmp_path / "proof.json"
    evidence = _api_evidence(repo)
    api_data.write_text(json.dumps(evidence), encoding="utf-8")
    evidence_arguments: list[str] = []
    for artifact_name, manifest in _validation_evidence(repo, evidence).items():
        path = tmp_path / f"{artifact_name}.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence_arguments.extend(("--evidence", f"{artifact_name}={path}"))

    assert (
        proof_module.main(
            [
                "generate",
                "--repo-root",
                str(repo),
                "--repository",
                REPOSITORY,
                "--api-data",
                str(api_data),
                "--output",
                str(output),
                *evidence_arguments,
            ]
        )
        == 0
    )
    assert (
        proof_module.main(
            [
                "verify",
                "--repo-root",
                str(repo),
                "--repository",
                REPOSITORY,
                "--proof",
                str(output),
            ]
        )
        == 0
    )


def test_cli_fails_closed_for_incomplete_run_selection(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    with pytest.raises(MainValidationProofError, match="all three"):
        proof_module._parse_runs(["CI=1", "CodeQL=2"])

    assert not (repo / "proof.json").exists()


def test_generation_rejects_manifest_from_another_job_or_tree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    api_evidence = _api_evidence(repo)
    manifests = _validation_evidence(repo, api_evidence)
    manifest = manifests["python-evidence-unit"]
    assert isinstance(manifest, dict)
    manifest["source_tree"] = "f" * 40

    with pytest.raises(MainValidationProofError, match="another source revision"):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


def test_generation_requires_publishable_windows_candidate_installer(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    api_evidence = _api_evidence(repo)
    manifests = _validation_evidence(repo, api_evidence)
    candidate = manifests["windows-desktop-alpha-candidate-manifest"]
    assert isinstance(candidate, dict)
    payloads = candidate["payloads"]
    assert isinstance(payloads, list) and isinstance(payloads[0], dict)
    payloads[0]["kind"] = "provenance"
    candidate.pop("tauri")
    candidate["manifest_sha256"] = manifest_digest(candidate)

    with pytest.raises(MainValidationProofError, match="Tauri unsigned installer"):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


def test_generation_rejects_manifest_from_another_job(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    api_evidence = _api_evidence(repo)
    manifests = _validation_evidence(repo, api_evidence)
    manifest = manifests["python-evidence-unit"]
    assert isinstance(manifest, dict)
    producer = manifest["producer"]
    assert isinstance(producer, dict)
    producer["job_id"] = "999999"
    with pytest.raises(MainValidationProofError, match="GitHub job identity"):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


def test_verification_rejects_resigned_artifact_substitution(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)
    evidence = proof["validation_evidence"]
    assert isinstance(evidence, dict)
    manifest = evidence["web-build-manifest"]
    assert isinstance(manifest, dict)
    payloads = manifest["payloads"]
    assert isinstance(payloads, list)
    payloads[0]["sha256"] = "9" * 64
    _resign(proof)

    with pytest.raises(MainValidationProofError, match="manifest is invalid"):
        proof_module.verify_proof(
            proof,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )


def test_artifact_consumption_rejects_payload_substitution(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    proof = _proof(repo)
    evidence = proof["validation_evidence"]
    assert isinstance(evidence, dict)
    roots: dict[str, Path] = {}
    attestations: dict[str, object] = {}
    for artifact_name, manifest_value in evidence.items():
        assert isinstance(manifest_value, dict)
        root = tmp_path / "artifacts" / artifact_name
        root.mkdir(parents=True)
        payloads = manifest_value["payloads"]
        assert isinstance(payloads, list)
        for payload in payloads:
            path = root / payload["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        roots[artifact_name] = root
        attestations[artifact_name] = create_attestation_binding(manifest_value)

    proof_module.verify_proved_artifacts(
        proof,
        artifact_roots=roots,
        artifact_attestations=attestations,
    )
    substituted = roots["web-build-manifest"] / "web-build-manifest.json"
    substituted.write_bytes(b"y")

    with pytest.raises(MainValidationProofError, match="payload SHA-256 mismatch"):
        proof_module.verify_proved_artifacts(
            proof,
            artifact_roots=roots,
            artifact_attestations=attestations,
        )


def test_post_gh_verify_binding_is_bound_to_exact_file_and_job(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    proof = _proof(repo)
    proof_bytes = (json.dumps(proof, sort_keys=True) + "\n").encode()
    workflows = proof["workflows"]
    assert isinstance(workflows, dict)
    ci = workflows["CI"]
    assert isinstance(ci, dict)
    generation_job = ci["generation_job"]
    assert isinstance(generation_job, dict)
    binding: dict[str, object] = {
        "schema": proof_module.POST_GH_VERIFY_BINDING_SCHEMA,
        "repository": REPOSITORY,
        "commit_sha": proof["commit_sha"],
        "tree_sha": proof["tree_sha"],
        "proof_file_sha256": proof_module.hashlib.sha256(proof_bytes).hexdigest(),
        "attestation_id": "attestation-123",
        "verified_at": TIMESTAMP,
        "verification_gate": "gh-attestation-verify",
        "producer": {
            "workflow": "CI",
            "run_id": ci["run_id"],
            "run_attempt": ci["run_attempt"],
            "job_id": str(generation_job["id"]),
            "job_name": "Publish immutable main validation proof",
        },
    }
    proof_module.verify_post_gh_attestation_binding(
        proof,
        proof_bytes=proof_bytes,
        binding_value=binding,
        expected_repository=REPOSITORY,
    )

    binding["proof_file_sha256"] = "0" * 64
    with pytest.raises(MainValidationProofError, match="subject digest"):
        proof_module.verify_post_gh_attestation_binding(
            proof,
            proof_bytes=proof_bytes,
            binding_value=binding,
            expected_repository=REPOSITORY,
        )


def test_legacy_schema_requires_explicit_rollback_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    legacy_api = _api_evidence(repo, proof_module.LEGACY_WORKFLOW_POLICIES)
    monkeypatch.setattr(
        proof_module, "WORKFLOW_POLICIES", proof_module.LEGACY_WORKFLOW_POLICIES
    )
    monkeypatch.setattr(proof_module, "EVIDENCE_POLICIES", {})
    legacy = proof_module.generate_proof(
        repo_root=repo,
        repository=REPOSITORY,
        ref=REF,
        api_evidence=legacy_api,
        validation_evidence={},
    )
    legacy["schema"] = proof_module.LEGACY_SCHEMA
    del legacy["validation_evidence"]
    del legacy["fixture_hashes"]
    legacy["critical_inputs"] = {
        path: legacy["critical_inputs"][path]
        for path in proof_module.LEGACY_CRITICAL_INPUTS
    }
    _resign(legacy)

    with pytest.raises(MainValidationProofError, match="explicit rollback mode"):
        proof_module.verify_proof(
            legacy,
            repo_root=repo,
            expected_repository=REPOSITORY,
            expected_ref=REF,
        )
    proof_module.verify_proof(
        legacy,
        repo_root=repo,
        expected_repository=REPOSITORY,
        expected_ref=REF,
        allow_legacy_v1=True,
    )
