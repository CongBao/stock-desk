from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Final

from PIL import Image, ImageChops, ImageStat


BACKGROUNDS: Final = {
    "windows-light": (245, 247, 251, 255),
    "windows-dark": (17, 24, 39, 255),
}
RGB_RMS_LIMITS: Final = {16: 14.0, 32: 10.0}
ALPHA_VISIBLE_MINIMUM: Final = 16
NORMALIZED_SIZE: Final = 64
SCHEMA_VERSION: Final = "stock-desk-packaged-icon-evidence-v1"


class PackagedIconError(ValueError):
    """A packaged Windows entry does not expose the reviewed icon identity."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PackagedIconError(f"icon evidence must be a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rgba(path: Path, size: int) -> Image.Image:
    with Image.open(path) as opened:
        image = opened.convert("RGBA")
    if image.size != (size, size):
        raise PackagedIconError(f"{path.name} must be exactly {size}x{size}")
    if image.getchannel("A").getbbox() is None:
        raise PackagedIconError(f"{path.name} has no visible pixels")
    return image


def _visible_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    mask = image.getchannel("A").point(
        lambda value: 255 if value >= ALPHA_VISIBLE_MINIMUM else 0
    )
    bbox = mask.getbbox()
    if bbox is None:
        raise PackagedIconError("icon has no visible alpha identity")
    return bbox


def _normalize_visible(image: Image.Image) -> Image.Image:
    return image.crop(_visible_bbox(image)).resize(
        (NORMALIZED_SIZE, NORMALIZED_SIZE),
        Image.Resampling.LANCZOS,
    )


def _alpha_iou(left: Image.Image, right: Image.Image) -> float:
    left_mask = left.getchannel("A").point(
        lambda value: 255 if value >= ALPHA_VISIBLE_MINIMUM else 0
    )
    right_mask = right.getchannel("A").point(
        lambda value: 255 if value >= ALPHA_VISIBLE_MINIMUM else 0
    )
    intersection = ImageChops.logical_and(
        left_mask.convert("1"), right_mask.convert("1")
    )
    union = ImageChops.logical_or(left_mask.convert("1"), right_mask.convert("1"))
    intersection_count = intersection.convert("L").histogram()[255]
    union_count = union.convert("L").histogram()[255]
    if union_count == 0:
        raise PackagedIconError("icon alpha masks are empty")
    return intersection_count / union_count


def _composite(
    image: Image.Image, background: tuple[int, int, int, int]
) -> Image.Image:
    canvas = Image.new("RGBA", image.size, background)
    canvas.alpha_composite(image)
    return canvas.convert("RGB")


def _entry_metrics(
    packaged: Image.Image,
    canonical: Image.Image,
    *,
    size: int,
) -> dict[str, object]:
    packaged_bbox = _visible_bbox(packaged)
    canonical_bbox = _visible_bbox(canonical)
    normalized_packaged = _normalize_visible(packaged)
    normalized_canonical = _normalize_visible(canonical)
    alpha_iou = _alpha_iou(normalized_packaged, normalized_canonical)
    backgrounds: dict[str, object] = {}
    limit = RGB_RMS_LIMITS[size]
    for name, color in BACKGROUNDS.items():
        difference = ImageChops.difference(
            _composite(packaged, color),
            _composite(canonical, color),
        )
        rms = [round(value, 6) for value in ImageStat.Stat(difference).rms]
        if any(not math.isfinite(value) or value > limit for value in rms):
            raise PackagedIconError(
                f"packaged {size}px icon differs from the reviewed identity on "
                f"{name}: rgb_rms={rms}, limit={limit}"
            )
        backgrounds[name] = {"rgb_rms": rms, "rgb_rms_maximum": limit}
    return {
        "normalized_alpha_mask_iou": round(alpha_iou, 6),
        "alpha_visible_minimum": ALPHA_VISIBLE_MINIMUM,
        "normalized_size": NORMALIZED_SIZE,
        "packaged_alpha_bbox": list(packaged_bbox),
        "canonical_alpha_bbox": list(canonical_bbox),
        "backgrounds": backgrounds,
    }


def verify_packaged_icons(
    canonical_dir: Path,
    entries: dict[str, dict[int, Path]],
    output: Path,
) -> dict[str, object]:
    if set(entries) != {"host", "installer"}:
        raise PackagedIconError("host and installer icon evidence are both required")
    evidence: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "reviewed_identity": {},
        "entries": {},
        "packaged_entries_match_reviewed_identity": True,
    }
    reviewed_identity = evidence["reviewed_identity"]
    packaged_entries = evidence["entries"]
    assert isinstance(reviewed_identity, dict)
    assert isinstance(packaged_entries, dict)
    for size in sorted(RGB_RMS_LIMITS):
        canonical_path = canonical_dir / f"{size}x{size}.png"
        canonical = _rgba(canonical_path, size)
        reviewed_identity[str(size)] = {
            "file": canonical_path.name,
            "sha256": _sha256(canonical_path),
        }
        for entry_name, paths in entries.items():
            if set(paths) != set(RGB_RMS_LIMITS):
                raise PackagedIconError(
                    f"{entry_name} must provide exactly the reviewed icon sizes"
                )
            packaged_path = paths[size]
            packaged = _rgba(packaged_path, size)
            entry = packaged_entries.setdefault(entry_name, {})
            assert isinstance(entry, dict)
            entry[str(size)] = {
                "file": packaged_path.name,
                "sha256": _sha256(packaged_path),
                **_entry_metrics(packaged, canonical, size=size),
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return evidence


def _sized_path(value: str) -> tuple[int, Path]:
    size_text, separator, path_text = value.partition("=")
    if not separator or not size_text.isdigit() or not path_text:
        raise argparse.ArgumentTypeError("icon input must use SIZE=PATH")
    return int(size_text), Path(path_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify packaged Windows icons against reviewed public assets."
    )
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--host", action="append", type=_sized_path, required=True)
    parser.add_argument("--installer", action="append", type=_sized_path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    verify_packaged_icons(
        arguments.canonical,
        {"host": dict(arguments.host), "installer": dict(arguments.installer)},
        arguments.output,
    )


if __name__ == "__main__":
    main()
