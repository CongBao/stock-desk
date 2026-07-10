from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Final, TypeGuard
from urllib.parse import urlencode, urlsplit


if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts.source_fingerprint import compute_source_fingerprint


SCHEMA: Final = "stock-desk-main-validation-proof-v1"
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
REF_PATTERN: Final = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")

CRITICAL_INPUTS: Final = (
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
    ".dockerignore",
    "Dockerfile",
    "Makefile",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "scripts/build_installer.py",
    "scripts/main_validation_proof.py",
    "scripts/source_fingerprint.py",
    "scripts/verify_release.py",
    "uv.lock",
)


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    path: str
    required_jobs: frozenset[str]
    allowed_skipped_jobs: frozenset[str] = frozenset()
    generation_job: str | None = None


WORKFLOW_POLICIES: Final = {
    "CI": WorkflowPolicy(
        path=".github/workflows/ci.yml",
        required_jobs=frozenset(
            {
                "Select required test scope",
                "Windows runtime ACL execution",
                "Public tree and repository health",
                "Locked production dependency audit",
                "Python quality, tests, and package",
                "Web quality, tests, and build",
                "Chromium E2E and Ubuntu x64 4-core/16GB target evidence",
                "Clean Compose build and smoke test",
            }
        ),
        generation_job="Publish immutable main validation proof",
    ),
    "CodeQL": WorkflowPolicy(
        path=".github/workflows/codeql.yml",
        required_jobs=frozenset(
            {
                "Analyze python",
                "Analyze javascript-typescript",
            }
        ),
    ),
    "Security": WorkflowPolicy(
        path=".github/workflows/security.yml",
        required_jobs=frozenset(
            {
                "Audit locked production dependencies and application boundaries",
                "Generate image SBOM, report CVEs, and reject fixable findings",
            }
        ),
        allowed_skipped_jobs=frozenset({"Review dependency changes"}),
    ),
}


class MainValidationProofError(RuntimeError):
    """Raised when validation evidence is incomplete, ambiguous, or mismatched."""


class GitHubApiClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        timeout_seconds: int = 30,
    ) -> None:
        parsed = urlsplit(api_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MainValidationProofError(
                "GitHub API URL must be a plain HTTPS origin"
            )
        self._hostname = parsed.hostname
        self._port = parsed.port
        self._prefix = parsed.path.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def get_object(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise MainValidationProofError("GitHub API path is invalid")
        target = f"{self._prefix}{path}"
        if query:
            target = f"{target}?{urlencode(query)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "stock-desk-main-validation-proof/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        connection = http.client.HTTPSConnection(
            self._hostname,
            port=self._port,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except OSError as error:
            raise MainValidationProofError("GitHub API request failed") from error
        finally:
            connection.close()
        if response.status != 200:
            raise MainValidationProofError(
                f"GitHub API returned HTTP {response.status} for {path}"
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MainValidationProofError(
                "GitHub API returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise MainValidationProofError("GitHub API response must be a JSON object")
        return value

    def workflow_evidence(
        self,
        *,
        repository: str,
        run_id: int,
    ) -> dict[str, Any]:
        repository_path = "/".join(repository.split("/"))
        run_path = f"/repos/{repository_path}/actions/runs/{run_id}"
        run = self.get_object(run_path)
        jobs: list[Any] = []
        expected_total: int | None = None
        page = 1
        while True:
            page_payload = self.get_object(
                f"{run_path}/jobs",
                query={"filter": "latest", "per_page": "100", "page": str(page)},
            )
            page_jobs = page_payload.get("jobs")
            total_count = page_payload.get("total_count")
            if not isinstance(page_jobs, list) or not _is_int(total_count):
                raise MainValidationProofError("GitHub jobs response is incomplete")
            if expected_total is None:
                expected_total = total_count
            elif total_count != expected_total:
                raise MainValidationProofError(
                    "GitHub jobs total changed during pagination"
                )
            jobs.extend(page_jobs)
            if len(jobs) >= expected_total:
                break
            if not page_jobs:
                raise MainValidationProofError("GitHub jobs pagination ended early")
            page += 1
        if len(jobs) != expected_total:
            raise MainValidationProofError("GitHub jobs response count is inconsistent")
        return {"run": run, "jobs": {"total_count": expected_total, "jobs": jobs}}


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise MainValidationProofError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise MainValidationProofError(
            f"{label} has invalid fields; missing={missing}, extra={extra}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MainValidationProofError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not _is_int(value) or value <= 0:
        raise MainValidationProofError(f"{label} must be a positive integer")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise MainValidationProofError(
            f"{label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise MainValidationProofError(f"{label} must include a timezone")
    return text


def _sha(value: object, label: str, *, git: bool = False) -> str:
    text = _string(value, label)
    pattern = GIT_SHA_PATTERN if git else SHA256_PATTERN
    if pattern.fullmatch(text) is None:
        raise MainValidationProofError(f"{label} has an invalid digest")
    return text


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _proof_digest(proof_without_digest: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(proof_without_digest)).hexdigest()


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise MainValidationProofError(f"critical input is missing or unsafe: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise MainValidationProofError(
            f"unable to hash critical input: {path}"
        ) from error
    return digest.hexdigest()


def critical_input_hashes(repo_root: Path) -> dict[str, str]:
    root = repo_root.resolve(strict=True)
    return {relative: _file_sha256(root / relative) for relative in CRITICAL_INPUTS}


def _git(repo_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "-C", os.fspath(repo_root), *arguments),  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise MainValidationProofError(
            "unable to inspect the local Git checkout"
        ) from error
    return result.stdout.strip()


def local_git_state(repo_root: Path) -> tuple[str, str]:
    commit_sha = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    tree_sha = _git(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    return _sha(commit_sha, "local commit", git=True), _sha(
        tree_sha, "local tree", git=True
    )


def _validate_repository_and_ref(repository: str, ref: str) -> tuple[str, str]:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise MainValidationProofError("repository must use owner/name form")
    if REF_PATTERN.fullmatch(ref) is None or ".." in ref or "//" in ref:
        raise MainValidationProofError("ref must be a canonical branch ref")
    return repository, ref.removeprefix("refs/heads/")


def _validate_job(
    job_value: object,
    *,
    workflow: str,
    run_id: int,
    commit_sha: str,
) -> tuple[str, dict[str, object]]:
    job = _object(job_value, f"{workflow} job")
    name = _string(job.get("name"), f"{workflow} job name")
    if _integer(job.get("run_id"), f"{workflow}/{name} run_id") != run_id:
        raise MainValidationProofError(f"{workflow}/{name} belongs to another run")
    if _sha(job.get("head_sha"), f"{workflow}/{name} head_sha", git=True) != commit_sha:
        raise MainValidationProofError(f"{workflow}/{name} belongs to another commit")
    status = _string(job.get("status"), f"{workflow}/{name} status")
    raw_conclusion = job.get("conclusion")
    conclusion = (
        None
        if raw_conclusion is None
        else _string(raw_conclusion, f"{workflow}/{name} conclusion")
    )
    started_at = _timestamp(job.get("started_at"), f"{workflow}/{name} started_at")
    raw_completed_at = job.get("completed_at")
    completed_at = (
        None
        if raw_completed_at is None
        else _timestamp(raw_completed_at, f"{workflow}/{name} completed_at")
    )
    proof_job: dict[str, object] = {
        "id": _integer(job.get("id"), f"{workflow}/{name} id"),
        "name": name,
        "head_sha": commit_sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at,
        "html_url": _string(job.get("html_url"), f"{workflow}/{name} html_url"),
    }
    return name, proof_job


def _workflow_proof(
    *,
    workflow: str,
    evidence_value: object,
    repository: str,
    branch: str,
    commit_sha: str,
    tree_sha: str,
) -> dict[str, object]:
    policy = WORKFLOW_POLICIES[workflow]
    evidence = _object(evidence_value, f"{workflow} evidence")
    _exact_keys(evidence, {"run", "jobs"}, f"{workflow} evidence")
    run = _object(evidence["run"], f"{workflow} run")
    jobs_response = _object(evidence["jobs"], f"{workflow} jobs response")
    _exact_keys(jobs_response, {"total_count", "jobs"}, f"{workflow} jobs response")
    jobs_value = jobs_response["jobs"]
    if not isinstance(jobs_value, list):
        raise MainValidationProofError(f"{workflow} jobs must be a list")
    total_count = jobs_response["total_count"]
    if not _is_int(total_count) or total_count != len(jobs_value):
        raise MainValidationProofError(f"{workflow} jobs response is incomplete")

    run_repository = _object(run.get("repository"), f"{workflow} run repository")
    head_commit = _object(run.get("head_commit"), f"{workflow} head_commit")
    run_id = _integer(run.get("id"), f"{workflow} run id")
    raw_run_conclusion = run.get("conclusion")
    run_conclusion = (
        None
        if raw_run_conclusion is None
        else _string(raw_run_conclusion, f"{workflow} run conclusion")
    )
    checks: dict[str, object] = {
        "name": _string(run.get("name"), f"{workflow} run name"),
        "path": _string(run.get("path"), f"{workflow} run path"),
        "event": _string(run.get("event"), f"{workflow} run event"),
        "status": _string(run.get("status"), f"{workflow} run status"),
        "conclusion": run_conclusion,
        "head_branch": _string(run.get("head_branch"), f"{workflow} head_branch"),
        "head_sha": _sha(run.get("head_sha"), f"{workflow} head_sha", git=True),
        "tree_sha": _sha(head_commit.get("tree_id"), f"{workflow} tree_sha", git=True),
        "repository": _string(
            run_repository.get("full_name"), f"{workflow} repository"
        ),
    }
    expected = {
        "name": workflow,
        "path": policy.path,
        "event": "push",
        "status": "in_progress" if policy.generation_job is not None else "completed",
        "conclusion": None if policy.generation_job is not None else "success",
        "head_branch": branch,
        "head_sha": commit_sha,
        "tree_sha": tree_sha,
        "repository": repository,
    }
    for key, expected_value in expected.items():
        if checks[key] != expected_value:
            raise MainValidationProofError(
                f"{workflow} run {key} does not match the required main validation"
            )

    jobs_by_name: dict[str, dict[str, object]] = {}
    for job_value in jobs_value:
        name, proof_job = _validate_job(
            job_value,
            workflow=workflow,
            run_id=run_id,
            commit_sha=commit_sha,
        )
        if name in jobs_by_name:
            raise MainValidationProofError(f"{workflow} contains duplicate job {name}")
        jobs_by_name[name] = proof_job

    required_names = set(policy.required_jobs)
    if policy.generation_job is not None:
        required_names.add(policy.generation_job)
    missing = required_names - jobs_by_name.keys()
    unknown = jobs_by_name.keys() - required_names - policy.allowed_skipped_jobs
    if missing or unknown:
        raise MainValidationProofError(
            f"{workflow} job set is invalid; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    for name, job in jobs_by_name.items():
        conclusion = job["conclusion"]
        status = job["status"]
        completed_at = job["completed_at"]
        if name in policy.required_jobs and (
            status != "completed" or conclusion != "success" or completed_at is None
        ):
            raise MainValidationProofError(f"{workflow}/{name} did not succeed")
        if name in policy.allowed_skipped_jobs and (
            status != "completed" or conclusion != "skipped" or completed_at is None
        ):
            raise MainValidationProofError(
                f"{workflow}/{name} must be skipped for a main push"
            )
        if name == policy.generation_job and (
            status != "in_progress"
            or conclusion is not None
            or completed_at is not None
        ):
            raise MainValidationProofError(
                f"{workflow}/{name} must be the currently running proof job"
            )

    return {
        "workflow_id": _integer(run.get("workflow_id"), f"{workflow} workflow_id"),
        "run_id": run_id,
        "run_attempt": _integer(run.get("run_attempt"), f"{workflow} run_attempt"),
        "name": workflow,
        "path": policy.path,
        "event": "push",
        "status": checks["status"],
        "conclusion": checks["conclusion"],
        "created_at": _timestamp(run.get("created_at"), f"{workflow} created_at"),
        "updated_at": _timestamp(run.get("updated_at"), f"{workflow} updated_at"),
        "html_url": _string(run.get("html_url"), f"{workflow} html_url"),
        "required_jobs": [jobs_by_name[name] for name in sorted(policy.required_jobs)],
        "allowed_skipped_jobs": [
            jobs_by_name[name]
            for name in sorted(policy.allowed_skipped_jobs)
            if name in jobs_by_name
        ],
        "generation_job": (
            jobs_by_name[policy.generation_job]
            if policy.generation_job is not None
            else None
        ),
    }


def generate_proof(
    *,
    repo_root: Path,
    repository: str,
    ref: str,
    api_evidence: Mapping[str, object],
) -> dict[str, object]:
    repository, branch = _validate_repository_and_ref(repository, ref)
    root = repo_root.resolve(strict=True)
    commit_sha, tree_sha = local_git_state(root)
    if set(api_evidence) != set(WORKFLOW_POLICIES):
        raise MainValidationProofError(
            "API evidence must contain exactly CI, CodeQL, and Security"
        )
    try:
        source_fingerprint = compute_source_fingerprint(root)
    except (OSError, RuntimeError) as error:
        raise MainValidationProofError(
            "unable to compute source fingerprint"
        ) from error
    proof: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": repository,
        "ref": ref,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "source_fingerprint": _sha(source_fingerprint, "source_fingerprint"),
        "critical_inputs": critical_input_hashes(root),
        "workflows": {
            workflow: _workflow_proof(
                workflow=workflow,
                evidence_value=api_evidence[workflow],
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
            )
            for workflow in sorted(WORKFLOW_POLICIES)
        },
    }
    proof["proof_sha256"] = _proof_digest(proof)
    return proof


def _validate_stored_job(
    value: object,
    *,
    workflow: str,
    expected_name: str,
    commit_sha: str,
    conclusion: str | None,
    status: str = "completed",
) -> None:
    job = _object(value, f"stored {workflow}/{expected_name}")
    _exact_keys(
        job,
        {
            "id",
            "name",
            "head_sha",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
            "html_url",
        },
        f"stored {workflow}/{expected_name}",
    )
    if _integer(job["id"], "stored job id") <= 0:
        raise MainValidationProofError("stored job id is invalid")
    if job["name"] != expected_name or job["head_sha"] != commit_sha:
        raise MainValidationProofError("stored job identity does not match proof")
    if job["status"] != status or job["conclusion"] != conclusion:
        raise MainValidationProofError("stored job result is not acceptable")
    _timestamp(job["started_at"], "stored job started_at")
    if status == "completed":
        _timestamp(job["completed_at"], "stored job completed_at")
    elif job["completed_at"] is not None:
        raise MainValidationProofError("running proof job has a completion timestamp")
    _string(job["html_url"], "stored job html_url")


def _validate_stored_workflow(
    value: object,
    *,
    workflow: str,
    commit_sha: str,
) -> None:
    stored = _object(value, f"stored {workflow} workflow")
    _exact_keys(
        stored,
        {
            "workflow_id",
            "run_id",
            "run_attempt",
            "name",
            "path",
            "event",
            "status",
            "conclusion",
            "created_at",
            "updated_at",
            "html_url",
            "required_jobs",
            "allowed_skipped_jobs",
            "generation_job",
        },
        f"stored {workflow} workflow",
    )
    policy = WORKFLOW_POLICIES[workflow]
    expected_scalars: dict[str, object] = {
        "name": workflow,
        "path": policy.path,
        "event": "push",
        "status": "in_progress" if policy.generation_job is not None else "completed",
        "conclusion": None if policy.generation_job is not None else "success",
    }
    if any(stored[key] != value for key, value in expected_scalars.items()):
        raise MainValidationProofError(f"stored {workflow} result is invalid")
    for key in ("workflow_id", "run_id", "run_attempt"):
        _integer(stored[key], f"stored {workflow} {key}")
    _timestamp(stored["created_at"], f"stored {workflow} created_at")
    _timestamp(stored["updated_at"], f"stored {workflow} updated_at")
    _string(stored["html_url"], f"stored {workflow} html_url")
    required = stored["required_jobs"]
    skipped = stored["allowed_skipped_jobs"]
    if not isinstance(required, list) or not isinstance(skipped, list):
        raise MainValidationProofError(f"stored {workflow} jobs must be lists")
    required_names = []
    for job in required:
        job_object = _object(job, f"stored {workflow} required job")
        name = _string(job_object.get("name"), "stored required job name")
        required_names.append(name)
        _validate_stored_job(
            job,
            workflow=workflow,
            expected_name=name,
            commit_sha=commit_sha,
            conclusion="success",
        )
    if required_names != sorted(policy.required_jobs):
        raise MainValidationProofError(f"stored {workflow} required jobs are invalid")
    skipped_names = []
    for job in skipped:
        job_object = _object(job, f"stored {workflow} skipped job")
        name = _string(job_object.get("name"), "stored skipped job name")
        skipped_names.append(name)
        _validate_stored_job(
            job,
            workflow=workflow,
            expected_name=name,
            commit_sha=commit_sha,
            conclusion="skipped",
        )
    if skipped_names != sorted(set(skipped_names)) or not set(skipped_names) <= set(
        policy.allowed_skipped_jobs
    ):
        raise MainValidationProofError(f"stored {workflow} skipped jobs are invalid")
    generation_job = stored["generation_job"]
    if policy.generation_job is None:
        if generation_job is not None:
            raise MainValidationProofError(
                f"stored {workflow} must not contain a generation job"
            )
    else:
        _validate_stored_job(
            generation_job,
            workflow=workflow,
            expected_name=policy.generation_job,
            commit_sha=commit_sha,
            conclusion=None,
            status="in_progress",
        )


def verify_proof(
    proof_value: object,
    *,
    repo_root: Path,
    expected_repository: str,
    expected_ref: str,
) -> None:
    proof = _object(proof_value, "proof")
    _exact_keys(
        proof,
        {
            "schema",
            "generated_at",
            "repository",
            "ref",
            "commit_sha",
            "tree_sha",
            "source_fingerprint",
            "critical_inputs",
            "workflows",
            "proof_sha256",
        },
        "proof",
    )
    if proof["schema"] != SCHEMA:
        raise MainValidationProofError("proof schema is unsupported")
    _timestamp(proof["generated_at"], "generated_at")
    repository, _ = _validate_repository_and_ref(expected_repository, expected_ref)
    if proof["repository"] != repository or proof["ref"] != expected_ref:
        raise MainValidationProofError("proof repository or ref does not match")
    commit_sha = _sha(proof["commit_sha"], "commit_sha", git=True)
    tree_sha = _sha(proof["tree_sha"], "tree_sha", git=True)
    _sha(proof["source_fingerprint"], "source_fingerprint")
    proof_sha256 = _sha(proof["proof_sha256"], "proof_sha256")
    unsigned = dict(proof)
    del unsigned["proof_sha256"]
    if _proof_digest(unsigned) != proof_sha256:
        raise MainValidationProofError("proof digest does not match its contents")

    critical_inputs = _object(proof["critical_inputs"], "critical_inputs")
    if set(critical_inputs) != set(CRITICAL_INPUTS):
        raise MainValidationProofError("proof critical input set is incomplete")
    for relative, digest in critical_inputs.items():
        _sha(digest, f"critical input {relative}")
    workflows = _object(proof["workflows"], "workflows")
    if set(workflows) != set(WORKFLOW_POLICIES):
        raise MainValidationProofError("proof workflow set is incomplete")
    for workflow in sorted(WORKFLOW_POLICIES):
        _validate_stored_workflow(
            workflows[workflow],
            workflow=workflow,
            commit_sha=commit_sha,
        )

    root = repo_root.resolve(strict=True)
    local_commit, local_tree = local_git_state(root)
    if local_commit != commit_sha or local_tree != tree_sha:
        raise MainValidationProofError("local commit or tree does not match proof")
    if critical_input_hashes(root) != critical_inputs:
        raise MainValidationProofError("local critical inputs do not match proof")
    try:
        local_fingerprint = compute_source_fingerprint(root)
    except (OSError, RuntimeError) as error:
        raise MainValidationProofError(
            "unable to compute source fingerprint"
        ) from error
    if local_fingerprint != proof["source_fingerprint"]:
        raise MainValidationProofError("local source fingerprint does not match proof")


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MainValidationProofError(f"unable to load {label}") from error


def _parse_runs(values: Sequence[str]) -> dict[str, int]:
    runs: dict[str, int] = {}
    for value in values:
        workflow, separator, raw_run_id = value.partition("=")
        if not separator or workflow not in WORKFLOW_POLICIES or workflow in runs:
            raise MainValidationProofError(
                "--run must be unique and use CI=<id>, CodeQL=<id>, or Security=<id>"
            )
        try:
            run_id = int(raw_run_id)
        except ValueError as error:
            raise MainValidationProofError(
                "workflow run ID must be an integer"
            ) from error
        if run_id <= 0:
            raise MainValidationProofError("workflow run ID must be positive")
        runs[workflow] = run_id
    if set(runs) != set(WORKFLOW_POLICIES):
        raise MainValidationProofError("all three main workflow run IDs are required")
    return runs


def _write_proof(path: Path, proof: Mapping[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        raise MainValidationProofError("unable to write proof") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify immutable main-branch validation evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--repo-root", type=Path, default=Path.cwd())
    generate.add_argument("--repository", required=True)
    generate.add_argument("--ref", default="refs/heads/main")
    generate.add_argument("--output", type=Path, required=True)
    source = generate.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--api-data",
        type=Path,
        help="JSON object containing raw run and jobs API responses for each workflow.",
    )
    source.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="WORKFLOW=ID",
        help="Fetch a run and all jobs from GitHub; repeat for CI, CodeQL, Security.",
    )
    generate.add_argument("--api-url", default="https://api.github.com")
    generate.add_argument("--token-env", default="GITHUB_TOKEN")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, default=Path.cwd())
    verify.add_argument("--repository", required=True)
    verify.add_argument("--ref", default="refs/heads/main")
    verify.add_argument("--proof", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        if options.command == "generate":
            if options.api_data is not None:
                api_evidence = _object(
                    _load_json(options.api_data, "API data"), "API data"
                )
            else:
                runs = _parse_runs(options.run)
                client = GitHubApiClient(
                    token=os.environ.get(options.token_env),
                    api_url=options.api_url,
                )
                api_evidence = {
                    workflow: client.workflow_evidence(
                        repository=options.repository,
                        run_id=run_id,
                    )
                    for workflow, run_id in runs.items()
                }
            proof = generate_proof(
                repo_root=options.repo_root,
                repository=options.repository,
                ref=options.ref,
                api_evidence=api_evidence,
            )
            _write_proof(options.output, proof)
        else:
            loaded_proof = _load_json(options.proof, "proof")
            verify_proof(
                loaded_proof,
                repo_root=options.repo_root,
                expected_repository=options.repository,
                expected_ref=options.ref,
            )
    except MainValidationProofError as error:
        print(f"main validation proof rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
