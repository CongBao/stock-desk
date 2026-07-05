from __future__ import annotations

from dataclasses import dataclass, field
import io
from pathlib import Path
import subprocess
import tarfile
import tomllib
import zipfile

import pytest

from scripts.verify_release import (
    GateCommand,
    ReleaseVerificationError,
    SubprocessGateRunner,
    check_build_artifacts,
    check_changelog,
    check_remote,
    verify_release,
)


EXPECTED_IDENTITY = ("CongBao", "bao_cong@outlook.com")
EXPECTED_REMOTE = "git@github.com:CongBao/stock-desk.git"
E2E_BASE_URL = "http://127.0.0.1:8000"


def git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@dataclass
class FakeGateRunner:
    repo: Path
    fail_command: tuple[str, ...] | None = None
    calls: list[GateCommand] = field(default_factory=list)

    def run(self, gate: GateCommand) -> None:
        self.calls.append(gate)
        if gate.command == self.fail_command:
            raise subprocess.CalledProcessError(7, gate.command)
        if gate.command == ("make", "release-check"):
            project = tomllib.loads(
                (self.repo / "pyproject.toml").read_text(encoding="utf-8")
            )
            write_valid_artifacts(self.repo, project["project"]["version"])


def metadata_payload(name: str, version: str) -> str:
    return f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n"


def write_wheel(
    repo: Path,
    version: str,
    *,
    metadata_name: str = "stock-desk",
    metadata_version: str | None = None,
    unrelated_only: bool = False,
    wheel_payload: bytes | None = None,
    record_paths: tuple[str, ...] | None = None,
) -> None:
    package_dist = repo / "dist"
    package_dist.mkdir(exist_ok=True)
    wheel = package_dist / f"stock_desk-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if unrelated_only:
            archive.writestr("unrelated.txt", b"not a wheel payload\n")
            return
        dist_info = f"stock_desk-{version}.dist-info"
        package_path = "stock_desk/__init__.py"
        metadata_path = f"{dist_info}/METADATA"
        wheel_path = f"{dist_info}/WHEEL"
        record_path = f"{dist_info}/RECORD"
        archive.writestr(package_path, b"__version__ = 'fixture'\n")
        archive.writestr(
            metadata_path,
            metadata_payload(
                metadata_name, metadata_version if metadata_version else version
            ),
        )
        archive.writestr(
            wheel_path,
            wheel_payload
            if wheel_payload is not None
            else (
                b"Wheel-Version: 1.0\n"
                b"Generator: test fixture\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n"
            ),
        )
        if record_paths is None:
            record_paths = (package_path, metadata_path, wheel_path, record_path)
        archive.writestr(
            record_path,
            "".join(f"{path},,\n" for path in record_paths),
        )


def add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    metadata = tarfile.TarInfo(name)
    metadata.size = len(payload)
    archive.addfile(metadata, io.BytesIO(payload))


def add_tar_text(archive: tarfile.TarFile, name: str, content: str) -> None:
    add_tar_bytes(archive, name, content.encode())


def sdist_pyproject_payload(
    version: str,
    *,
    name: str = "stock-desk",
    build_backend: str = "hatchling.build",
    build_requirement: str = "hatchling>=1.27,<2",
) -> bytes:
    return (
        f'[project]\nname = "{name}"\nversion = "{version}"\n\n'
        "[build-system]\n"
        f'requires = ["{build_requirement}"]\n'
        f'build-backend = "{build_backend}"\n'
    ).encode()


def write_sdist(
    repo: Path,
    version: str,
    *,
    metadata_name: str = "stock-desk",
    metadata_version: str | None = None,
    unrelated_only: bool = False,
    pyproject_payload: bytes | None = None,
) -> None:
    package_dist = repo / "dist"
    package_dist.mkdir(exist_ok=True)
    source = package_dist / f"stock_desk-{version}.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        root = f"stock_desk-{version}"
        if unrelated_only:
            add_tar_text(archive, f"{root}/unrelated.txt", "not an sdist payload\n")
            return
        add_tar_bytes(
            archive,
            f"{root}/pyproject.toml",
            pyproject_payload
            if pyproject_payload is not None
            else sdist_pyproject_payload(version),
        )
        add_tar_text(
            archive,
            f"{root}/PKG-INFO",
            metadata_payload(
                metadata_name, metadata_version if metadata_version else version
            ),
        )
        add_tar_text(
            archive,
            f"{root}/src/stock_desk/__init__.py",
            "__version__ = 'fixture'\n",
        )


def write_valid_artifacts(repo: Path, version: str) -> None:
    web_dist = repo / "web" / "dist"
    web_dist.mkdir(parents=True, exist_ok=True)
    (web_dist / "index.html").write_text(
        "<!doctype html><title>stock-desk</title>", encoding="utf-8"
    )
    write_wheel(repo, version)
    write_sdist(repo, version)


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "phase/release-test")
    git(repo, "config", "user.name", EXPECTED_IDENTITY[0])
    git(repo, "config", "user.email", EXPECTED_IDENTITY[1])
    git(repo, "remote", "add", "origin", EXPECTED_REMOTE)

    (repo / ".gitignore").write_text("dist/\nweb/dist/\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "stock-desk"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text(
        '{"name":"@stock-desk/web","version":"0.1.0"}\n',
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-07-05\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "test fixture")
    return repo


def run_verifier(repo: Path, runner: FakeGateRunner) -> None:
    verify_release(repo, "0.1.0", runner, fingerprint=lambda _repo: "stable")


def test_rejects_a_dirty_worktree_before_running_gates(release_repo: Path) -> None:
    (release_repo / "README.md").write_text("dirty\n", encoding="utf-8")
    runner = FakeGateRunner(release_repo)

    with pytest.raises(ReleaseVerificationError, match="worktree is not clean"):
        run_verifier(release_repo, runner)

    assert runner.calls == []


def test_rejects_detached_head_with_a_branch_policy_diagnostic(
    release_repo: Path,
) -> None:
    git(release_repo, "checkout", "-q", "--detach")

    with pytest.raises(ReleaseVerificationError, match="release branch"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


@pytest.mark.parametrize(
    "push_urls",
    [
        ("ssh://bad.example.invalid/private/repository.git",),
        (EXPECTED_REMOTE, "ssh://bad.example.invalid/second.git"),
        (EXPECTED_REMOTE, EXPECTED_REMOTE),
    ],
)
def test_rejects_any_noncanonical_or_multiple_push_destinations(
    release_repo: Path, push_urls: tuple[str, ...]
) -> None:
    for push_url in push_urls:
        git(release_repo, "config", "--add", "remote.origin.pushurl", push_url)

    with pytest.raises(ReleaseVerificationError, match="origin remote") as captured:
        check_remote(release_repo)

    assert all(push_url not in str(captured.value) for push_url in push_urls)


def test_accepts_one_explicit_canonical_push_destination(release_repo: Path) -> None:
    git(release_repo, "config", "remote.origin.pushurl", EXPECTED_REMOTE)

    check_remote(release_repo)


@pytest.mark.parametrize(
    "fetch_urls",
    [
        (EXPECTED_REMOTE, "ssh://bad.example.invalid/fetch.git"),
        (EXPECTED_REMOTE, EXPECTED_REMOTE),
    ],
)
def test_rejects_multiple_fetch_destinations(
    release_repo: Path, fetch_urls: tuple[str, ...]
) -> None:
    git(release_repo, "config", "--unset-all", "remote.origin.url")
    for fetch_url in fetch_urls:
        git(release_repo, "config", "--add", "remote.origin.url", fetch_url)

    with pytest.raises(ReleaseVerificationError, match="origin remote") as captured:
        check_remote(release_repo)

    assert all(fetch_url not in str(captured.value) for fetch_url in fetch_urls)


def test_subprocess_runner_merges_forced_environment_overrides(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INHERITED_RELEASE_MARKER", "present")
    monkeypatch.setenv("STOCK_DESK_E2E_BASE_URL", "https://malicious.invalid")
    captured: dict[str, object] = {}

    def fake_run(command: tuple[str, ...], **options: object) -> None:
        captured["command"] = command
        captured.update(options)

    monkeypatch.setattr(subprocess, "run", fake_run)
    gate = GateCommand(
        ("pnpm", "e2e"),
        timeout_seconds=600,
        environment=(("STOCK_DESK_E2E_BASE_URL", E2E_BASE_URL),),
    )

    SubprocessGateRunner(release_repo).run(gate)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["INHERITED_RELEASE_MARKER"] == "present"
    assert environment["STOCK_DESK_E2E_BASE_URL"] == E2E_BASE_URL


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("identity", "configured Git identity"),
        ("remote", "origin remote"),
        ("python-version", "project versions"),
        ("web-version", "project versions"),
        ("changelog", "release changelog entry"),
    ],
)
def test_rejects_bad_release_metadata(
    release_repo: Path, mutation: str, message: str
) -> None:
    if mutation == "identity":
        git(release_repo, "config", "user.email", "wrong@example.com")
    elif mutation == "remote":
        git(release_repo, "remote", "set-url", "origin", "git@example.invalid:x/y.git")
    elif mutation == "python-version":
        path = release_repo / "pyproject.toml"
        path.write_text(path.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8")
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "change Python version")
    elif mutation == "web-version":
        path = release_repo / "web" / "package.json"
        path.write_text(path.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8")
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "change web version")
    else:
        path = release_repo / "CHANGELOG.md"
        path.write_text(
            path.read_text().replace("2026-07-05", "Unreleased"), encoding="utf-8"
        )
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "remove release date")

    with pytest.raises(ReleaseVerificationError, match=message):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_rejects_a_bad_identity_anywhere_in_reachable_history(
    release_repo: Path,
) -> None:
    path = release_repo / "CHANGELOG.md"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    git(release_repo, "add", str(path))
    git(
        release_repo,
        "-c",
        "user.name=Someone Else",
        "-c",
        "user.email=else@example.com",
        "commit",
        "-q",
        "-m",
        "bad identity",
    )
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    git(release_repo, "add", str(path))
    git(release_repo, "commit", "-q", "-m", "restore expected identity at HEAD")

    with pytest.raises(ReleaseVerificationError, match="reachable commit identities"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_rejects_forbidden_paths_even_when_deleted_from_head(
    release_repo: Path,
) -> None:
    forbidden = release_repo / "docs" / "superpowers" / "private.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("private\n", encoding="utf-8")
    git(release_repo, "add", str(forbidden))
    git(release_repo, "commit", "-q", "-m", "add forbidden history")
    git(release_repo, "rm", "-q", str(forbidden))
    git(release_repo, "commit", "-q", "-m", "remove forbidden history")

    with pytest.raises(ReleaseVerificationError, match="reachable Git history"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_reports_a_failed_canonical_gate(release_repo: Path) -> None:
    runner = FakeGateRunner(release_repo, fail_command=("make", "release-check"))

    with pytest.raises(ReleaseVerificationError, match="release gate failed"):
        run_verifier(release_repo, runner)

    assert [call.command for call in runner.calls] == [("make", "release-check")]


def test_accepts_a_future_release_with_a_different_valid_date(
    release_repo: Path,
) -> None:
    pyproject = release_repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8"
    )
    web_package = release_repo / "web" / "package.json"
    web_package.write_text(
        web_package.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8"
    )
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("[0.1.0] - 2026-07-05", "[0.2.0] - 2027-01-19"),
        encoding="utf-8",
    )
    git(release_repo, "add", ".")
    git(release_repo, "commit", "-q", "-m", "prepare future release")

    verify_release(
        release_repo,
        "0.2.0",
        FakeGateRunner(release_repo),
        fingerprint=lambda _repo: "stable",
    )


@pytest.mark.parametrize(
    "heading",
    [
        "## [0.1.0] - Unreleased",
        "## [0.1.0] - 2026-7-5",
        "## [0.1.0] - 2026-02-30",
        "## [0.1.0] - 2026-07-05\n## [0.1.0] - 2026-07-06",
    ],
)
def test_rejects_unreleased_malformed_or_duplicate_release_dates(
    release_repo: Path, heading: str
) -> None:
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(f"# Changelog\n\n{heading}\n", encoding="utf-8")

    with pytest.raises(ReleaseVerificationError, match="release changelog entry"):
        check_changelog(release_repo, "0.1.0")


def test_accepts_exact_valid_current_release_artifacts(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")

    check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_valid_archives_with_only_unrelated_files(
    release_repo: Path, artifact: str
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    if artifact == "wheel":
        write_wheel(release_repo, "0.1.0", unrelated_only=True)
    else:
        write_sdist(release_repo, "0.1.0", unrelated_only=True)

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_package_metadata_with_a_different_version(
    release_repo: Path, artifact: str
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    if artifact == "wheel":
        write_wheel(release_repo, "0.1.0", metadata_version="0.2.0")
    else:
        write_sdist(release_repo, "0.1.0", metadata_version="0.2.0")

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_package_metadata_with_a_different_name(
    release_repo: Path, artifact: str
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    if artifact == "wheel":
        write_wheel(release_repo, "0.1.0", metadata_name="another-package")
    else:
        write_sdist(release_repo, "0.1.0", metadata_name="another-package")

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize(
    "wheel_payload",
    [
        b"Wheel-Version: \xff\xfe\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    ],
)
def test_rejects_malformed_or_unsupported_wheel_metadata(
    release_repo: Path, wheel_payload: bytes
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(release_repo, "0.1.0", wheel_payload=wheel_payload)

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize(
    "record_paths",
    [
        (
            "stock_desk/__init__.py",
            "stock_desk-0.1.0.dist-info/METADATA",
            "stock_desk-0.1.0.dist-info/WHEEL",
        ),
        (
            "stock_desk/__init__.py",
            "stock_desk-0.1.0.dist-info/METADATA",
            "stock_desk-0.1.0.dist-info/WHEEL",
            "stock_desk-0.1.0.dist-info/RECORD",
            "stock_desk/__init__.py",
        ),
        (
            "stock_desk/__init__.py",
            "stock_desk-0.1.0.dist-info/METADATA",
            "stock_desk-0.1.0.dist-info/WHEEL",
            "stock_desk-0.1.0.dist-info/RECORD",
            "../escape.py",
        ),
        (
            "stock_desk/__init__.py",
            "stock_desk-0.1.0.dist-info/METADATA",
            "stock_desk-0.1.0.dist-info/WHEEL",
            "stock_desk-0.1.0.dist-info/RECORD",
            "stock_desk/not-in-the-wheel.py",
        ),
    ],
)
def test_rejects_missing_duplicate_unsafe_or_inconsistent_record_paths(
    release_repo: Path, record_paths: tuple[str, ...]
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(release_repo, "0.1.0", record_paths=record_paths)

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_invalid_sdist_pyproject_toml(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(release_repo, "0.1.0", pyproject_payload=b"[project\n")

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize(
    "pyproject_payload",
    [
        sdist_pyproject_payload("0.1.0", build_backend="setuptools.build_meta"),
        sdist_pyproject_payload("0.1.0", build_requirement="setuptools>=80"),
        sdist_pyproject_payload("0.1.0", name="another-package"),
        sdist_pyproject_payload("0.2.0"),
    ],
)
def test_rejects_incorrect_sdist_project_or_build_metadata(
    release_repo: Path, pyproject_payload: bytes
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        pyproject_payload=pyproject_payload,
    )

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_wrong_or_empty_release_artifacts(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.2.0")
    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")

    write_valid_artifacts(release_repo, "0.1.0")
    (release_repo / "dist" / "stock_desk-0.1.0-py3-none-any.whl").write_bytes(b"")
    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_stale_release_archives(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    (release_repo / "dist" / "stock_desk-0.0.9-py3-none-any.whl").write_bytes(b"stale")
    (release_repo / "dist" / "stock_desk-0.0.9.tar.gz").write_bytes(b"stale")

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("index_content", ["", "<title>another-app</title>"])
def test_rejects_empty_or_wrong_web_entrypoint(
    release_repo: Path, index_content: str
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    (release_repo / "web" / "dist" / "index.html").write_text(
        index_content, encoding="utf-8"
    )

    with pytest.raises(ReleaseVerificationError, match="web build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_success_runs_timed_gates_and_rechecks_clean_sources(
    release_repo: Path,
) -> None:
    runner = FakeGateRunner(release_repo)

    run_verifier(release_repo, runner)

    assert runner.calls == [
        GateCommand(("make", "release-check"), timeout_seconds=1800),
        GateCommand(
            ("pnpm", "e2e"),
            timeout_seconds=600,
            environment=(("STOCK_DESK_E2E_BASE_URL", E2E_BASE_URL),),
        ),
    ]
