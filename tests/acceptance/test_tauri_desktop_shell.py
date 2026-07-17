import json
from pathlib import Path
import tomllib

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
TAURI_ROOT = ROOT / "src-tauri"
WINDOWS_ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def test_tauri_shell_is_windows_only_single_window_and_uses_bundled_assets() -> None:
    config = json.loads((TAURI_ROOT / "tauri.conf.json").read_text(encoding="utf-8"))

    assert config["productName"] == "Stock Desk"
    assert config["build"]["frontendDist"] == "../web/dist"
    assert config["app"]["windows"] == [
        {
            "label": "main",
            "title": "Stock Desk",
            "width": 1440,
            "height": 900,
            "minWidth": 640,
            "minHeight": 360,
            "center": True,
            "visible": False,
        }
    ]
    assert config["app"]["security"]["capabilities"] == [
        "default",
        "macos-smoke",
    ]
    assert "http://127.0.0.1:*" in config["app"]["security"]["csp"]
    assert config["plugins"]["updater"] == {"endpoints": [], "pubkey": ""}


def test_tauri_bundle_is_current_user_windows_x64_without_shell_acl() -> None:
    config = json.loads((TAURI_ROOT / "tauri.conf.json").read_text(encoding="utf-8"))
    windows_config = json.loads(
        (TAURI_ROOT / "tauri.windows.conf.json").read_text(encoding="utf-8")
    )
    bundle = config["bundle"]

    assert bundle["targets"] == ["nsis"]
    assert bundle["icon"] == [
        *(f"icons/{size}x{size}.png" for size in WINDOWS_ICON_SIZES),
        "icons/icon.ico",
    ]
    assert "externalBin" not in bundle
    assert windows_config["bundle"]["externalBin"] == ["binaries/stock-desk-sidecar"]
    assert bundle["windows"]["nsis"]["installMode"] == "currentUser"
    assert bundle["windows"]["webviewInstallMode"] == {"type": "offlineInstaller"}

    capability = json.loads(
        (TAURI_ROOT / "capabilities" / "default.json").read_text(encoding="utf-8")
    )
    assert capability["windows"] == ["main"]
    assert capability["platforms"] == ["windows"]
    serialized_permissions = "\n".join(capability["permissions"])
    assert "shell" not in serialized_permissions
    assert "fs:" not in serialized_permissions
    assert "process" not in serialized_permissions


def test_macos_test_config_is_explicit_and_never_a_release_target() -> None:
    config = json.loads(
        (TAURI_ROOT / "tauri.macos-test.conf.json").read_text(encoding="utf-8")
    )

    assert config["bundle"]["externalBin"] == ["binaries/stock-desk-sidecar"]
    assert config["bundle"]["targets"] == ["app"]
    assert "dmg" not in json.dumps(config).lower()


def test_windows_icon_assets_are_rgba_nonempty_and_multisize() -> None:
    icons = TAURI_ROOT / "icons"

    for size in WINDOWS_ICON_SIZES:
        with Image.open(icons / f"{size}x{size}.png") as image:
            assert image.mode == "RGBA"
            assert image.size == (size, size)
            alpha = image.getchannel("A")
            assert alpha.getbbox() is not None
            assert alpha.getextrema() == (0, 255)

    with Image.open(icons / "icon.ico") as icon:
        embedded_sizes = icon.ico.sizes()
        assert embedded_sizes == {(size, size) for size in WINDOWS_ICON_SIZES}
        for size in WINDOWS_ICON_SIZES:
            icon.size = (size, size)
            assert icon.convert("RGBA").getchannel("A").getbbox() is not None


def test_windows_icon_svg_is_private_text_free_and_uses_required_visual_language() -> (
    None
):
    source = (TAURI_ROOT / "icons" / "icon.svg").read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "<text" not in lowered
    assert "<path" not in lowered
    assert "file://" not in lowered
    assert "/" + "users/" not in lowered
    assert r":\\" + r"users\\" not in lowered
    assert "#08182f" in lowered
    assert "#39c7e6" in lowered
    assert "#f3a64a" in lowered
    assert source.count("<line") == 4
    assert source.count("<rect") == 5


def test_tauri_dependencies_are_exactly_pinned_and_single_instance_is_first() -> None:
    cargo = tomllib.loads((TAURI_ROOT / "Cargo.toml").read_text(encoding="utf-8"))

    assert cargo["build-dependencies"]["tauri-build"]["version"] == "=2.6.3"
    assert cargo["dependencies"]["tauri"]["version"] == "=2.11.5"
    assert cargo["dependencies"]["tauri-plugin-single-instance"]["version"] == (
        "=2.4.2"
    )
    updater = cargo["dependencies"]["tauri-plugin-updater"]
    assert updater == {
        "version": "=2.10.1",
        "default-features": False,
        "features": ["native-tls", "zip"],
    }

    main_source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
    first_plugin = main_source.index(".plugin(")
    single_instance = main_source.index("tauri_plugin_single_instance::init")
    next_plugin = main_source.find(".plugin(", first_plugin + 1)
    assert single_instance >= first_plugin
    assert next_plugin == -1 or single_instance < next_plugin
    assert "webbrowser" not in main_source.casefold()
    assert ".plugin(updater::plugin())" in main_source
    assert "desktop_check_for_updates" in main_source


def test_frozen_sidecar_is_a_dedicated_binary_without_browser_assets() -> None:
    sidecar_spec = (ROOT / "packaging" / "stock-desk-sidecar.spec").read_text(
        encoding="utf-8"
    )

    assert '"stock_desk" / "sidecar.py"' in sidecar_spec
    assert "name=sidecar_name" in sidecar_spec
    assert '"stock-desk-sidecar-x86_64-pc-windows-msvc"' in sidecar_spec
    assert "web-dist" not in sidecar_spec
    assert '"stock_desk" / "desktop.py"' not in sidecar_spec
