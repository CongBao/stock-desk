from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from scripts import main_validation_proof
from scripts.verify_ci_cache_policy import verify_workflow_cache_policy


ROOT = Path(__file__).resolve().parents[2]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _commands(job: dict[str, object]) -> str:
    steps = job["steps"]
    assert isinstance(steps, list)
    return "\n".join(
        str(step.get("run", "")) for step in steps if isinstance(step, dict)
    )


def test_artifact_impact_always_builds_a_and_main_alone_builds_b() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    builder_a = jobs["windows-desktop-builder-a"]
    builder_b = jobs["windows-desktop-builder-b"]
    compare = jobs["windows-desktop-compare"]
    assert isinstance(builder_a, dict) and isinstance(builder_b, dict)
    assert isinstance(compare, dict)
    assert builder_a["runs-on"] == builder_b["runs-on"] == "windows-2025"
    assert "artifact-proof" in str(builder_a["if"])
    assert "github.event_name == 'push'" not in str(builder_a["if"])
    assert "github.event_name == 'push'" in str(builder_b["if"])
    assert compare["runs-on"] == "windows-2025"
    assert "github.event_name == 'push'" in str(compare["if"])
    assert set(compare["needs"]) == {
        "impact",
        "windows-desktop-builder-a",
        "windows-desktop-builder-b",
    }


def test_windows_workflow_models_the_pinned_direct_file_nsis_template() -> None:
    template = (ROOT / "packaging" / "nsis" / "installer.nsi").read_text(
        encoding="utf-8"
    )
    assert 'File "${MAINBINARYSRCPATH}"' in template
    assert "app.7z" not in template.casefold()
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    builder_a = jobs["windows-desktop-builder-a"]
    builder_b = jobs["windows-desktop-builder-b"]
    assert isinstance(builder_a, dict) and isinstance(builder_b, dict)
    commands = _commands(builder_a) + _commands(builder_b)
    assert "direct NSIS application payload identity is invalid" in commands
    assert "$desktopHost" in commands
    assert "$host =" not in commands.casefold()
    assert "stock-desk-sidecar.exe" in commands
    assert "uninstall.exe" in commands
    assert "app.7z" not in commands.casefold()


def test_builders_use_exact_source_frozen_inputs_and_preserve_acl_contracts() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    a = jobs["windows-desktop-builder-a"]
    b = jobs["windows-desktop-builder-b"]
    assert isinstance(a, dict) and isinstance(b, dict)
    a_commands = _commands(a)
    both = a_commands + _commands(b)
    assert both.count("scripts/build_windows_desktop.py") == 2
    assert both.count("uv sync --frozen --all-groups --extra providers") == 2
    assert both.count("pnpm install --frozen-lockfile") == 2
    assert both.count("rustup target add x86_64-pc-windows-msvc") == 2
    assert "tests/integration/test_windows_runtime_acl.py" in a_commands
    assert "tests/unit/storage/test_backup.py -k restore_journal" in a_commands
    assert "make e2e" not in both
    assert "tests/unit tests/integration" not in both
    assert "git rev-parse HEAD" in both and "$env:SOURCE_SHA" in both
    assert both.count("Assert-SafeArchive") == 4
    assert both.count("7z l -slt -ba") == 2
    assert "scripts/verify_windows_desktop_bundle.py" in both
    assert "stock-desk-sidecar.exe" in both
    assert "app(?:-.+)?\\.7z" not in both
    assert "nested payload" not in both


def test_windows_cache_and_cleanup_boundaries_fail_closed() -> None:
    assert verify_workflow_cache_policy([CI_PATH]) > 0
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    for job_name in (
        "windows-desktop-builder-a",
        "windows-desktop-builder-b",
        "windows-desktop-compare",
    ):
        job = jobs[job_name]
        assert isinstance(job, dict)
        commands = _commands(job)
        assert "scripts/clean_build_artifacts.py" in commands
        assert "Stock Desk\\v1.1" in commands
        assert "Join-Path $env:LOCALAPPDATA 'stock-desk'" not in commands
        steps = job["steps"]
        assert isinstance(steps, list)
        names = [str(step.get("name")) for step in steps if isinstance(step, dict)]
        diagnostic = next(i for i, name in enumerate(names) if "diagnostics" in name)
        cleanup = next(i for i, name in enumerate(names) if name.startswith("Clean"))
        assert diagnostic < cleanup


def test_comparison_promotes_only_a_and_main_proof_attests_both_identities() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    compare = jobs["windows-desktop-compare"]
    proof = jobs["validation-proof"]
    assert isinstance(compare, dict) and isinstance(proof, dict)
    commands = _commands(compare)
    assert "python -m scripts.compare_windows_payloads" in commands
    assert "function Get-CompleteCandidate" in commands
    assert "Get-ChildItem $downloadRoot -Recurse -File" in commands
    assert "[PSCustomObject]@{ Manifest=$manifest.FullName" in commands
    assert "candidate root is invalid" in commands
    assert "complete=$($complete.Count)" in commands
    assert "Copy-Item $left.Installer" in commands
    assert "Copy-Item $right.Installer" not in commands
    assert "windows-desktop-alpha-candidate-manifest.json" in commands
    assert "windows-payload-comparison-manifest.json" in commands
    assert "$left = Get-CompleteCandidate (Join-Path $root 'a') 'left'" in commands
    assert "$right = Get-CompleteCandidate (Join-Path $root 'b') 'right'" in commands
    assert "a\\windows-desktop-bundle.json" not in commands
    assert "b\\windows-desktop-bundle.json" not in commands
    assert "manifest-binding.json" in commands
    assert "create_attestation_binding" in commands
    assert 'Path(r"__EVIDENCE__")' in commands
    assert 'Path(r"__PROMOTED__")' in commands
    for critical_input in (
        "packaging/stock-desk-sidecar.spec",
        "scripts/build_windows_desktop.py",
        "scripts/verify_windows_desktop_bundle.py",
        "scripts/compare_windows_payloads.py",
        "src-tauri/tauri.conf.json",
        "src-tauri/tauri.windows.conf.json",
        "src-tauri/Cargo.toml",
        "packaging/nsis/installer.nsi",
    ):
        assert critical_input in commands
    assert set(proof["needs"]) >= {
        "windows-desktop-builder-a",
        "windows-desktop-builder-b",
        "windows-desktop-compare",
    }
    proof_commands = _commands(proof)
    assert "windows-payload-comparison-manifest=" in proof_commands
    assert "windows-desktop-alpha-candidate-manifest=" in proof_commands
    proof_steps = proof["steps"]
    assert isinstance(proof_steps, list)
    names = [str(step.get("name")) for step in proof_steps if isinstance(step, dict)]
    comparison_download = names.index(
        "Download Windows payload comparison manifest outside the worktree"
    )
    generation = names.index("Generate exact validation proof")
    assert comparison_download < generation
    download = proof_steps[comparison_download]
    assert isinstance(download, dict)
    settings = download["with"]
    assert isinstance(settings, dict)
    assert settings["name"] == "windows-payload-comparison-manifest"
    policies = main_validation_proof.EVIDENCE_POLICIES
    assert policies["windows-payload-comparison"].job_id == "windows-desktop-compare"
    assert policies["windows-alpha-candidate"].job_id == "windows-desktop-compare"
    assert {
        "packaging/stock-desk-sidecar.spec",
        "scripts/build_windows_desktop.py",
        "scripts/verify_windows_desktop_bundle.py",
        "scripts/compare_windows_payloads.py",
        "packaging/nsis/installer.nsi",
        "src-tauri/Cargo.lock",
        "src-tauri/Cargo.toml",
        "src-tauri/tauri.windows.conf.json",
    } <= set(main_validation_proof.CRITICAL_INPUTS)


def test_rust_quality_gate_is_exact_sha_risk_selected_and_proved() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    rust = jobs["rust-quality"]
    proof = jobs["validation-proof"]
    assert isinstance(rust, dict) and isinstance(proof, dict)
    assert rust["runs-on"] == "windows-2025"
    assert "required_jobs" in str(rust["if"])
    assert "rust" in str(rust["if"])
    commands = _commands(rust)
    assert "git rev-parse HEAD" in commands
    assert "$env:SOURCE_SHA" in commands
    assert "stock-desk-sidecar-x86_64-pc-windows-msvc.exe" in commands
    assert "[IO.File]::WriteAllBytes($stub, [byte[]](0x4d, 0x5a))" in commands
    assert "Remove-Item -LiteralPath $stub" in commands
    assert "cargo fmt --manifest-path src-tauri/Cargo.toml -- --check" in commands
    assert (
        "cargo clippy --locked --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings"
        in commands
    )
    assert "cargo test --locked --manifest-path src-tauri/Cargo.toml" in commands
    assert "rust-quality" in proof["needs"]
    assert (
        "Rust desktop quality and tests"
        in main_validation_proof.WORKFLOW_POLICIES["CI"].required_jobs
    )
