import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
WINDOWS_CONFIG = ROOT / "src-tauri" / "tauri.windows.conf.json"
NSIS_TEMPLATE = ROOT / "packaging" / "nsis" / "installer.nsi"
NSIS_NOTICE = ROOT / "packaging" / "nsis" / "NOTICE.md"
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
