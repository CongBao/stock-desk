from pathlib import Path
import signal
import sys

import pytest

from scripts import e2e_dev


def test_main_removes_temporary_profile_when_seed_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "seed-failure-profile"
    profile.mkdir()

    def fail_seed(data_dir: Path) -> None:
        assert data_dir == profile.resolve()
        (data_dir / "partial.db").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(sys, "argv", ["e2e_dev.py"])
    monkeypatch.setattr(e2e_dev.tempfile, "mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(e2e_dev, "_seed", fail_seed)
    monkeypatch.setattr(
        e2e_dev,
        "supervise",
        lambda *_args, **_kwargs: pytest.fail("services must not start"),
    )

    with pytest.raises(RuntimeError, match="injected seed failure"):
        e2e_dev.main()

    assert not profile.exists()


def test_main_honors_sigterm_received_during_seed_and_removes_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "sigterm-profile"
    profile.mkdir()

    def interrupt_seed(data_dir: Path) -> None:
        assert data_dir == profile.resolve()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    def stopped_supervisor(
        _commands: object,
        *,
        requested_signal: object,
    ) -> int:
        assert callable(requested_signal)
        assert requested_signal() == signal.SIGTERM
        return 128 + signal.SIGTERM

    monkeypatch.setattr(sys, "argv", ["e2e_dev.py"])
    monkeypatch.setattr(e2e_dev.tempfile, "mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(e2e_dev, "_seed", interrupt_seed)
    monkeypatch.setattr(e2e_dev, "supervise", stopped_supervisor)

    assert e2e_dev.main() == 128 + signal.SIGTERM
    assert not profile.exists()
