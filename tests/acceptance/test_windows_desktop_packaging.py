import json
from pathlib import Path

from scripts.verify_windows_desktop_bundle import SIDECAR_EXE
from scripts.verify_windows_desktop_bundle import WEBVIEW2_INSTALLERS


ROOT = Path(__file__).resolve().parents[2]


def test_external_sidecar_source_and_installed_names_are_not_confused() -> None:
    windows_config = json.loads(
        (ROOT / "src-tauri" / "tauri.windows.conf.json").read_text(encoding="utf-8")
    )
    assert windows_config["bundle"]["externalBin"] == ["binaries/stock-desk-sidecar"]

    build_source = (
        ROOT
        / "src-tauri"
        / "binaries"
        / "stock-desk-sidecar-x86_64-pc-windows-msvc.exe"
    )
    assert build_source.name.endswith("-x86_64-pc-windows-msvc.exe")

    assert SIDECAR_EXE == "stock-desk-sidecar.exe"


def test_offline_webview_name_matches_the_pinned_nsis_output_name() -> None:
    template = (ROOT / "packaging" / "nsis" / "installer.nsi").read_text(
        encoding="utf-8"
    )
    emitted_name = "MicrosoftEdgeWebView2RuntimeInstaller.exe"

    assert f'File "/oname=$TEMP\\{emitted_name}"' in template
    assert emitted_name.casefold() in WEBVIEW2_INSTALLERS


def test_windows_desktop_build_is_reproducible_and_sidecar_first() -> None:
    source = (ROOT / "scripts" / "build_windows_desktop.py").read_text(encoding="utf-8")

    assert "STOCK_DESK_SOURCE_REVISION" in source
    assert "SOURCE_DATE_EPOCH" in source
    assert "PYTHONHASHSEED" in source
    assert source.index('"-m",\n            "PyInstaller"') < source.index('"tauri"')
    assert '"--config",\n            "src-tauri/tauri.conf.json"' in source
    assert '"--bundles",\n            "nsis"' in source
    assert '"--target",\n            WINDOWS_TARGET' in source


def test_generated_sidecar_executable_is_ignored_but_sources_are_not() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "src-tauri/binaries/*.exe" in ignore
    assert "src-tauri/binaries/" not in ignore
