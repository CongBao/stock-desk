from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from scripts import build_installer
from scripts.source_fingerprint import compute_source_fingerprint


@dataclass(frozen=True, slots=True)
class CleanInstallResult:
    source_checkout: Path
    initial_paths: frozenset[str]
    wheel: Path
    source_archive: Path
    web_entrypoint: Path
    package_name: str
    import_succeeded: bool
    installed_module_path: Path
    installed_health_status: str
    web_title: str
    source_revision: str
    current_revision: str
    source_fingerprint: str
    installer_manifest: Path
    installer_manifest_bound: bool


def _run(command: tuple[str, ...], *, cwd: Path) -> str:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    environment["CI"] = "1"
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AssertionError(f"clean-install command failed: {command[0]}") from error
    return completed.stdout


def _tracked_paths(repo: Path) -> tuple[PurePosixPath, ...]:
    try:
        completed = subprocess.run(  # noqa: S603
            ("git", "-C", os.fspath(repo), "ls-files", "-z"),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AssertionError("unable to enumerate the public checkout") from error
    paths: list[PurePosixPath] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        decoded = os.fsdecode(raw)
        path = PurePosixPath(decoded)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != decoded:
            raise AssertionError("tracked checkout path is unsafe")
        paths.append(path)
    return tuple(sorted(paths, key=PurePosixPath.as_posix))


def _copy_public_checkout(repo: Path, destination: Path) -> frozenset[str]:
    if destination.exists():
        raise AssertionError("clean checkout destination already exists")
    destination.mkdir(parents=True)
    initial_paths: set[str] = set()
    for relative in _tracked_paths(repo):
        source = repo.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise AssertionError("tracked checkout input is not a regular file")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        initial_paths.add(relative.as_posix())
    return frozenset(initial_paths)


def _runtime_python(runtime: Path) -> Path:
    windows = runtime / "Scripts" / "python.exe"
    return windows if windows.is_file() else runtime / "bin" / "python"


def _revision(repo: Path) -> str:
    return _run(("git", "-C", os.fspath(repo), "rev-parse", "HEAD"), cwd=repo).strip()


def assert_bound_source_identity(repo: Path, result: CleanInstallResult) -> None:
    resolved_repo = repo.resolve(strict=True)
    if _revision(resolved_repo) != result.source_revision:
        raise AssertionError("source revision changed after clean-install binding")
    if compute_source_fingerprint(resolved_repo) != result.source_fingerprint:
        raise AssertionError("source fingerprint changed after clean-install binding")


def build_clean_install(repo: Path, root: Path) -> CleanInstallResult:
    source_repo = repo.resolve(strict=True)
    source_revision = _revision(source_repo)
    source_fingerprint = compute_source_fingerprint(source_repo)
    installer_identity = build_installer._source_identity()
    if installer_identity != {
        "source_fingerprint": source_fingerprint,
        "source_revision": source_revision,
    }:
        raise AssertionError("installer identity is not bound to the clean source")
    checkout = root / "public-checkout"
    initial_files = _copy_public_checkout(source_repo, checkout)
    initial_components = {
        component
        for path in initial_files
        for component in (
            path.split("/", maxsplit=1)[0],
            "docs/superpowers" if path.startswith("docs/superpowers/") else "",
        )
        if component
    }

    _run(("uv", "build", "--no-sources", "--out-dir", "dist"), cwd=checkout)
    _run(("pnpm", "install", "--frozen-lockfile", "--ignore-scripts"), cwd=checkout)
    _run(("pnpm", "build"), cwd=checkout)
    if compute_source_fingerprint(checkout) != source_fingerprint:
        raise AssertionError("clean checkout changed the source fingerprint")

    wheels = tuple((checkout / "dist").glob("*.whl"))
    source_archives = tuple((checkout / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_archives) != 1:
        raise AssertionError("clean checkout produced an unexpected artifact set")
    wheel = wheels[0]
    source_archive = source_archives[0]
    installer_manifest = root / "installer-manifest-binding.json"
    build_installer._write_installer_manifest(
        installer_manifest,
        version="clean-install-contract",
        os_name="clean-install-contract",
        architecture="source-free-runtime",
        artifact=wheel,
        build_provenance={"contract": "clean-install"},
        source_identity=installer_identity,
    )
    manifest_payload = json.loads(installer_manifest.read_text(encoding="utf-8"))
    manifest_bound = (
        manifest_payload.get("source_revision") == source_revision
        and manifest_payload.get("source_fingerprint") == source_fingerprint
    )
    if not manifest_bound:
        raise AssertionError("installer manifest is not bound to the clean source")

    runtime = root / "installed-runtime"
    _run(("uv", "venv", os.fspath(runtime)), cwd=root)
    python = _runtime_python(runtime)
    _run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            os.fspath(python),
            os.fspath(wheel),
        ),
        cwd=root,
    )
    isolated_checkout = root / "source-unavailable-during-installed-startup"
    checkout.rename(isolated_checkout)
    try:
        probe = _run(
            (
                os.fspath(python),
                "-c",
                (
                    "import importlib.metadata, json, pathlib, tempfile; "
                    "from fastapi.testclient import TestClient; "
                    "from stock_desk.config import Settings; "
                    "from stock_desk.main import create_app; "
                    "import stock_desk.main; "
                    "module_path=pathlib.Path(stock_desk.main.__file__).resolve(); "
                    "data_dir=pathlib.Path(tempfile.mkdtemp(prefix='stock-desk-clean-')); "
                    "settings=Settings(database_url=f\"sqlite:///{data_dir / 'stock-desk.db'}\", "
                    "data_dir=data_dir); "
                    "client=TestClient(create_app(settings)); client.__enter__(); "
                    "response=client.get('/api/health'); client.__exit__(None, None, None); "
                    "print(json.dumps({'name': importlib.metadata.metadata('stock-desk')['Name'], "
                    "'module_path': str(module_path), "
                    "'health_status_code': response.status_code, "
                    "'health_status': response.json().get('status')}))"
                ),
            ),
            cwd=root,
        )
    finally:
        isolated_checkout.rename(checkout)
    installed = json.loads(probe)
    module_path = Path(installed["module_path"])
    if not module_path.is_relative_to(runtime.resolve(strict=True)):
        raise AssertionError("clean install imported from the source checkout")
    if installed["health_status_code"] != 200 or installed["health_status"] != "ok":
        raise AssertionError("clean install API did not start successfully")

    web_entrypoint = checkout / "web" / "dist" / "index.html"
    html = web_entrypoint.read_text(encoding="utf-8")
    title = re.search(r"<title>\s*([^<]+?)\s*</title>", html, re.IGNORECASE)
    if title is None:
        raise AssertionError("clean web build has no title")
    return CleanInstallResult(
        source_checkout=checkout,
        initial_paths=frozenset(initial_components),
        wheel=wheel,
        source_archive=source_archive,
        web_entrypoint=web_entrypoint,
        package_name=str(installed["name"]),
        import_succeeded=True,
        installed_module_path=module_path,
        installed_health_status=str(installed["health_status"]),
        web_title=title.group(1),
        source_revision=source_revision,
        current_revision=_revision(source_repo),
        source_fingerprint=source_fingerprint,
        installer_manifest=installer_manifest,
        installer_manifest_bound=manifest_bound,
    )
