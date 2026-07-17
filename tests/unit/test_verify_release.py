from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from threading import Event, enumerate as enumerate_threads
import time
import tomllib
import tracemalloc
import zipfile

import pytest

import scripts.verify_release as verify_release_module
from scripts.verify_release import (
    GateCommand,
    ProvedReleaseInputs,
    ReleaseVerificationError,
    ReleaseLeakScanner,
    SubprocessGateRunner,
    check_build_artifacts,
    check_changelog,
    check_remote,
    verify_candidate,
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


def git_output(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def create_github_merge_commit(
    repo: Path,
    *,
    parent_count: int = 2,
    author_name: str = "Cong Bao",
    author_email: str = EXPECTED_IDENTITY[1],
    committer_name: str = "GitHub",
    committer_email: str = "noreply@github.com",
    subject: str = "Merge pull request #1 from CongBao/phase/0-foundation",
) -> None:
    head = git_output(repo, "rev-parse", "HEAD")
    tree = git_output(repo, "rev-parse", "HEAD^{tree}")
    parents = [head]
    for parent_number in range(1, parent_count):
        parents.append(
            git_output(
                repo,
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                f"side parent {parent_number}",
            )
        )

    identity_environment = os.environ.copy()
    identity_environment.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
        }
    )
    commit_arguments = ["commit-tree", tree]
    for parent in parents:
        commit_arguments.extend(("-p", parent))
    commit_arguments.extend(("-m", subject))
    merge_commit = git_output(repo, *commit_arguments, env=identity_environment)
    git(repo, "reset", "--hard", merge_commit)


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


def record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return f"sha256={digest.rstrip(b'=').decode()}"


def write_wheel(
    repo: Path,
    version: str,
    *,
    metadata_name: str = "stock-desk",
    metadata_version: str | None = None,
    unrelated_only: bool = False,
    wheel_payload: bytes | None = None,
    record_paths: tuple[str, ...] | None = None,
    record_overrides: dict[str, tuple[str, str]] | None = None,
    record_payload: bytes | None = None,
    package_payload: bytes = b"__version__ = 'fixture'\n",
    core_metadata: bytes | None = None,
    license_payload: bytes = b"fixture license\n",
    extra_members: dict[str, bytes] | None = None,
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
        license_path = f"{dist_info}/licenses/LICENSE"
        member_payloads = {
            package_path: package_payload,
            metadata_path: (
                core_metadata
                if core_metadata is not None
                else metadata_payload(
                    metadata_name, metadata_version if metadata_version else version
                ).encode()
            ),
            wheel_path: (
                wheel_payload
                if wheel_payload is not None
                else (
                    b"Wheel-Version: 1.0\n"
                    b"Generator: test fixture\n"
                    b"Root-Is-Purelib: true\n"
                    b"Tag: py3-none-any\n"
                )
            ),
            license_path: license_payload,
        }
        member_payloads.update(extra_members or {})
        for path, payload in member_payloads.items():
            archive.writestr(path, payload)
        if record_paths is None:
            record_paths = (*member_payloads, record_path)
        if record_payload is None:
            rows: list[str] = []
            for path in record_paths:
                if path == record_path:
                    hash_value, size = "", ""
                elif path in member_payloads:
                    payload = member_payloads[path]
                    hash_value, size = record_digest(payload), str(len(payload))
                else:
                    hash_value, size = "", ""
                if record_overrides and path in record_overrides:
                    hash_value, size = record_overrides[path]
                rows.append(f"{path},{hash_value},{size}\n")
            record_payload = "".join(rows).encode()
        archive.writestr(record_path, record_payload)


def add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    metadata = tarfile.TarInfo(name)
    metadata.size = len(payload)
    archive.addfile(metadata, io.BytesIO(payload))


def add_tar_text(archive: tarfile.TarFile, name: str, content: str) -> None:
    add_tar_bytes(archive, name, content.encode())


def add_tar_directory(archive: tarfile.TarFile, name: str) -> None:
    metadata = tarfile.TarInfo(name)
    metadata.type = tarfile.DIRTYPE
    archive.addfile(metadata)


def sdist_pyproject_payload(
    version: str,
    *,
    name: str = "stock-desk",
    build_backend: str = "hatchling.build",
    build_requirement: str = "hatchling>=1.27,<2",
    dependencies: str | None = None,
    backend_path: str | None = None,
) -> bytes:
    dependency_line = "" if dependencies is None else f"dependencies = {dependencies}\n"
    backend_path_line = (
        "" if backend_path is None else f'backend-path = ["{backend_path}"]\n'
    )
    return (
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        f"{dependency_line}\n"
        "[build-system]\n"
        f'requires = ["{build_requirement}"]\n'
        f'build-backend = "{build_backend}"\n'
        f"{backend_path_line}"
    ).encode()


def write_sdist(
    repo: Path,
    version: str,
    *,
    metadata_name: str = "stock-desk",
    metadata_version: str | None = None,
    unrelated_only: bool = False,
    pyproject_payload: bytes | None = None,
    package_payload: bytes = b"__version__ = 'fixture'\n",
    core_metadata: bytes | None = None,
    extra_members: dict[str, bytes] | None = None,
    directory_members: tuple[str, ...] = (),
    directories_after_files: bool = False,
    special_members: tuple[tuple[str, bytes, str], ...] = (),
) -> None:
    package_dist = repo / "dist"
    package_dist.mkdir(exist_ok=True)
    source = package_dist / f"stock_desk-{version}.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        root = f"stock_desk-{version}"
        if unrelated_only:
            add_tar_text(archive, f"{root}/unrelated.txt", "not an sdist payload\n")
            return

        def add_directories() -> None:
            for relative_path in directory_members:
                name = root if not relative_path else f"{root}/{relative_path}"
                add_tar_directory(archive, name)

        if not directories_after_files:
            add_directories()
        add_tar_bytes(
            archive,
            f"{root}/pyproject.toml",
            pyproject_payload
            if pyproject_payload is not None
            else sdist_pyproject_payload(version),
        )
        add_tar_bytes(
            archive,
            f"{root}/PKG-INFO",
            core_metadata
            if core_metadata is not None
            else metadata_payload(
                metadata_name, metadata_version if metadata_version else version
            ).encode(),
        )
        add_tar_bytes(
            archive,
            f"{root}/src/stock_desk/__init__.py",
            package_payload,
        )
        for relative_path, payload in (extra_members or {}).items():
            add_tar_bytes(archive, f"{root}/{relative_path}", payload)
        if directories_after_files:
            add_directories()
        for relative_path, member_type, linkname in special_members:
            member = tarfile.TarInfo(f"{root}/{relative_path}")
            member.type = member_type
            member.linkname = linkname
            archive.addfile(member)


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
    (repo / "pyproject.toml").write_bytes(sdist_pyproject_payload("0.1.0"))
    source_package = repo / "src" / "stock_desk"
    source_package.mkdir(parents=True)
    (source_package / "__init__.py").write_bytes(b"__version__ = 'fixture'\n")
    (repo / "LICENSE").write_bytes(b"fixture license\n")
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


@pytest.mark.parametrize("author_name", ["Cong Bao", "CongBao"])
def test_accepts_strict_github_web_merge_identity(
    release_repo: Path,
    author_name: str,
) -> None:
    create_github_merge_commit(release_repo, author_name=author_name)

    verify_release_module.check_identity(release_repo)


@pytest.mark.parametrize(
    (
        "parent_count",
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
        "subject",
    ),
    [
        (
            1,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            3,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Someone Else",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            "wrong@example.com",
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub Actions",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "github-actions[bot]@users.noreply.github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from OtherOwner/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/0-foundation extra",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #0 from CongBao/phase/0-foundation",
        ),
        (
            2,
            "Cong Bao",
            EXPECTED_IDENTITY[1],
            "GitHub",
            "noreply@github.com",
            "Merge pull request #1 from CongBao/phase/../main",
        ),
    ],
    ids=[
        "single-parent",
        "three-parents",
        "wrong-author-name",
        "wrong-author-email",
        "wrong-committer-name",
        "wrong-committer-email",
        "wrong-owner",
        "malformed-subject",
        "non-positive-pr-number",
        "unsafe-branch",
    ],
)
def test_rejects_noncanonical_github_merge_identity(
    release_repo: Path,
    parent_count: int,
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
    subject: str,
) -> None:
    create_github_merge_commit(
        release_repo,
        parent_count=parent_count,
        author_name=author_name,
        author_email=author_email,
        committer_name=committer_name,
        committer_email=committer_email,
        subject=subject,
    )

    with pytest.raises(ReleaseVerificationError, match="reachable commit identities"):
        verify_release_module.check_identity(release_repo)


def test_ordinary_commits_still_require_the_exact_release_identity(
    release_repo: Path,
) -> None:
    create_github_merge_commit(
        release_repo,
        parent_count=1,
        author_name="Cong Bao",
        committer_name=EXPECTED_IDENTITY[0],
        committer_email=EXPECTED_IDENTITY[1],
        subject="ordinary commit",
    )

    with pytest.raises(ReleaseVerificationError, match="reachable commit identities"):
        verify_release_module.check_identity(release_repo)


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

    assert [call.command for call in runner.calls] == [
        verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command,
        ("make", "release-check"),
    ]


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


@pytest.mark.parametrize("channel", ["alpha", "beta"])
def test_prerelease_tag_uses_unreleased_entry_and_unsigned_release_note(
    release_repo: Path, channel: str
) -> None:
    tag_name = f"v0.1.0-{channel}.1"
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(
        f"# Changelog\n\n## [Unreleased]\n\n- `{tag_name}` delivery preview.\n",
        encoding="utf-8",
    )
    release_note = release_repo / "docs" / "releases" / f"{tag_name}.md"
    release_note.parent.mkdir(parents=True, exist_ok=True)
    release_note.write_text(
        f"# Stock Desk {tag_name}\n\nUnsigned prerelease.\n",
        encoding="utf-8",
    )

    check_changelog(
        release_repo,
        "0.1.0",
        tag_name=tag_name,
    )
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


def test_rejects_record_hash_that_is_not_sha256_urlsafe_base64(
    release_repo: Path,
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        record_overrides={"stock_desk/__init__.py": ("sha256=not+urlsafe", "24")},
    )

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("size", ["not-decimal", "999"])
def test_rejects_record_size_that_is_not_decimal_or_exact(
    release_repo: Path, size: str
) -> None:
    package_payload = b"__version__ = 'fixture'\n"
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        record_overrides={
            "stock_desk/__init__.py": (record_digest(package_payload), size)
        },
    )

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_record_digest_that_does_not_match_archive_bytes(
    release_repo: Path,
) -> None:
    package_payload = b"__version__ = 'fixture'\n"
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        record_overrides={
            "stock_desk/__init__.py": (
                record_digest(b"different content\n"),
                str(len(package_payload)),
            )
        },
    )

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_nonempty_hash_or_size_for_record_itself(release_repo: Path) -> None:
    record_path = "stock_desk-0.1.0.dist-info/RECORD"
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        record_overrides={record_path: (record_digest(b"record"), "6")},
    )

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_artifact_validation_never_invokes_a_subprocess(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")

    def fail_if_called(*_arguments: object, **_options: object) -> None:
        pytest.fail("artifact validation must remain static")

    monkeypatch.setattr(
        subprocess,
        "run",
        fail_if_called,
    )

    check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_invalid_core_metadata_without_executing_artifacts(
    release_repo: Path,
    artifact: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: stock-desk\n"
        b"Version: 0.1.0\n"
        b"Requires-Dist: ???\n\n"
    )
    write_valid_artifacts(release_repo, "0.1.0")
    if artifact == "wheel":
        write_wheel(release_repo, "0.1.0", core_metadata=invalid_metadata)
    else:
        write_sdist(release_repo, "0.1.0", core_metadata=invalid_metadata)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_arguments, **_options: subprocess.CompletedProcess((), 0),
    )

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_different_wheel_and_sdist_core_metadata(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel_metadata = metadata_payload("stock-desk", "0.1.0").replace(
        "\n\n", "\nSummary: wheel payload\n\n"
    )
    source_metadata = metadata_payload("stock-desk", "0.1.0").replace(
        "\n\n", "\nSummary: source payload\n\n"
    )
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(release_repo, "0.1.0", core_metadata=wheel_metadata.encode())
    write_sdist(release_repo, "0.1.0", core_metadata=source_metadata.encode())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_arguments, **_options: subprocess.CompletedProcess((), 0),
    )

    with pytest.raises(ReleaseVerificationError, match="metadata"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_package_source_that_differs_from_the_repository(
    release_repo: Path,
    artifact: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    if artifact == "wheel":
        write_wheel(release_repo, "0.1.0", package_payload=b"untrusted\n")
    else:
        write_sdist(release_repo, "0.1.0", package_payload=b"untrusted\n")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_arguments, **_options: subprocess.CompletedProcess((), 0),
    )

    with pytest.raises(ReleaseVerificationError, match="build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize(
    "extra_path",
    [
        "sitecustomize.py",
        "stock_desk-0.1.0.dist-info/entry_points.txt",
    ],
)
def test_rejects_wheel_members_outside_the_complete_allowlist(
    release_repo: Path, extra_path: str
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        extra_members={extra_path: b"untrusted\n"},
    )

    with pytest.raises(ReleaseVerificationError, match="wheel build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_wheel_license_that_differs_from_the_repository(
    release_repo: Path,
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_wheel(
        release_repo,
        "0.1.0",
        license_payload=b"different license\n",
    )

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


def test_rejects_invalid_pep621_sdist_metadata_without_executing_it(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        pyproject_payload=sdist_pyproject_payload("0.1.0", dependencies="[1]"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_arguments, **_options: subprocess.CompletedProcess((), 0),
    )

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_rejects_backend_path_sdist_without_executing_the_backend(
    release_repo: Path,
) -> None:
    side_effect = release_repo / "backend-was-executed"
    backend = (
        "from pathlib import Path\n"
        f"Path({str(side_effect)!r}).write_text('executed', encoding='utf-8')\n"
    ).encode()
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        pyproject_payload=sdist_pyproject_payload("0.1.0", backend_path="backend"),
        extra_members={
            "backend/hatchling/__init__.py": b"",
            "backend/hatchling/build.py": backend,
        },
    )

    with pytest.raises(ReleaseVerificationError):
        check_build_artifacts(release_repo, "0.1.0")

    assert not side_effect.exists()


def test_rejects_sdist_path_traversal_without_writing_outside_the_archive(
    release_repo: Path,
) -> None:
    side_effect = release_repo.parent / "stock-desk-escape.py"
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        extra_members={"../../stock-desk-escape.py": b"untrusted\n"},
    )

    try:
        with pytest.raises(ReleaseVerificationError, match="source build artifact"):
            check_build_artifacts(release_repo, "0.1.0")
    finally:
        assert not side_effect.exists()


def test_rejects_unbound_regular_sdist_member(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        extra_members={"unbound.py": b"untrusted\n"},
    )

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


def test_accepts_safe_directory_members_in_sdist(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        directory_members=("", "src", "src/stock_desk"),
    )

    check_build_artifacts(release_repo, "0.1.0")


def test_rejects_unsafe_directory_member_in_sdist(release_repo: Path) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        directory_members=("../escape",),
    )

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("directories_after_files", [False, True])
def test_rejects_file_directory_prefix_shadowing_in_sdist(
    release_repo: Path,
    directories_after_files: bool,
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        directory_members=("src/stock_desk/__init__.py/child",),
        directories_after_files=directories_after_files,
    )

    with pytest.raises(ReleaseVerificationError, match="source build artifact"):
        check_build_artifacts(release_repo, "0.1.0")


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_rejects_sdist_symbolic_and_hard_links(
    release_repo: Path, member_type: bytes
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    write_sdist(
        release_repo,
        "0.1.0",
        special_members=(("linked.py", member_type, "src/stock_desk/__init__.py"),),
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


def test_verifier_rechecks_clean_sources_after_static_artifact_validation(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        verify_release_module,
        "check_clean_worktree",
        lambda _repo: events.append("clean"),
    )
    monkeypatch.setattr(
        verify_release_module,
        "check_build_artifacts",
        lambda _repo, _version: events.append("artifacts"),
    )

    def fingerprint(_repo: Path) -> str:
        events.append("fingerprint")
        return "stable"

    verify_release(
        release_repo,
        "0.1.0",
        FakeGateRunner(release_repo),
        fingerprint=fingerprint,
    )

    assert events == ["clean", "fingerprint", "artifacts", "clean", "fingerprint"]


def test_success_runs_timed_gates_and_rechecks_clean_sources(
    release_repo: Path,
) -> None:
    runner = FakeGateRunner(release_repo)

    run_verifier(release_repo, runner)

    assert runner.calls == [
        verify_release_module.PRE_PUBLISH_EVIDENCE_GATE,
        GateCommand(("make", "release-check"), timeout_seconds=1800),
        GateCommand(
            ("pnpm", "e2e"),
            timeout_seconds=600,
            environment=(("STOCK_DESK_E2E_BASE_URL", E2E_BASE_URL),),
        ),
    ]


def _proved_inputs(repo: Path) -> ProvedReleaseInputs:
    return ProvedReleaseInputs(
        proof_path=repo / "proof.json",
        proof_verification_binding_path=repo / "proof-verification-binding.json",
        proof_gh_verification_path=repo / "proof-gh-verification.json",
        artifact_roots={"web-build-manifest": repo / "web-artifact"},
        artifact_attestation_paths={
            "web-build-manifest": repo / "web-attestation.json"
        },
    )


def test_controlled_gh_proof_output_requires_release_runner_and_exact_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    digest = "b" * 64
    evidence = [
        {
            "verificationResult": {
                "statement": {
                    "subject": [{"name": "proof.json", "digest": {"sha256": digest}}]
                }
            }
        }
    ]
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "CongBao/stock-desk",
        "GITHUB_SHA": commit,
        "GITHUB_WORKFLOW": "Release",
    }.items():
        monkeypatch.setenv(name, value)

    verify_release_module._verify_controlled_gh_proof_output(
        evidence, proof_sha256=digest, commit_sha=commit
    )

    monkeypatch.setenv("GITHUB_WORKFLOW", "Untrusted")
    with pytest.raises(ReleaseVerificationError, match="controlled GitHub Release"):
        verify_release_module._verify_controlled_gh_proof_output(
            evidence, proof_sha256=digest, commit_sha=commit
        )

    monkeypatch.setenv("GITHUB_WORKFLOW", "Release")
    with pytest.raises(ReleaseVerificationError, match="does not bind"):
        verify_release_module._verify_controlled_gh_proof_output(
            evidence, proof_sha256="c" * 64, commit_sha=commit
        )


def test_exact_sha_proof_replaces_compatible_source_test_reruns(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(release_repo, "tag", "v0.1.0")
    observed: list[ProvedReleaseInputs] = []
    monkeypatch.setattr(
        verify_release_module,
        "verify_proved_release_inputs",
        lambda _repo, inputs: observed.append(inputs),
    )
    runner = FakeGateRunner(release_repo)
    inputs = _proved_inputs(release_repo)

    verify_release(
        release_repo,
        "0.1.0",
        runner,
        fingerprint=lambda _repo: "stable",
        proved_inputs=inputs,
    )

    assert observed == [inputs]
    assert runner.calls == []


@pytest.mark.parametrize("channel", ["alpha", "beta"])
def test_proved_prerelease_accepts_the_explicit_exact_supported_tag(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    tag_name = f"v0.1.0-{channel}.1"
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(
        f"# Changelog\n\n## [Unreleased]\n\n- `{tag_name}` delivery preview.\n",
        encoding="utf-8",
    )
    release_note = release_repo / "docs" / "releases" / f"{tag_name}.md"
    release_note.parent.mkdir(parents=True, exist_ok=True)
    release_note.write_text(
        f"# Stock Desk {tag_name}\n\nUnsigned prerelease.\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".")
    git(release_repo, "commit", "-q", "-m", "prepare prerelease")
    git(release_repo, "tag", tag_name)
    monkeypatch.setattr(
        verify_release_module,
        "verify_proved_release_inputs",
        lambda _repo, _inputs: None,
    )

    verify_release(
        release_repo,
        "0.1.0",
        FakeGateRunner(release_repo),
        fingerprint=lambda _repo: "stable",
        proved_inputs=_proved_inputs(release_repo),
        tag_name=tag_name,
    )


def test_proved_prerelease_rejects_unsupported_channel(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(release_repo, "tag", "v0.1.0-rc.1")
    monkeypatch.setattr(
        verify_release_module,
        "verify_proved_release_inputs",
        lambda _repo, _inputs: None,
    )
    with pytest.raises(ReleaseVerificationError, match="tag name is not allowed"):
        verify_release(
            release_repo,
            "0.1.0",
            FakeGateRunner(release_repo),
            fingerprint=lambda _repo: "stable",
            proved_inputs=_proved_inputs(release_repo),
            tag_name="v0.1.0-rc.1",
        )


def test_proved_release_rejects_tag_pointing_to_another_commit(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(release_repo, "tag", "v0.1.0")
    (release_repo / "next.txt").write_text("next\n", encoding="utf-8")
    git(release_repo, "add", "next.txt")
    git(release_repo, "commit", "-q", "-m", "next")
    monkeypatch.setattr(
        verify_release_module,
        "verify_proved_release_inputs",
        lambda _repo, _inputs: None,
    )

    with pytest.raises(ReleaseVerificationError, match="tag does not point"):
        verify_release(
            release_repo,
            "0.1.0",
            FakeGateRunner(release_repo),
            fingerprint=lambda _repo: "stable",
            proved_inputs=_proved_inputs(release_repo),
        )


def test_candidate_reuses_proof_without_running_compatible_source_tests(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = release_repo / "test-results" / "release" / "proved.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore proved report")
    inputs = _proved_inputs(release_repo)
    observed: list[ProvedReleaseInputs] = []
    monkeypatch.setattr(
        verify_release_module,
        "verify_proved_release_inputs",
        lambda _repo, value: observed.append(value),
    )
    runner = FakeGateRunner(release_repo)

    verify_candidate(
        release_repo,
        "0.1.0",
        runner,
        report_path=report,
        fingerprint=lambda _repo: "stable",
        fixture_hashes=lambda _repo: {"requirements.yml": "sha256:" + "1" * 64},
        proved_inputs=inputs,
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert observed == [inputs]
    assert runner.calls == []
    assert payload["gates"] == [
        {"command": ["reuse-main-validation-proof"], "status": "passed"}
    ]


def test_candidate_and_final_release_require_complete_requirement_evidence(
    release_repo: Path,
) -> None:
    budget = verify_release_module.RELEASE_EVIDENCE_TIMEOUT_BUDGET
    evidence_gate = GateCommand(
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/check_requirement_coverage.py",
            "--mode",
            "pre-publish",
        ),
        timeout_seconds=budget.outer_gate_timeout_seconds,
    )
    assert budget.collection_timeout_seconds + budget.cleanup_margin_seconds <= (
        evidence_gate.timeout_seconds
    )
    assert evidence_gate in verify_release_module._candidate_gates(
        target_performance=False
    )

    runner = FakeGateRunner(release_repo)
    run_verifier(release_repo, runner)
    assert evidence_gate in runner.calls


def test_candidate_full_python_gate_uses_measured_suite_timeout() -> None:
    gates = verify_release_module._candidate_gates(target_performance=False)
    python_gate = next(gate for gate in gates if gate.command == ("make", "test"))

    assert python_gate.timeout_seconds == 90 * 60
    assert python_gate.timeout_seconds > 60 * 60


@pytest.mark.parametrize(
    "payload",
    [
        b"/home/" + b"real-operator" + b"/stock-desk/data.db",
        b"/".join((b"", b"Users", b"release-user", b"Workspace", b"stock-desk")),
        b"C:\\Users\\" + b"release-user" + b"\\stock-desk\\data.db",
        b"C:\\Users\\" + b"Jane Doe\\stock-desk\\data.db",
        "/".join(("", "Users", "发布用户", "Workspace", "stock-desk")).encode(),
        (b"/home/" + (b"long-profile-" * 7) + b"/stock-desk/data.db"),
        b"/".join((b"", b"Users", b"bao", b"Workspace", b"stock-desk")),
        b"OPENAI_API_" + b"KEY=sk-" + b"A7" * 24,
        b"DEEPSEEK_API_" + b"KEY=sk-" + b"B8" * 24,
        b"DASHSCOPE_API_" + b"KEY=sk-" + b"C9" * 24,
        b"TUSHARE_" + b"TOKEN=" + b"d4" * 24,
    ],
)
def test_release_leak_scanner_rejects_cross_platform_paths_and_provider_tokens(
    payload: bytes,
) -> None:
    scanner = ReleaseLeakScanner(label="fixture")

    with pytest.raises(ReleaseVerificationError, match="release payload"):
        scanner.feed(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"Use OPENAI_API_KEY as the environment variable name.",
        b"TUSHARE_TOKEN=secret",
        b"/home/example/private/session",
        b"/Users/alice/private.db",
        b"/Users/operator/worktree",
        b"/Users/" + b"Bao/synthetic-redaction-fixture",
        b"C:\\Users\\owner\\AppData\\Local",
        b"masked key: sk-a" + "\u2022".encode() * 8 + b"tail",
    ],
)
def test_release_leak_scanner_allows_documentation_and_synthetic_placeholders(
    payload: bytes,
) -> None:
    scanner = ReleaseLeakScanner(label="fixture")
    scanner.feed(payload)
    scanner.finish()


def test_release_leak_scanner_does_not_treat_regex_syntax_as_a_profile() -> None:
    scanner = ReleaseLeakScanner(label="pattern definition")

    scanner.feed(b'r"(?:~|/Users/[^/]+)/Workspace/stock-desk"')
    scanner.feed(b'b"/Users/" + b"release-user" + b"/Workspace"')
    scanner.finish()


def test_windows_evidence_verifier_source_is_release_scan_safe() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "verify_windows_desktop_raw_evidence.py"
    )
    scanner = ReleaseLeakScanner(label=source.name)

    scanner.feed(source.read_bytes())
    scanner.finish()


def test_release_leak_scanner_detects_tokens_split_across_chunks() -> None:
    token = b"OPENAI_API_" + b"KEY=sk-" + b"Q7" * 24
    token_value_start = token.index(b"sk-") + len(b"sk-")
    for split in range(token_value_start + 1, token_value_start + 24):
        scanner = ReleaseLeakScanner(label="split fixture")
        scanner.feed(token[:split])
        with pytest.raises(ReleaseVerificationError, match="release payload"):
            scanner.feed(token[split:])


def test_release_leak_scanner_has_bounded_rss_and_consumes_chunks_once() -> None:
    chunk = b"ordinary public release bytes\n" * 2048
    iterations = 0

    def chunks() -> object:
        nonlocal iterations
        for _ in range(512):
            iterations += 1
            yield chunk

    tracemalloc.start()
    scanner = ReleaseLeakScanner(label="large stream")
    for payload in chunks():
        assert isinstance(payload, bytes)
        scanner.feed(payload)
    scanner.finish()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert iterations == 512
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("scan_kind", ("reachable-blobs", "source-archive"))
def test_streaming_git_scans_kill_blocked_reads_before_the_deadline(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    scan_kind: str,
) -> None:
    class BlockingStdout:
        def __init__(self) -> None:
            self.closed = False
            self._released = Event()

        def read(self, _size: int = -1) -> bytes:
            self._released.wait(5)
            return b""

        def readline(self, _size: int = -1) -> bytes:
            return self.read(_size)

        def close(self) -> None:
            self.closed = True
            self._released.set()

    class BlockingProcess:
        def __init__(self) -> None:
            self.stdout = BlockingStdout()
            self.killed = False
            self.waited = False

        def poll(self) -> int | None:
            return -9 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waited = True
            return -9

    process = BlockingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        verify_release_module,
        "_reachable_object_ids",
        lambda _repo: (b"a" * 40,),
    )

    started = time.monotonic()
    with pytest.raises(ReleaseVerificationError, match="timed out"):
        if scan_kind == "reachable-blobs":
            verify_release_module._scan_reachable_git_blobs(
                release_repo, timeout_seconds=0.05
            )
        else:
            verify_release_module._scan_git_archive(release_repo, timeout_seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert process.killed is True
    assert process.waited is True
    assert process.stdout.closed is True
    assert not any(
        thread.name.startswith("release-payload-reader")
        for thread in enumerate_threads()
    )


def test_final_verifier_reports_initial_and_final_fingerprint_failures(
    release_repo: Path,
) -> None:
    with pytest.raises(ReleaseVerificationError, match="unable to fingerprint"):
        verify_release(
            release_repo,
            "0.1.0",
            FakeGateRunner(release_repo),
            fingerprint=lambda _repo: (_ for _ in ()).throw(OSError("private")),
        )

    calls = 0

    def failing_recheck(_repo: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private")
        return "stable"

    with pytest.raises(ReleaseVerificationError, match="unable to recheck"):
        verify_release(
            release_repo,
            "0.1.0",
            FakeGateRunner(release_repo),
            fingerprint=failing_recheck,
        )


def test_final_verifier_rejects_source_fingerprint_change(
    release_repo: Path,
) -> None:
    fingerprints = iter(("initial", "changed"))

    with pytest.raises(ReleaseVerificationError, match="fingerprint changed"):
        verify_release(
            release_repo,
            "0.1.0",
            FakeGateRunner(release_repo),
            fingerprint=lambda _repo: next(fingerprints),
        )


def test_build_artifact_os_errors_are_wrapped(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_valid_artifacts(release_repo, "0.1.0")
    monkeypatch.setattr(
        verify_release_module,
        "_check_wheel_artifact",
        lambda *_args: (_ for _ in ()).throw(OSError("private")),
    )

    with pytest.raises(ReleaseVerificationError, match="artifact is invalid"):
        check_build_artifacts(release_repo, "0.1.0")


def test_candidate_stops_at_first_failed_gate_and_writes_safe_report(
    release_repo: Path,
    tmp_path: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")
    runner = FakeGateRunner(
        release_repo,
        fail_command=("make", "acceptance-formula"),
    )

    with pytest.raises(ReleaseVerificationError, match="candidate gate failed"):
        verify_candidate(
            release_repo,
            "1.0.0",
            runner,
            report_path=report,
            fingerprint=lambda _repo: "stable-source",
            fixture_hashes=lambda _repo: {
                "tests/fixtures/example.json": "sha256:" + "a" * 64
            },
        )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": "stock-desk-release-candidate-report-v1",
        "mode": "candidate",
        "version": "1.0.0",
        "status": "failed",
        "source_revision": git_output(release_repo, "rev-parse", "HEAD"),
        "source_fingerprint": "stable-source",
        "source_unchanged": True,
        "fixture_hashes": {"tests/fixtures/example.json": "sha256:" + "a" * 64},
        "gates": [
            {
                "command": list(
                    verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command
                ),
                "status": "passed",
            },
            {"command": ["make", "test"], "status": "passed"},
            {"command": ["make", "acceptance"], "status": "passed"},
            {
                "command": ["make", "acceptance-formula"],
                "status": "failed",
            },
        ],
        "failure": {
            "kind": "gate_failed",
            "gate": ["make", "acceptance-formula"],
            "message": "release candidate gate failed",
        },
    }
    assert str(tmp_path) not in report.read_text(encoding="utf-8")


def test_candidate_rejects_gate_source_mutation_before_next_gate(
    release_repo: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")

    @dataclass
    class MutatingRunner:
        calls: list[GateCommand] = field(default_factory=list)

        def run(self, gate: GateCommand) -> None:
            self.calls.append(gate)
            (release_repo / "src" / "stock_desk" / "__init__.py").write_text(
                "mutated during gate\n",
                encoding="utf-8",
            )

    runner = MutatingRunner()

    with pytest.raises(ReleaseVerificationError, match="modified release sources"):
        verify_candidate(
            release_repo,
            "1.0.0",
            runner,
            report_path=report,
            fingerprint=lambda repo: hashlib.sha256(
                (repo / "src" / "stock_desk" / "__init__.py").read_bytes()
            ).hexdigest(),
            fixture_hashes=lambda _repo: {},
        )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["source_unchanged"] is False
    assert payload["gates"] == [
        {
            "command": list(verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command),
            "status": "failed",
        }
    ]
    assert payload["failure"] == {
        "kind": "source_changed",
        "gate": list(verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command),
        "message": "release candidate gate modified release sources",
    }
    assert len(runner.calls) == 1


def test_candidate_rejects_revision_change_with_identical_source(
    release_repo: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")

    class RevisionChangingRunner:
        def run(self, _gate: GateCommand) -> None:
            git(release_repo, "commit", "-q", "--allow-empty", "-m", "changed head")

    with pytest.raises(ReleaseVerificationError, match="modified release sources"):
        verify_candidate(
            release_repo,
            "1.0.0",
            RevisionChangingRunner(),
            report_path=report,
            fingerprint=lambda _repo: "stable",
            fixture_hashes=lambda _repo: {},
        )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["failure"]["kind"] == "source_changed"
    assert payload["source_unchanged"] is False
    assert len(payload["gates"]) == 1


def test_candidate_precheck_failure_still_writes_machine_report(
    release_repo: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")
    (release_repo / "README.md").write_text(
        "dirty before candidate\n", encoding="utf-8"
    )
    runner = FakeGateRunner(release_repo)

    with pytest.raises(ReleaseVerificationError, match="worktree is not clean"):
        verify_candidate(
            release_repo,
            "1.0.0",
            runner,
            report_path=report,
            fingerprint=lambda _repo: "dirty-source",
            fixture_hashes=lambda _repo: {},
        )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["source_unchanged"] is False
    assert payload["gates"] == []
    assert payload["failure"] == {
        "kind": "precheck_failed",
        "gate": None,
        "message": "release candidate precheck failed",
    }
    assert runner.calls == []


def test_candidate_success_report_is_deterministic_and_uses_target_performance(
    release_repo: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")
    runner = FakeGateRunner(release_repo)

    verify_candidate(
        release_repo,
        "1.0.0",
        runner,
        report_path=report,
        target_performance=True,
        fingerprint=lambda _repo: "stable-source",
        fixture_hashes=lambda _repo: {
            "tests/fixtures/example.json": "sha256:" + "b" * 64
        },
    )
    first = report.read_bytes()
    verify_candidate(
        release_repo,
        "1.0.0",
        FakeGateRunner(release_repo),
        report_path=report,
        target_performance=True,
        fingerprint=lambda _repo: "stable-source",
        fixture_hashes=lambda _repo: {
            "tests/fixtures/example.json": "sha256:" + "b" * 64
        },
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert report.read_bytes() == first
    assert payload["status"] == "passed"
    assert payload["failure"] is None
    commands = [tuple(item["command"]) for item in payload["gates"]]
    assert commands[0] == verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command
    assert commands.count(verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command) == 1
    assert commands == [
        verify_release_module.PRE_PUBLISH_EVIDENCE_GATE.command,
        ("make", "test"),
        ("make", "acceptance"),
        ("make", "acceptance-formula"),
        ("make", "acceptance-backtest"),
        ("make", "acceptance-analysis"),
        ("make", "acceptance-domain-contracts"),
        ("make", "acceptance-full-journey"),
        ("make", "performance-regressions"),
        ("make", "performance-target"),
        ("make", "e2e-foundation"),
        ("make", "e2e-market"),
        ("make", "e2e-formula"),
        ("make", "e2e-backtest"),
        ("make", "e2e-analysis"),
        ("make", "e2e-task-center"),
        ("make", "e2e-accessibility"),
        ("make", "lint"),
        ("make", "typecheck"),
        ("make", "security"),
        ("uv", "run", "--frozen", "python", "scripts/verify_docs.py"),
        ("make", "public-tree"),
    ]


def test_candidate_cli_delegates_with_machine_report_and_target_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def capture(
        repo: Path,
        version: str,
        runner: object,
        *,
        report_path: Path,
        target_performance: bool,
    ) -> None:
        observed.update(
            repo=repo,
            version=version,
            runner=runner,
            report_path=report_path,
            target_performance=target_performance,
        )

    monkeypatch.setattr(verify_release_module, "verify_candidate", capture)

    result = verify_release_module.main(
        [
            "1.0.0",
            "--candidate",
            "--target-performance",
            "--report",
            "test-results/release/ci-candidate.json",
        ]
    )

    expected_repo = Path(verify_release_module.__file__).resolve().parent.parent
    assert result == 0
    assert observed["repo"] == expected_repo
    assert observed["version"] == "1.0.0"
    assert isinstance(observed["runner"], SubprocessGateRunner)
    assert observed["report_path"] == (
        expected_repo / "test-results/release/ci-candidate.json"
    )
    assert observed["target_performance"] is True


def test_candidate_fixture_hashes_bind_only_tracked_regular_files(
    release_repo: Path,
) -> None:
    fixture = release_repo / "tests" / "fixtures" / "sample.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"fixture":true}\n', encoding="utf-8")
    requirements = release_repo / "tests" / "acceptance" / "requirements.yml"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("requirements: []\n", encoding="utf-8")
    git(release_repo, "add", "tests")
    git(release_repo, "commit", "-q", "-m", "add release fixtures")

    hashes = verify_release_module.compute_fixture_hashes(release_repo)

    assert hashes == {
        "tests/acceptance/requirements.yml": (
            "sha256:" + hashlib.sha256(requirements.read_bytes()).hexdigest()
        ),
        "tests/fixtures/sample.json": (
            "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
        ),
    }


def test_candidate_fixture_hashes_reject_missing_unsafe_and_symlink_inputs(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ReleaseVerificationError, match="fixtures are missing"):
        verify_release_module.compute_fixture_hashes(release_repo)

    monkeypatch.setattr(
        verify_release_module,
        "_git_paths",
        lambda *_args: ["../private-fixture"],
    )
    with pytest.raises(ReleaseVerificationError, match="path is invalid"):
        verify_release_module.compute_fixture_hashes(release_repo)

    fixture = release_repo / "tests" / "fixtures" / "linked.json"
    fixture.parent.mkdir(parents=True)
    fixture.symlink_to(release_repo / "README.md")
    monkeypatch.setattr(
        verify_release_module,
        "_git_paths",
        lambda *_args: ["tests/fixtures/linked.json"],
    )
    with pytest.raises(ReleaseVerificationError, match="regular file"):
        verify_release_module.compute_fixture_hashes(release_repo)


@pytest.mark.parametrize(
    "report_path",
    [
        Path("outside.json"),
        Path("test-results/release/.hidden.json"),
        Path("test-results/nested/candidate.json"),
    ],
)
def test_candidate_report_rejects_paths_outside_its_fixed_directory(
    release_repo: Path,
    tmp_path: Path,
    report_path: Path,
) -> None:
    requested = (
        tmp_path / report_path
        if report_path == Path("outside.json")
        else release_repo / report_path
    )

    with pytest.raises(ReleaseVerificationError, match="report path is invalid"):
        verify_release_module._write_candidate_report(
            release_repo,
            requested,
            {"status": "failed"},
        )


def test_candidate_report_rejects_symlink_parent_and_temporary_collision(
    release_repo: Path,
    tmp_path: Path,
) -> None:
    (release_repo / "test-results").symlink_to(tmp_path, target_is_directory=True)
    target = release_repo / "test-results" / "release" / "candidate.json"
    with pytest.raises(ReleaseVerificationError, match="report path is invalid"):
        verify_release_module._write_candidate_report(
            release_repo, target, {"status": "failed"}
        )
    (release_repo / "test-results").unlink()
    target.parent.mkdir(parents=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text("occupied", encoding="utf-8")
    with pytest.raises(ReleaseVerificationError, match="temporary path is unsafe"):
        verify_release_module._write_candidate_report(
            release_repo, target, {"status": "failed"}
        )


def test_candidate_initialization_and_public_history_fail_safely(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")

    with pytest.raises(ReleaseVerificationError, match="initialize"):
        verify_candidate(
            release_repo,
            "1.0.0",
            FakeGateRunner(release_repo),
            report_path=report,
            fingerprint=lambda _repo: (_ for _ in ()).throw(OSError("private")),
            fixture_hashes=lambda _repo: {},
        )

    monkeypatch.setattr(
        verify_release_module,
        "check_public_history",
        lambda _repo: (_ for _ in ()).throw(
            ReleaseVerificationError("private history detail")
        ),
    )
    with pytest.raises(ReleaseVerificationError, match="private history detail"):
        verify_candidate(
            release_repo,
            "1.0.0",
            FakeGateRunner(release_repo),
            report_path=report,
            fingerprint=lambda _repo: "stable",
            fixture_hashes=lambda _repo: {},
        )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["failure"]["message"] == "release candidate precheck failed"
    assert payload["source_unchanged"] is True
    assert "private" not in report.read_text(encoding="utf-8")


def test_candidate_preserves_safe_domain_initialization_errors(
    release_repo: Path,
) -> None:
    with pytest.raises(ReleaseVerificationError, match="fixture inventory rejected"):
        verify_candidate(
            release_repo,
            "1.0.0",
            FakeGateRunner(release_repo),
            report_path=release_repo / "test-results/release/candidate.json",
            fingerprint=lambda _repo: "stable",
            fixture_hashes=lambda _repo: (_ for _ in ()).throw(
                ReleaseVerificationError("fixture inventory rejected")
            ),
        )


@pytest.mark.parametrize(
    "gate_error",
    [OSError("runner missing"), subprocess.TimeoutExpired(("make", "test"), 1)],
)
def test_candidate_reports_os_and_timeout_gate_failures_without_details(
    release_repo: Path,
    gate_error: BaseException,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")

    class ErrorRunner:
        def run(self, _gate: GateCommand) -> None:
            raise gate_error

    with pytest.raises(ReleaseVerificationError, match="candidate gate failed"):
        verify_candidate(
            release_repo,
            "1.0.0",
            ErrorRunner(),
            report_path=report,
            fingerprint=lambda _repo: "stable",
            fixture_hashes=lambda _repo: {},
        )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["failure"]["kind"] == "gate_failed"
    assert "runner missing" not in report.read_text(encoding="utf-8")


def test_candidate_cli_rejects_candidate_only_options_without_candidate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert verify_release_module.main(["1.0.0", "--target-performance"]) == 1
    assert "candidate report options require --candidate" in capsys.readouterr().err


def test_candidate_rejects_invalid_version_before_running_gates(
    release_repo: Path,
) -> None:
    runner = FakeGateRunner(release_repo)

    with pytest.raises(ReleaseVerificationError, match="stable numeric version"):
        verify_candidate(
            release_repo,
            "1.0.0-rc1",
            runner,
            report_path=release_repo / "test-results/release/candidate.json",
        )

    assert runner.calls == []


def test_candidate_report_rejects_directory_target_and_write_failure(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = release_repo / "test-results" / "release" / "candidate.json"
    target.mkdir(parents=True)
    with pytest.raises(ReleaseVerificationError, match="report path is invalid"):
        verify_release_module._write_candidate_report(
            release_repo, target, {"status": "failed"}
        )
    target.rmdir()

    monkeypatch.setattr(
        verify_release_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("private write detail")),
    )
    with pytest.raises(ReleaseVerificationError, match="unable to write"):
        verify_release_module._write_candidate_report(
            release_repo, target, {"status": "failed"}
        )
    assert not tuple(target.parent.glob("*.tmp"))


def test_git_helpers_wrap_os_failures_without_leaking_details(
    release_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verify_release_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private git detail")),
    )

    with pytest.raises(ReleaseVerificationError, match="inspect the Git repository"):
        verify_release_module._git(release_repo, "status")
    with pytest.raises(ReleaseVerificationError, match="inspect Git paths"):
        verify_release_module._git_paths(release_repo, "ls-files", "-z")


def test_candidate_default_uses_reference_performance_gate(
    release_repo: Path,
) -> None:
    report = release_repo / "test-results" / "release" / "candidate.json"
    gitignore = release_repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "test-results/\n",
        encoding="utf-8",
    )
    git(release_repo, "add", ".gitignore")
    git(release_repo, "commit", "-q", "-m", "ignore candidate reports")

    verify_candidate(
        release_repo,
        "1.0.0",
        FakeGateRunner(release_repo),
        report_path=report,
        fingerprint=lambda _repo: "stable",
        fixture_hashes=lambda _repo: {},
    )

    commands = [
        tuple(item["command"])
        for item in json.loads(report.read_text(encoding="utf-8"))["gates"]
    ]
    assert ("make", "performance") in commands
    assert ("make", "performance-target") not in commands


def test_legacy_cli_delegates_to_final_verifier(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, str, object]] = []
    monkeypatch.setattr(
        verify_release_module,
        "verify_release",
        lambda repo, version, runner: calls.append((repo, version, runner)),
    )

    assert verify_release_module.main(["1.0.0"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == "1.0.0"
    assert isinstance(calls[0][2], SubprocessGateRunner)
    assert "Release verification passed for 1.0.0." in capsys.readouterr().out
