from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Iterable, Sequence
from typing import Final, Protocol, cast

from PIL import Image, ImageChops, ImageStat


ICON_SIZES: Final = (16, 20, 24, 32, 48, 64, 128, 256)
BACKGROUNDS: Final = {
    "windows-light": (245, 247, 251, 255),
    "windows-dark": (17, 24, 39, 255),
}
SCHEMA_VERSION: Final = "stock-desk-windows-icon-evidence-v1"
HEX_40: Final = re.compile(r"[0-9a-f]{40}")


class IconEvidenceError(ValueError):
    """The Windows icon set cannot produce trustworthy visual evidence."""


class IcoFrames(Protocol):
    def sizes(self) -> set[tuple[int, int]]: ...

    def getimage(self, size: tuple[int, int]) -> Image.Image: ...


class IcoImage(Protocol):
    ico: IcoFrames


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise IconEvidenceError(f"icon asset must be a regular file: {path.name}")
    return _sha256_bytes(path.read_bytes())


def _rgba(path: Path, *, size: int) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGBA")
    if image.size != (size, size):
        raise IconEvidenceError(f"{path.name} must be exactly {size}x{size}")
    alpha = image.getchannel("A")
    if alpha.getbbox() is None or alpha.getextrema() != (0, 255):
        raise IconEvidenceError(
            f"{path.name} must contain transparent and opaque pixels"
        )
    return image


def _ico_frame(icon: IcoImage, size: int) -> Image.Image:
    if (size, size) not in icon.ico.sizes():
        raise IconEvidenceError(f"icon.ico is missing its {size}x{size} frame")
    return icon.ico.getimage((size, size)).convert("RGBA")


def _pixel_inventory(icon_dir: Path) -> tuple[dict[str, object], list[Image.Image]]:
    expected = {(size, size) for size in ICON_SIZES}
    ico_path = icon_dir / "icon.ico"
    with Image.open(ico_path) as opened:
        if not hasattr(opened, "ico"):
            raise IconEvidenceError("icon.ico has no Windows icon frame directory")
        ico = cast(IcoImage, opened)
        if ico.ico.sizes() != expected:
            raise IconEvidenceError(
                "icon.ico must contain exactly the required frame sizes"
            )
        records: dict[str, object] = {}
        images: list[Image.Image] = []
        for size in ICON_SIZES:
            png_path = icon_dir / f"{size}x{size}.png"
            image = _rgba(png_path, size=size)
            frame = _ico_frame(ico, size)
            difference = ImageChops.difference(image, frame)
            raw_rms = cast(Sequence[float], ImageStat.Stat(difference).rms)
            rms = [round(value, 6) for value in raw_rms]
            if max(rms[:3]) > 10 or rms[3] > 1:
                raise IconEvidenceError(
                    f"icon.ico frame {size}x{size} is not visually equivalent to its public PNG"
                )
            alpha = image.getchannel("A")
            bbox = alpha.getbbox()
            if bbox is None:
                raise IconEvidenceError(f"{png_path.name} has no visible pixels")
            alpha_values = cast(Iterable[int], alpha.get_flattened_data())
            visible = sum(1 for value in alpha_values if value > 0)
            opaque = sum(1 for value in alpha_values if value == 255)
            records[str(size)] = {
                "png_sha256": _sha256_file(png_path),
                "rgba_sha256": _sha256_bytes(image.tobytes()),
                "ico_rgba_sha256": _sha256_bytes(frame.tobytes()),
                "ico_difference_rms": rms,
                "alpha_bbox": list(bbox),
                "visible_pixels": visible,
                "opaque_pixels": opaque,
            }
            images.append(image)
    return records, images


def _contact_sheet(images: list[Image.Image]) -> Image.Image:
    cell = max(ICON_SIZES) + 32
    sheet = Image.new("RGBA", (cell * len(BACKGROUNDS), cell * len(images)))
    for column, background in enumerate(BACKGROUNDS.values()):
        for row, image in enumerate(images):
            panel = Image.new("RGBA", (cell, cell), background)
            x = (cell - image.width) // 2
            y = (cell - image.height) // 2
            panel.alpha_composite(image, (x, y))
            sheet.alpha_composite(panel, (column * cell, row * cell))
    return sheet


def create_icon_evidence(
    icon_dir: Path,
    output_dir: Path,
    *,
    source_sha: str,
    source_tree: str,
) -> dict[str, object]:
    if HEX_40.fullmatch(source_sha) is None or HEX_40.fullmatch(source_tree) is None:
        raise IconEvidenceError(
            "source SHA and tree must be lowercase 40-character git ids"
        )
    icon_dir = icon_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    records, images = _pixel_inventory(icon_dir)
    sheet_path = output_dir / "windows-icon-light-dark-contact-sheet.png"
    sheet = _contact_sheet(images)
    sheet.save(sheet_path, format="PNG", optimize=False)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "backgrounds": {key: list(value) for key, value in BACKGROUNDS.items()},
        "source_svg_sha256": _sha256_file(icon_dir / "icon.svg"),
        "ico_sha256": _sha256_file(icon_dir / "icon.ico"),
        "contact_sheet": {
            "path": sheet_path.name,
            # The reviewed identity is the decoded RGBA raster. PNG compression is
            # allowed to vary with the platform's zlib build without changing the
            # visual evidence. Keep the encoded checksum separately for artifact
            # provenance within an individual CI run.
            "rgba_sha256": _sha256_bytes(sheet.tobytes()),
            "sha256": _sha256_file(sheet_path),
            "width": sheet.width,
            "height": sheet.height,
        },
        "sizes": records,
    }
    # Keep the assignment above explicit in the evidence while rejecting accidental
    # non-deterministic image dimensions during future refactors.
    if sheet.width != (max(ICON_SIZES) + 32) * len(BACKGROUNDS):
        raise IconEvidenceError("contact sheet width is not deterministic")
    manifest_path = output_dir / "windows-icon-evidence.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate exact-SHA Windows icon readability evidence."
    )
    parser.add_argument("--icons", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    arguments = parser.parse_args()
    create_icon_evidence(
        arguments.icons,
        arguments.output,
        source_sha=arguments.source_sha,
        source_tree=arguments.source_tree,
    )


if __name__ == "__main__":
    main()
