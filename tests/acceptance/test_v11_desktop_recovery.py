from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_packaged_desktop_recovers_from_startup_and_runtime_sidecar_failures() -> None:
    source = (ROOT / "src-tauri" / "src" / "app.rs").read_text(encoding="utf-8")

    assert "MAX_CONSECUTIVE_HEALTH_FAILURES: u8 = 3" in source
    assert "HEALTH_CHECK_INTERVAL: Duration = Duration::from_secs(5)" in source
    assert "ensure_user_data_root" in source
    assert "StartupFailure::PermissionDenied" in source
    assert "StartupFailure::SidecarUnavailable" in source
    assert "monitor_ready_health(app, generation, authority).await" in source
    assert '"version_mismatch"' in source
    assert '"restart_limit_reached"' in source


def test_starting_or_recovery_exit_cleans_resources_without_a_ready_session() -> None:
    source = (ROOT / "src-tauri" / "src" / "exit.rs").read_text(encoding="utf-8")
    non_ready_branch = source.split("if !runtime.is_ready()", maxsplit=1)[1].split(
        "let session = runtime.ready_session()", maxsplit=1
    )[0]

    assert "controller.confirm_without_ready_session(&app)" in non_ready_branch
    assert "runtime.terminate_non_ready_for_exit()" in non_ready_branch
    assert "app.exit(0)" in non_ready_branch
    assert "runtime.ready_session" not in non_ready_branch

    runtime = (ROOT / "src-tauri" / "src" / "app.rs").read_text(encoding="utf-8")
    terminate = runtime.split("pub(crate) fn terminate_non_ready_for_exit", maxsplit=1)[
        1
    ].split("pub(crate) fn terminate_generation_for_exit", maxsplit=1)[0]
    assert 'reason: "exit_committed"' in terminate
    assert "inner.slot.child.take()" in terminate
    assert "inner.slot.job.take()" in terminate
