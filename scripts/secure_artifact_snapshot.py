"""Create a private, fail-closed snapshot of mutable build artifacts.

The snapshot is the only directory a later packaging step should consume.  On
POSIX, every source component is traversed relative to an already-open directory
descriptor and every open uses ``O_NOFOLLOW``.  Windows opens final files as
reparse points first and rejects them before converting the handle to a Python
file descriptor.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
from typing import Any, BinaryIO, Final
import unicodedata


_READ_BLOCK: Final = 1024 * 1024
_REPARSE_ATTRIBUTE: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_FILE_SHARE_READ: Final = 0x00000001
_WINDOWS_DIRECTORY_SHARE: Final = 0x00000003
_WINDOWS_SHARING_RETRY_DELAYS: Final = (0.05, 0.1, 0.2, 0.4)
_WINDOWS_TRANSIENT_SHARING_ERRORS: Final = frozenset({32, 33})
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_SYSTEM_SID: Final = "S-1-5-18"
_WINDOWS_ADMINISTRATORS_SID: Final = "S-1-5-32-544"
_WINDOWS_FILE_ALL_ACCESS: Final = 0x001F01FF


class SecureArtifactSnapshotError(ValueError):
    """The requested snapshot cannot be created without trusting mutable input."""


@dataclass(frozen=True)
class SnapshotLimits:
    """Hard resource limits applied before and while artifacts are copied."""

    max_files: int = 4096
    max_file_size: int = 2 * 1024 * 1024 * 1024
    max_total_size: int = 8 * 1024 * 1024 * 1024
    max_depth: int = 32

    def validate(self) -> None:
        for field in ("max_files", "max_file_size", "max_total_size", "max_depth"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SecureArtifactSnapshotError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotResult:
    root: Path
    files: tuple[SnapshotFile, ...]
    file_count: int
    total_size: int
    snapshot_sha256: str

    def summary(self) -> dict[str, object]:
        """Return a path-free summary suitable for logs and workflow outputs."""

        return {
            "schema": "stock-desk-secure-artifact-snapshot-v1",
            "file_count": self.file_count,
            "total_size": self.total_size,
            "snapshot_sha256": self.snapshot_sha256,
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in self.files
            ],
        }


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    links: int
    attributes: int


@dataclass(frozen=True)
class _Inventory:
    files: dict[str, _Identity]
    directories: dict[str, _Identity]


@dataclass(frozen=True)
class _WindowsAclEntry:
    sid: str
    mask: int
    flags: int
    ace_type: int


@dataclass(frozen=True)
class _WindowsAcl:
    protected: bool
    entries: tuple[_WindowsAclEntry, ...]


@dataclass(frozen=True)
class _PrivateDirectoryLease:
    path: Path
    identity: _Identity
    parent_fd: int | None
    root_fd: int | None


def _identity(metadata: os.stat_result) -> _Identity:
    return _Identity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        links=metadata.st_nlink,
        attributes=int(getattr(metadata, "st_file_attributes", 0)),
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE)


def _running_on_windows() -> bool:
    return os.name == "nt"


def _windows_current_user_sid() -> str:  # pragma: no cover - native Windows API
    """Return the exact SID carried by the current process token."""

    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    kernel32 = win_dll("kernel32", use_last_error=True)
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise OSError(get_last_error(), "current process token could not be opened")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise OSError(get_last_error(), "current process SID size is unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, required, ctypes.byref(required)
        ):
            raise OSError(get_last_error(), "current process SID is unavailable")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            token_user.user.sid, ctypes.byref(sid_text)
        ):
            raise OSError(get_last_error(), "current process SID cannot be encoded")
        try:
            value = str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)
    if not value.startswith("S-1-") or len(value) > 184:
        raise SecureArtifactSnapshotError("current Windows user SID is invalid")
    return value


def _set_windows_private_dacl(  # pragma: no cover - native Windows API
    path: Path, allowed_sids: frozenset[str], *, create: bool = False
) -> None:
    """Create or secure a path with exact full-control allow ACEs."""

    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    descriptor = ctypes.c_void_p()
    if create:
        inheritance = "OICI"
    else:
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as stat_error:
            raise OSError("private Windows ACL target is unavailable") from stat_error
        inheritance = "OICI" if stat.S_ISDIR(metadata.st_mode) else ""
    sddl = _windows_private_sddl(allowed_sids, inheritance=inheritance)
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise OSError(get_last_error(), "private Windows DACL could not be built")
    try:
        if create:
            attributes = SecurityAttributes(
                ctypes.sizeof(SecurityAttributes), descriptor, False
            )
            if not kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                failure_code = int(get_last_error())
                if failure_code in {80, 183}:
                    raise FileExistsError(
                        failure_code, "private directory already exists", path
                    )
                raise OSError(
                    failure_code, "private Windows directory could not be created"
                )
            return
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if (
            not advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            )
            or not present.value
            or not dacl.value
        ):
            raise OSError(get_last_error(), "private Windows DACL is unavailable")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL + PROTECTED_DACL
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(int(result), "private Windows DACL could not be applied")
    finally:
        kernel32.LocalFree(descriptor)


def _windows_private_sddl(allowed_sids: frozenset[str], *, inheritance: str) -> str:
    """Build one protected DACL with no inherited or unexpected principals."""

    return "D:P" + "".join(
        f"(A;{inheritance};FA;;;{sid})" for sid in sorted(allowed_sids)
    )


def _read_windows_dacl(  # pragma: no cover - native Windows API
    path: Path,
) -> _WindowsAcl:
    """Read the protected DACL and return every ACE for exact verification."""

    import ctypes
    from ctypes import wintypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        ]

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000004,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value or not dacl.value:
        if descriptor.value:
            kernel32.LocalFree(descriptor)
        raise OSError(int(result), "Windows DACL could not be read")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise OSError("Windows DACL control flags could not be read")
        information = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(information), ctypes.sizeof(information), 2
        ):
            raise OSError("Windows DACL size could not be read")
        entries: list[_WindowsAclEntry] = []
        for index in range(information.ace_count):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise OSError("Windows DACL ACE could not be read")
            if ace_pointer.value is None:
                raise OSError("Windows DACL ACE pointer is null")
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(AccessAllowedAce)).contents
            sid_pointer = ctypes.c_void_p(
                int(ace_pointer.value) + int(AccessAllowedAce.sid_start.offset)
            )
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise OSError("Windows DACL SID could not be encoded")
            try:
                sid = str(sid_text.value)
            finally:
                kernel32.LocalFree(sid_text)
            entries.append(
                _WindowsAclEntry(
                    sid=sid,
                    mask=int(ace.mask),
                    flags=int(ace.header.ace_flags),
                    ace_type=int(ace.header.ace_type),
                )
            )
        return _WindowsAcl(
            protected=bool(control.value & 0x1000),
            entries=tuple(entries),
        )
    finally:
        kernel32.LocalFree(descriptor)


def _expected_windows_private_sids() -> frozenset[str]:
    return frozenset(
        {
            _windows_current_user_sid(),
            _WINDOWS_SYSTEM_SID,
            _WINDOWS_ADMINISTRATORS_SID,
        }
    )


def _verify_windows_private_acl(path: Path, allowed_sids: frozenset[str]) -> None:
    acl = _read_windows_dacl(path)
    entries_by_sid: dict[str, _WindowsAclEntry] = {}
    for entry in acl.entries:
        if entry.sid in entries_by_sid:
            raise SecureArtifactSnapshotError("Windows private DACL has duplicate ACEs")
        entries_by_sid[entry.sid] = entry
    if not acl.protected or set(entries_by_sid) != set(allowed_sids):
        raise SecureArtifactSnapshotError(
            "Windows private DACL is inherited or permits an unexpected principal"
        )
    try:
        expected_flags = (
            0x03 if stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode) else 0
        )
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "Windows private DACL target is unavailable"
        ) from error
    if any(
        entry.ace_type != 0
        or entry.mask != _WINDOWS_FILE_ALL_ACCESS
        or entry.flags != expected_flags
        for entry in entries_by_sid.values()
    ):
        raise SecureArtifactSnapshotError(
            "Windows private DACL is not exact full control"
        )


def _apply_windows_private_acl(
    path: Path, allowed_sids: frozenset[str] | None = None
) -> frozenset[str]:
    allowed = allowed_sids or _expected_windows_private_sids()
    try:
        _set_windows_private_dacl(path, allowed)
        _verify_windows_private_acl(path, allowed)
    except SecureArtifactSnapshotError:
        raise
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "Windows owner-only DACL could not be established"
        ) from error
    return allowed


def _absolute_directory(path: Path, field: str) -> Path:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise SecureArtifactSnapshotError(
            f"{field} must be an absolute normalized path"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise SecureArtifactSnapshotError(f"{field} is missing or unsafe") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise SecureArtifactSnapshotError(
            f"{field} must be a non-link, non-reparse directory"
        )
    return path


def _relative_entry(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise SecureArtifactSnapshotError(
            "snapshot entry must be a normalized POSIX relative path"
        )
    value = PurePosixPath(raw)
    if (
        value.is_absolute()
        or value.as_posix() != raw
        or raw == "."
        or any(part in {"", ".", ".."} for part in value.parts)
        or any(any(ord(character) < 32 for character in part) for part in value.parts)
    ):
        raise SecureArtifactSnapshotError(
            "snapshot entry must be a normalized POSIX relative path"
        )
    for component in value.parts:
        _validate_name(component)
    return raw


def _validate_name(name: str) -> None:
    try:
        encoded = name.encode("utf-8")
    except UnicodeError as error:
        raise SecureArtifactSnapshotError("artifact path is not valid UTF-8") from error
    windows_basename = name.split(".", maxsplit=1)[0].upper()
    if (
        not name
        or name in {".", ".."}
        or unicodedata.normalize("NFKC", name) != name
        or b"/" in encoded
        or b"\\" in encoded
        or b":" in encoded
        or any(byte < 32 for byte in encoded)
        or name.endswith((".", " "))
        or windows_basename in _WINDOWS_RESERVED_NAMES
    ):
        raise SecureArtifactSnapshotError(
            "artifact path contains a non-portable or unsafe name"
        )


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise SecureArtifactSnapshotError(
            "POSIX snapshot traversal requires O_NOFOLLOW and O_DIRECTORY"
        )
    return int(os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0))


@contextmanager
def _open_posix_root(path: Path) -> Iterator[int]:
    """Open an absolute directory one component at a time from the filesystem root."""

    flags = _directory_flags()
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            _validate_name(component)
            child = -1
            try:
                child = os.open(component, flags, dir_fd=descriptor)
                child_metadata = os.fstat(child)
            except OSError:
                if child >= 0:
                    os.close(child)
                raise
            if not stat.S_ISDIR(child_metadata.st_mode) or _is_reparse(child_metadata):
                os.close(child)
                raise SecureArtifactSnapshotError(
                    "source root contains a link or reparse point"
                )
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise SecureArtifactSnapshotError(
            "source root contains a missing or unsafe component"
        ) from error
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_posix_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    descriptor = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
                os.close(child)
                raise SecureArtifactSnapshotError(
                    "artifact parent is a link or reparse point"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except OSError as error:
        os.close(descriptor)
        raise SecureArtifactSnapshotError(
            "artifact parent changed or is unsafe"
        ) from error


def _add_inventory_file(
    files: dict[str, _Identity],
    relative: str,
    metadata: os.stat_result,
    limits: SnapshotLimits,
    *,
    allow_hardlinks: bool = False,
) -> None:
    if relative in files:
        raise SecureArtifactSnapshotError("snapshot entries overlap or are duplicated")
    if metadata.st_nlink < 1:
        raise SecureArtifactSnapshotError("artifact identity is unavailable")
    if not allow_hardlinks and metadata.st_nlink != 1:
        raise SecureArtifactSnapshotError("artifact hard links are forbidden")
    if metadata.st_size > limits.max_file_size:
        raise SecureArtifactSnapshotError("artifact exceeds the per-file size limit")
    if len(files) >= limits.max_files:
        raise SecureArtifactSnapshotError("artifact count exceeds the file limit")
    files[relative] = _identity(metadata)
    if sum(item.size for item in files.values()) > limits.max_total_size:
        raise SecureArtifactSnapshotError("artifacts exceed the total size limit")


def _walk_posix_directory(
    descriptor: int,
    relative: str,
    *,
    files: dict[str, _Identity],
    directories: dict[str, _Identity],
    limits: SnapshotLimits,
) -> None:
    if len(PurePosixPath(relative).parts) > limits.max_depth:
        raise SecureArtifactSnapshotError("artifact tree exceeds the depth limit")
    try:
        names = sorted(os.listdir(descriptor), key=lambda value: value.encode("utf-8"))
    except (OSError, UnicodeError) as error:
        raise SecureArtifactSnapshotError(
            "artifact directory cannot be enumerated"
        ) from error
    folded: set[str] = set()
    for name in names:
        _validate_name(name)
        normalized = name.casefold()
        if normalized in folded:
            raise SecureArtifactSnapshotError(
                "artifact directory has a case-insensitive path collision"
            )
        folded.add(normalized)
        child_relative = f"{relative}/{name}" if relative else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "artifact entry changed or is unsafe"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise SecureArtifactSnapshotError(
                "artifact links and reparse points are forbidden"
            )
        if stat.S_ISREG(metadata.st_mode):
            _add_inventory_file(files, child_relative, metadata, limits)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecureArtifactSnapshotError("artifact contains a non-regular entry")
        try:
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "artifact directory changed or is unsafe"
            ) from error
        try:
            opened = os.fstat(child)
            if _identity(opened) != _identity(metadata):
                raise SecureArtifactSnapshotError(
                    "artifact directory changed while opening"
                )
            if child_relative in directories:
                raise SecureArtifactSnapshotError(
                    "snapshot entries overlap or are duplicated"
                )
            directories[child_relative] = _identity(opened)
            _walk_posix_directory(
                child,
                child_relative,
                files=files,
                directories=directories,
                limits=limits,
            )
        finally:
            os.close(child)


def _inventory_posix(
    root_fd: int, entries: Sequence[str], limits: SnapshotLimits
) -> _Inventory:
    files: dict[str, _Identity] = {}
    directories: dict[str, _Identity] = {}
    for relative in entries:
        parent, name = _open_posix_parent(root_fd, relative)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise SecureArtifactSnapshotError(
                    "artifact links and reparse points are forbidden"
                )
            if stat.S_ISREG(metadata.st_mode):
                _add_inventory_file(files, relative, metadata, limits)
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise SecureArtifactSnapshotError(
                    "snapshot entry must be a regular file or directory"
                )
            directory = os.open(name, _directory_flags(), dir_fd=parent)
            try:
                opened = os.fstat(directory)
                if _identity(opened) != _identity(metadata):
                    raise SecureArtifactSnapshotError(
                        "snapshot directory changed while opening"
                    )
                if relative in directories:
                    raise SecureArtifactSnapshotError(
                        "snapshot entries overlap or are duplicated"
                    )
                directories[relative] = _identity(opened)
                _walk_posix_directory(
                    directory,
                    relative,
                    files=files,
                    directories=directories,
                    limits=limits,
                )
            finally:
                os.close(directory)
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "snapshot entry is missing or unsafe"
            ) from error
        finally:
            os.close(parent)
    if not files:
        raise SecureArtifactSnapshotError("snapshot selection contains no files")
    inventory = _Inventory(files=files, directories=directories)
    _validate_global_collisions(inventory)
    return inventory


def _validate_global_collisions(inventory: _Inventory) -> None:
    """Reject collisions across separately selected entries and their subtrees."""

    folded: dict[str, str] = {}
    for relative in sorted(
        (*inventory.files, *inventory.directories), key=lambda value: value.encode()
    ):
        key = unicodedata.normalize("NFKC", relative).casefold()
        previous = folded.get(key)
        if previous is not None and previous != relative:
            raise SecureArtifactSnapshotError(
                "artifact selection has a case-insensitive path collision"
            )
        folded[key] = relative


def _open_windows_directory_handle(
    path: Path, *, share_mode: int = _WINDOWS_FILE_SHARE_READ
) -> int:
    """Open and pin one directory path without following its reparse point."""

    if share_mode not in {_WINDOWS_FILE_SHARE_READ, _WINDOWS_DIRECTORY_SHARE}:
        raise SecureArtifactSnapshotError("Windows directory share mode is invalid")

    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    get_info = kernel32.GetFileInformationByHandleEx
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x80000000,
        share_mode,
        None,
        3,
        0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(get_last_error(), "artifact ancestor could not be opened")
    info = FileAttributeTagInfo()
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = get_last_error()
        close_handle(handle)
        raise OSError(error, "artifact ancestor could not be inspected")
    if info.file_attributes & _REPARSE_ATTRIBUTE:
        close_handle(handle)
        raise SecureArtifactSnapshotError(
            "Windows directory reparse points are forbidden"
        )
    if not info.file_attributes & 0x10:
        close_handle(handle)
        raise SecureArtifactSnapshotError(
            "Windows artifact ancestor is not a directory"
        )
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    import ctypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(handle):
        raise OSError("Windows directory handle could not be closed")


def _windows_ancestor_paths(path: Path) -> tuple[Path, ...]:
    current = Path(path.anchor)
    paths = [current]
    for component in path.parts[1:]:
        current = current / component
        paths.append(current)
    return tuple(paths)


def _windows_retry_sleep(delay: float) -> None:
    time.sleep(delay)


def _windows_error_code(error: OSError) -> int | None:
    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int):
        return winerror
    return error.errno if isinstance(error.errno, int) else None


def _open_windows_source_handles(path: Path) -> list[int]:
    for attempt in range(len(_WINDOWS_SHARING_RETRY_DELAYS) + 1):
        handles: list[int] = []
        try:
            for component in _windows_ancestor_paths(path):
                handles.append(
                    _open_windows_directory_handle(
                        component, share_mode=_WINDOWS_DIRECTORY_SHARE
                    )
                )
            return handles
        except SecureArtifactSnapshotError:
            for handle in reversed(handles):
                _close_windows_handle(handle)
            raise
        except OSError as error:
            for handle in reversed(handles):
                _close_windows_handle(handle)
            code = _windows_error_code(error)
            if code in _WINDOWS_TRANSIENT_SHARING_ERRORS and attempt < len(
                _WINDOWS_SHARING_RETRY_DELAYS
            ):
                _windows_retry_sleep(_WINDOWS_SHARING_RETRY_DELAYS[attempt])
                continue
            detail = f" (Windows error {code})" if code is not None else ""
            raise SecureArtifactSnapshotError(
                f"source root contains a missing, mutable, or unsafe component{detail}"
            ) from error
    raise AssertionError("Windows sharing retry loop did not terminate")


@contextmanager
def _hold_windows_source_root(path: Path) -> Iterator[None]:
    """Pin every directory without allowing replacement during source reads.

    Directory handles permit concurrent writes because build and antivirus processes can
    legitimately retain write-capable handles.  Deliberately omitting ``FILE_SHARE_DELETE``
    still prevents any held path component from being renamed or replaced; individual
    artifact files remain opened read-share-only and the complete inventory is rechecked.
    """

    handles = _open_windows_source_handles(path)
    try:
        yield
    except SecureArtifactSnapshotError:
        raise
    except OSError as error:
        code = _windows_error_code(error)
        detail = f" (Windows error {code})" if code is not None else ""
        raise SecureArtifactSnapshotError(
            f"source root contains a missing, mutable, or unsafe component{detail}"
        ) from error
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


def _inventory_windows(
    root: Path, entries: Sequence[str], limits: SnapshotLimits
) -> _Inventory:
    # Native Windows build tools legitimately reuse files through hard links. The
    # enclosing snapshot holds every ancestor without delete sharing, opens each file
    # with read sharing only, and compares a second complete identity inventory, so an
    # alias cannot mutate a file unnoticed while it is consumed. POSIX keeps the
    # stricter one-link policy because it has no equivalent mandatory sharing lock.
    files: dict[str, _Identity] = {}
    directories: dict[str, _Identity] = {}

    def walk(directory: Path, relative: str) -> None:
        if len(PurePosixPath(relative).parts) > limits.max_depth:
            raise SecureArtifactSnapshotError("artifact tree exceeds the depth limit")
        try:
            scanned = sorted(os.scandir(directory), key=lambda item: item.name.encode())
        except (OSError, UnicodeError) as error:
            raise SecureArtifactSnapshotError(
                "artifact directory cannot be enumerated"
            ) from error
        folded: set[str] = set()
        for entry in scanned:
            _validate_name(entry.name)
            normalized = entry.name.casefold()
            if normalized in folded:
                raise SecureArtifactSnapshotError(
                    "artifact directory has a case-insensitive path collision"
                )
            folded.add(normalized)
            child_relative = f"{relative}/{entry.name}" if relative else entry.name
            child_path = Path(entry.path)
            try:
                # DirEntry.stat() leaves dev/inode/nlink as zero on Windows. A
                # full path stat is required for the stable identity compared
                # with the later handle fstat.
                metadata = child_path.stat(follow_symlinks=False)
            except OSError as error:
                raise SecureArtifactSnapshotError(
                    "artifact entry changed or is unsafe"
                ) from error
            if entry.is_symlink() or _is_reparse(metadata):
                raise SecureArtifactSnapshotError(
                    "artifact links and reparse points are forbidden"
                )
            if stat.S_ISREG(metadata.st_mode):
                try:
                    opened = _stat_windows_source_file(child_path)
                except OSError as error:
                    raise SecureArtifactSnapshotError(
                        "artifact entry changed or is unsafe"
                    ) from error
                _add_inventory_file(
                    files, child_relative, opened, limits, allow_hardlinks=True
                )
            elif stat.S_ISDIR(metadata.st_mode):
                if child_relative in directories:
                    raise SecureArtifactSnapshotError(
                        "snapshot entries overlap or are duplicated"
                    )
                directories[child_relative] = _identity(metadata)
                walk(child_path, child_relative)
            else:
                raise SecureArtifactSnapshotError(
                    "artifact contains a non-regular entry"
                )

    for relative in entries:
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "snapshot entry is missing or unsafe"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise SecureArtifactSnapshotError(
                "artifact links and reparse points are forbidden"
            )
        if stat.S_ISREG(metadata.st_mode):
            try:
                opened = _stat_windows_source_file(path)
            except OSError as error:
                raise SecureArtifactSnapshotError(
                    "snapshot entry is missing or unsafe"
                ) from error
            _add_inventory_file(files, relative, opened, limits, allow_hardlinks=True)
        elif stat.S_ISDIR(metadata.st_mode):
            if relative in directories:
                raise SecureArtifactSnapshotError(
                    "snapshot entries overlap or are duplicated"
                )
            directories[relative] = _identity(metadata)
            walk(path, relative)
        else:
            raise SecureArtifactSnapshotError(
                "snapshot entry must be a regular file or directory"
            )
    if not files:
        raise SecureArtifactSnapshotError("snapshot selection contains no files")
    inventory = _Inventory(files=files, directories=directories)
    _validate_global_collisions(inventory)
    return inventory


def _open_windows_non_reparse(path: Path) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
    binary_flag = int(getattr(os, "O_BINARY", 0))
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    get_info = kernel32.GetFileInformationByHandleEx
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x80000000,
        _WINDOWS_FILE_SHARE_READ,
        None,
        3,
        0x00200000 | 0x08000000,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(get_last_error(), "artifact could not be opened")
    info = FileAttributeTagInfo()
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = get_last_error()
        close_handle(handle)
        raise OSError(error, "artifact could not be inspected")
    if info.file_attributes & _REPARSE_ATTRIBUTE:
        close_handle(handle)
        raise SecureArtifactSnapshotError("Windows reparse points are forbidden")
    if info.file_attributes & 0x10:
        close_handle(handle)
        raise SecureArtifactSnapshotError("artifact must be a regular file")
    try:
        return int(open_osfhandle(int(handle), os.O_RDONLY | binary_flag))
    except OSError:
        close_handle(handle)
        raise


def _stat_windows_source_file(path: Path) -> os.stat_result:
    """Read exact Windows change time and identity from a secured file handle."""

    if not _running_on_windows():
        return path.stat(follow_symlinks=False)
    descriptor = _open_windows_non_reparse(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
            raise SecureArtifactSnapshotError("artifact is no longer a regular file")
        return metadata
    finally:
        os.close(descriptor)


@contextmanager
def _open_source_file(
    root: Path, root_fd: int | None, relative: str, expected: _Identity
) -> Iterator[BinaryIO]:
    descriptor = -1
    parent = -1
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        if _running_on_windows():
            descriptor = _open_windows_non_reparse(path)
        else:
            if root_fd is None:
                raise SecureArtifactSnapshotError(
                    "POSIX root descriptor is unavailable"
                )
            parent, name = _open_posix_parent(root_fd, relative)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise SecureArtifactSnapshotError(
                    "POSIX snapshot reads require O_NOFOLLOW"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
            raise SecureArtifactSnapshotError("artifact is no longer a regular file")
        if _identity(before) != expected:
            raise SecureArtifactSnapshotError("artifact changed before it was read")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
        after = os.fstat(stream.fileno())
        if _identity(after) != expected:
            raise SecureArtifactSnapshotError("artifact changed while it was read")
        if _running_on_windows():
            path_after = _stat_windows_source_file(path)
        else:
            path_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(path_after) != expected or _is_reparse(path_after):
            raise SecureArtifactSnapshotError("artifact path changed while it was read")
    except SecureArtifactSnapshotError:
        raise
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "artifact could not be read safely"
        ) from error
    finally:
        if "stream" in locals():
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)


def _same_object(first: _Identity, second: _Identity) -> bool:
    return (first.device, first.inode) == (second.device, second.inode)


def _validate_private_directory_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or path.name in {"", ".", ".."}
    ):
        raise SecureArtifactSnapshotError(
            "private directory must be an absolute normalized path"
        )
    _validate_name(path.name)
    _absolute_directory(path.parent, "private directory parent")


def _remove_owned_tree(root: Path, expected: _Identity | None = None) -> None:
    """Best-effort Windows rollback; never follows or deletes a replacement root."""

    try:
        metadata = root.stat(follow_symlinks=False)
    except OSError:
        return
    if expected is not None and not _same_object(_identity(metadata), expected):
        return
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        return
    for entry in os.scandir(root):
        path = Path(entry.path)
        child = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(child.st_mode) and not _is_reparse(child):
            os.chmod(path, 0o700)
            _remove_owned_tree(path, _identity(child))
        elif stat.S_ISREG(child.st_mode):
            current = path.stat(follow_symlinks=False)
            if _same_object(_identity(current), _identity(child)):
                os.chmod(path, 0o600, follow_symlinks=False)
                path.unlink(missing_ok=True)
        else:
            return
    current_root = root.stat(follow_symlinks=False)
    if _same_object(_identity(current_root), _identity(metadata)):
        os.chmod(root, 0o700)
        root.rmdir()


def _remove_posix_contents(descriptor: int) -> None:
    os.fchmod(descriptor, 0o700)
    for name in sorted(os.listdir(descriptor), key=lambda value: value.encode()):
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                if not _same_object(_identity(os.fstat(child)), _identity(before)):
                    raise SecureArtifactSnapshotError(
                        "rollback directory identity changed"
                    )
                _remove_posix_contents(child)
            finally:
                os.close(child)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not _same_object(_identity(after), _identity(before)):
                raise SecureArtifactSnapshotError("rollback directory was replaced")
            os.rmdir(name, dir_fd=descriptor)
        elif stat.S_ISREG(before.st_mode):
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not _same_object(_identity(after), _identity(before)):
                raise SecureArtifactSnapshotError("rollback file was replaced")
            os.unlink(name, dir_fd=descriptor)
        else:
            raise SecureArtifactSnapshotError("rollback encountered an unsafe entry")


def _rollback_posix_private_directory(lease: _PrivateDirectoryLease) -> None:
    if lease.parent_fd is None or lease.root_fd is None:
        return
    try:
        current = os.stat(
            lease.path.name, dir_fd=lease.parent_fd, follow_symlinks=False
        )
    except OSError:
        return
    if not _same_object(_identity(current), lease.identity):
        return
    _remove_posix_contents(lease.root_fd)
    current = os.stat(lease.path.name, dir_fd=lease.parent_fd, follow_symlinks=False)
    if _same_object(_identity(current), lease.identity):
        os.rmdir(lease.path.name, dir_fd=lease.parent_fd)


@contextmanager
def _create_private_directory(path: Path) -> Iterator[_PrivateDirectoryLease]:
    """Create and pin one private directory, rolling back only that exact object."""

    _validate_private_directory_path(path)
    if _running_on_windows():
        created_identity: _Identity | None = None
        target_handle: int | None = None
        allowed_sids = _expected_windows_private_sids()
        with _hold_windows_source_root(path.parent):
            try:
                _set_windows_private_dacl(path, allowed_sids, create=True)
                created_identity = _identity(path.stat(follow_symlinks=False))
                # The leased directory must remain writable while its identity is
                # pinned.  Omitting DELETE sharing still prevents replacement.
                target_handle = _open_windows_directory_handle(
                    path, share_mode=_WINDOWS_DIRECTORY_SHARE
                )
                if not _same_object(
                    _identity(path.stat(follow_symlinks=False)), created_identity
                ):
                    raise SecureArtifactSnapshotError(
                        "private directory identity changed while opening"
                    )
                lease = _PrivateDirectoryLease(path, created_identity, None, None)
                yield lease
                if not _same_object(
                    _identity(path.stat(follow_symlinks=False)), created_identity
                ):
                    raise SecureArtifactSnapshotError("private directory was replaced")
                _verify_windows_private_acl(path, allowed_sids)
                return
            except FileExistsError as error:
                raise SecureArtifactSnapshotError(
                    "private directory must not already exist"
                ) from error
            except Exception:
                if target_handle is not None:
                    try:
                        _close_windows_handle(target_handle)
                    except Exception:
                        pass
                    finally:
                        target_handle = None
                if created_identity is not None:
                    try:
                        _remove_owned_tree(path, created_identity)
                    except Exception:
                        pass
                raise
            finally:
                if target_handle is not None:
                    _close_windows_handle(target_handle)

    with _open_posix_root(path.parent) as parent_fd:
        root_fd = -1
        posix_lease: _PrivateDirectoryLease | None = None
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(path.name, _directory_flags(), dir_fd=parent_fd)
            os.fchmod(root_fd, 0o700)
            created = _identity(os.fstat(root_fd))
            path_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_object(_identity(path_stat), created):
                raise SecureArtifactSnapshotError(
                    "private directory identity changed while opening"
                )
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
                raise SecureArtifactSnapshotError("private directory is not mode 0700")
            posix_lease = _PrivateDirectoryLease(path, created, parent_fd, root_fd)
            yield posix_lease
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_object(_identity(current), created) or not _same_object(
                _identity(os.fstat(root_fd)), created
            ):
                raise SecureArtifactSnapshotError("private directory was replaced")
        except FileExistsError as error:
            raise SecureArtifactSnapshotError(
                "private directory must not already exist"
            ) from error
        except Exception:
            if posix_lease is not None:
                _rollback_posix_private_directory(posix_lease)
            elif root_fd >= 0:
                partial = _PrivateDirectoryLease(
                    path, _identity(os.fstat(root_fd)), parent_fd, root_fd
                )
                _rollback_posix_private_directory(partial)
            raise
        finally:
            if root_fd >= 0:
                os.close(root_fd)


def prepare_private_directory(path: Path) -> Path:
    """Create an empty private directory suitable for a PowerShell workflow."""

    try:
        with _create_private_directory(path):
            pass
    except SecureArtifactSnapshotError:
        raise
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "private directory could not be prepared"
        ) from error
    return path


@contextmanager
def private_directory_lease(path: Path) -> Iterator[Path]:
    """Keep a newly created private directory pinned and roll it back on failure."""

    try:
        with _create_private_directory(path):
            yield path
    except SecureArtifactSnapshotError:
        raise
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "private directory lease could not be established"
        ) from error


def verify_private_directory(path: Path) -> Path:
    """Fail closed unless an existing directory is the exact private object."""

    _validate_private_directory_path(path)
    if _running_on_windows():
        with _hold_windows_source_root(path):
            before = _identity(path.stat(follow_symlinks=False))
            _verify_windows_private_acl(path, _expected_windows_private_sids())
            after = _identity(path.stat(follow_symlinks=False))
            if not _same_object(before, after):
                raise SecureArtifactSnapshotError(
                    "private directory changed during verification"
                )
        return path
    with _open_posix_root(path.parent) as parent_fd:
        descriptor = -1
        try:
            before_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            descriptor = os.open(path.name, _directory_flags(), dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            after_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not _same_object(_identity(before_stat), _identity(opened))
                or not _same_object(_identity(after_stat), _identity(opened))
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise SecureArtifactSnapshotError(
                    "private directory is replaced or not mode 0700"
                )
        except SecureArtifactSnapshotError:
            raise
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "private directory has a missing or unsafe component"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return path


def _open_private_output_parent(
    lease: _PrivateDirectoryLease, relative: str
) -> tuple[int | None, Path]:
    if lease.root_fd is None:
        current = lease.path
        allowed = _expected_windows_private_sids()
        for component in PurePosixPath(relative).parts[:-1]:
            current /= component
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            metadata = current.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
                raise SecureArtifactSnapshotError(
                    "private snapshot directory is unsafe"
                )
            os.chmod(current, 0o700)
            _apply_windows_private_acl(current, allowed)
        return None, current

    descriptor = os.dup(lease.root_fd)
    try:
        for component in PurePosixPath(relative).parts[:-1]:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise SecureArtifactSnapshotError(
                    "private snapshot directory is unsafe"
                )
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            opened = os.fstat(child)
            if not _same_object(_identity(opened), _identity(before)):
                os.close(child)
                raise SecureArtifactSnapshotError(
                    "private snapshot directory changed while opening"
                )
            os.fchmod(child, 0o700)
            os.close(descriptor)
            descriptor = child
        return descriptor, lease.path.joinpath(*PurePosixPath(relative).parts[:-1])
    except Exception:
        os.close(descriptor)
        raise


def _write_snapshot_file(
    lease: _PrivateDirectoryLease,
    source_root: Path,
    root_fd: int | None,
    relative: str,
    expected: _Identity,
    limits: SnapshotLimits,
) -> SnapshotFile:
    output = lease.path.joinpath(*PurePosixPath(relative).parts)
    output_parent_fd, _output_parent = _open_private_output_parent(lease, relative)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = (
            os.open(
                output.name,
                flags,
                0o600,
                dir_fd=output_parent_fd,
            )
            if output_parent_fd is not None
            else os.open(output, flags, 0o600)
        )
    except OSError as error:
        raise SecureArtifactSnapshotError(
            "private snapshot file could not be created"
        ) from error
    if _running_on_windows():
        try:
            _apply_windows_private_acl(output)
        except Exception:
            os.close(descriptor)
            output.unlink(missing_ok=True)
            raise
    digest = hashlib.sha256()
    written = 0
    stream: BinaryIO | None = None
    try:
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with stream as target:
            with _open_source_file(source_root, root_fd, relative, expected) as source:
                while block := source.read(_READ_BLOCK):
                    written += len(block)
                    if written > limits.max_file_size:
                        raise SecureArtifactSnapshotError(
                            "artifact grew beyond the per-file size limit"
                        )
                    target.write(block)
                    digest.update(block)
            target.flush()
            os.fsync(target.fileno())
            if not _running_on_windows():
                os.fchmod(target.fileno(), 0o400)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if output_parent_fd is not None:
                os.unlink(output.name, dir_fd=output_parent_fd)
            else:
                output.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if output_parent_fd is not None:
            os.close(output_parent_fd)
    if written != expected.size:
        raise SecureArtifactSnapshotError("artifact size changed while it was read")
    if _running_on_windows():
        os.chmod(output, 0o400)
    return SnapshotFile(path=relative, size=written, sha256=digest.hexdigest())


def _verify_posix_snapshot(
    descriptor: int, prefix: str, actual: dict[str, SnapshotFile]
) -> None:
    metadata = os.fstat(descriptor)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SecureArtifactSnapshotError("snapshot directory is not owner-only")
    for name in sorted(os.listdir(descriptor), key=lambda value: value.encode()):
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                if not _same_object(_identity(os.fstat(child)), _identity(before)):
                    raise SecureArtifactSnapshotError(
                        "snapshot directory changed during verification"
                    )
                _verify_posix_snapshot(child, relative, actual)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise SecureArtifactSnapshotError("snapshot contains an unsafe entry")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SecureArtifactSnapshotError(
                "POSIX snapshot verification requires O_NOFOLLOW"
            )
        file_fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=descriptor)
        try:
            opened = os.fstat(file_fd)
            if not _same_object(_identity(opened), _identity(before)):
                raise SecureArtifactSnapshotError(
                    "snapshot file changed during verification"
                )
            digest = hashlib.sha256()
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                for block in iter(lambda: stream.read(_READ_BLOCK), b""):
                    digest.update(block)
            after = os.fstat(file_fd)
            path_after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _identity(after) != _identity(opened) or not _same_object(
                _identity(path_after), _identity(opened)
            ):
                raise SecureArtifactSnapshotError(
                    "snapshot file changed during verification"
                )
            if stat.S_IMODE(after.st_mode) & 0o077:
                raise SecureArtifactSnapshotError("snapshot file is not owner-only")
            actual[relative] = SnapshotFile(relative, after.st_size, digest.hexdigest())
        finally:
            os.close(file_fd)


def _verify_private_snapshot(
    destination: Path,
    files: Sequence[SnapshotFile],
    lease: _PrivateDirectoryLease | None = None,
) -> None:
    if not _running_on_windows():
        if lease is not None and lease.root_fd is not None:
            actual: dict[str, SnapshotFile] = {}
            _verify_posix_snapshot(lease.root_fd, "", actual)
        else:
            with _open_posix_root(destination) as descriptor:
                actual = {}
                _verify_posix_snapshot(descriptor, "", actual)
        expected_records = {item.path: item for item in files}
        if set(actual) != set(expected_records):
            raise SecureArtifactSnapshotError(
                "snapshot contents changed after creation"
            )
        if actual != expected_records:
            raise SecureArtifactSnapshotError("snapshot verification digest mismatch")
        return

    expected_paths = {item.path for item in files}
    actual_paths: set[str] = set()
    windows_sids = _expected_windows_private_sids() if _running_on_windows() else None
    for directory, names, filenames in os.walk(destination, followlinks=False):
        directory_path = Path(directory)
        metadata = directory_path.stat(follow_symlinks=False)
        if _is_reparse(metadata) or (
            not _running_on_windows() and stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SecureArtifactSnapshotError("snapshot directory is not owner-only")
        if windows_sids is not None:
            _verify_windows_private_acl(directory_path, windows_sids)
        for name in names:
            child = directory_path / name
            child_metadata = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(child_metadata.st_mode) or _is_reparse(child_metadata):
                raise SecureArtifactSnapshotError(
                    "snapshot contains an unsafe directory"
                )
        for name in filenames:
            child = directory_path / name
            relative = child.relative_to(destination).as_posix()
            actual_paths.add(relative)
            child_metadata = child.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(child_metadata.st_mode)
                or _is_reparse(child_metadata)
                or (
                    not _running_on_windows()
                    and stat.S_IMODE(child_metadata.st_mode) & 0o077
                )
            ):
                raise SecureArtifactSnapshotError(
                    "snapshot file is not owner-only and regular"
                )
            if windows_sids is not None:
                _verify_windows_private_acl(child, windows_sids)
    if actual_paths != expected_paths:
        raise SecureArtifactSnapshotError("snapshot contents changed after creation")
    expected_by_path = {item.path: item for item in files}
    for relative in sorted(actual_paths, key=lambda value: value.encode()):
        path = destination.joinpath(*PurePosixPath(relative).parts)
        digest = hashlib.sha256()
        try:
            if _running_on_windows():
                descriptor = _open_windows_non_reparse(path)
            else:
                nofollow = getattr(os, "O_NOFOLLOW", None)
                if nofollow is None:
                    raise SecureArtifactSnapshotError(
                        "POSIX snapshot verification requires O_NOFOLLOW"
                    )
                descriptor = os.open(path, os.O_RDONLY | nofollow)
            with os.fdopen(descriptor, "rb") as stream:
                for block in iter(lambda: stream.read(_READ_BLOCK), b""):
                    digest.update(block)
        except OSError as error:
            raise SecureArtifactSnapshotError(
                "snapshot could not be verified"
            ) from error
        expected = expected_by_path[relative]
        if (
            path.stat().st_size != expected.size
            or digest.hexdigest() != expected.sha256
        ):
            raise SecureArtifactSnapshotError("snapshot verification digest mismatch")


def _snapshot_digest(files: Sequence[SnapshotFile]) -> str:
    value = [
        {"path": item.path, "size": item.size, "sha256": item.sha256} for item in files
    ]
    canonical = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _finalize_posix_snapshot(descriptor: int) -> None:
    for name in sorted(os.listdir(descriptor), key=lambda value: value.encode()):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                if not _same_object(_identity(os.fstat(child)), _identity(metadata)):
                    raise SecureArtifactSnapshotError(
                        "snapshot directory changed during finalization"
                    )
                _finalize_posix_snapshot(child)
                os.fchmod(child, 0o500)
            finally:
                os.close(child)
    os.fchmod(descriptor, 0o500)


def snapshot_artifacts(
    source_root: Path,
    entries: Sequence[str],
    destination: Path,
    *,
    limits: SnapshotLimits = SnapshotLimits(),
) -> SnapshotResult:
    """Copy selected artifacts once into a verified owner-only snapshot.

    ``destination`` must not exist.  A failed call attempts to remove only the exact
    private destination it created.  If that rollback cannot complete, the primary
    failure is preserved and the caller must discard the isolated run parent.
    Callers must pass ``SnapshotResult.root`` (and no path below ``source_root``) to
    every later packaging or signing operation.
    """

    limits.validate()
    source = _absolute_directory(source_root, "artifact source root")
    normalized_entries = tuple(_relative_entry(entry) for entry in entries)
    if not normalized_entries:
        raise SecureArtifactSnapshotError("at least one snapshot entry is required")
    if len(set(normalized_entries)) != len(normalized_entries):
        raise SecureArtifactSnapshotError("snapshot entries must be unique")

    try:
        common = Path(os.path.commonpath((source, destination)))
    except ValueError:
        common = None
    if common == source:
        raise SecureArtifactSnapshotError(
            "snapshot destination must be outside the mutable source tree"
        )

    with _create_private_directory(destination) as lease:
        if _running_on_windows():
            with _hold_windows_source_root(source):
                first = _inventory_windows(source, normalized_entries, limits)
                root_fd: int | None = None
                root_identity = _identity(source.stat(follow_symlinks=False))
                records = tuple(
                    _write_snapshot_file(
                        lease,
                        source,
                        root_fd,
                        relative,
                        first.files[relative],
                        limits,
                    )
                    for relative in sorted(
                        first.files, key=lambda value: value.encode()
                    )
                )
                second = _inventory_windows(source, normalized_entries, limits)
                if _identity(source.stat(follow_symlinks=False)) != root_identity:
                    raise SecureArtifactSnapshotError("artifact source root changed")
        else:
            with _open_posix_root(source) as opened_root:
                root_identity = _identity(os.fstat(opened_root))
                if _identity(source.stat(follow_symlinks=False)) != root_identity:
                    raise SecureArtifactSnapshotError("artifact source root changed")
                first = _inventory_posix(opened_root, normalized_entries, limits)
                records = tuple(
                    _write_snapshot_file(
                        lease,
                        source,
                        opened_root,
                        relative,
                        first.files[relative],
                        limits,
                    )
                    for relative in sorted(
                        first.files, key=lambda value: value.encode()
                    )
                )
                second = _inventory_posix(opened_root, normalized_entries, limits)
                if (
                    _identity(os.fstat(opened_root)) != root_identity
                    or _identity(source.stat(follow_symlinks=False)) != root_identity
                ):
                    raise SecureArtifactSnapshotError("artifact source root changed")
        if first != second:
            raise SecureArtifactSnapshotError(
                "artifact tree changed while snapshotting"
            )
        total_size = sum(item.size for item in records)
        if total_size > limits.max_total_size:
            raise SecureArtifactSnapshotError("artifacts exceed the total size limit")
        _verify_private_snapshot(destination, records, lease)
        if lease.root_fd is not None:
            _finalize_posix_snapshot(lease.root_fd)
        else:
            for directory, names, _files in os.walk(destination, topdown=False):
                for name in names:
                    os.chmod(Path(directory) / name, 0o500)
                os.chmod(directory, 0o500)
        return SnapshotResult(
            root=destination,
            files=records,
            file_count=len(records),
            total_size=total_size,
            snapshot_sha256=_snapshot_digest(records),
        )


def _emit_github_error_annotation(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title=Secure artifact snapshot::{escaped}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a private immutable-input snapshot for NSIS repackaging."
    )
    private_mode = parser.add_mutually_exclusive_group()
    private_mode.add_argument("--prepare-private-directory", type=Path)
    private_mode.add_argument("--verify-private-directory", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--entry", action="append")
    parser.add_argument("--max-files", type=int, default=SnapshotLimits.max_files)
    parser.add_argument(
        "--max-file-size", type=int, default=SnapshotLimits.max_file_size
    )
    parser.add_argument(
        "--max-total-size", type=int, default=SnapshotLimits.max_total_size
    )
    parser.add_argument("--max-depth", type=int, default=SnapshotLimits.max_depth)
    arguments = parser.parse_args(argv)
    try:
        private_path = (
            arguments.prepare_private_directory or arguments.verify_private_directory
        )
        if private_path is not None:
            if any(
                value is not None
                for value in (
                    arguments.source_root,
                    arguments.destination,
                    arguments.entry,
                )
            ):
                parser.error(
                    "private-directory mode cannot be combined with snapshot arguments"
                )
            action = (
                "created"
                if arguments.prepare_private_directory is not None
                else "verified"
            )
            if action == "created":
                prepare_private_directory(private_path)
            else:
                verify_private_directory(private_path)
            print(
                json.dumps(
                    {
                        "schema": "stock-desk-private-directory-v1",
                        "status": action,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        if (
            arguments.source_root is None
            or arguments.destination is None
            or not arguments.entry
        ):
            parser.error(
                "snapshot mode requires --source-root, --destination, and --entry"
            )
        result = snapshot_artifacts(
            arguments.source_root,
            arguments.entry,
            arguments.destination,
            limits=SnapshotLimits(
                max_files=arguments.max_files,
                max_file_size=arguments.max_file_size,
                max_total_size=arguments.max_total_size,
                max_depth=arguments.max_depth,
            ),
        )
    except SecureArtifactSnapshotError as error:
        _emit_github_error_annotation(str(error))
        parser.error(str(error))
    print(json.dumps(result.summary(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
