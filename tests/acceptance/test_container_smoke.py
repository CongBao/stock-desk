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

from scripts.source_fingerprint import compute_source_fingerprint


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
    assert 'ENTRYPOINT ["python", "-m", "stock_desk.runtime_entrypoint"]' in dockerfile
    assert 'CMD ["uvicorn", "stock_desk.main:app"' in dockerfile
    assert "--chown=10001:10001 /app/.venv" not in dockerfile
    assert "--chown=10001:10001 /build/web/dist" not in dockerfile
    assert "AS fingerprint-builder" in dockerfile
    assert "scripts/source_fingerprint.py" in dockerfile
    fingerprint_stage, remaining_stages = dockerfile.split(
        "FROM node:${NODE_VERSION}-bookworm-slim AS web-builder", maxsplit=1
    )
    assert "COPY .dockerignore Dockerfile README.md" in fingerprint_stage
    python_builder = remaining_stages.split(
        "FROM python:${PYTHON_VERSION}-slim-bookworm AS python-builder", maxsplit=1
    )[1].split("FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime", maxsplit=1)[0]
    dependency_sync = "uv sync --frozen --no-dev --no-install-project"
    package_readme_copy = "COPY README.md ./README.md"
    project_sync = "uv sync --frozen --no-dev --no-editable"
    assert dependency_sync in python_builder
    assert package_readme_copy in python_builder
    assert project_sync in python_builder
    assert (
        python_builder.index(dependency_sync)
        < python_builder.index(package_readme_copy)
        < python_builder.index(project_sync)
    )
    assert "/app/source-fingerprint" in dockerfile
    assert "chmod -R a-w /app/.venv /app/web-dist" in dockerfile
    assert "COPY . " not in dockerfile

    compose = _read("compose.yaml")
    assert not compose.startswith("name:")
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
    assert 'user: "0:0"' in compose
    assert "STOCK_DESK_UID:" in compose
    assert "STOCK_DESK_GID:" in compose
    assert "init: true" not in compose
    assert "STOCK_DESK_IMAGE" in compose
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
    release_check = re.search(r"^release-check:(.*)$", makefile, re.MULTILINE)
    assert release_check is not None
    assert "smoke" in release_check.group(1).split()

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
        "**/.env",
        "**/.env.*",
        "data",
        "docs/superpowers",
        "node_modules",
        "openspec",
        "outputs",
        "web/dist",
        "work",
        "*.tsbuildinfo",
    ):
        assert excluded in dockerignore.splitlines()


def test_container_source_validation_rejects_another_checkout(
    tmp_path: Path,
) -> None:
    other_root = tmp_path / "other" / REPO_ROOT.name
    other_compose = other_root / "compose.yaml"
    container = {
        "Config": {
            "Labels": {
                "com.docker.compose.project.working_dir": os.fspath(other_root),
                "com.docker.compose.project.config_files": os.fspath(other_compose),
                "com.docker.compose.service": "api",
            }
        }
    }

    with pytest.raises(AssertionError, match="working directory"):
        _validate_container_source(container, service="api")


def test_worker_state_validation_rejects_any_restart() -> None:
    container = {"RestartCount": 1, "State": {"Running": True, "Status": "running"}}

    with pytest.raises(AssertionError, match="restarted"):
        _assert_running_without_restarts(container, service="worker")


def test_source_fingerprint_validation_rejects_wrong_image() -> None:
    with pytest.raises(AssertionError, match="source fingerprint"):
        _assert_source_fingerprint(
            "expected-fingerprint",
            "other-checkout-fingerprint",
            service="api",
        )


def _assert_source_fingerprint(expected: str, actual: str, *, service: str) -> None:
    assert actual == expected, (
        f"{service} source fingerprint does not match this checkout: "
        f"expected {expected}, found {actual}"
    )


class HttpResult(NamedTuple):
    status: int
    content_type: str
    body: bytes


class ComposeStack(NamedTuple):
    api_id: str
    worker_id: str
    image_id: str
    project: str
    source_fingerprint: str


def _run(
    command: list[str],
    *,
    timeout: float = 10,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"command timed out after {timeout}s: {command!r}: {error}")
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _compose(*arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *arguments], timeout=timeout)


def _docker_exec(
    container_id: str,
    *arguments: str,
    user: tuple[int, int] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec"]
    if user is not None:
        command.extend(["--user", f"{user[0]}:{user[1]}"])
    command.extend([container_id, *arguments])
    return _run(command, timeout=timeout)


def _service_id(service: str) -> str:
    result = _compose("ps", "--all", "--quiet", service)
    container_ids = [line for line in result.stdout.splitlines() if line]
    assert len(container_ids) == 1, (
        f"expected one current {service} container, found {container_ids!r}"
    )
    return container_ids[0]


def _inspect(container_id: str) -> dict[str, object]:
    result = _run(["docker", "inspect", container_id])
    documents = json.loads(result.stdout)
    assert isinstance(documents, list) and len(documents) == 1
    container = documents[0]
    assert isinstance(container, dict)
    return container


def _labels(container: dict[str, object]) -> dict[str, str]:
    configuration = container.get("Config")
    assert isinstance(configuration, dict)
    labels = configuration.get("Labels")
    assert isinstance(labels, dict)
    assert all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    )
    return labels


def _validate_container_source(
    container: dict[str, object],
    *,
    service: str,
) -> None:
    labels = _labels(container)
    working_dir = labels.get("com.docker.compose.project.working_dir")
    assert working_dir is not None, "Compose working directory label is missing"
    assert Path(working_dir).resolve() == REPO_ROOT.resolve(), (
        f"Compose working directory is from another checkout: {working_dir}"
    )

    raw_config_files = labels.get("com.docker.compose.project.config_files")
    assert raw_config_files is not None, "Compose config files label is missing"
    config_files = {
        Path(item).resolve() for item in raw_config_files.split(",") if item.strip()
    }
    assert (REPO_ROOT / "compose.yaml").resolve() in config_files, (
        f"Compose config label does not include this checkout: {raw_config_files}"
    )
    assert labels.get("com.docker.compose.service") == service


def _assert_running_without_restarts(
    container: dict[str, object],
    *,
    service: str,
) -> None:
    restart_count = container.get("RestartCount")
    assert restart_count == 0, f"{service} restarted {restart_count!r} times"
    state = container.get("State")
    assert isinstance(state, dict)
    assert state.get("Running") is True, (
        f"{service} is not running (status={state.get('Status')!r})"
    )


def _image_id(container: dict[str, object]) -> str:
    image_id = container.get("Image")
    assert isinstance(image_id, str) and image_id.startswith("sha256:")
    return image_id


def _project(container: dict[str, object]) -> str:
    project = _labels(container).get("com.docker.compose.project")
    assert project
    return project


def _pid_one_identity(container_id: str) -> tuple[int, int]:
    result = _docker_exec(
        container_id,
        "python",
        "-c",
        "from pathlib import Path; "
        "lines = Path('/proc/1/status').read_text().splitlines(); "
        "uid = next(line for line in lines if line.startswith('Uid:')).split()[2]; "
        "gid = next(line for line in lines if line.startswith('Gid:')).split()[2]; "
        "print(uid, gid)",
    )
    uid_text, gid_text = result.stdout.split()
    uid, gid = int(uid_text), int(gid_text)
    assert uid > 0 and gid > 0
    return uid, gid


def _container_source_fingerprint(container_id: str) -> str:
    identity = _pid_one_identity(container_id)
    result = _docker_exec(
        container_id,
        "python",
        "-c",
        "from pathlib import Path; "
        "print(Path('/app/source-fingerprint').read_text(encoding='ascii').strip())",
        user=identity,
    )
    fingerprint = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    return fingerprint


def _current_stack(stack: ComposeStack) -> tuple[dict[str, object], dict[str, object]]:
    assert _service_id("api") == stack.api_id, "api container changed during smoke"
    assert _service_id("worker") == stack.worker_id, (
        "worker container changed during smoke"
    )
    api = _inspect(stack.api_id)
    worker = _inspect(stack.worker_id)
    _validate_container_source(api, service="api")
    _validate_container_source(worker, service="worker")
    _assert_running_without_restarts(api, service="api")
    _assert_running_without_restarts(worker, service="worker")
    assert _image_id(api) == stack.image_id
    assert _image_id(worker) == stack.image_id
    assert _project(api) == stack.project
    assert _project(worker) == stack.project
    _assert_source_fingerprint(
        stack.source_fingerprint,
        _container_source_fingerprint(stack.api_id),
        service="api",
    )
    _assert_source_fingerprint(
        stack.source_fingerprint,
        _container_source_fingerprint(stack.worker_id),
        service="worker",
    )
    return api, worker


def _container_logs(container_id: str) -> str:
    result = _run(["docker", "logs", container_id])
    return result.stdout + result.stderr


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> HttpResult:
    base_url = os.environ.get("STOCK_DESK_SMOKE_URL", "http://127.0.0.1:8000")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "*/*"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
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
def running_compose_stack() -> ComposeStack:
    if os.environ.get("STOCK_DESK_CONTAINER_TESTS") != "1":
        pytest.skip("set STOCK_DESK_CONTAINER_TESTS=1 to test a running Compose stack")

    api_id = _service_id("api")
    worker_id = _service_id("worker")
    api = _inspect(api_id)
    worker = _inspect(worker_id)
    _validate_container_source(api, service="api")
    _validate_container_source(worker, service="worker")
    assert _image_id(api) == _image_id(worker), (
        "api and worker must use the exact same image"
    )
    assert _project(api) == _project(worker)
    source_fingerprint = compute_source_fingerprint(REPO_ROOT)
    stack = ComposeStack(
        api_id,
        worker_id,
        _image_id(api),
        _project(api),
        source_fingerprint,
    )

    deadline = time.monotonic() + 60
    worker_logs = ""
    while time.monotonic() < deadline:
        _current_stack(stack)
        worker_logs = _container_logs(worker_id)
        worker_ready = "Stock Desk task worker ready" in worker_logs
        api_ready = False
        try:
            api_ready = _request("/api/health").status == 200
        except URLError:
            pass
        if api_ready and worker_ready:
            _current_stack(stack)
            return stack
        time.sleep(0.5)
    pytest.fail(
        "Compose API and current worker did not become ready within 60 seconds; "
        f"worker logs:\n{worker_logs}"
    )


@pytest.mark.container
def test_compose_services_belong_to_checkout_and_share_image(
    running_compose_stack: ComposeStack,
) -> None:
    _current_stack(running_compose_stack)


@pytest.mark.container
def test_compose_api_health_json(running_compose_stack: ComposeStack) -> None:
    response = _request("/api/health")

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {
        "api_version": "v1",
        "name": "stock-desk",
        "status": "ok",
    }


@pytest.mark.container
def test_compose_worker_is_running_and_ready(
    running_compose_stack: ComposeStack,
) -> None:
    _current_stack(running_compose_stack)
    assert "Stock Desk task worker ready" in _container_logs(
        running_compose_stack.worker_id
    )


@pytest.mark.container
def test_compose_pid_one_is_nonroot(
    running_compose_stack: ComposeStack,
) -> None:
    for service in ("api", "worker"):
        result = _compose(
            "exec",
            "-T",
            service,
            "python",
            "-c",
            "from pathlib import Path; "
            "line = next(line for line in Path('/proc/1/status').read_text().splitlines() "
            "if line.startswith('Uid:')); print(line.split()[2])",
        )
        assert int(result.stdout.strip()) > 0
    _current_stack(running_compose_stack)


@pytest.mark.container
def test_runtime_code_is_immutable_and_data_is_writable_by_app_uid(
    running_compose_stack: ComposeStack,
) -> None:
    identity = _pid_one_identity(running_compose_stack.api_id)
    result = _docker_exec(
        running_compose_stack.api_id,
        "python",
        "-c",
        "from pathlib import Path; import site; import stock_desk.main as main; "
        "import stock_desk.runtime_entrypoint as entrypoint; "
        "site_dir = Path(site.getsitepackages()[0]); "
        "pth = next(iter(sorted(site_dir.glob('*.pth')))); "
        "targets = (Path(entrypoint.__file__), Path(main.__file__), pth, "
        "Path('/app/web-dist/index.html'), Path('/app/source-fingerprint')); "
        "assert all(path.stat().st_uid == 0 for path in targets); "
        "assert all(path.stat().st_mode & 0o022 == 0 for path in targets); "
        "denied = 0; "
        'exec("for path in targets:\\n'
        "    try:\\n"
        "        with path.open('ab'):\\n"
        "            pass\\n"
        "    except PermissionError:\\n"
        "        denied += 1\\n"
        "    else:\\n"
        "        raise RuntimeError(f'writable runtime path: {path}')\"); "
        "assert denied == len(targets); "
        "probe = Path('/app/data/write-policy-probe'); "
        "probe.write_text('ok', encoding='utf-8'); assert probe.read_text() == 'ok'; "
        "probe.unlink(); print('runtime-immutable-data-writable')",
        user=identity,
    )

    assert result.stdout.strip() == "runtime-immutable-data-writable"
    _current_stack(running_compose_stack)


@pytest.mark.container
def test_compose_worker_completes_demo_task_through_shared_sqlite(
    running_compose_stack: ComposeStack,
) -> None:
    created = _request(
        "/api/tasks",
        method="POST",
        payload={"kind": "demo.double", "payload": {"value": 21}},
    )
    assert created.status == 201
    task = json.loads(created.body)
    assert isinstance(task, dict)
    task_id = task.get("id")
    assert isinstance(task_id, str)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = _request(f"/api/tasks/{task_id}")
        assert response.status == 200
        task = json.loads(response.body)
        if task["status"] == "succeeded":
            assert task["result"] == {"value": 42}
            _current_stack(running_compose_stack)
            return
        assert task["status"] in {"queued", "running"}
        time.sleep(0.2)
    pytest.fail(f"worker did not complete demo task within 20 seconds: {task!r}")


@pytest.mark.container
@pytest.mark.parametrize("path", ["/", "/market"])
def test_compose_serves_root_and_spa_deep_link(
    running_compose_stack: ComposeStack,
    path: str,
) -> None:
    response = _request(path)

    assert response.status == 200
    assert response.content_type == "text/html"
    assert b"<title>stock-desk</title>" in response.body


@pytest.mark.container
def test_compose_unknown_api_is_json_404(
    running_compose_stack: ComposeStack,
) -> None:
    response = _request("/api/does-not-exist")

    assert response.status == 404
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {"detail": "Not Found"}
