from __future__ import annotations

import os
from pathlib import Path

import pytest

from stock_desk.desktop import _restrict_owner_access


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="requires the Windows ACL implementation"
)


@pytest.mark.parametrize(
    ("relative", "directory"),
    [
        (Path("directory with spaces") / "owner's 数据", True),
        (Path("file with spaces") / "owner's 记录.txt", False),
    ],
)
def test_windows_runtime_acl_executes_for_untrusted_path_characters(
    tmp_path: Path,
    relative: Path,
    directory: bool,
) -> None:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        target.mkdir()
    else:
        target.write_text("private\n", encoding="utf-8")

    _restrict_owner_access(target, directory=directory)
