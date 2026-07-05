from __future__ import annotations

from collections.abc import Sequence
import importlib
from pathlib import Path
from types import ModuleType

import pytest


def _entrypoint() -> ModuleType:
    try:
        return importlib.import_module("stock_desk.runtime_entrypoint")
    except ModuleNotFoundError:
        pytest.fail("stock_desk.runtime_entrypoint is missing")


def test_identity_uses_explicit_nonroot_values() -> None:
    entrypoint = _entrypoint()

    identity = entrypoint._select_identity(
        {"STOCK_DESK_UID": "2001", "STOCK_DESK_GID": "2002"},
        owner_uid=501,
        owner_gid=20,
    )

    assert identity == (2001, 2002)


def test_identity_uses_existing_nonroot_bind_owner() -> None:
    entrypoint = _entrypoint()

    identity = entrypoint._select_identity({}, owner_uid=501, owner_gid=20)

    assert identity == (501, 20)


def test_identity_uses_safe_fallback_for_root_owned_fresh_bind() -> None:
    entrypoint = _entrypoint()

    identity = entrypoint._select_identity({}, owner_uid=0, owner_gid=0)

    assert identity == (10001, 10001)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"STOCK_DESK_UID": "0"}, "STOCK_DESK_UID"),
        ({"STOCK_DESK_GID": "0"}, "STOCK_DESK_GID"),
        ({"STOCK_DESK_UID": "not-an-id"}, "STOCK_DESK_UID"),
        ({"STOCK_DESK_GID": "-1"}, "STOCK_DESK_GID"),
    ],
)
def test_identity_rejects_root_or_invalid_explicit_values(
    environment: dict[str, str],
    message: str,
) -> None:
    entrypoint = _entrypoint()

    with pytest.raises(RuntimeError, match=message):
        entrypoint._select_identity(environment, owner_uid=0, owner_gid=0)


def test_privilege_drop_clears_groups_before_gid_and_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _entrypoint()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        entrypoint.os,
        "setgroups",
        lambda groups: calls.append(("groups", groups)),
    )
    monkeypatch.setattr(
        entrypoint.os,
        "setgid",
        lambda gid: calls.append(("gid", gid)),
    )
    monkeypatch.setattr(
        entrypoint.os,
        "setuid",
        lambda uid: calls.append(("uid", uid)),
    )

    entrypoint._drop_privileges(2001, 2002)

    assert calls == [("groups", []), ("gid", 2002), ("uid", 2001)]


def test_data_preparation_does_not_follow_symlinks_outside_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entrypoint = _entrypoint()
    data_dir = tmp_path / "data"
    nested = data_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "database.db").write_text("database", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (data_dir / "outside-link").symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    changed: list[Path] = []

    def record_chown(
        path: str | bytes | int,
        _uid: int,
        _gid: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        assert follow_symlinks is False
        assert isinstance(path, (str, bytes))
        changed.append(Path(path))

    monkeypatch.setattr(entrypoint.os, "chown", record_chown)

    entrypoint._prepare_data_tree(data_dir, 2001, 2002)

    assert outside not in changed
    assert data_dir in changed
    assert nested / "database.db" in changed


def test_nonroot_runtime_requires_writable_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entrypoint = _entrypoint()
    monkeypatch.setattr(entrypoint.os, "access", lambda _path, _mode: False)

    with pytest.raises(RuntimeError, match="writable"):
        entrypoint._verify_nonroot_runtime(
            tmp_path,
            current_uid=10001,
            current_gid=10001,
            environment={},
        )


def test_runtime_requires_a_command() -> None:
    entrypoint = _entrypoint()

    with pytest.raises(RuntimeError, match="command"):
        entrypoint._validate_command(())


def test_runtime_accepts_a_nonempty_command() -> None:
    entrypoint = _entrypoint()

    command = entrypoint._validate_command(("python", "-m", "worker"))

    assert command == ("python", "-m", "worker")
    assert isinstance(command, Sequence)
