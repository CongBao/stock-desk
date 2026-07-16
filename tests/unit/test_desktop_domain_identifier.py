from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER = "com.baozijuan.stockdesk"


def test_desktop_domain_identifiers_use_the_owner_domain() -> None:
    config = json.loads(
        (ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    macos_smoke = (ROOT / "scripts/macos_tauri_smoke.py").read_text(encoding="utf-8")
    windows_capture = (ROOT / "scripts/capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )
    pyinstaller = (ROOT / "packaging/stock-desk.spec").read_text(encoding="utf-8")

    assert config["identifier"] == IDENTIFIER
    assert f'APP_IDENTIFIER = "{IDENTIFIER}"' in macos_smoke
    assert f"$webviewApplicationUserModelId = '{IDENTIFIER}'" in windows_capture
    assert f"Join-Path $env:LOCALAPPDATA '{IDENTIFIER}'" in windows_capture
    assert f'bundle_identifier="{IDENTIFIER}"' in pyinstaller


def test_unowned_reverse_domain_identifiers_are_absent() -> None:
    paths = (
        ROOT / "src-tauri/tauri.conf.json",
        ROOT / "scripts/macos_tauri_smoke.py",
        ROOT / "scripts/capture_windows_desktop_evidence.ps1",
        ROOT / "packaging/stock-desk.spec",
        ROOT / "tests/unit/test_nsis_install_isolation.py",
        ROOT / "tests/unit/test_windows_desktop_evidence_contract.py",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "com." + "congbao.stockdesk" not in combined
    assert "io.github." + "congbao.stock-desk" not in combined
