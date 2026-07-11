from collections.abc import Callable
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import platform
from typing import cast

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


V11_PRODUCT_DIRECTORY = "Stock Desk"
V11_DATA_VERSION = "v1.1"


class _WindowsGuid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


_FOLDER_ID_LOCAL_APP_DATA = _WindowsGuid(
    0xF1B32785,
    0x6FBA,
    0x4FCF,
    (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
)


def _windows_local_app_data_known_folder() -> Path:
    if os.name != "nt":
        raise OSError("Windows Known Folder API is unavailable")
    win_dll = cast(Callable[..., ctypes.CDLL], getattr(ctypes, "WinDLL"))
    shell32 = win_dll("shell32", use_last_error=True)
    ole32 = win_dll("ole32", use_last_error=True)
    output = ctypes.c_wchar_p()
    get_known_folder = shell32.SHGetKnownFolderPath
    get_known_folder.argtypes = (
        ctypes.POINTER(_WindowsGuid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    get_known_folder.restype = ctypes.c_long
    result = get_known_folder(
        ctypes.byref(_FOLDER_ID_LOCAL_APP_DATA),
        0,
        None,
        ctypes.byref(output),
    )
    if result != 0 or not output.value:
        raise OSError("Windows Known Folder API failed")
    try:
        return Path(output.value)
    finally:
        free_memory = ole32.CoTaskMemFree
        free_memory.argtypes = (ctypes.c_void_p,)
        free_memory.restype = None
        free_memory(output)


def resolve_v11_data_root(
    *,
    platform_name: str | None = None,
    known_folder_resolver: Callable[[], Path] | None = None,
) -> Path:
    """Resolve the isolated current-user data root for the Windows v1.1 app.

    The desktop host normally supplies the Windows known-folder path. This
    Python boundary validates the same current-user environment for tests and
    sidecar-only startup without probing or migrating any legacy directory.
    """
    resolved_platform = platform.system() if platform_name is None else platform_name
    if resolved_platform != "Windows":
        raise RuntimeError("the v1.1 desktop data root is Windows-only")
    resolver = (
        _windows_local_app_data_known_folder
        if known_folder_resolver is None
        else known_folder_resolver
    )
    try:
        local_app_data = resolver()
    except OSError as error:
        raise RuntimeError("current-user application data is unavailable") from error
    if not local_app_data.is_absolute():
        raise RuntimeError("current-user application data is unavailable")
    return local_app_data / V11_PRODUCT_DIRECTORY / V11_DATA_VERSION


class Settings(BaseSettings):
    """Runtime settings loaded from Stock Desk environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="STOCK_DESK_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "stock-desk"
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/stock-desk.db"
    master_key: SecretStr | None = None
    web_dist_dir: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
