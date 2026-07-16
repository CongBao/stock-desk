from pathlib import Path

import pytest

from stock_desk import windows_acl


ALLOWED = frozenset({"S-1-5-18", "S-1-5-21-42", "S-1-5-32-544"})


def _acl(*entries: windows_acl._AclEntry, protected: bool = True) -> windows_acl._Acl:
    return windows_acl._Acl(protected=protected, entries=entries)


def _entry(
    sid: str, *, mask: int = 0x001F01FF, flags: int = 0x03, ace_type: int = 0
) -> windows_acl._AclEntry:
    return windows_acl._AclEntry(
        sid=sid,
        mask=mask,
        flags=flags,
        ace_type=ace_type,
    )


def test_exact_protected_directory_dacl_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        windows_acl,
        "_read_private_dacl",
        lambda _path: _acl(*(_entry(sid) for sid in sorted(ALLOWED))),
    )

    windows_acl._verify_private_dacl(Path("private"), ALLOWED, directory=True)


@pytest.mark.parametrize(
    "acl",
    [
        _acl(*(_entry(sid) for sid in sorted(ALLOWED)), protected=False),
        _acl(*(_entry(sid) for sid in sorted(ALLOWED)), _entry("S-1-1-0")),
        _acl(*(_entry(sid) for sid in sorted(ALLOWED)), _entry("S-1-5-18")),
        _acl(
            _entry("S-1-5-18", mask=0x00120089),
            _entry("S-1-5-21-42"),
            _entry("S-1-5-32-544"),
        ),
        _acl(
            _entry("S-1-5-18", ace_type=1),
            _entry("S-1-5-21-42"),
            _entry("S-1-5-32-544"),
        ),
    ],
)
def test_inexact_private_dacl_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    acl: windows_acl._Acl,
) -> None:
    monkeypatch.setattr(windows_acl, "_read_private_dacl", lambda _path: acl)

    with pytest.raises(windows_acl.WindowsAclError):
        windows_acl._verify_private_dacl(Path("private"), ALLOWED, directory=True)
