from __future__ import annotations

from pathlib import Path

import pytest

from stock_desk.market.providers.base import (
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderPermissionDenied,
    ProviderTransientFailure,
    ProviderUnavailable,
)
from stock_desk.market.types import BarResult, Exchange

from tests.unit.market.providers.tdx_test_helpers import (
    FETCHED_AT,
    bar_query,
    golden_payload,
    make_vipdoc_root,
    tdx_local,
    tdx_windows,
)


def test_tdx_windows_open_policy_locks_paths_and_opens_reparse_points() -> None:
    module = tdx_windows()

    assert module._WINDOWS_SHARE_MODE & module._FILE_SHARE_WRITE == 0
    assert module._WINDOWS_SHARE_MODE & module._FILE_SHARE_DELETE == 0
    assert module._windows_open_flags(directory=True) & (
        module._FILE_FLAG_BACKUP_SEMANTICS | module._FILE_FLAG_OPEN_REPARSE_POINT
    ) == (module._FILE_FLAG_BACKUP_SEMANTICS | module._FILE_FLAG_OPEN_REPARSE_POINT)
    assert module._windows_open_flags(directory=False) & (
        module._FILE_FLAG_BACKUP_SEMANTICS
        | module._FILE_FLAG_OPEN_REPARSE_POINT
        | module._FILE_FLAG_SEQUENTIAL_SCAN
    ) == (
        module._FILE_FLAG_BACKUP_SEMANTICS
        | module._FILE_FLAG_OPEN_REPARSE_POINT
        | module._FILE_FLAG_SEQUENTIAL_SCAN
    )


@pytest.mark.parametrize("directory", [False, True])
def test_tdx_windows_api_passes_locking_policy_to_createfile(
    directory: bool,
) -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    calls: list[tuple[object, ...]] = []

    def create_file(*args: object) -> object:
        calls.append(args)
        return module.wintypes.HANDLE(7)

    api._create_file = create_file

    handle = api.open_path("C:\\vipdoc", directory=directory)

    assert handle == 7
    assert len(calls) == 1
    path, access, share, security, disposition, flags, template = calls[0]
    assert path == "\\\\?\\C:\\vipdoc"
    assert access == (
        module._FILE_READ_DATA | module._FILE_READ_ATTRIBUTES
        if directory
        else module._GENERIC_READ
    )
    assert share == module._WINDOWS_SHARE_MODE
    assert share & module._FILE_SHARE_WRITE == 0
    assert share & module._FILE_SHARE_DELETE == 0
    assert security is None
    assert disposition == module._OPEN_EXISTING
    assert flags == module._windows_open_flags(directory=directory)
    assert template is None


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (2, ProviderMissingCoverage),
        (3, ProviderMissingCoverage),
        (5, ProviderPermissionDenied),
        (32, ProviderTransientFailure),
        (33, ProviderTransientFailure),
        (995, ProviderTransientFailure),
        (1224, ProviderTransientFailure),
        (87, ProviderUnavailable),
    ],
)
def test_tdx_windows_error_codes_map_to_safe_typed_failures(
    error_code: int,
    expected: type[Exception],
) -> None:
    module = tdx_windows()

    with pytest.raises(expected):
        module._raise_windows_error(error_code)


def test_tdx_windows_api_reads_ctypes_private_last_error_copy() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    api._ctypes_get_last_error = lambda: 32
    api._get_last_error = lambda: 5

    assert api._error_code() == 32


def test_tdx_windows_api_extends_find_search_path() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    captured: list[str] = []

    def find_first(path: str, data: object) -> object:
        captured.append(path)
        return module.wintypes.HANDLE(module._INVALID_HANDLE_VALUE)

    api.final_path = lambda handle: "C:\\vipdoc\\sh\\lday"
    api._find_first = find_first
    api._error_code = lambda: 2

    assert api.list_names(7) == ()
    assert captured == ["\\\\?\\C:\\vipdoc\\sh\\lday\\*"]


def test_tdx_windows_api_findclose_error_does_not_mask_primary_failure() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    errors = iter((87, 5))
    api.final_path = lambda handle: "C:\\vipdoc\\sh\\lday"
    api._find_first = lambda path, data: module.wintypes.HANDLE(7)
    api._find_next = lambda handle, data: False
    api._find_close = lambda handle: False
    api._error_code = lambda: next(errors)

    with pytest.raises(ProviderUnavailable):
        api.list_names(7)


def test_tdx_windows_api_reads_real_change_time_from_file_basic_info() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)

    def file_information(handle: object, pointer: object) -> bool:
        metadata = module.ctypes.cast(
            pointer,
            module.ctypes.POINTER(module._ByHandleFileInformation),
        ).contents
        metadata.dwFileAttributes = 0
        metadata.dwVolumeSerialNumber = 7
        metadata.nFileSizeLow = 64
        metadata.nFileIndexLow = 9
        return True

    def basic_information(
        handle: object,
        info_class: int,
        pointer: object,
        size: int,
    ) -> bool:
        assert info_class == module._FILE_BASIC_INFO_CLASS
        assert size == module.ctypes.sizeof(module._FileBasicInformation)
        basic = module.ctypes.cast(
            pointer,
            module.ctypes.POINTER(module._FileBasicInformation),
        ).contents
        basic.CreationTime = 11
        basic.LastWriteTime = 22
        basic.ChangeTime = 33
        return True

    api._get_file_information = file_information
    api._get_file_information_ex = basic_information
    api._get_file_type = lambda handle: module._FILE_TYPE_DISK

    info = api.info(7)

    assert info.size == 64
    assert info.volume_serial == 7
    assert info.file_index == 9
    assert info.creation_time == 11
    assert info.last_write_time == 22
    assert info.change_time == 33


def test_tdx_windows_api_uses_whole_file_exclusive_nonblocking_lock() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    lock_calls: list[tuple[object, ...]] = []
    unlock_calls: list[tuple[object, ...]] = []
    api._lock_file = lambda *args: lock_calls.append(args) or True
    api._unlock_file = lambda *args: unlock_calls.append(args) or True

    api.lock_file(7)
    api.unlock_file(7)

    assert len(lock_calls) == len(unlock_calls) == 1
    _, flags, reserved, low, high, _ = lock_calls[0]
    assert flags == (
        module._LOCKFILE_FAIL_IMMEDIATELY | module._LOCKFILE_EXCLUSIVE_LOCK
    )
    assert reserved == 0
    assert (low, high) == (module._LOCK_RANGE, module._LOCK_RANGE)
    _, reserved, low, high, _ = unlock_calls[0]
    assert reserved == 0
    assert (low, high) == (module._LOCK_RANGE, module._LOCK_RANGE)


def test_tdx_windows_api_arms_polls_and_closes_directory_watch() -> None:
    module = tdx_windows()
    api = object.__new__(module._CtypesWindowsApi)
    arm_calls: list[tuple[object, ...]] = []
    poll_results = iter((module._WAIT_TIMEOUT, module._WAIT_OBJECT_0))
    close_calls: list[object] = []
    api.final_path = lambda handle: "C:\\vipdoc\\sh\\lday"
    api._find_first_change = lambda *args: (
        arm_calls.append(args) or module.wintypes.HANDLE(9)
    )
    api._wait_for_single_object = lambda handle, timeout: next(poll_results)
    api._find_close_change = lambda handle: close_calls.append(handle) or True

    watch = api.arm_directory_watch(7)
    unchanged = api.directory_watch_changed(watch)
    changed = api.directory_watch_changed(watch)
    api.close_directory_watch(watch)

    assert watch == 9
    assert arm_calls == [
        (
            "\\\\?\\C:\\vipdoc\\sh\\lday",
            False,
            module._DIRECTORY_WATCH_FILTER,
        )
    ]
    assert module._DIRECTORY_WATCH_FILTER & module._FILE_NOTIFY_CHANGE_ATTRIBUTES
    assert not unchanged
    assert changed
    assert len(close_calls) == 1


def test_tdx_windows_backend_rejects_unc_roots_before_opening() -> None:
    module = tdx_windows()

    class UnusedWindowsApi:
        def open_path(self, path: str, *, directory: bool) -> int:
            raise AssertionError("UNC root must fail before CreateFileW")

        def close(self, handle: int) -> None:
            raise AssertionError("nothing was opened")

        def info(self, handle: int) -> object:
            raise AssertionError("nothing was opened")

        def final_path(self, handle: int) -> str:
            raise AssertionError("nothing was opened")

        def list_names(self, handle: int) -> tuple[str, ...]:
            raise AssertionError("nothing was opened")

        def read_exact(self, handle: int, size: int) -> bytes:
            raise AssertionError("nothing was opened")

    backend = module._WindowsHandleBackend(UnusedWindowsApi())

    with pytest.raises(ProviderInvalidResponse):
        backend.inspect_market(Path("//server/share/vipdoc"), Exchange.SH)


def test_tdx_windows_backend_holds_chain_during_enumeration_and_leaf_read() -> None:
    module = tdx_windows()
    paths = {
        "c:\\vipdoc": module._WindowsFileInfo.directory_info(file_index=1),
        "c:\\vipdoc\\sh": module._WindowsFileInfo.directory_info(file_index=2),
        "c:\\vipdoc\\sh\\lday": module._WindowsFileInfo.directory_info(file_index=3),
        "c:\\vipdoc\\sh\\lday\\sh600000.day": (
            module._WindowsFileInfo.file_info(
                file_index=4,
                size=len(golden_payload("600000.SH")),
            )
        ),
    }

    class FakeWindowsApi:
        def __init__(self) -> None:
            self.handles: dict[int, str] = {}
            self.next_handle = 1
            self.enumerated_while_locked = False
            self.read_while_locked = False
            self.events: list[str] = []

        def open_path(self, path: str, *, directory: bool) -> int:
            normalized = module._normalize_windows_path(path)
            assert paths[normalized].is_directory is directory
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = normalized
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            return paths[self.handles[handle]]

        def final_path(self, handle: int) -> str:
            return "\\\\?\\" + self.handles[handle]

        def list_names(self, handle: int) -> tuple[str, ...]:
            assert self.handles[handle] == "c:\\vipdoc\\sh\\lday"
            self.events.append("enumerate")
            self.enumerated_while_locked = set(self.handles.values()) == {
                "c:\\vipdoc",
                "c:\\vipdoc\\sh",
                "c:\\vipdoc\\sh\\lday",
            }
            return ("sh600000.day",)

        def arm_directory_watch(self, handle: int) -> int:
            assert self.handles[handle] == "c:\\vipdoc\\sh\\lday"
            self.events.append("arm_watch")
            return 99

        def directory_watch_changed(self, watch: int) -> bool:
            assert watch == 99
            self.events.append("poll_watch")
            return False

        def close_directory_watch(self, watch: int) -> None:
            assert watch == 99
            self.events.append("close_watch")

        def lock_file(self, handle: int) -> None:
            assert self.handles[handle].endswith("sh600000.day")
            self.events.append("lock")

        def unlock_file(self, handle: int) -> None:
            assert self.handles[handle].endswith("sh600000.day")
            self.events.append("unlock")

        def read_exact(self, handle: int, size: int) -> bytes:
            assert self.handles[handle].endswith("sh600000.day")
            self.events.append("read")
            self.read_while_locked = len(self.handles) == 4
            return golden_payload("600000.SH")

    api = FakeWindowsApi()
    backend = module._WindowsHandleBackend(api)

    count = backend.inspect_market(Path("C:/vipdoc"), Exchange.SH)
    payload = backend.read_snapshot(Path("C:/vipdoc"), Exchange.SH, "sh600000.day")

    assert count == 1
    assert payload == golden_payload("600000.SH")
    assert api.enumerated_while_locked
    assert api.read_while_locked
    assert api.events == [
        "arm_watch",
        "enumerate",
        "poll_watch",
        "close_watch",
        "lock",
        "read",
        "unlock",
    ]
    assert api.handles == {}


def test_tdx_windows_backend_rejects_reparse_and_escaped_final_paths() -> None:
    module = tdx_windows()

    class FakeWindowsApi:
        def __init__(self, *, escaped: bool) -> None:
            self.escaped = escaped
            self.handles: dict[int, str] = {}

        def open_path(self, path: str, *, directory: bool) -> int:
            handle = len(self.handles) + 1
            self.handles[handle] = module._normalize_windows_path(path)
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            return module._WindowsFileInfo.directory_info(
                file_index=handle,
                is_reparse=handle == 2 and not self.escaped,
            )

        def final_path(self, handle: int) -> str:
            if self.escaped and handle == 2:
                return "C:\\vipdoc-escaped\\sh"
            return self.handles[handle]

        def list_names(self, handle: int) -> tuple[str, ...]:
            raise AssertionError("invalid chain must not be enumerated")

        def read_exact(self, handle: int, size: int) -> bytes:
            raise AssertionError("invalid chain must not be read")

    for escaped in (False, True):
        api = FakeWindowsApi(escaped=escaped)
        backend = module._WindowsHandleBackend(api)

        with pytest.raises(ProviderInvalidResponse):
            backend.inspect_market(Path("C:/vipdoc"), Exchange.SH)

        assert api.handles == {}


def test_tdx_windows_backend_reports_changed_locked_leaf_as_transient() -> None:
    module = tdx_windows()
    payload = golden_payload("600000.SH")

    class FakeWindowsApi:
        def __init__(self) -> None:
            self.handles: dict[int, str] = {}
            self.info_calls: dict[int, int] = {}

        def open_path(self, path: str, *, directory: bool) -> int:
            handle = len(self.handles) + 1
            self.handles[handle] = module._normalize_windows_path(path)
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            calls = self.info_calls.get(handle, 0)
            self.info_calls[handle] = calls + 1
            if self.handles[handle].endswith(".day"):
                return module._WindowsFileInfo.file_info(
                    file_index=4,
                    size=len(payload),
                    change_time=2 if calls >= 2 else 1,
                )
            return module._WindowsFileInfo.directory_info(file_index=handle)

        def final_path(self, handle: int) -> str:
            return self.handles[handle]

        def list_names(self, handle: int) -> tuple[str, ...]:
            return ("sh600000.day",)

        def lock_file(self, handle: int) -> None:
            return None

        def unlock_file(self, handle: int) -> None:
            return None

        def read_exact(self, handle: int, size: int) -> bytes:
            return payload

    api = FakeWindowsApi()
    backend = module._WindowsHandleBackend(api)

    with pytest.raises(ProviderTransientFailure):
        backend.read_snapshot(Path("C:/vipdoc"), Exchange.SH, "sh600000.day")

    assert api.handles == {}


@pytest.mark.parametrize("mutation", ["add", "remove", "replace", "attribute"])
def test_tdx_windows_preflight_detects_change_during_enumeration(
    mutation: str,
) -> None:
    module = tdx_windows()
    paths = {
        "c:\\vipdoc": module._WindowsFileInfo.directory_info(file_index=1),
        "c:\\vipdoc\\sh": module._WindowsFileInfo.directory_info(file_index=2),
        "c:\\vipdoc\\sh\\lday": module._WindowsFileInfo.directory_info(file_index=3),
        "c:\\vipdoc\\sh\\lday\\sh600000.day": (
            module._WindowsFileInfo.file_info(
                file_index=4,
                size=len(golden_payload("600000.SH")),
            )
        ),
    }

    class ChangingWindowsApi:
        def __init__(self) -> None:
            self.handles: dict[int, str] = {}
            self.next_handle = 1
            self.watch_open = False

        def open_path(self, path: str, *, directory: bool) -> int:
            normalized = module._normalize_windows_path(path)
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = normalized
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            return paths[self.handles[handle]]

        def final_path(self, handle: int) -> str:
            return self.handles[handle]

        def arm_directory_watch(self, handle: int) -> int:
            self.watch_open = True
            return 77

        def list_names(self, handle: int) -> tuple[str, ...]:
            assert self.watch_open
            return ("sh600000.day",)

        def directory_watch_changed(self, watch: int) -> bool:
            assert watch == 77
            return True

        def close_directory_watch(self, watch: int) -> None:
            assert watch == 77
            self.watch_open = False
            raise ProviderUnavailable(f"injected cleanup after {mutation}")

        def lock_file(self, handle: int) -> None:
            raise AssertionError("preflight does not lock leaf data")

        def unlock_file(self, handle: int) -> None:
            raise AssertionError("preflight does not lock leaf data")

        def read_exact(self, handle: int, size: int) -> bytes:
            raise AssertionError("preflight does not read leaf data")

    api = ChangingWindowsApi()
    backend = module._WindowsHandleBackend(api)

    with pytest.raises(ProviderTransientFailure):
        backend.inspect_market(Path("C:/vipdoc"), Exchange.SH)

    assert not api.watch_open
    assert api.handles == {}


@pytest.mark.parametrize("failure_stage", ["lock", "read", "unlock"])
def test_tdx_windows_snapshot_lock_and_unlock_cleanup(
    failure_stage: str,
) -> None:
    module = tdx_windows()
    payload = golden_payload("600000.SH")

    class FailingWindowsApi:
        def __init__(self) -> None:
            self.handles: dict[int, str] = {}
            self.next_handle = 1
            self.unlock_called = False

        def open_path(self, path: str, *, directory: bool) -> int:
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = module._normalize_windows_path(path)
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            if self.handles[handle].endswith(".day"):
                return module._WindowsFileInfo.file_info(
                    file_index=4,
                    size=len(payload),
                )
            return module._WindowsFileInfo.directory_info(file_index=handle)

        def final_path(self, handle: int) -> str:
            return self.handles[handle]

        def list_names(self, handle: int) -> tuple[str, ...]:
            return ("sh600000.day",)

        def arm_directory_watch(self, handle: int) -> int:
            return 1

        def directory_watch_changed(self, watch: int) -> bool:
            return False

        def close_directory_watch(self, watch: int) -> None:
            return None

        def lock_file(self, handle: int) -> None:
            if failure_stage == "lock":
                raise ProviderTransientFailure()

        def unlock_file(self, handle: int) -> None:
            self.unlock_called = True
            if failure_stage in {"read", "unlock"}:
                raise ProviderUnavailable()

        def read_exact(self, handle: int, size: int) -> bytes:
            if failure_stage == "read":
                raise ProviderInvalidResponse()
            return payload

    api = FailingWindowsApi()
    backend = module._WindowsHandleBackend(api)

    with pytest.raises(
        ProviderTransientFailure
        if failure_stage == "lock"
        else ProviderInvalidResponse
        if failure_stage == "read"
        else ProviderUnavailable
    ):
        backend.read_snapshot(Path("C:/vipdoc"), Exchange.SH, "sh600000.day")

    assert api.unlock_called is (failure_stage != "lock")
    assert api.handles == {}


def test_tdx_windows_preflight_rejects_day_directory_after_handle_open() -> None:
    module = tdx_windows()

    class DirectoryLeafWindowsApi:
        def __init__(self) -> None:
            self.handles: dict[int, str] = {}
            self.next_handle = 1
            self.watch_open = False

        def open_path(self, path: str, *, directory: bool) -> int:
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = module._normalize_windows_path(path)
            return handle

        def close(self, handle: int) -> None:
            del self.handles[handle]

        def info(self, handle: int) -> object:
            return module._WindowsFileInfo.directory_info(file_index=handle)

        def final_path(self, handle: int) -> str:
            return self.handles[handle]

        def arm_directory_watch(self, handle: int) -> int:
            self.watch_open = True
            return 1

        def directory_watch_changed(self, watch: int) -> bool:
            return False

        def close_directory_watch(self, watch: int) -> None:
            self.watch_open = False

        def list_names(self, handle: int) -> tuple[str, ...]:
            return ("sh600000.day",)

        def lock_file(self, handle: int) -> None:
            raise AssertionError("preflight does not lock leaf data")

        def unlock_file(self, handle: int) -> None:
            raise AssertionError("preflight does not lock leaf data")

        def read_exact(self, handle: int, size: int) -> bytes:
            raise AssertionError("preflight does not read leaf data")

    api = DirectoryLeafWindowsApi()
    backend = module._WindowsHandleBackend(api)

    with pytest.raises(ProviderInvalidResponse):
        backend.inspect_market(Path("C:/vipdoc"), Exchange.SH)

    assert not api.watch_open
    assert api.handles == {}


def test_tdx_provider_routes_windows_operations_to_handle_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    calls: list[tuple[str, Exchange]] = []

    class FakeWindowsBackend:
        def inspect_market(self, root: Path, exchange: Exchange) -> int:
            calls.append(("inspect", exchange))
            return 1 if exchange is Exchange.SH else 0

        def read_snapshot(
            self,
            root: Path,
            exchange: Exchange,
            name: str,
        ) -> bytes:
            calls.append(("read", exchange))
            return golden_payload("600000.SH")

    backend = FakeWindowsBackend()
    path_calls: list[str] = []

    def unexpected_path_io(*args: object, **kwargs: object) -> object:
        path_calls.append("called")
        raise AssertionError("Windows routing must not use path I/O")

    monkeypatch.setattr(module, "_PLATFORM", "nt")
    monkeypatch.setattr(module, "_WINDOWS_BACKEND_FACTORY", lambda: backend)
    monkeypatch.setattr(module, "_USE_POSIX_DESCRIPTOR_IO", False)
    monkeypatch.setattr(module.os, "open", unexpected_path_io)
    monkeypatch.setattr(module.os, "scandir", unexpected_path_io)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    inspection = provider.preflight()
    outcome = provider.fetch_bars(bar_query())

    assert isinstance(inspection, module.TdxInspectionSuccess)
    assert isinstance(outcome, BarResult)
    assert calls == [
        ("inspect", Exchange.SH),
        ("inspect", Exchange.SZ),
        ("read", Exchange.SH),
    ]
    assert path_calls == []
