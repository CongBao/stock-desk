from __future__ import annotations

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH).parent.resolve()
SIDECAR_EXCLUDES = ["stock_desk.desktop", "stock_desk.web"]
MIGRATIONS_ROOT = ROOT / "migrations"


def include_sidecar_module(module_name: str) -> bool:
    return not any(
        module_name == legacy_module or module_name.startswith(f"{legacy_module}.")
        for legacy_module in SIDECAR_EXCLUDES
    )


migration_datas = [
    (
        str(source),
        (Path("stock_desk/migrations") / source.relative_to(MIGRATIONS_ROOT).parent)
        .as_posix(),
    )
    for source in sorted(MIGRATIONS_ROOT.rglob("*.py"))
]

datas = [
    (str(ROOT / "alembic.ini"), "stock_desk"),
    *migration_datas,
    (
        str(ROOT / "src" / "stock_desk" / "formula" / "grammar.lark"),
        "stock_desk/formula",
    ),
    (
        str(ROOT / "src" / "stock_desk" / "demo" / "market_snapshot.json"),
        "stock_desk/demo",
    ),
    (str(ROOT / "LICENSE"), "legal"),
    (str(ROOT / "packaging" / "NOTICE.txt"), "legal"),
]
binaries = []
hiddenimports = collect_submodules("stock_desk", filter=include_sidecar_module)

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
    [str(ROOT / "src" / "stock_desk" / "sidecar.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=SIDECAR_EXCLUDES,
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="stock-desk-sidecar-x86_64-pc-windows-msvc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
