from __future__ import annotations

from contextlib import contextmanager
import ctypes
import inspect
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from typing import Any, BinaryIO, Callable, Iterator, cast

import pytest

import scripts.secure_artifact_snapshot as secure_snapshot
from scripts.secure_artifact_snapshot import (
    SecureArtifactSnapshotError,
    SnapshotLimits,
    main,
    snapshot_artifacts,
    prepare_private_directory,
    verify_private_directory,
)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "mutable-build"
    (source / "app" / "resources").mkdir(parents=True)
    (source / "app" / "stock-desk-desktop.exe").write_bytes(b"desktop\n")
    (source / "app" / "resources" / "sidecar.exe").write_bytes(b"sidecar\n")
    (source / "stock-desk.nsi").write_text("OutFile stock-desk.exe\n", encoding="utf-8")
    return source


def test_snapshot_copies_selected_tree_once_with_stable_identity_and_private_modes(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "private-snapshot"

    result = snapshot_artifacts(
        source.resolve(), ["stock-desk.nsi", "app"], destination.resolve()
    )

    assert result.root == destination.resolve()
    assert result.file_count == 3
    assert result.total_size == sum(item.size for item in result.files)
    assert [item.path for item in result.files] == [
        "app/resources/sidecar.exe",
        "app/stock-desk-desktop.exe",
        "stock-desk.nsi",
    ]
    assert len(result.snapshot_sha256) == 64
    assert result.summary()["schema"] == "stock-desk-secure-artifact-snapshot-v1"
    assert "mutable-build" not in json.dumps(result.summary())
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    assert stat.S_IMODE((destination / "app").stat().st_mode) == 0o500
    assert (
        stat.S_IMODE((destination / "app" / "stock-desk-desktop.exe").stat().st_mode)
        == 0o400
    )

    (source / "app" / "stock-desk-desktop.exe").write_bytes(b"mutated\n")
    assert (destination / "app" / "stock-desk-desktop.exe").read_bytes() == b"desktop\n"


def test_snapshot_digest_and_file_order_do_not_depend_on_entry_order(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path).resolve()

    first = snapshot_artifacts(
        source, ["app", "stock-desk.nsi"], (tmp_path / "one").resolve()
    )
    second = snapshot_artifacts(
        source, ["stock-desk.nsi", "app"], (tmp_path / "two").resolve()
    )

    assert first.files == second.files
    assert first.snapshot_sha256 == second.snapshot_sha256


def test_deep_toolchain_parents_are_all_owner_only(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()
    deep = source / "toolchain" / "Plugins" / "x86-unicode" / "additional"
    deep.mkdir(parents=True)
    (deep / "nsis_tauri_utils.dll").write_bytes(b"plugin")
    destination = (tmp_path / "deep-snapshot").resolve()

    snapshot_artifacts(source, ["toolchain"], destination)

    current = destination
    for component in ("toolchain", "Plugins", "x86-unicode", "additional"):
        current /= component
        assert stat.S_IMODE(current.stat().st_mode) == 0o500
    assert (deep / "nsis_tauri_utils.dll").read_bytes() == (
        destination
        / "toolchain"
        / "Plugins"
        / "x86-unicode"
        / "additional"
        / "nsis_tauri_utils.dll"
    ).read_bytes()


def test_cli_emits_path_free_canonical_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "snapshot").resolve()

    assert (
        main(
            [
                "--source-root",
                str(source),
                "--destination",
                str(destination),
                "--entry",
                "stock-desk.nsi",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["file_count"] == 1
    assert str(source) not in output
    assert str(destination) not in output
    assert output == json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n"


def test_prepare_verify_private_directory_api_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    direct = (tmp_path / "direct-private").resolve()
    assert prepare_private_directory(direct) == direct
    assert stat.S_IMODE(direct.stat().st_mode) == 0o700
    assert verify_private_directory(direct) == direct

    cli = (tmp_path / "cli-private").resolve()
    assert main(["--prepare-private-directory", str(cli)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "stock-desk-private-directory-v1",
        "status": "created",
    }
    assert main(["--verify-private-directory", str(cli)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "stock-desk-private-directory-v1",
        "status": "verified",
    }


def test_private_directory_api_rejects_existing_symlink_and_weak_permissions(
    tmp_path: Path,
) -> None:
    existing = (tmp_path / "existing-private").resolve()
    existing.mkdir(mode=0o700)
    with pytest.raises(SecureArtifactSnapshotError, match="must not already exist"):
        prepare_private_directory(existing)

    linked = tmp_path / "linked-private"
    linked.symlink_to(existing, target_is_directory=True)
    with pytest.raises(SecureArtifactSnapshotError, match="unsafe component|reparse"):
        verify_private_directory(linked.absolute())

    existing.chmod(0o755)
    with pytest.raises(SecureArtifactSnapshotError, match="mode 0700"):
        verify_private_directory(existing)


@pytest.mark.skipif(os.name == "nt", reason="POSIX anchored object test")
def test_private_directory_rename_swap_fails_without_deleting_replacement(
    tmp_path: Path,
) -> None:
    destination = (tmp_path / "private").resolve()
    moved = (tmp_path / "moved-private").resolve()

    with pytest.raises(SecureArtifactSnapshotError, match="replaced"):
        with secure_snapshot._create_private_directory(destination):
            destination.rename(moved)
            destination.mkdir(mode=0o700)
            (destination / "replacement.txt").write_text("preserve", encoding="utf-8")

    assert (destination / "replacement.txt").read_text(encoding="utf-8") == "preserve"
    assert moved.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX failure-injection test")
def test_private_directory_post_create_failure_rolls_back_exact_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = (tmp_path / "private").resolve()
    original = os.fchmod
    calls = 0

    def fail_once(descriptor: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected permission failure")
        original(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", fail_once)
    with pytest.raises(SecureArtifactSnapshotError, match="could not be prepared"):
        prepare_private_directory(destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "entry",
    [
        "",
        ".",
        "../outside",
        "/absolute",
        "app\\file",
        "app//file",
        "app/./file",
        "bad\nname",
    ],
    ids=(
        "empty",
        "dot",
        "parent",
        "absolute",
        "backslash",
        "double-slash",
        "dot-segment",
        "newline",
    ),
)
def test_entry_paths_must_be_normalized_safe_posix_paths(
    tmp_path: Path, entry: str
) -> None:
    source = _source(tmp_path).resolve()

    with pytest.raises(SecureArtifactSnapshotError, match="normalized POSIX"):
        snapshot_artifacts(source, [entry], (tmp_path / "snapshot").resolve())


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "con.txt",
        "PRN",
        "AUX.log",
        "NUL",
        "COM1.exe",
        "COM9",
        "LPT1.txt",
        "LPT9",
        "payload:stream",
        "trailing.",
        "trailing ",
        "Ｆｕｌｌｗｉｄｔｈ.exe",
        "e\u0301.exe",
    ],
)
def test_windows_reserved_ads_trailing_and_non_nfkc_names_are_rejected(
    tmp_path: Path, name: str
) -> None:
    source = _source(tmp_path).resolve()

    with pytest.raises(SecureArtifactSnapshotError, match="non-portable|unsafe"):
        snapshot_artifacts(source, [f"app/{name}"], (tmp_path / "snapshot").resolve())


def test_source_destination_and_selection_must_be_explicit(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()

    with pytest.raises(SecureArtifactSnapshotError, match="at least one"):
        snapshot_artifacts(source, [], (tmp_path / "empty-selection").resolve())
    with pytest.raises(SecureArtifactSnapshotError, match="unique"):
        snapshot_artifacts(
            source,
            ["stock-desk.nsi", "stock-desk.nsi"],
            (tmp_path / "duplicate").resolve(),
        )
    with pytest.raises(SecureArtifactSnapshotError, match="outside"):
        snapshot_artifacts(source, ["app"], source / "snapshot")
    with pytest.raises(SecureArtifactSnapshotError, match="absolute"):
        snapshot_artifacts(Path("relative"), ["app"], (tmp_path / "relative").resolve())


def test_destination_must_not_exist_and_failure_does_not_reuse_it(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "existing").resolve()
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(SecureArtifactSnapshotError, match="must not already exist"):
        snapshot_artifacts(source, ["app"], destination)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_missing_source_parent_and_empty_directory_selection_fail_closed(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path).resolve()
    (source / "empty").mkdir()

    with pytest.raises(SecureArtifactSnapshotError, match="contains no files"):
        snapshot_artifacts(source, ["empty"], (tmp_path / "empty-snapshot").resolve())
    with pytest.raises(SecureArtifactSnapshotError, match="missing or unsafe"):
        snapshot_artifacts(
            (tmp_path / "missing-source").resolve(),
            ["payload"],
            (tmp_path / "missing-snapshot").resolve(),
        )
    with pytest.raises(SecureArtifactSnapshotError, match="parent.*missing or unsafe"):
        snapshot_artifacts(
            source,
            ["app"],
            (tmp_path / "missing-parent" / "snapshot").resolve(),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics")
def test_symlink_file_directory_parent_and_root_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()
    (source / "linked-file").symlink_to(source / "stock-desk.nsi")
    (source / "linked-directory").symlink_to(source / "app", target_is_directory=True)

    for index, entry in enumerate(("linked-file", "linked-directory")):
        with pytest.raises(SecureArtifactSnapshotError, match="links|link"):
            snapshot_artifacts(
                source, [entry], (tmp_path / f"snapshot-{index}").resolve()
            )

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(source, target_is_directory=True)
    with pytest.raises(SecureArtifactSnapshotError, match="link"):
        snapshot_artifacts(
            linked_root.absolute(), ["app"], (tmp_path / "root-snapshot").resolve()
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file semantics")
def test_non_regular_files_and_hardlinks_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()
    os.mkfifo(source / "named-pipe")
    os.link(source / "stock-desk.nsi", source / "hard-link.nsi")

    with pytest.raises(SecureArtifactSnapshotError, match="regular file or directory"):
        snapshot_artifacts(
            source, ["named-pipe"], (tmp_path / "pipe-snapshot").resolve()
        )
    with pytest.raises(SecureArtifactSnapshotError, match="hard links"):
        snapshot_artifacts(
            source, ["hard-link.nsi"], (tmp_path / "link-snapshot").resolve()
        )


def test_overlapping_directory_and_file_entries_fail_closed(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()

    with pytest.raises(SecureArtifactSnapshotError, match="overlap"):
        snapshot_artifacts(
            source,
            ["app", "app/resources/sidecar.exe"],
            (tmp_path / "snapshot").resolve(),
        )


@pytest.mark.skipif(os.name == "nt", reason="case-distinct names require POSIX")
def test_case_insensitive_collisions_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()
    (source / "app" / "README").write_text("one", encoding="utf-8")
    (source / "app" / "readme").write_text("two", encoding="utf-8")
    if (
        len(
            [
                item
                for item in (source / "app").iterdir()
                if item.name.casefold() == "readme"
            ]
        )
        < 2
    ):
        pytest.skip("filesystem is case-insensitive")

    with pytest.raises(SecureArtifactSnapshotError, match="case-insensitive"):
        snapshot_artifacts(source, ["app"], (tmp_path / "snapshot").resolve())


def test_global_casefold_collision_is_rejected_across_separate_entries() -> None:
    identity = secure_snapshot._Identity(1, 1, stat.S_IFREG, 1, 1, 1, 1, 0)
    inventory = secure_snapshot._Inventory(
        files={"A/payload.exe": identity, "a/PAYLOAD.exe": identity}, directories={}
    )

    with pytest.raises(SecureArtifactSnapshotError, match="case-insensitive"):
        secure_snapshot._validate_global_collisions(inventory)


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (SnapshotLimits(max_files=1), "file limit"),
        (SnapshotLimits(max_file_size=4), "per-file size"),
        (SnapshotLimits(max_total_size=12), "total size"),
        (SnapshotLimits(max_depth=1), "depth limit"),
    ],
)
def test_resource_limits_are_enforced_before_publication(
    tmp_path: Path, limits: SnapshotLimits, message: str
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / f"snapshot-{message.replace(' ', '-')}").resolve()

    with pytest.raises(SecureArtifactSnapshotError, match=message):
        snapshot_artifacts(source, ["app"], destination, limits=limits)

    assert not destination.exists()


@pytest.mark.parametrize(
    "field",
    ["max_files", "max_file_size", "max_total_size", "max_depth"],
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_limits_must_be_positive_integers(
    tmp_path: Path, field: str, value: int
) -> None:
    source = _source(tmp_path).resolve()
    values = {
        "max_files": 4,
        "max_file_size": 1024,
        "max_total_size": 4096,
        "max_depth": 4,
    }
    values[field] = value

    with pytest.raises(SecureArtifactSnapshotError, match=field):
        snapshot_artifacts(
            source,
            ["app"],
            (tmp_path / f"snapshot-{field}-{value}").resolve(),
            limits=SnapshotLimits(**values),
        )


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX anchored revalidation")
def test_source_mutation_between_read_and_second_inventory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "snapshot").resolve()
    original = secure_snapshot._inventory_posix
    calls = 0

    def mutating_inventory(
        root_fd: int, entries: tuple[str, ...], limits: SnapshotLimits
    ) -> secure_snapshot._Inventory:
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "app" / "stock-desk-desktop.exe").write_bytes(b"changed\n")
        return original(root_fd, entries, limits)

    monkeypatch.setattr(secure_snapshot, "_inventory_posix", mutating_inventory)

    with pytest.raises(SecureArtifactSnapshotError, match="changed"):
        snapshot_artifacts(source, ["app"], destination)

    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor reads")
def test_file_replacement_after_inventory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "snapshot").resolve()
    original = secure_snapshot._open_source_file
    replaced = False

    @contextmanager
    def replacing_open(
        root: Path,
        root_fd: int | None,
        relative: str,
        expected: secure_snapshot._Identity,
    ) -> Iterator[BinaryIO]:
        nonlocal replaced
        if not replaced:
            replaced = True
            target = source / relative
            replacement = source / "replacement"
            replacement.write_bytes(b"replacement\n")
            replacement.replace(target)
        with original(root, root_fd, relative, expected) as stream:
            yield stream

    monkeypatch.setattr(secure_snapshot, "_open_source_file", replacing_open)

    with pytest.raises(SecureArtifactSnapshotError, match="changed before"):
        snapshot_artifacts(source, ["app"], destination)

    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX destination lease swap test")
def test_snapshot_never_returns_replaced_destination_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "snapshot").resolve()
    moved = (tmp_path / "moved-snapshot").resolve()
    original = secure_snapshot._verify_private_snapshot

    def swap_after_verification(
        path: Path,
        files: tuple[secure_snapshot.SnapshotFile, ...],
        lease: secure_snapshot._PrivateDirectoryLease | None = None,
    ) -> None:
        original(path, files, lease)
        path.rename(moved)
        path.mkdir(mode=0o700)
        (path / "replacement.txt").write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(
        secure_snapshot, "_verify_private_snapshot", swap_after_verification
    )

    with pytest.raises(SecureArtifactSnapshotError, match="replaced"):
        snapshot_artifacts(source, ["app"], destination)

    assert (destination / "replacement.txt").read_text(encoding="utf-8") == "preserve"
    assert moved.is_dir()


def test_windows_reparse_attribute_is_explicitly_recognized() -> None:
    reparse = SimpleNamespace(st_file_attributes=0x400)
    regular = SimpleNamespace(st_file_attributes=0)

    assert secure_snapshot._is_reparse(reparse) is True  # type: ignore[arg-type]
    assert secure_snapshot._is_reparse(regular) is False  # type: ignore[arg-type]


def test_windows_source_directories_allow_writes_but_not_delete_and_are_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []

    def open_handle(path: Path, *, share_mode: int) -> int:
        opened.append((path, share_mode))
        return len(opened)

    monkeypatch.setattr(secure_snapshot, "_open_windows_directory_handle", open_handle)
    monkeypatch.setattr(secure_snapshot, "_close_windows_handle", closed.append)

    with secure_snapshot._hold_windows_source_root(Path("/build/root")):
        assert opened == [
            (Path("/"), secure_snapshot._WINDOWS_DIRECTORY_SHARE),
            (Path("/build"), secure_snapshot._WINDOWS_DIRECTORY_SHARE),
            (Path("/build/root"), secure_snapshot._WINDOWS_DIRECTORY_SHARE),
        ]
        assert closed == []

    assert closed == [3, 2, 1]
    assert secure_snapshot._WINDOWS_FILE_SHARE_READ == 0x00000001
    assert secure_snapshot._WINDOWS_DIRECTORY_SHARE == 0x00000003


def test_windows_source_handle_failure_closes_previously_opened_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    closed: list[int] = []

    def open_handle(_path: Path, *, share_mode: int) -> int:
        nonlocal calls
        assert share_mode == secure_snapshot._WINDOWS_DIRECTORY_SHARE
        calls += 1
        if calls == 2:
            raise OSError("unsafe")
        return 71

    monkeypatch.setattr(secure_snapshot, "_open_windows_directory_handle", open_handle)
    monkeypatch.setattr(secure_snapshot, "_close_windows_handle", closed.append)

    with pytest.raises(SecureArtifactSnapshotError, match="unsafe component"):
        with secure_snapshot._hold_windows_source_root(Path("/build/root")):
            pass
    assert closed == [71]


def test_windows_inventory_has_the_same_stable_limits_and_link_policy(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path).resolve()

    inventory = secure_snapshot._inventory_windows(
        source, ["app", "stock-desk.nsi"], SnapshotLimits()
    )

    assert sorted(inventory.files) == [
        "app/resources/sidecar.exe",
        "app/stock-desk-desktop.exe",
        "stock-desk.nsi",
    ]
    if os.name != "nt":
        (source / "unsafe-link").symlink_to(source / "stock-desk.nsi")
        with pytest.raises(SecureArtifactSnapshotError, match="links"):
            secure_snapshot._inventory_windows(
                source, ["unsafe-link"], SnapshotLimits()
            )


def test_windows_snapshot_branch_holds_root_and_consumes_only_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "windows-snapshot").resolve()
    held: list[Path] = []

    @contextmanager
    def hold(path: Path) -> Iterator[None]:
        held.append(path)
        yield

    monkeypatch.setattr(secure_snapshot, "_running_on_windows", lambda: True)
    monkeypatch.setattr(secure_snapshot, "_hold_windows_source_root", hold)
    monkeypatch.setattr(
        secure_snapshot,
        "_expected_windows_private_sids",
        lambda: frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"}),
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_apply_windows_private_acl",
        lambda _path, allowed_sids=None: (
            allowed_sids or frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})
        ),
    )
    monkeypatch.setattr(
        secure_snapshot, "_verify_windows_private_acl", lambda _path, _sids: None
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_set_windows_private_dacl",
        lambda path, _sids, *, create=False: path.mkdir(mode=0o700) if create else None,
    )
    monkeypatch.setattr(
        secure_snapshot, "_open_windows_directory_handle", lambda _path: 91
    )
    monkeypatch.setattr(secure_snapshot, "_close_windows_handle", lambda _handle: None)
    monkeypatch.setattr(
        secure_snapshot,
        "_open_windows_non_reparse",
        lambda path: os.open(path, os.O_RDONLY),
    )

    def forbid_fchmod(*_args: object) -> None:
        raise AssertionError("Windows snapshot writes must not call os.fchmod")

    monkeypatch.setattr(os, "fchmod", forbid_fchmod)

    result = snapshot_artifacts(source, ["app"], destination)

    assert held == [destination.parent, source]
    assert result.file_count == 2
    assert (destination / "app" / "stock-desk-desktop.exe").read_bytes() == b"desktop\n"


class _FakeFunction:
    def __init__(self, callback: Callable[..., object]) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self.callback(*args)


class _FakeKernel:
    def __init__(
        self,
        *,
        attributes: int,
        converted_descriptor: int | None = None,
        inspect_success: bool = True,
    ) -> None:
        self.share_modes: list[int] = []
        self.closed: list[int] = []
        self.attributes = attributes
        self.converted_descriptor = converted_descriptor
        self.inspect_success = inspect_success
        self.CreateFileW = _FakeFunction(self._create_file)
        self.GetFileInformationByHandleEx = _FakeFunction(self._get_info)
        self.CloseHandle = _FakeFunction(self._close_handle)

    def _create_file(self, *args: object) -> int:
        self.share_modes.append(cast(int, args[2]))
        return 91

    def _get_info(self, *args: object) -> int:
        if not self.inspect_success:
            return 0
        pointer: Any = args[2]
        pointer._obj.file_attributes = self.attributes
        return 1

    def _close_handle(self, handle: object) -> int:
        self.closed.append(cast(int, handle))
        return 1


def _fake_windows_runtime(
    monkeypatch: pytest.MonkeyPatch,
    kernel: _FakeKernel,
    *,
    open_osfhandle: Callable[[int, int], int] | None = None,
) -> None:
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    if open_osfhandle is not None:
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=open_osfhandle),
        )


def test_windows_directory_handle_rejects_reparse_and_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reparse = _FakeKernel(attributes=0x410)
    _fake_windows_runtime(monkeypatch, reparse)
    with pytest.raises(SecureArtifactSnapshotError, match="reparse"):
        secure_snapshot._open_windows_directory_handle(tmp_path)
    assert reparse.share_modes == [secure_snapshot._WINDOWS_FILE_SHARE_READ]
    assert reparse.closed == [91]

    regular_file = _FakeKernel(attributes=0x80)
    _fake_windows_runtime(monkeypatch, regular_file)
    with pytest.raises(SecureArtifactSnapshotError, match="not a directory"):
        secure_snapshot._open_windows_directory_handle(tmp_path)
    assert regular_file.closed == [91]

    writable_directory = _FakeKernel(attributes=0x10)
    _fake_windows_runtime(monkeypatch, writable_directory)
    handle = secure_snapshot._open_windows_directory_handle(
        tmp_path, share_mode=secure_snapshot._WINDOWS_DIRECTORY_SHARE
    )
    secure_snapshot._close_windows_handle(handle)
    assert writable_directory.share_modes == [
        secure_snapshot._WINDOWS_FILE_SHARE_READ | 0x00000002
    ]

    with pytest.raises(SecureArtifactSnapshotError, match="share mode"):
        secure_snapshot._open_windows_directory_handle(tmp_path, share_mode=0x7)


def test_windows_file_handle_is_held_and_reparse_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.exe"
    payload.write_bytes(b"payload")
    opened_descriptor = -1

    def convert(_handle: int, _flags: int) -> int:
        nonlocal opened_descriptor
        opened_descriptor = os.open(payload, os.O_RDONLY)
        return opened_descriptor

    regular = _FakeKernel(attributes=0x80)
    _fake_windows_runtime(monkeypatch, regular, open_osfhandle=convert)
    descriptor = secure_snapshot._open_windows_non_reparse(payload)
    try:
        assert os.read(descriptor, 7) == b"payload"
        assert regular.closed == []
    finally:
        os.close(descriptor)
    assert regular.share_modes == [secure_snapshot._WINDOWS_FILE_SHARE_READ]

    reparse = _FakeKernel(attributes=0x400)
    _fake_windows_runtime(monkeypatch, reparse, open_osfhandle=convert)
    with pytest.raises(SecureArtifactSnapshotError, match="reparse"):
        secure_snapshot._open_windows_non_reparse(payload)
    assert reparse.closed == [91]


def test_windows_native_inspection_failures_close_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _FakeKernel(attributes=0x10, inspect_success=False)
    _fake_windows_runtime(monkeypatch, kernel, open_osfhandle=lambda _handle, _flags: 1)

    with pytest.raises(OSError, match="inspected"):
        secure_snapshot._open_windows_directory_handle(tmp_path)
    assert kernel.closed == [91]
    kernel.closed.clear()
    with pytest.raises(OSError, match="inspected"):
        secure_snapshot._open_windows_non_reparse(tmp_path / "payload")
    assert kernel.closed == [91]


def test_every_windows_handle_api_has_explicit_64_bit_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _FakeKernel(attributes=0x10)
    _fake_windows_runtime(monkeypatch, kernel)
    handle = secure_snapshot._open_windows_directory_handle(tmp_path)
    secure_snapshot._close_windows_handle(handle)
    for function in (
        kernel.CreateFileW,
        kernel.GetFileInformationByHandleEx,
        kernel.CloseHandle,
    ):
        assert function.argtypes is not None
        assert function.restype is not None

    native_source = "\n".join(
        inspect.getsource(function)
        for function in (
            secure_snapshot._windows_current_user_sid,
            secure_snapshot._set_windows_private_dacl,
            secure_snapshot._read_windows_dacl,
        )
    )
    for api in (
        "GetCurrentProcess",
        "LocalFree",
        "CloseHandle",
        "OpenProcessToken",
        "GetTokenInformation",
        "ConvertSidToStringSidW",
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        "GetSecurityDescriptorDacl",
        "SetNamedSecurityInfoW",
        "GetNamedSecurityInfoW",
        "GetSecurityDescriptorControl",
        "GetAclInformation",
        "GetAce",
    ):
        assert f"{api}.argtypes" in native_source
        assert f"{api}.restype" in native_source


def test_windows_private_acl_verification_rejects_inheritance_extra_aces_and_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = "S-1-5-21-1"
    allowed = frozenset({user, "S-1-5-18", "S-1-5-32-544"})

    def entry(
        sid: str, *, mask: int = 0x001F01FF, flags: int = 0x03
    ) -> secure_snapshot._WindowsAclEntry:
        return secure_snapshot._WindowsAclEntry(sid, mask, flags, 0)

    cases = [
        secure_snapshot._WindowsAcl(
            False, tuple(entry(sid) for sid in sorted(allowed))
        ),
        secure_snapshot._WindowsAcl(
            True, tuple(entry(sid) for sid in sorted((*allowed, "S-1-1-0")))
        ),
        secure_snapshot._WindowsAcl(
            True,
            tuple(
                entry(sid, mask=1 if sid == user else 0x001F01FF)
                for sid in sorted(allowed)
            ),
        ),
        secure_snapshot._WindowsAcl(
            True,
            tuple(
                entry(sid, flags=0x10 if sid == user else 0x03)
                for sid in sorted(allowed)
            ),
        ),
    ]
    for acl in cases:
        monkeypatch.setattr(
            secure_snapshot, "_read_windows_dacl", lambda _path, value=acl: value
        )
        with pytest.raises(SecureArtifactSnapshotError, match="DACL"):
            secure_snapshot._verify_windows_private_acl(tmp_path, allowed)


def test_windows_private_acl_application_is_set_then_verified_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})
    events: list[str] = []
    monkeypatch.setattr(
        secure_snapshot,
        "_expected_windows_private_sids",
        lambda: allowed,
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_set_windows_private_dacl",
        lambda _path, _sids: events.append("set"),
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_verify_windows_private_acl",
        lambda _path, _sids: events.append("verify"),
    )

    assert secure_snapshot._apply_windows_private_acl(tmp_path) == allowed
    assert events == ["set", "verify"]

    monkeypatch.setattr(
        secure_snapshot,
        "_set_windows_private_dacl",
        lambda _path, _sids: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(SecureArtifactSnapshotError, match="could not be established"):
        secure_snapshot._apply_windows_private_acl(tmp_path)


def test_windows_private_acl_accepts_only_exact_directory_and_file_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})

    def acl(flags: int) -> secure_snapshot._WindowsAcl:
        return secure_snapshot._WindowsAcl(
            True,
            tuple(
                secure_snapshot._WindowsAclEntry(
                    sid, secure_snapshot._WINDOWS_FILE_ALL_ACCESS, flags, 0
                )
                for sid in sorted(allowed)
            ),
        )

    monkeypatch.setattr(secure_snapshot, "_read_windows_dacl", lambda _path: acl(0x03))
    secure_snapshot._verify_windows_private_acl(tmp_path, allowed)

    payload = tmp_path / "payload.exe"
    payload.write_bytes(b"payload")
    monkeypatch.setattr(secure_snapshot, "_read_windows_dacl", lambda _path: acl(0))
    secure_snapshot._verify_windows_private_acl(payload, allowed)

    monkeypatch.setattr(secure_snapshot, "_read_windows_dacl", lambda _path: acl(0x08))
    with pytest.raises(SecureArtifactSnapshotError, match="exact full control"):
        secure_snapshot._verify_windows_private_acl(payload, allowed)


def test_windows_private_acl_rejects_duplicate_aces_and_missing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})
    duplicate = secure_snapshot._WindowsAclEntry(
        "S-1-5-21-1", secure_snapshot._WINDOWS_FILE_ALL_ACCESS, 0x03, 0
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_read_windows_dacl",
        lambda _path: secure_snapshot._WindowsAcl(True, (duplicate, duplicate)),
    )
    with pytest.raises(SecureArtifactSnapshotError, match="duplicate"):
        secure_snapshot._verify_windows_private_acl(tmp_path, allowed)

    valid_entries = tuple(
        secure_snapshot._WindowsAclEntry(
            sid, secure_snapshot._WINDOWS_FILE_ALL_ACCESS, 0, 0
        )
        for sid in sorted(allowed)
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_read_windows_dacl",
        lambda _path: secure_snapshot._WindowsAcl(True, valid_entries),
    )
    with pytest.raises(SecureArtifactSnapshotError, match="target is unavailable"):
        secure_snapshot._verify_windows_private_acl(tmp_path / "missing", allowed)


def test_private_snapshot_post_write_verification_rejects_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    payload = root / "payload.exe"
    payload.write_bytes(b"payload")
    payload.chmod(0o400)
    record = secure_snapshot.SnapshotFile(path="payload.exe", size=7, sha256="0" * 64)

    with pytest.raises(SecureArtifactSnapshotError, match="digest mismatch"):
        secure_snapshot._verify_private_snapshot(root, [record])

    extra = root / "extra.exe"
    extra.write_bytes(b"extra")
    extra.chmod(0o400)
    with pytest.raises(SecureArtifactSnapshotError, match="contents changed"):
        secure_snapshot._verify_private_snapshot(root, [record])

    extra.unlink()
    payload.chmod(0o644)
    with pytest.raises(SecureArtifactSnapshotError, match="owner-only"):
        secure_snapshot._verify_private_snapshot(root, [record])


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink rollback semantics")
def test_owned_snapshot_rollback_unlinks_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    linked = tmp_path / "linked-snapshot"
    linked.symlink_to(outside)

    secure_snapshot._remove_owned_tree(linked)
    secure_snapshot._remove_owned_tree(linked)

    assert outside.read_text(encoding="utf-8") == "preserve"
    assert linked.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX capability contract")
def test_posix_fails_closed_without_nofollow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(SecureArtifactSnapshotError, match="O_NOFOLLOW"):
        secure_snapshot._directory_flags()


def test_cli_fails_closed_for_invalid_limit(tmp_path: Path) -> None:
    source = _source(tmp_path).resolve()

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--source-root",
                str(source),
                "--destination",
                str((tmp_path / "snapshot").resolve()),
                "--entry",
                "app",
                "--max-files",
                "0",
            ]
        )


def test_windows_private_sid_resolution_and_policy_error_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_sid = "S-1-5-21-123"
    monkeypatch.setattr(secure_snapshot, "_windows_current_user_sid", lambda: user_sid)
    expected = frozenset(
        {
            user_sid,
            secure_snapshot._WINDOWS_SYSTEM_SID,
            secure_snapshot._WINDOWS_ADMINISTRATORS_SID,
        }
    )
    assert secure_snapshot._expected_windows_private_sids() == expected

    def reject_policy(_path: Path, _allowed: frozenset[str]) -> None:
        raise SecureArtifactSnapshotError("policy rejected")

    monkeypatch.setattr(secure_snapshot, "_set_windows_private_dacl", reject_policy)
    with pytest.raises(SecureArtifactSnapshotError, match="policy rejected"):
        secure_snapshot._apply_windows_private_acl(tmp_path, expected)


def test_windows_private_sddl_is_protected_sorted_and_inheritance_explicit() -> None:
    sids = frozenset({"S-1-5-32-544", "S-1-5-18", "S-1-5-21-9"})

    assert secure_snapshot._windows_private_sddl(sids, inheritance="OICI") == (
        "D:P(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-21-9)(A;OICI;FA;;;S-1-5-32-544)"
    )
    assert secure_snapshot._windows_private_sddl(sids, inheritance="") == (
        "D:P(A;;FA;;;S-1-5-18)(A;;FA;;;S-1-5-21-9)(A;;FA;;;S-1-5-32-544)"
    )


def test_invalid_unicode_name_is_rejected_before_filesystem_access() -> None:
    with pytest.raises(SecureArtifactSnapshotError, match="valid UTF-8"):
        secure_snapshot._validate_name("\ud800")


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX-only special files")
def test_windows_inventory_failure_paths_reject_unsafe_or_unstable_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    (source / "empty").mkdir()

    with pytest.raises(SecureArtifactSnapshotError, match="missing or unsafe"):
        secure_snapshot._inventory_windows(source, ["missing"], SnapshotLimits())
    with pytest.raises(SecureArtifactSnapshotError, match="contains no files"):
        secure_snapshot._inventory_windows(source, ["empty"], SnapshotLimits())
    with pytest.raises(SecureArtifactSnapshotError, match="depth limit"):
        secure_snapshot._inventory_windows(source, ["app"], SnapshotLimits(max_depth=0))

    (source / "app" / "linked").symlink_to(source / "stock-desk.nsi")
    with pytest.raises(SecureArtifactSnapshotError, match="links"):
        secure_snapshot._inventory_windows(source, ["app"], SnapshotLimits())
    (source / "app" / "linked").unlink()

    pipe = source / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(SecureArtifactSnapshotError, match="regular file or directory"):
        secure_snapshot._inventory_windows(source, ["pipe"], SnapshotLimits())

    original_scandir = os.scandir

    def fail_scandir(path: Any) -> Any:
        if Path(path) == source / "app":
            raise OSError("enumeration denied")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_scandir)
    with pytest.raises(SecureArtifactSnapshotError, match="cannot be enumerated"):
        secure_snapshot._inventory_windows(source, ["app"], SnapshotLimits())


def test_windows_native_open_and_close_failures_release_owned_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ctypes import wintypes

    invalid = _FakeKernel(attributes=0x10)
    invalid.CreateFileW = _FakeFunction(lambda *_args: wintypes.HANDLE(-1).value)
    _fake_windows_runtime(monkeypatch, invalid)
    with pytest.raises(OSError, match="could not be opened"):
        secure_snapshot._open_windows_directory_handle(tmp_path)

    close_failure = _FakeKernel(attributes=0x10)
    close_failure.CloseHandle = _FakeFunction(lambda _handle: 0)
    _fake_windows_runtime(monkeypatch, close_failure)
    with pytest.raises(OSError, match="could not be closed"):
        secure_snapshot._close_windows_handle(91)

    directory = _FakeKernel(attributes=0x10)
    _fake_windows_runtime(
        monkeypatch, directory, open_osfhandle=lambda _handle, _flags: 1
    )
    with pytest.raises(SecureArtifactSnapshotError, match="regular file"):
        secure_snapshot._open_windows_non_reparse(tmp_path)
    assert directory.closed == [91]

    conversion_failure = _FakeKernel(attributes=0x80)

    def reject_conversion(_handle: int, _flags: int) -> int:
        raise OSError("descriptor conversion failed")

    _fake_windows_runtime(
        monkeypatch, conversion_failure, open_osfhandle=reject_conversion
    )
    with pytest.raises(OSError, match="conversion failed"):
        secure_snapshot._open_windows_non_reparse(tmp_path / "payload")
    assert conversion_failure.closed == [91]


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor reads")
def test_source_read_revalidates_descriptor_and_path_after_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    target = source / "stock-desk.nsi"

    with secure_snapshot._open_posix_root(source) as root_fd:
        expected = secure_snapshot._identity(target.stat(follow_symlinks=False))
        with pytest.raises(
            SecureArtifactSnapshotError, match="changed while it was read"
        ):
            with secure_snapshot._open_source_file(
                source, root_fd, "stock-desk.nsi", expected
            ) as stream:
                assert stream.read()
                with target.open("ab") as mutable:
                    mutable.write(b"mutation")

    source = _source(tmp_path / "second").resolve()
    target = source / "stock-desk.nsi"
    moved = source / "moved.nsi"
    with secure_snapshot._open_posix_root(source) as root_fd:
        expected = secure_snapshot._identity(target.stat(follow_symlinks=False))
        original_identity = secure_snapshot._identity

        def stable_open_identity(metadata: os.stat_result) -> secure_snapshot._Identity:
            current = original_identity(metadata)
            return expected if current.inode == expected.inode else current

        monkeypatch.setattr(secure_snapshot, "_identity", stable_open_identity)
        with pytest.raises(SecureArtifactSnapshotError, match="path changed"):
            with secure_snapshot._open_source_file(
                source, root_fd, "stock-desk.nsi", expected
            ) as stream:
                assert stream.read()
                target.rename(moved)
                target.write_bytes(moved.read_bytes())


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX descriptor contract")
def test_source_read_requires_root_descriptor_and_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path).resolve()
    target = source / "stock-desk.nsi"
    expected = secure_snapshot._identity(target.stat(follow_symlinks=False))

    with pytest.raises(SecureArtifactSnapshotError, match="root descriptor"):
        with secure_snapshot._open_source_file(
            source, None, "stock-desk.nsi", expected
        ):
            pass

    with secure_snapshot._open_posix_root(source) as root_fd:
        monkeypatch.delattr(os, "O_NOFOLLOW")
        with pytest.raises(SecureArtifactSnapshotError, match="require O_NOFOLLOW"):
            with secure_snapshot._open_source_file(
                source, root_fd, "stock-desk.nsi", expected
            ):
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX rollback fixture")
def test_owned_tree_rollback_removes_only_the_expected_recursive_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owned"
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    (root / "top.bin").write_bytes(b"top")
    (nested / "payload.bin").write_bytes(b"payload")
    expected = secure_snapshot._identity(root.stat(follow_symlinks=False))

    secure_snapshot._remove_owned_tree(root, expected)
    assert not root.exists()

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    secure_snapshot._remove_owned_tree(
        replacement, secure_snapshot._identity(other.stat(follow_symlinks=False))
    )
    assert replacement.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rollback fixture")
def test_posix_rollback_rejects_special_entries_and_incomplete_lease(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rollback"
    root.mkdir(mode=0o700)
    os.mkfifo(root / "unsafe")
    descriptor = os.open(root, secure_snapshot._directory_flags())
    try:
        with pytest.raises(SecureArtifactSnapshotError, match="unsafe entry"):
            secure_snapshot._remove_posix_contents(descriptor)
    finally:
        os.close(descriptor)

    identity = secure_snapshot._identity(root.stat(follow_symlinks=False))
    secure_snapshot._rollback_posix_private_directory(
        secure_snapshot._PrivateDirectoryLease(root, identity, None, None)
    )


def test_windows_private_directory_swap_and_existing_target_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = (tmp_path / "private").resolve()
    moved = (tmp_path / "moved-private").resolve()
    allowed = frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})

    @contextmanager
    def hold(_path: Path) -> Iterator[None]:
        yield

    monkeypatch.setattr(secure_snapshot, "_running_on_windows", lambda: True)
    monkeypatch.setattr(secure_snapshot, "_hold_windows_source_root", hold)
    monkeypatch.setattr(
        secure_snapshot, "_apply_windows_private_acl", lambda _path: allowed
    )
    monkeypatch.setattr(
        secure_snapshot, "_expected_windows_private_sids", lambda: allowed
    )
    monkeypatch.setattr(
        secure_snapshot, "_verify_windows_private_acl", lambda _path, _sids: None
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_set_windows_private_dacl",
        lambda path, _sids, *, create=False: path.mkdir(mode=0o700) if create else None,
    )
    monkeypatch.setattr(
        secure_snapshot, "_open_windows_directory_handle", lambda _path: 91
    )
    monkeypatch.setattr(secure_snapshot, "_close_windows_handle", lambda _handle: None)

    with pytest.raises(SecureArtifactSnapshotError, match="was replaced"):
        with secure_snapshot._create_private_directory(destination):
            destination.rename(moved)
            destination.mkdir(mode=0o700)
            (destination / "replacement.txt").write_text("preserve", encoding="utf-8")
    assert (destination / "replacement.txt").read_text(encoding="utf-8") == "preserve"

    with pytest.raises(SecureArtifactSnapshotError, match="must not already exist"):
        with secure_snapshot._create_private_directory(destination):
            pass


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX private output")
def test_private_output_and_copy_failures_remove_partial_files(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path).resolve()
    destination = (tmp_path / "snapshot").resolve()
    expected = secure_snapshot._identity(
        (source / "stock-desk.nsi").stat(follow_symlinks=False)
    )

    with secure_snapshot._open_posix_root(source) as root_fd:
        with secure_snapshot._create_private_directory(destination) as lease:
            (destination / "not-a-directory").write_bytes(b"occupied")
            with pytest.raises(
                SecureArtifactSnapshotError, match="directory is unsafe"
            ):
                secure_snapshot._open_private_output_parent(
                    lease, "not-a-directory/payload.exe"
                )

            with pytest.raises(SecureArtifactSnapshotError, match="grew beyond"):
                secure_snapshot._write_snapshot_file(
                    lease,
                    source,
                    root_fd,
                    "stock-desk.nsi",
                    expected,
                    SnapshotLimits(max_file_size=1),
                )
            assert not (destination / "stock-desk.nsi").exists()

            (destination / "stock-desk.nsi").write_bytes(b"occupied")
            with pytest.raises(
                SecureArtifactSnapshotError, match="could not be created"
            ):
                secure_snapshot._write_snapshot_file(
                    lease,
                    source,
                    root_fd,
                    "stock-desk.nsi",
                    expected,
                    SnapshotLimits(),
                )


@pytest.mark.skipif(os.name == "nt", reason="POSIX snapshot verification fixture")
def test_posix_snapshot_verifier_rejects_weak_directory_and_unsafe_entry(
    tmp_path: Path,
) -> None:
    weak = tmp_path / "weak"
    weak.mkdir(mode=0o755)
    weak.chmod(0o755)
    descriptor = os.open(weak, secure_snapshot._directory_flags())
    try:
        with pytest.raises(
            SecureArtifactSnapshotError, match="directory is not owner-only"
        ):
            secure_snapshot._verify_posix_snapshot(descriptor, "", {})
    finally:
        os.close(descriptor)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    os.mkfifo(unsafe / "pipe")
    descriptor = os.open(unsafe, secure_snapshot._directory_flags())
    try:
        with pytest.raises(SecureArtifactSnapshotError, match="unsafe entry"):
            secure_snapshot._verify_posix_snapshot(descriptor, "", {})
    finally:
        os.close(descriptor)


def test_windows_snapshot_verifier_rejects_links_contents_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = frozenset({"S-1-5-21-1", "S-1-5-18", "S-1-5-32-544"})
    monkeypatch.setattr(secure_snapshot, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        secure_snapshot, "_expected_windows_private_sids", lambda: allowed
    )
    monkeypatch.setattr(
        secure_snapshot, "_verify_windows_private_acl", lambda _path, _sids: None
    )
    monkeypatch.setattr(
        secure_snapshot,
        "_open_windows_non_reparse",
        lambda path: os.open(path, os.O_RDONLY),
    )

    empty = tmp_path / "empty-windows"
    empty.mkdir()
    missing = secure_snapshot.SnapshotFile("missing.exe", 1, "0" * 64)
    with pytest.raises(SecureArtifactSnapshotError, match="contents changed"):
        secure_snapshot._verify_private_snapshot(empty, [missing])

    linked = tmp_path / "linked-windows"
    linked.mkdir()
    outside = tmp_path / "outside-windows"
    outside.mkdir()
    (linked / "unsafe").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecureArtifactSnapshotError, match="unsafe directory"):
        secure_snapshot._verify_private_snapshot(linked, [])

    payload_root = tmp_path / "digest-windows"
    payload_root.mkdir()
    payload = payload_root / "payload.exe"
    payload.write_bytes(b"payload")
    wrong = secure_snapshot.SnapshotFile("payload.exe", 7, "0" * 64)
    with pytest.raises(SecureArtifactSnapshotError, match="digest mismatch"):
        secure_snapshot._verify_private_snapshot(payload_root, [wrong])


def test_cli_rejects_mixed_private_mode_and_incomplete_snapshot_args(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--prepare-private-directory",
                str((tmp_path / "private").resolve()),
                "--source-root",
                str(tmp_path.resolve()),
            ]
        )
    with pytest.raises(SystemExit, match="2"):
        main([])
