from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Final

from PIL import Image


ROOT: Final = Path(__file__).resolve().parents[1]
CANONICAL_SOURCE: Final = ROOT / "web" / "public" / "brand-icon.svg"
DEFAULT_OUTPUT: Final = ROOT / "src-tauri" / "icons"
ICON_SIZES: Final = (16, 20, 24, 32, 48, 64, 128, 256)


def _rasterize_svg(source: Path, temporary: Path) -> Path:
    subprocess.run(
        [
            "pnpm",
            "exec",
            "tauri",
            "icon",
            str(source),
            "--output",
            str(temporary),
            "--png",
            "512",
        ],
        cwd=ROOT,
        check=True,
    )
    raster = temporary / "512x512.png"
    if not raster.is_file():
        raise RuntimeError("Tauri icon rasterizer did not create 512x512.png")
    return raster


def generate_desktop_icons(source: Path, output: Path) -> None:
    source = source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stock-desk-icon-") as raw:
        raster_path = _rasterize_svg(source, Path(raw))
        with Image.open(raster_path) as opened:
            canonical = opened.convert("RGBA")
        if canonical.size != (512, 512):
            raise RuntimeError("canonical icon raster must be 512x512")
        canonical.save(output / "icon.png", format="PNG", optimize=False)
        generated: dict[int, Image.Image] = {}
        for size in ICON_SIZES:
            image = canonical.resize((size, size), Image.Resampling.LANCZOS)
            image.save(
                output / f"{size}x{size}.png",
                format="PNG",
                optimize=False,
            )
            generated[size] = image
        canonical.save(
            output / "icon.ico",
            format="ICO",
            sizes=[(size, size) for size in ICON_SIZES],
        )
        for image in generated.values():
            image.close()
    shutil.copyfile(source, output / "icon.svg")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Stock Desk desktop icons from the canonical SVG."
    )
    parser.add_argument("--source", type=Path, default=CANONICAL_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    generate_desktop_icons(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
