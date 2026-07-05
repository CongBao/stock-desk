from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_DEPLOYMENT_FILES = {
    ".dockerignore",
    "Dockerfile",
    "Makefile",
    "compose.yaml",
    "scripts/dev.py",
}


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_deployment_contract_is_complete_and_public_only() -> None:
    missing = sorted(
        path for path in REQUIRED_DEPLOYMENT_FILES if not (REPO_ROOT / path).is_file()
    )
    assert missing == []

    dockerfile = _read("Dockerfile")
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert re.search(r"^ARG PYTHON_VERSION=3\.12\.\d+$", dockerfile, re.MULTILINE)
    assert "FROM python:${PYTHON_VERSION}-slim-bookworm" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "COPY --from=web-builder" in dockerfile
    assert "STOCK_DESK_WEB_DIST_DIR=/app/web-dist" in dockerfile
    assert 'CMD ["uvicorn", "stock_desk.main:app"' in dockerfile
    assert "COPY . " not in dockerfile

    compose = _read("compose.yaml")
    assert re.findall(r"^  (api|worker):$", compose, flags=re.MULTILINE) == [
        "api",
        "worker",
    ]
    assert "sqlite:////app/data/stock-desk.db" in compose
    assert "./data:/app/data" in compose
    assert compose.count("./data:/app/data") == 1
    assert '"stock_desk.tasks.worker"' in compose
    assert "healthcheck:" in compose
    assert "restart: unless-stopped" in compose
    common, services = compose.split("services:", maxsplit=1)
    api_service, worker_service = services.split("  worker:", maxsplit=1)
    assert "build:" not in common
    assert "build:" in api_service
    assert "build:" not in worker_service

    makefile = _read("Makefile")
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, flags=re.MULTILINE))
    assert targets == {
        "bootstrap",
        "build",
        "dev",
        "lint",
        "public-tree",
        "release-check",
        "smoke",
        "test",
        "typecheck",
    }

    dev_script = _read("scripts/dev.py")
    assert "stock_desk.tasks.worker" in dev_script
    assert "stock_desk.main:app" in dev_script
    assert '"pnpm"' in dev_script

    dockerignore = _read(".dockerignore")
    for excluded in (
        ".git",
        ".agents",
        ".codex",
        ".superpowers",
        ".env",
        "data",
        "docs/superpowers",
        "node_modules",
        "openspec",
        "outputs",
        "web/dist",
        "work",
    ):
        assert excluded in dockerignore.splitlines()


class HttpResult(NamedTuple):
    status: int
    content_type: str
    body: bytes


def _request(path: str) -> HttpResult:
    base_url = os.environ.get("STOCK_DESK_SMOKE_URL", "http://127.0.0.1:8000")
    request = Request(f"{base_url}{path}", headers={"Accept": "*/*"})
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - local smoke URL
            return HttpResult(
                response.status,
                response.headers.get_content_type(),
                response.read(),
            )
    except HTTPError as error:
        return HttpResult(
            error.code,
            error.headers.get_content_type(),
            error.read(),
        )


@pytest.fixture(scope="session")
def running_compose_stack() -> None:
    if os.environ.get("STOCK_DESK_CONTAINER_TESTS") != "1":
        pytest.skip("set STOCK_DESK_CONTAINER_TESTS=1 to test a running Compose stack")

    result = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "--services"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) >= {"api", "worker"}

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if _request("/api/health").status == 200:
                return
        except URLError:
            pass
        time.sleep(0.5)
    pytest.fail("Compose API did not become healthy within 45 seconds")


@pytest.mark.container
def test_compose_api_health_json(running_compose_stack: None) -> None:
    response = _request("/api/health")

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {
        "api_version": "v1",
        "name": "stock-desk",
        "status": "ok",
    }


@pytest.mark.container
def test_compose_worker_is_running_and_ready(running_compose_stack: None) -> None:
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-color", "worker"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Stock Desk task worker ready" in result.stdout + result.stderr


@pytest.mark.container
@pytest.mark.parametrize("path", ["/", "/market"])
def test_compose_serves_root_and_spa_deep_link(
    running_compose_stack: None,
    path: str,
) -> None:
    response = _request(path)

    assert response.status == 200
    assert response.content_type == "text/html"
    assert b"<title>stock-desk</title>" in response.body


@pytest.mark.container
def test_compose_unknown_api_is_json_404(running_compose_stack: None) -> None:
    response = _request("/api/does-not-exist")

    assert response.status == 404
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {"detail": "Not Found"}
