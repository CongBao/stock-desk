from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import scripts.main_validation_proof as proof_module
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


def _api_evidence(repo: Path) -> dict[str, object]:
    commit_sha = _git(repo, "rev-parse", "HEAD")
    tree_sha = _git(repo, "rev-parse", "HEAD^{tree}")
    evidence: dict[str, object] = {}
    for workflow_number, (workflow, policy) in enumerate(
        proof_module.WORKFLOW_POLICIES.items(), start=1
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


def _proof(repo: Path, evidence: dict[str, object] | None = None) -> dict[str, object]:
    return proof_module.generate_proof(
        repo_root=repo,
        repository=REPOSITORY,
        ref=REF,
        api_evidence=evidence if evidence is not None else _api_evidence(repo),
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
    api_data.write_text(json.dumps(_api_evidence(repo)), encoding="utf-8")

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
