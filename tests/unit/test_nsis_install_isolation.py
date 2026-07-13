import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
WINDOWS_CONFIG = ROOT / "src-tauri" / "tauri.windows.conf.json"
NSIS_TEMPLATE = ROOT / "packaging" / "nsis" / "installer.nsi"
NSIS_NOTICE = ROOT / "packaging" / "nsis" / "NOTICE.md"
NSIS_HOOKS = ROOT / "packaging" / "nsis" / "installer-hooks.nsh"
NSIS_LANGUAGES = ROOT / "packaging" / "nsis" / "languages"
UNINSTALL_SOURCE = ROOT / "src-tauri" / "src" / "uninstall.rs"
RUST_TOOLCHAIN = ROOT / "rust-toolchain.toml"
UPSTREAM_SHA256 = "20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079"
UPSTREAM_INSTALL_LINE = b'      StrCpy $INSTDIR "$LOCALAPPDATA\\${PRODUCTNAME}"\n'
LOCAL_INSTALL_LINE = (
    b'      StrCpy $INSTDIR "$LOCALAPPDATA\\Programs\\${PRODUCTNAME}"\n'
)
LOCAL_REPRODUCIBLE_TIMESTAMP_PATCH = (
    b"; Independent CI runners check out identical bytes with different mtimes.\n"
    b"; Do not serialize those host timestamps into the otherwise identical payload.\n"
    b"SetDateSave off\n\n"
)
USER_DATA_ROOT = r"$LOCALAPPDATA\Stock Desk\v1.1"
LEGACY_DATA_ROOT = r"$LOCALAPPDATA\stock-desk"


def _config() -> dict[str, object]:
    return json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))


def test_custom_nsis_template_has_only_the_two_auditable_local_patches() -> None:
    local = NSIS_TEMPLATE.read_bytes()

    assert local.count(LOCAL_INSTALL_LINE) == 1
    assert UPSTREAM_INSTALL_LINE not in local
    assert local.count(LOCAL_REPRODUCIBLE_TIMESTAMP_PATCH) == 1
    reconstructed = local.replace(LOCAL_REPRODUCIBLE_TIMESTAMP_PATCH, b"").replace(
        LOCAL_INSTALL_LINE, UPSTREAM_INSTALL_LINE
    )
    assert hashlib.sha256(reconstructed).hexdigest() == UPSTREAM_SHA256

    notice = NSIS_NOTICE.read_text(encoding="utf-8")
    assert "tauri-cli-v2.11.4" in notice
    assert UPSTREAM_SHA256 in notice
    assert hashlib.sha256(local).hexdigest() in notice
    assert UPSTREAM_INSTALL_LINE.decode().strip() in notice
    assert LOCAL_INSTALL_LINE.decode().strip() in notice
    assert "SetDateSave off" in notice


def test_nsis_configuration_has_no_reachable_admin_install_mode() -> None:
    config = _config()
    nsis = config["bundle"]["windows"]["nsis"]  # type: ignore[index]
    windows_override = json.loads(WINDOWS_CONFIG.read_text(encoding="utf-8"))
    source = NSIS_TEMPLATE.read_text(encoding="utf-8")

    assert nsis["installMode"] == "currentUser"
    assert nsis["template"] == "../packaging/nsis/installer.nsi"
    assert nsis["installerHooks"] == "../packaging/nsis/installer-hooks.nsh"
    assert nsis["customLanguageFiles"] == {
        "English": "../packaging/nsis/languages/English.nsh",
        "SimpChinese": "../packaging/nsis/languages/SimpChinese.nsh",
    }
    assert "installMode" not in windows_override.get("bundle", {}).get(
        "windows", {}
    ).get("nsis", {})
    assert (
        '!if "${INSTALLMODE}" == "currentUser"\n  RequestExecutionLevel user' in source
    )
    assert source.count("RequestExecutionLevel user") == 1
    assert source.count("RequestExecutionLevel admin") == 1
    assert (
        '!if "${INSTALLMODE}" == "perMachine"\n  RequestExecutionLevel admin' in source
    )


def test_program_and_data_roots_are_physically_separate_and_uninstall_is_safe() -> None:
    source = NSIS_TEMPLATE.read_text(encoding="utf-8")
    install_root = r"$LOCALAPPDATA\Programs\Stock Desk"
    identifier = str(_config()["identifier"])
    bundle_cleanup_roots = (
        rf"$APPDATA\{identifier}",
        rf"$LOCALAPPDATA\{identifier}",
    )

    assert LOCAL_INSTALL_LINE.decode().strip() in source
    assert install_root != USER_DATA_ROOT
    assert not USER_DATA_ROOT.startswith(install_root + "\\")
    assert not LEGACY_DATA_ROOT.startswith(install_root + "\\")
    assert f'RmDir /r "{USER_DATA_ROOT}"' not in source
    assert f'RmDir /r "{LEGACY_DATA_ROOT}"' not in source
    assert 'RmDir /r "$APPDATA\\${BUNDLEID}"' in source
    assert 'RmDir /r "$LOCALAPPDATA\\${BUNDLEID}"' in source
    assert identifier == "com.congbao.stockdesk"
    for cleanup_root in bundle_cleanup_roots:
        assert cleanup_root not in {USER_DATA_ROOT, LEGACY_DATA_ROOT}
        assert USER_DATA_ROOT not in cleanup_root
        assert LEGACY_DATA_ROOT not in cleanup_root


def test_v11_data_cleanup_is_explicit_default_off_and_never_uses_nsis_rmdir() -> None:
    source = NSIS_TEMPLATE.read_text(encoding="utf-8")
    hooks = NSIS_HOOKS.read_text(encoding="utf-8")
    languages = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(NSIS_LANGUAGES.glob("*.nsh"))
    }

    assert "SendMessage $DeleteAppDataCheckbox ${BM_SETCHECK}" not in source
    assert 'RmDir /r "$LOCALAPPDATA\\Stock Desk\\v1.1"' not in source
    assert "RmDir /r" not in hooks
    assert "--stock-desk-uninstall-v11-data" in hooks
    assert "CopyFiles /SILENT" in hooks
    assert "$PLUGINSDIR\\stock-desk-cleanup.exe" in hooks
    assert "$DeleteAppDataCheckboxState = 1" in hooks
    assert "$UpdateMode <> 1" in hooks
    assert "MB_RETRYCANCEL" in hooks
    assert "SetErrorLevel 70" in hooks

    assert set(languages) == {"English.nsh", "SimpChinese.nsh"}
    for language in languages.values():
        assert "v1.1" in language
        assert "v1.0.0" in language
        assert "stockDeskCleanupFailed" in language
        assert "stockDeskCleanupUnavailable" in language


def test_cleanup_hook_failure_can_only_retry_or_keep_data() -> None:
    hooks = NSIS_HOOKS.read_text(encoding="utf-8")
    post = hooks.split("!macro NSIS_HOOK_POSTUNINSTALL", maxsplit=1)[1]

    assert "ExecWait" in post
    assert "IDRETRY stock_desk_cleanup_retry" in post
    assert "IDCANCEL stock_desk_cleanup_done" in post
    assert "Delete " not in post
    assert "RMDir" not in post
    assert "RmDir" not in post
    unavailable = post.split("$StockDeskCleanupReady <> 1", maxsplit=1)[1].split(
        "stock_desk_cleanup_retry:", maxsplit=1
    )[0]
    assert "MB_OK|MB_ICONEXCLAMATION" in unavailable
    assert "IDRETRY" not in unavailable


def test_windows_deletion_uses_the_pinned_handle_relative_standard_library() -> None:
    source = UNINSTALL_SOURCE.read_text(encoding="utf-8")
    toolchain = RUST_TOOLCHAIN.read_text(encoding="utf-8")

    assert 'channel = "1.88.0"' in toolchain
    assert "fs::remove_dir_all(root)" in source
    assert "FILE_FLAG_OPEN_REPARSE_POINT" in source
    assert "NtOpenFile" in source
    assert "remove_directory_entries_no_follow" not in source
