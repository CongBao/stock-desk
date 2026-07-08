from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date
import ntpath
import os
from pathlib import Path
import re
import sys
from typing import Any, NoReturn, Protocol, Self

from stock_desk.market.providers.base import (
    ProviderCorrupt,
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderPermissionDenied,
    ProviderTransientFailure,
    ProviderUnavailable,
)
from stock_desk.market.providers.tdx_binary import (
    DAY_RECORD_STRUCT,
    MAX_DAY_BYTES,
    parse_day_bytes,
)
from stock_desk.market.types import Exchange


MAX_DIRECTORY_ENTRIES = 10_000

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_SHARE_MODE = _FILE_SHARE_READ

_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_DEVICE = 0x00000040
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_TYPE_DISK = 0x00000001
_FILE_READ_DATA = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_BASIC_INFO_CLASS = 0
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_LOCK_RANGE = 0xFFFFFFFF
_FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
_FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
_FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x00000004
_FILE_NOTIFY_CHANGE_SIZE = 0x00000008
_FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
_FILE_NOTIFY_CHANGE_CREATION = 0x00000040
_DIRECTORY_WATCH_FILTER = (
    _FILE_NOTIFY_CHANGE_FILE_NAME
    | _FILE_NOTIFY_CHANGE_DIR_NAME
    | _FILE_NOTIFY_CHANGE_ATTRIBUTES
    | _FILE_NOTIFY_CHANGE_SIZE
    | _FILE_NOTIFY_CHANGE_LAST_WRITE
    | _FILE_NOTIFY_CHANGE_CREATION
)
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_NO_MORE_FILES = 18
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_OPERATION_ABORTED = 995
_ERROR_USER_MAPPED_FILE = 1224
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_WINDOWS_PATH = 32_768

_MARKET_DIRECTORY = {Exchange.SH: "sh", Exchange.SZ: "sz"}
_TDX_FILE_PATTERNS = {
    exchange: re.compile(rf"^{directory}[0-9]{{6}}\.day$")
    for exchange, directory in _MARKET_DIRECTORY.items()
}


def _windows_open_flags(*, directory: bool) -> int:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
    if directory:
        return flags
    return flags | _FILE_FLAG_SEQUENTIAL_SCAN


def _normalize_windows_path(value: str) -> str:
    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        normalized = "\\\\" + normalized[8:]
    elif folded.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return ntpath.normcase(ntpath.normpath(normalized))


@dataclass(frozen=True, slots=True)
class _WindowsFileInfo:
    is_directory: bool
    is_reparse: bool
    is_disk: bool
    size: int
    volume_serial: int
    file_index: int
    creation_time: int
    last_write_time: int
    change_time: int

    @classmethod
    def directory_info(
        cls,
        *,
        file_index: int,
        is_reparse: bool = False,
        last_write_time: int = 1,
        change_time: int = 1,
    ) -> Self:
        return cls(
            is_directory=True,
            is_reparse=is_reparse,
            is_disk=True,
            size=0,
            volume_serial=1,
            file_index=file_index,
            creation_time=1,
            last_write_time=last_write_time,
            change_time=change_time,
        )

    @classmethod
    def file_info(
        cls,
        *,
        file_index: int,
        size: int,
        is_reparse: bool = False,
        last_write_time: int = 1,
        change_time: int = 1,
    ) -> Self:
        return cls(
            is_directory=False,
            is_reparse=is_reparse,
            is_disk=True,
            size=size,
            volume_serial=1,
            file_index=file_index,
            creation_time=1,
            last_write_time=last_write_time,
            change_time=change_time,
        )


class _WindowsApi(Protocol):
    def open_path(self, path: str, *, directory: bool) -> int: ...

    def close(self, handle: int) -> None: ...

    def info(self, handle: int) -> _WindowsFileInfo: ...

    def final_path(self, handle: int) -> str: ...

    def list_names(self, handle: int) -> tuple[str, ...]: ...

    def arm_directory_watch(self, handle: int) -> int: ...

    def directory_watch_changed(self, watch: int) -> bool: ...

    def close_directory_watch(self, watch: int) -> None: ...

    def lock_file(self, handle: int) -> None: ...

    def unlock_file(self, handle: int) -> None: ...

    def read_exact(self, handle: int, size: int) -> bytes: ...


class WindowsBackend(Protocol):
    def inspect_market(self, root: Path, exchange: Exchange) -> int: ...

    def latest_market_day(
        self, root: Path, exchange: Exchange, *, observed_on: date
    ) -> date | None: ...

    def read_snapshot(self, root: Path, exchange: Exchange, name: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _OpenWindowsPath:
    handle: int
    final_path: str
    info: _WindowsFileInfo


def _validate_final_path(
    actual: str,
    expected: str,
    *,
    parent: str | None,
) -> str:
    actual_normalized = _normalize_windows_path(actual)
    expected_normalized = _normalize_windows_path(expected)
    if parent is not None:
        parent_normalized = _normalize_windows_path(parent)
        try:
            contained = (
                ntpath.commonpath((parent_normalized, actual_normalized))
                == parent_normalized
            )
        except ValueError:
            contained = False
        if not contained:
            raise ProviderInvalidResponse()
    if actual_normalized != expected_normalized:
        raise ProviderInvalidResponse()
    return actual_normalized


def _validate_day_file_info(info: _WindowsFileInfo, *, allow_empty: bool) -> None:
    if info.is_directory or info.is_reparse or not info.is_disk:
        raise ProviderInvalidResponse()
    if info.size < 0 or info.size > MAX_DAY_BYTES:
        raise ProviderCorrupt()
    if not allow_empty and info.size == 0:
        raise ProviderCorrupt()
    if info.size % DAY_RECORD_STRUCT.size != 0:
        raise ProviderCorrupt()


class _WindowsHandleBackend:
    def __init__(self, api: _WindowsApi) -> None:
        self._api = api

    def _open_checked(
        self,
        path: str,
        *,
        expected: str,
        parent: str | None,
        directory: bool,
    ) -> _OpenWindowsPath:
        handle = self._api.open_path(path, directory=directory)
        try:
            info = self._api.info(handle)
            if (
                info.is_reparse
                or not info.is_disk
                or info.is_directory is not directory
            ):
                raise ProviderInvalidResponse()
            final_path = _validate_final_path(
                self._api.final_path(handle),
                expected,
                parent=parent,
            )
            return _OpenWindowsPath(
                handle=handle,
                final_path=final_path,
                info=info,
            )
        except Exception:
            try:
                self._api.close(handle)
            except Exception:
                pass
            raise

    def _open_chain(
        self,
        root: Path,
        exchange: Exchange,
    ) -> tuple[_OpenWindowsPath, _OpenWindowsPath, _OpenWindowsPath]:
        opened: list[_OpenWindowsPath] = []
        try:
            expected_root = _normalize_windows_path(ntpath.abspath(os.fspath(root)))
            root_drive, _ = ntpath.splitdrive(expected_root)
            if root_drive.startswith("\\\\"):
                raise ProviderInvalidResponse()
            root_path = self._open_checked(
                os.fspath(root),
                expected=expected_root,
                parent=None,
                directory=True,
            )
            opened.append(root_path)

            expected_market = ntpath.join(
                root_path.final_path,
                _MARKET_DIRECTORY[exchange],
            )
            market_path = self._open_checked(
                expected_market,
                expected=expected_market,
                parent=root_path.final_path,
                directory=True,
            )
            opened.append(market_path)

            expected_lday = ntpath.join(market_path.final_path, "lday")
            lday_path = self._open_checked(
                expected_lday,
                expected=expected_lday,
                parent=market_path.final_path,
                directory=True,
            )
            opened.append(lday_path)
        except Exception:
            self._close_paths(opened)
            raise
        return opened[0], opened[1], opened[2]

    def _cleanup_resources(
        self,
        opened: list[_OpenWindowsPath],
        *,
        locked_handle: int | None = None,
        directory_watch: int | None = None,
    ) -> None:
        primary_error = sys.exception()
        cleanup_error: Exception | None = None
        if locked_handle is not None:
            try:
                self._api.unlock_file(locked_handle)
            except Exception as error:
                cleanup_error = error
        if directory_watch is not None:
            try:
                self._api.close_directory_watch(directory_watch)
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        for item in reversed(opened):
            try:
                self._api.close(item.handle)
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error

    def _close_paths(self, opened: list[_OpenWindowsPath]) -> None:
        self._cleanup_resources(opened)

    def inspect_market(self, root: Path, exchange: Exchange) -> int:
        chain = self._open_chain(root, exchange)
        opened = list(chain)
        directory_watch: int | None = None
        try:
            directory_watch = self._api.arm_directory_watch(chain[-1].handle)
            names = self._api.list_names(chain[-1].handle)
            if len(names) > MAX_DIRECTORY_ENTRIES:
                raise ProviderCorrupt()
            pattern = _TDX_FILE_PATTERNS[exchange]
            count = 0
            for name in names:
                if not name.endswith(".day"):
                    continue
                if pattern.fullmatch(name) is None:
                    raise ProviderCorrupt()
                expected = ntpath.join(chain[-1].final_path, name)
                leaf = self._open_checked(
                    expected,
                    expected=expected,
                    parent=chain[-1].final_path,
                    directory=False,
                )
                try:
                    _validate_day_file_info(leaf.info, allow_empty=False)
                finally:
                    primary_error = sys.exception()
                    try:
                        self._api.close(leaf.handle)
                    except Exception:
                        if primary_error is None:
                            raise
                count += 1
            if self._api.directory_watch_changed(directory_watch):
                raise ProviderTransientFailure()
            return count
        finally:
            self._cleanup_resources(opened, directory_watch=directory_watch)

    def latest_market_day(
        self, root: Path, exchange: Exchange, *, observed_on: date
    ) -> date | None:
        chain = self._open_chain(root, exchange)
        opened = list(chain)
        directory_watch: int | None = None
        try:
            directory_watch = self._api.arm_directory_watch(chain[-1].handle)
            names = self._api.list_names(chain[-1].handle)
            if len(names) > MAX_DIRECTORY_ENTRIES:
                raise ProviderCorrupt()
            pattern = _TDX_FILE_PATTERNS[exchange]
            latest: date | None = None
            for name in names:
                if not name.endswith(".day"):
                    continue
                if pattern.fullmatch(name) is None:
                    raise ProviderCorrupt()
                records = parse_day_bytes(
                    self.read_snapshot(root, exchange, name),
                    observed_on=observed_on,
                )
                candidate = records[-1].day
                latest = candidate if latest is None else max(latest, candidate)
            if self._api.directory_watch_changed(directory_watch):
                raise ProviderTransientFailure()
            return latest
        finally:
            self._cleanup_resources(opened, directory_watch=directory_watch)

    def read_snapshot(self, root: Path, exchange: Exchange, name: str) -> bytes:
        chain = self._open_chain(root, exchange)
        opened = list(chain)
        locked_handle: int | None = None
        try:
            expected = ntpath.join(chain[-1].final_path, name)
            leaf = self._open_checked(
                expected,
                expected=expected,
                parent=chain[-1].final_path,
                directory=False,
            )
            opened.append(leaf)
            self._api.lock_file(leaf.handle)
            locked_handle = leaf.handle
            leaf_before = self._api.info(leaf.handle)
            _validate_day_file_info(leaf_before, allow_empty=True)
            payload = self._api.read_exact(leaf.handle, leaf_before.size)
            if len(payload) != leaf_before.size:
                raise ProviderTransientFailure()
            if self._api.info(leaf.handle) != leaf_before:
                raise ProviderTransientFailure()
            return payload
        finally:
            self._cleanup_resources(opened, locked_handle=locked_handle)


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    )


class _Win32FindData(ctypes.Structure):
    _fields_ = (
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("dwReserved0", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("cFileName", wintypes.WCHAR * 260),
        ("cAlternateFileName", wintypes.WCHAR * 14),
    )


class _FileBasicInformation(ctypes.Structure):
    _fields_ = (
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    )


class _Overlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


def _filetime_value(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _extended_windows_path(path: str) -> str:
    normalized = ntpath.abspath(path.replace("/", "\\"))
    folded = normalized.casefold()
    if folded.startswith("\\\\?\\"):
        return normalized
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def _raise_windows_error(error_code: int) -> NoReturn:
    if error_code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        raise ProviderMissingCoverage()
    if error_code == _ERROR_ACCESS_DENIED:
        raise ProviderPermissionDenied()
    if error_code in {
        _ERROR_SHARING_VIOLATION,
        _ERROR_LOCK_VIOLATION,
        _ERROR_OPERATION_ABORTED,
        _ERROR_USER_MAPPED_FILE,
    }:
        raise ProviderTransientFailure()
    raise ProviderUnavailable()


class _CtypesWindowsApi:
    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if os.name != "nt" or loader is None:
            raise ProviderUnavailable()
        kernel32: Any = loader("kernel32", use_last_error=True)
        ctypes_get_last_error = getattr(ctypes, "get_last_error", None)
        if ctypes_get_last_error is None:
            raise ProviderUnavailable()
        self._ctypes_get_last_error: Any = ctypes_get_last_error
        self._create_file: Any = kernel32.CreateFileW
        self._close_handle: Any = kernel32.CloseHandle
        self._get_file_information: Any = kernel32.GetFileInformationByHandle
        self._get_file_information_ex: Any = kernel32.GetFileInformationByHandleEx
        self._get_file_type: Any = kernel32.GetFileType
        self._get_final_path: Any = kernel32.GetFinalPathNameByHandleW
        self._find_first: Any = kernel32.FindFirstFileW
        self._find_next: Any = kernel32.FindNextFileW
        self._find_close: Any = kernel32.FindClose
        self._find_first_change: Any = kernel32.FindFirstChangeNotificationW
        self._wait_for_single_object: Any = kernel32.WaitForSingleObject
        self._find_close_change: Any = kernel32.FindCloseChangeNotification
        self._lock_file: Any = kernel32.LockFileEx
        self._unlock_file: Any = kernel32.UnlockFileEx
        self._read_file: Any = kernel32.ReadFile
        self._configure_functions()

    def _configure_functions(self) -> None:
        self._create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self._create_file.restype = wintypes.HANDLE
        self._close_handle.argtypes = (wintypes.HANDLE,)
        self._close_handle.restype = wintypes.BOOL
        self._get_file_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        )
        self._get_file_information.restype = wintypes.BOOL
        self._get_file_information_ex.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._get_file_information_ex.restype = wintypes.BOOL
        self._get_file_type.argtypes = (wintypes.HANDLE,)
        self._get_file_type.restype = wintypes.DWORD
        self._get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self._get_final_path.restype = wintypes.DWORD
        self._find_first.argtypes = (
            wintypes.LPCWSTR,
            ctypes.POINTER(_Win32FindData),
        )
        self._find_first.restype = wintypes.HANDLE
        self._find_next.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Win32FindData),
        )
        self._find_next.restype = wintypes.BOOL
        self._find_close.argtypes = (wintypes.HANDLE,)
        self._find_close.restype = wintypes.BOOL
        self._find_first_change.argtypes = (
            wintypes.LPCWSTR,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._find_first_change.restype = wintypes.HANDLE
        self._wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._wait_for_single_object.restype = wintypes.DWORD
        self._find_close_change.argtypes = (wintypes.HANDLE,)
        self._find_close_change.restype = wintypes.BOOL
        self._lock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        )
        self._lock_file.restype = wintypes.BOOL
        self._unlock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        )
        self._unlock_file.restype = wintypes.BOOL
        self._read_file.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        self._read_file.restype = wintypes.BOOL

    def _error_code(self) -> int:
        return int(self._ctypes_get_last_error())

    def open_path(self, path: str, *, directory: bool) -> int:
        handle = self._create_file(
            _extended_windows_path(path),
            (_FILE_READ_DATA | _FILE_READ_ATTRIBUTES) if directory else _GENERIC_READ,
            _WINDOWS_SHARE_MODE,
            None,
            _OPEN_EXISTING,
            _windows_open_flags(directory=directory),
            None,
        )
        handle_value = ctypes.cast(handle, ctypes.c_void_p).value
        if handle_value is None or handle_value == _INVALID_HANDLE_VALUE:
            _raise_windows_error(self._error_code())
        return handle_value

    def close(self, handle: int) -> None:
        if not self._close_handle(wintypes.HANDLE(handle)):
            _raise_windows_error(self._error_code())

    def info(self, handle: int) -> _WindowsFileInfo:
        metadata = _ByHandleFileInformation()
        basic = _FileBasicInformation()
        native_handle = wintypes.HANDLE(handle)
        if not self._get_file_information(native_handle, ctypes.byref(metadata)):
            _raise_windows_error(self._error_code())
        if not self._get_file_information_ex(
            native_handle,
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            _raise_windows_error(self._error_code())
        attributes = int(metadata.dwFileAttributes)
        return _WindowsFileInfo(
            is_directory=bool(attributes & _FILE_ATTRIBUTE_DIRECTORY),
            is_reparse=bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            is_disk=int(self._get_file_type(native_handle)) == _FILE_TYPE_DISK
            and not bool(attributes & _FILE_ATTRIBUTE_DEVICE),
            size=(int(metadata.nFileSizeHigh) << 32) | int(metadata.nFileSizeLow),
            volume_serial=int(metadata.dwVolumeSerialNumber),
            file_index=(int(metadata.nFileIndexHigh) << 32)
            | int(metadata.nFileIndexLow),
            creation_time=int(basic.CreationTime),
            last_write_time=int(basic.LastWriteTime),
            change_time=int(basic.ChangeTime),
        )

    def final_path(self, handle: int) -> str:
        capacity = _MAX_WINDOWS_PATH
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(
            self._get_final_path(
                wintypes.HANDLE(handle),
                buffer,
                capacity,
                0,
            )
        )
        if length == 0:
            _raise_windows_error(self._error_code())
        if length >= capacity:
            capacity = length + 1
            buffer = ctypes.create_unicode_buffer(capacity)
            length = int(
                self._get_final_path(
                    wintypes.HANDLE(handle),
                    buffer,
                    capacity,
                    0,
                )
            )
            if length == 0 or length >= capacity:
                _raise_windows_error(self._error_code())
        return buffer.value

    def list_names(self, handle: int) -> tuple[str, ...]:
        search_path = _extended_windows_path(ntpath.join(self.final_path(handle), "*"))
        data = _Win32FindData()
        find_handle = self._find_first(search_path, ctypes.byref(data))
        find_handle_value = ctypes.cast(find_handle, ctypes.c_void_p).value
        if find_handle_value is None or find_handle_value == _INVALID_HANDLE_VALUE:
            error_code = self._error_code()
            if error_code == _ERROR_FILE_NOT_FOUND:
                return ()
            _raise_windows_error(error_code)
        names: list[str] = []
        try:
            while True:
                name = data.cFileName
                if name not in {".", ".."}:
                    names.append(name)
                    if len(names) > MAX_DIRECTORY_ENTRIES:
                        raise ProviderCorrupt()
                if self._find_next(
                    wintypes.HANDLE(find_handle_value),
                    ctypes.byref(data),
                ):
                    continue
                error_code = self._error_code()
                if error_code != _ERROR_NO_MORE_FILES:
                    _raise_windows_error(error_code)
                return tuple(names)
        finally:
            primary_error = sys.exception()
            if (
                not self._find_close(wintypes.HANDLE(find_handle_value))
                and primary_error is None
            ):
                _raise_windows_error(self._error_code())

    def lock_file(self, handle: int) -> None:
        overlapped = _Overlapped()
        if not self._lock_file(
            wintypes.HANDLE(handle),
            _LOCKFILE_FAIL_IMMEDIATELY | _LOCKFILE_EXCLUSIVE_LOCK,
            0,
            _LOCK_RANGE,
            _LOCK_RANGE,
            ctypes.byref(overlapped),
        ):
            _raise_windows_error(self._error_code())

    def arm_directory_watch(self, handle: int) -> int:
        watch = self._find_first_change(
            _extended_windows_path(self.final_path(handle)),
            False,
            _DIRECTORY_WATCH_FILTER,
        )
        watch_value = ctypes.cast(watch, ctypes.c_void_p).value
        if watch_value is None or watch_value == _INVALID_HANDLE_VALUE:
            _raise_windows_error(self._error_code())
        return watch_value

    def directory_watch_changed(self, watch: int) -> bool:
        result = int(self._wait_for_single_object(wintypes.HANDLE(watch), 0))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        if result == _WAIT_FAILED:
            _raise_windows_error(self._error_code())
        raise ProviderUnavailable()

    def close_directory_watch(self, watch: int) -> None:
        if not self._find_close_change(wintypes.HANDLE(watch)):
            _raise_windows_error(self._error_code())

    def unlock_file(self, handle: int) -> None:
        overlapped = _Overlapped()
        if not self._unlock_file(
            wintypes.HANDLE(handle),
            0,
            _LOCK_RANGE,
            _LOCK_RANGE,
            ctypes.byref(overlapped),
        ):
            _raise_windows_error(self._error_code())

    def read_exact(self, handle: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk_size = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(chunk_size)
            bytes_read = wintypes.DWORD()
            if not self._read_file(
                wintypes.HANDLE(handle),
                buffer,
                chunk_size,
                ctypes.byref(bytes_read),
                None,
            ):
                _raise_windows_error(self._error_code())
            if bytes_read.value == 0:
                raise ProviderTransientFailure()
            chunks.append(buffer.raw[: bytes_read.value])
            remaining -= int(bytes_read.value)
        return b"".join(chunks)


def create_windows_backend() -> WindowsBackend:
    if os.name != "nt":
        raise ProviderUnavailable()
    return _WindowsHandleBackend(_CtypesWindowsApi())
