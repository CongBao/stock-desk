"""DACL-only Windows protection for current-user desktop data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


_SYSTEM_SID: Final = "S-1-5-18"
_ADMINISTRATORS_SID: Final = "S-1-5-32-544"
_FILE_ALL_ACCESS: Final = 0x001F01FF
_CONTAINER_AND_OBJECT_INHERIT: Final = 0x03


class WindowsAclError(RuntimeError):
    """The current-user data path could not be protected exactly."""


@dataclass(frozen=True, slots=True)
class _AclEntry:
    sid: str
    mask: int
    flags: int
    ace_type: int


@dataclass(frozen=True, slots=True)
class _Acl:
    protected: bool
    entries: tuple[_AclEntry, ...]


def _windows_private_sddl(allowed_sids: frozenset[str], *, directory: bool) -> str:
    inheritance = "OICI" if directory else ""
    return "D:P" + "".join(
        f"(A;{inheritance};FA;;;{sid})" for sid in sorted(allowed_sids)
    )


def _current_user_sid() -> str:  # pragma: no cover - native Windows API
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
        raise WindowsAclError("current Windows user SID is invalid")
    return value


def _set_private_dacl(  # pragma: no cover - native Windows API
    path: Path, allowed_sids: frozenset[str], *, directory: bool
) -> None:
    import ctypes
    from ctypes import wintypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
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
    sddl = _windows_private_sddl(allowed_sids, directory=directory)
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise OSError(get_last_error(), "private Windows DACL could not be built")
    try:
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
            0x00000004 | 0x80000000,  # DACL + PROTECTED_DACL only
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(int(result), "private Windows DACL could not be applied")
    finally:
        kernel32.LocalFree(descriptor)


def _read_private_dacl(path: Path) -> _Acl:  # pragma: no cover - native Windows API
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
        entries: list[_AclEntry] = []
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
                _AclEntry(
                    sid=sid,
                    mask=int(ace.mask),
                    flags=int(ace.header.ace_flags),
                    ace_type=int(ace.header.ace_type),
                )
            )
        return _Acl(protected=bool(control.value & 0x1000), entries=tuple(entries))
    finally:
        kernel32.LocalFree(descriptor)


def _verify_private_dacl(
    path: Path, allowed_sids: frozenset[str], *, directory: bool
) -> None:
    acl = _read_private_dacl(path)
    entries_by_sid: dict[str, _AclEntry] = {}
    for entry in acl.entries:
        if entry.sid in entries_by_sid:
            raise WindowsAclError("Windows private DACL has duplicate ACEs")
        entries_by_sid[entry.sid] = entry
    if not acl.protected or set(entries_by_sid) != set(allowed_sids):
        raise WindowsAclError(
            "Windows private DACL is inherited or permits an unexpected principal"
        )
    expected_flags = _CONTAINER_AND_OBJECT_INHERIT if directory else 0
    if any(
        entry.ace_type != 0
        or entry.mask != _FILE_ALL_ACCESS
        or entry.flags != expected_flags
        for entry in entries_by_sid.values()
    ):
        raise WindowsAclError("Windows private DACL is not exact full control")


def apply_windows_private_dacl(path: Path, *, directory: bool) -> None:
    """Protect an owned path without touching its owner or SACL."""

    allowed = frozenset({_current_user_sid(), _SYSTEM_SID, _ADMINISTRATORS_SID})
    try:
        _set_private_dacl(path, allowed, directory=directory)
        _verify_private_dacl(path, allowed, directory=directory)
    except WindowsAclError:
        raise
    except OSError as error:
        raise WindowsAclError(
            "Windows private DACL could not be established"
        ) from error
