from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import scripts.main_validation_proof as proof_module
from scripts.artifact_manifest import (
    create_attestation_binding,
    manifest_digest,
    write_manifest,
)
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
        "run_attempt": 1,
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
            "stock-desk-1.1.0-beta.2-unsigned-x64-setup.exe"
            if payload_kind == "tauri-unsigned"
            else f"payload-{policy.artifact_name}.json"
        )
        payloads = [
            {
                "path": payload_name,
                "kind": payload_kind,
                "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        ]
        if policy.artifact_name == "windows-desktop-alpha-candidate-manifest":
            payloads.extend(
                {
                    "path": f"packaged-backtest/{name}",
                    "kind": "provenance",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
                for name in (
                    "windows-desktop-evidence.json",
                    "tauri-webview-evidence.json",
                    "packaged-backtest-evidence.json",
                    "packaged-backtest-seed.json",
                    "packaged-backtest-host-observation.json",
                    "windows-packaged-backtest-promotion.json",
                )
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
            "payloads": payloads,
            **({"image_digest": "sha256:" + "4" * 64} if payload_kind == "oci" else {}),
            **(
                {"tauri": {"cargo_lock_sha256": "5" * 64}}
                if payload_kind == "tauri-unsigned"
                else {}
            ),
        }
    return manifests


def _mixed_attempt_ci_evidence(
    repo: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Model a failed-jobs rerun whose artifacts span two workflow attempts."""

    api_evidence = _api_evidence(repo)
    ci = api_evidence["CI"]
    assert isinstance(ci, dict)
    run = ci["run"]
    jobs_response = ci["jobs"]
    assert isinstance(run, dict)
    assert isinstance(jobs_response, dict)
    jobs = jobs_response["jobs"]
    assert isinstance(jobs, list)

    run["run_attempt"] = 2
    rerun_result_names = {
        "Python acceptance and performance shard",
        "Aggregate Python evidence and coverage",
        "Publish immutable main validation proof",
    }
    attempt_two_jobs: list[dict[str, object]] = []
    for job_value in jobs:
        assert isinstance(job_value, dict)
        job_value["run_attempt"] = 1
        rerun_job = deepcopy(job_value)
        rerun_job["id"] = int(job_value["id"]) + 1_000_000
        rerun_job["run_attempt"] = 2
        if job_value["name"] == "Publish immutable main validation proof":
            job_value.update(
                status="completed",
                conclusion="skipped",
                completed_at="2026-07-10T04:01:00Z",
            )
            rerun_job.update(status="in_progress", conclusion=None, completed_at=None)
        elif job_value["name"] in rerun_result_names:
            job_value.update(
                status="completed",
                conclusion=(
                    "failure"
                    if job_value["name"] == "Python acceptance and performance shard"
                    else "skipped"
                ),
                completed_at="2026-07-10T04:01:00Z",
            )
            rerun_job.update(
                status="completed",
                conclusion="success",
                completed_at="2026-07-10T04:03:00Z",
            )
        attempt_two_jobs.append(rerun_job)
    jobs.extend(attempt_two_jobs)
    jobs_response["total_count"] = len(jobs)

    manifests = _validation_evidence(repo, api_evidence)
    attempt_two_artifacts = {
        "python-evidence-acceptance-performance",
        "python-evidence-aggregate",
    }
    for artifact_name, manifest_value in manifests.items():
        assert isinstance(manifest_value, dict)
        producer = manifest_value["producer"]
        assert isinstance(producer, dict)
        producer["run_attempt"] = 2 if artifact_name in attempt_two_artifacts else 1
    return api_evidence, manifests


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
    assert "scripts/verify_zero_telemetry.py" in proof["critical_inputs"]  # type: ignore[operator]
    assert "config/desktop-network-privacy.json" in proof["critical_inputs"]  # type: ignore[operator]
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


def test_generation_selects_each_artifacts_exact_job_from_mixed_rerun_attempts(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    api_evidence, manifests = _mixed_attempt_ci_evidence(repo)

    proof = proof_module.generate_proof(
        repo_root=repo,
        repository=REPOSITORY,
        ref=REF,
        api_evidence=api_evidence,
        validation_evidence=manifests,
    )

    workflows = proof["workflows"]
    assert isinstance(workflows, dict)
    ci = workflows["CI"]
    assert isinstance(ci, dict)
    required_jobs = ci["required_jobs"]
    assert isinstance(required_jobs, list)
    jobs_by_name = {job["name"]: job for job in required_jobs if isinstance(job, dict)}
    assert jobs_by_name["Chromium E2E immutable snapshot"]["run_attempt"] == 1
    assert jobs_by_name["Python acceptance and performance shard"]["run_attempt"] == 2
    assert jobs_by_name["Aggregate Python evidence and coverage"]["run_attempt"] == 2
    assert (
        jobs_by_name["Build and verify Windows desktop candidate A"]["run_attempt"] == 2
    )
    generation_job = ci["generation_job"]
    assert isinstance(generation_job, dict)
    assert generation_job["run_attempt"] == 2

    validation_evidence = proof["validation_evidence"]
    assert isinstance(validation_evidence, dict)
    assert (
        validation_evidence["e2e-evidence"]["producer"]["run_attempt"] == 1  # type: ignore[index]
    )
    assert (
        validation_evidence["python-evidence-acceptance-performance"]["producer"][
            "run_attempt"
        ]
        == 2  # type: ignore[index]
    )
    assert (
        validation_evidence["python-evidence-aggregate"]["producer"]["run_attempt"] == 2  # type: ignore[index]
    )


@pytest.mark.parametrize(
    "attack",
    [
        "nonexistent-attempt",
        "failed-producer-job",
        "duplicate-attempt-name",
        "missing-current-job",
    ],
)
def test_generation_rejects_ambiguous_or_invalid_mixed_rerun_producer(
    tmp_path: Path, attack: str
) -> None:
    repo = _repository(tmp_path)
    api_evidence, manifests = _mixed_attempt_ci_evidence(repo)

    # The unmodified mixed-attempt topology is valid. Keeping this assertion in
    # every attack case prevents an unrelated global duplicate-name rejection
    # from making these fail-closed tests pass for the wrong reason.
    proof_module.generate_proof(
        repo_root=repo,
        repository=REPOSITORY,
        ref=REF,
        api_evidence=api_evidence,
        validation_evidence=manifests,
    )

    ci = api_evidence["CI"]
    assert isinstance(ci, dict)
    jobs_response = ci["jobs"]
    assert isinstance(jobs_response, dict)
    jobs = jobs_response["jobs"]
    assert isinstance(jobs, list)
    e2e_job = next(
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "Chromium E2E immutable snapshot"
        and job.get("run_attempt") == 1
    )
    if attack == "nonexistent-attempt":
        e2e_manifest = manifests["e2e-evidence"]
        assert isinstance(e2e_manifest, dict)
        producer = e2e_manifest["producer"]
        assert isinstance(producer, dict)
        producer["run_attempt"] = 3
    elif attack == "failed-producer-job":
        e2e_job["conclusion"] = "failure"
    elif attack == "duplicate-attempt-name":
        duplicate = deepcopy(e2e_job)
        duplicate["id"] = int(e2e_job["id"]) + 2_000_000
        jobs.append(duplicate)
        jobs_response["total_count"] = len(jobs)
    else:
        jobs.remove(
            next(
                job
                for job in jobs
                if isinstance(job, dict)
                and job.get("name") == "Build and verify Windows desktop candidate A"
                and job.get("run_attempt") == 2
            )
        )
        jobs_response["total_count"] = len(jobs)

    with pytest.raises(MainValidationProofError):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


def test_github_client_fetches_every_run_attempt_and_marks_each_job() -> None:
    run_path = f"/repos/{REPOSITORY}/actions/runs/4242"

    class FakeGitHubClient(proof_module.GitHubApiClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, dict[str, str]]] = []

        def get_object(
            self, path: str, *, query: dict[str, str] | None = None
        ) -> dict[str, Any]:
            self.calls.append((path, dict(query or {})))
            if path == run_path:
                return {"id": 4242, "run_attempt": 2}
            if path == f"{run_path}/attempts/1/jobs":
                return {
                    "total_count": 1,
                    "jobs": [{"id": 101, "name": "attempt-one", "run_attempt": 1}],
                }
            if path == f"{run_path}/attempts/2/jobs":
                return {
                    "total_count": 1,
                    "jobs": [{"id": 201, "name": "attempt-two", "run_attempt": 2}],
                }
            raise AssertionError(f"unexpected GitHub API request: {path}")

    client = FakeGitHubClient()

    evidence = client.workflow_evidence(repository=REPOSITORY, run_id=4242)

    assert client.calls == [
        (run_path, {}),
        (f"{run_path}/attempts/1/jobs", {"per_page": "100", "page": "1"}),
        (f"{run_path}/attempts/2/jobs", {"per_page": "100", "page": "1"}),
    ]
    assert all("filter" not in query for _, query in client.calls)
    assert evidence["jobs"] == {
        "total_count": 2,
        "jobs": [
            {"id": 101, "name": "attempt-one", "run_attempt": 1},
            {"id": 201, "name": "attempt-two", "run_attempt": 2},
        ],
    }


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
    candidate["payloads"] = sorted(payloads, key=lambda payload: payload["path"])
    candidate["manifest_sha256"] = manifest_digest(candidate)

    with pytest.raises(MainValidationProofError, match="Tauri unsigned installer"):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


def test_generation_requires_packaged_backtest_promotion_payloads(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    api_evidence = _api_evidence(repo)
    manifests = _validation_evidence(repo, api_evidence)
    candidate = manifests["windows-desktop-alpha-candidate-manifest"]
    assert isinstance(candidate, dict)
    payloads = candidate["payloads"]
    assert isinstance(payloads, list)
    candidate["payloads"] = [
        payload
        for payload in payloads
        if payload["path"]
        != "packaged-backtest/windows-packaged-backtest-promotion.json"
    ]

    with pytest.raises(MainValidationProofError, match="backtest provenance"):
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
    with pytest.raises(
        MainValidationProofError, match="GitHub (job|workflow) identity"
    ):
        proof_module.generate_proof(
            repo_root=repo,
            repository=REPOSITORY,
            ref=REF,
            api_evidence=api_evidence,
            validation_evidence=manifests,
        )


@pytest.mark.parametrize("attack", ["missing-manifest", "failed-job", "wrong-producer"])
def test_generation_requires_exact_windows_observer_gate(
    tmp_path: Path, attack: str
) -> None:
    repo = _repository(tmp_path)
    api_evidence = _api_evidence(repo)
    manifests = _validation_evidence(repo, api_evidence)
    artifact = "windows-browser-observer-evidence"
    if attack == "missing-manifest":
        manifests.pop(artifact)
    elif attack == "failed-job":
        workflow = api_evidence["CI"]
        assert isinstance(workflow, dict)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        values = jobs["jobs"]
        assert isinstance(values, list)
        observer_job = next(
            job
            for job in values
            if isinstance(job, dict)
            and job.get("name") == "Verify Windows browser observer integration"
        )
        observer_job["conclusion"] = "failure"
    else:
        manifest = manifests[artifact]
        assert isinstance(manifest, dict)
        producer = manifest["producer"]
        assert isinstance(producer, dict)
        producer["job_id"] = "windows-browser-observer-forged"
        manifest["manifest_sha256"] = manifest_digest(manifest)
    with pytest.raises(MainValidationProofError):
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


def test_artifact_consumption_rejects_payload_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        write_manifest(root / f"{artifact_name}.json", manifest_value)
        (root / "manifest-binding.json").write_text(
            json.dumps(create_attestation_binding(manifest_value)), encoding="utf-8"
        )
        roots[artifact_name] = root
        attestations[artifact_name] = create_attestation_binding(manifest_value)

    monkeypatch.setattr(
        proof_module, "verify_packaged_backtest_promotion", lambda *args, **kwargs: None
    )
    proof_module.verify_proved_artifacts(
        proof,
        artifact_roots=roots,
        artifact_attestations=attestations,
    )
    monkeypatch.setattr(
        proof_module,
        "verify_packaged_backtest_promotion",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("tampered promotion")),
    )
    with pytest.raises(MainValidationProofError, match="packaged backtest"):
        proof_module.verify_proved_artifacts(
            proof,
            artifact_roots=roots,
            artifact_attestations=attestations,
        )
    monkeypatch.setattr(
        proof_module, "verify_packaged_backtest_promotion", lambda *args, **kwargs: None
    )
    web_manifest = evidence["web-build-manifest"]
    assert isinstance(web_manifest, dict)
    substituted = roots["web-build-manifest"] / web_manifest["payloads"][0]["path"]
    substituted.write_bytes(b"y")

    with pytest.raises(MainValidationProofError, match="payload SHA-256 mismatch"):
        proof_module.verify_proved_artifacts(
            proof,
            artifact_roots=roots,
            artifact_attestations=attestations,
        )


def test_artifact_consumption_rejects_unmanifested_extra_file(tmp_path: Path) -> None:
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
        for payload in manifest_value["payloads"]:
            path = root / payload["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        write_manifest(root / f"{artifact_name}.json", manifest_value)
        binding = create_attestation_binding(manifest_value)
        (root / "manifest-binding.json").write_text(
            json.dumps(binding), encoding="utf-8"
        )
        roots[artifact_name] = root
        attestations[artifact_name] = binding
    (roots["python-evidence-unit"] / "unlisted-debug.log").write_text(
        "must not ship", encoding="utf-8"
    )

    with pytest.raises(MainValidationProofError, match="not manifest-closed"):
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
