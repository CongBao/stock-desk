from __future__ import annotations

from collections.abc import Mapping
import http.client
from pathlib import Path
import subprocess
from typing import Any

import pytest

import scripts.main_validation_proof as proof


class _Response:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _Connection:
    def __init__(
        self,
        response: _Response | None = None,
        *,
        request_error: OSError | None = None,
    ) -> None:
        self.response = response or _Response(200, b"{}")
        self.request_error = request_error
        self.request: tuple[str, str, Mapping[str, str]] | None = None
        self.closed = False

    def send(self, method: str, target: str, headers: Mapping[str, str]) -> None:
        self.request = (method, target, headers)
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def _install_connection(
    monkeypatch: pytest.MonkeyPatch, connection: _Connection
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def factory(hostname: str, *, port: int | None, timeout: int) -> _Connection:
        captured.update(hostname=hostname, port=port, timeout=timeout)
        return connection

    monkeypatch.setattr(http.client, "HTTPSConnection", factory)
    monkeypatch.setattr(connection, "request", connection.send)
    return captured


@pytest.mark.parametrize(
    "api_url",
    [
        "http://api.github.com",
        "https://user@api.github.com",
        "https://user:secret@api.github.com",
        "https://api.github.com?query=yes",
        "https://api.github.com/#fragment",
        "not-a-url",
    ],
)
def test_github_client_rejects_non_origin_urls(api_url: str) -> None:
    with pytest.raises(proof.MainValidationProofError, match="plain HTTPS origin"):
        proof.GitHubApiClient(api_url=api_url)


@pytest.mark.parametrize("path", ["repos/o/r", "/repos/o/r?x=1", "/repos/o/r#x"])
def test_github_client_rejects_ambiguous_paths(path: str) -> None:
    client = proof.GitHubApiClient()
    with pytest.raises(proof.MainValidationProofError, match="path is invalid"):
        client.get_object(path)


def test_github_client_sends_authenticated_prefixed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(_Response(200, b'{"ok": true}'))
    captured = _install_connection(monkeypatch, connection)
    client = proof.GitHubApiClient(
        token="secret", api_url="https://example.test:8443/api/v3/", timeout_seconds=7
    )

    assert client.get_object("/repos/o/r", query={"page": "2", "q": "a b"}) == {
        "ok": True
    }
    assert captured == {"hostname": "example.test", "port": 8443, "timeout": 7}
    assert connection.request is not None
    method, target, headers = connection.request
    assert method == "GET"
    assert target == "/api/v3/repos/o/r?page=2&q=a+b"
    assert headers["Authorization"] == "Bearer secret"
    assert connection.closed


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_Response(503, b"{}"), "HTTP 503"),
        (_Response(200, b"not-json"), "invalid JSON"),
        (_Response(200, b"[]"), "JSON object"),
        (_Response(200, b"\xff"), "invalid JSON"),
    ],
)
def test_github_client_fails_closed_for_bad_responses(
    monkeypatch: pytest.MonkeyPatch, response: _Response, message: str
) -> None:
    connection = _Connection(response)
    _install_connection(monkeypatch, connection)

    with pytest.raises(proof.MainValidationProofError, match=message):
        proof.GitHubApiClient().get_object("/repos/o/r")
    assert connection.closed


def test_github_client_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(request_error=OSError("offline"))
    _install_connection(monkeypatch, connection)

    with pytest.raises(proof.MainValidationProofError, match="request failed"):
        proof.GitHubApiClient().get_object("/repos/o/r")
    assert connection.closed


class _PagedClient(proof.GitHubApiClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def get_object(
        self, path: str, *, query: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, query))
        return next(self.responses)


def test_workflow_evidence_paginates_until_exact_total() -> None:
    run = {"id": 42, "run_attempt": 1}
    client = _PagedClient(
        [
            run,
            {
                "total_count": 3,
                "jobs": [
                    {"id": 1, "run_attempt": 1},
                    {"id": 2, "run_attempt": 1},
                ],
            },
            {"total_count": 3, "jobs": [{"id": 3, "run_attempt": 1}]},
        ]
    )

    result = client.workflow_evidence(repository="owner/repo", run_id=42)

    assert result == {
        "run": run,
        "jobs": {
            "total_count": 3,
            "jobs": [
                {"id": 1, "run_attempt": 1},
                {"id": 2, "run_attempt": 1},
                {"id": 3, "run_attempt": 1},
            ],
        },
    }
    assert client.calls[-1] == (
        "/repos/owner/repo/actions/runs/42/attempts/1/jobs",
        {"per_page": "100", "page": "2"},
    )


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([{"total_count": "1", "jobs": []}], "response is incomplete"),
        ([{"total_count": 1, "jobs": "bad"}], "response is incomplete"),
        (
            [
                {"total_count": 2, "jobs": [{"id": 1}]},
                {"total_count": 3, "jobs": [{"id": 2}]},
            ],
            "total changed",
        ),
        (
            [
                {"total_count": 2, "jobs": [{"id": 1}]},
                {"total_count": 2, "jobs": []},
            ],
            "ended early",
        ),
        ([{"total_count": 1, "jobs": [{"id": 1}, {"id": 2}]}], "count is inconsistent"),
    ],
)
def test_workflow_evidence_rejects_inconsistent_pages(
    pages: list[dict[str, Any]], message: str
) -> None:
    for page in pages:
        jobs = page.get("jobs")
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict):
                    job["run_attempt"] = 1
    client = _PagedClient([{"id": 42, "run_attempt": 1}, *pages])
    with pytest.raises(proof.MainValidationProofError, match=message):
        client.workflow_evidence(repository="owner/repo", run_id=42)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: proof._object([], "item"), "JSON object"),
        (lambda: proof._object({1: "bad"}, "item"), "JSON object"),
        (lambda: proof._exact_keys({"a": 1}, {"b"}, "item"), "invalid fields"),
        (lambda: proof._string("", "item"), "non-empty string"),
        (lambda: proof._integer(True, "item"), "positive integer"),
        (lambda: proof._integer(0, "item"), "positive integer"),
        (lambda: proof._timestamp("tomorrow", "item"), "ISO-8601"),
        (lambda: proof._timestamp("2026-07-11T12:00:00", "item"), "timezone"),
        (lambda: proof._sha("f" * 63, "item"), "invalid digest"),
        (lambda: proof._sha("f" * 39, "item", git=True), "invalid digest"),
        (
            lambda: proof._validate_repository_and_ref("owner", "refs/heads/main"),
            "owner/name",
        ),
        (
            lambda: proof._validate_repository_and_ref("owner/repo", "refs/heads/a//b"),
            "canonical branch ref",
        ),
        (
            lambda: proof._validate_repository_and_ref(
                "owner/repo", "refs/heads/a/../b"
            ),
            "canonical branch ref",
        ),
    ],
)
def test_primitive_validators_fail_closed(call: Any, message: str) -> None:
    with pytest.raises(proof.MainValidationProofError, match=message):
        call()


@pytest.mark.parametrize(
    "values",
    [
        ["CI=1", "CI=2", "Security=3"],
        ["Unknown=1", "CodeQL=2", "Security=3"],
        ["CI", "CodeQL=2", "Security=3"],
    ],
)
def test_parse_runs_rejects_invalid_or_duplicate_names(values: list[str]) -> None:
    with pytest.raises(proof.MainValidationProofError, match="must be unique"):
        proof._parse_runs(values)


@pytest.mark.parametrize(("value", "message"), [("x", "integer"), ("0", "positive")])
def test_parse_runs_rejects_invalid_ids(value: str, message: str) -> None:
    with pytest.raises(proof.MainValidationProofError, match=message):
        proof._parse_runs([f"CI={value}", "CodeQL=2", "Security=3"])


def test_parse_runs_accepts_complete_selection() -> None:
    assert proof._parse_runs(["CI=1", "CodeQL=2", "Security=3"]) == {
        "CI": 1,
        "CodeQL": 2,
        "Security": 3,
    }


def _evidence_arguments(tmp_path: Path) -> list[str]:
    return [
        f"{policy.artifact_name}={tmp_path / (policy.artifact_name + '.json')}"
        for policy in proof.EVIDENCE_POLICIES.values()
    ]


def test_parse_evidence_paths_accepts_exact_set(tmp_path: Path) -> None:
    arguments = _evidence_arguments(tmp_path)
    parsed = proof._parse_evidence_paths(arguments)
    assert set(parsed) == {
        policy.artifact_name for policy in proof.EVIDENCE_POLICIES.values()
    }


def test_parse_evidence_paths_rejects_bad_and_incomplete_values(tmp_path: Path) -> None:
    arguments = _evidence_arguments(tmp_path)
    with pytest.raises(proof.MainValidationProofError, match="must uniquely"):
        proof._parse_evidence_paths([*arguments, arguments[0]])
    with pytest.raises(proof.MainValidationProofError, match="all required"):
        proof._parse_evidence_paths(arguments[:-1])
    with pytest.raises(proof.MainValidationProofError, match="must uniquely"):
        proof._parse_evidence_paths(["python-evidence-unit="])


def test_load_json_and_write_proof_fail_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(proof.MainValidationProofError, match="unable to load proof"):
        proof._load_json(invalid, "proof")
    with pytest.raises(proof.MainValidationProofError, match="unable to load missing"):
        proof._load_json(tmp_path / "missing.json", "missing")

    parent_file = tmp_path / "parent"
    parent_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(proof.MainValidationProofError, match="unable to write proof"):
        proof._write_proof(parent_file / "proof.json", {"ok": True})


def test_filesystem_and_git_helpers_wrap_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(proof.MainValidationProofError, match="missing or unsafe"):
        proof._file_sha256(missing)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git")),
    )
    with pytest.raises(
        proof.MainValidationProofError, match="enumerate release fixtures"
    ):
        proof.fixture_hashes(tmp_path)
    with pytest.raises(proof.MainValidationProofError, match="inspect the local Git"):
        proof._git(tmp_path, "status")


def test_fixture_hashes_rejects_empty_git_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=b"")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(proof.MainValidationProofError, match="fixtures are missing"):
        proof.fixture_hashes(tmp_path)


def test_online_cli_fetches_all_runs_and_writes_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    class Client:
        def __init__(self, *, token: str | None, api_url: str) -> None:
            assert token == "token"
            assert api_url == "https://github.example/api"

        def workflow_evidence(
            self, *, repository: str, run_id: int
        ) -> dict[str, object]:
            calls.append((repository, run_id))
            return {"run_id": run_id}

    written: dict[str, object] = {}
    monkeypatch.setenv("TEST_GITHUB_TOKEN", "token")
    monkeypatch.setattr(proof, "GitHubApiClient", Client)
    monkeypatch.setattr(
        proof, "_load_json", lambda path, label: {"path": str(path), "label": label}
    )
    monkeypatch.setattr(proof, "generate_proof", lambda **kwargs: {"generated": kwargs})
    monkeypatch.setattr(
        proof,
        "_write_proof",
        lambda path, value: written.update(path=path, value=value),
    )
    evidence_args = [
        item
        for value in _evidence_arguments(tmp_path)
        for item in ("--evidence", value)
    ]

    result = proof.main(
        [
            "generate",
            "--repo-root",
            str(tmp_path),
            "--repository",
            "owner/repo",
            "--output",
            str(tmp_path / "proof.json"),
            "--api-url",
            "https://github.example/api",
            "--token-env",
            "TEST_GITHUB_TOKEN",
            "--run",
            "CI=11",
            "--run",
            "CodeQL=12",
            "--run",
            "Security=13",
            *evidence_args,
        ]
    )

    assert result == 0
    assert calls == [("owner/repo", 11), ("owner/repo", 12), ("owner/repo", 13)]
    assert written["path"] == tmp_path / "proof.json"


def test_cli_returns_two_and_reports_domain_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        proof.main(["verify", "--repository", "owner/repo", "--proof", "/missing"]) == 2
    )
    assert (
        "main validation proof rejected: unable to load proof"
        in capsys.readouterr().err
    )
