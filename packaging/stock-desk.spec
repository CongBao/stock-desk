from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH).parent.resolve()
VERSION = os.environ.get("STOCK_DESK_BUILD_VERSION", "0+unknown")

# Stable bundle destinations: web/dist, migrations, alembic.ini, LICENSE, NOTICE.
datas = [
    (str(ROOT / "web" / "dist"), "web-dist"),
    (str(ROOT / "alembic.ini"), "stock_desk"),
    (str(ROOT / "migrations"), "stock_desk/migrations"),
    (
        str(ROOT / "src" / "stock_desk" / "formula" / "grammar.lark"),
        "stock_desk/formula",
    ),
    (str(ROOT / "LICENSE"), "legal"),
    (str(ROOT / "packaging" / "NOTICE.txt"), "legal"),
]
binaries = []
hiddenimports = collect_submodules("stock_desk")

for optional_provider in ("akshare", "baostock", "tushare"):
    if importlib.util.find_spec(optional_provider) is None:
        continue
    provider_datas, provider_binaries, provider_hiddenimports = collect_all(
        optional_provider
    )
    datas += provider_datas
    binaries += provider_binaries
    hiddenimports += provider_hiddenimports

analysis = Analysis(
    [str(ROOT / "src" / "stock_desk" / "desktop.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="stock-desk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="stock-desk",
)

if sys.platform == "darwin":
    application = BUNDLE(
        bundle,
        name="stock-desk.app",
        icon=None,
        bundle_identifier="com.baozijuan.stockdesk",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Stock Desk",
            "CFBundleName": "Stock Desk",
            "LSMinimumSystemVersion": "13.0",
            "LSMultipleInstancesProhibited": True,
        },
    )
