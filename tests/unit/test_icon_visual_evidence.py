from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from scripts.icon_visual_evidence import (
    BACKGROUNDS,
    ICON_SIZES,
    IconEvidenceError,
    create_icon_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
ICONS = ROOT / "src-tauri" / "icons"
BASELINE = ROOT / "tests" / "fixtures" / "desktop-icons" / "pixel-baseline.json"
SOURCE_ID = "a" * 40
TREE_ID = "b" * 40


def test_icon_pixel_inventory_matches_reviewed_multisize_baseline(
    tmp_path: Path,
) -> None:
    manifest = create_icon_evidence(
        ICONS, tmp_path, source_sha=SOURCE_ID, source_tree=TREE_ID
    )
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert manifest["source_svg_sha256"] == baseline["source_svg_sha256"]
    assert manifest["ico_sha256"] == baseline["ico_sha256"]
    assert {
        key: manifest["contact_sheet"][key]
        for key in ("path", "rgba_sha256", "width", "height")
    } == baseline["contact_sheet"]
    assert manifest["sizes"] == baseline["sizes"]
    assert manifest["backgrounds"] == {
        key: list(value) for key, value in BACKGROUNDS.items()
    }

    sheet_path = tmp_path / "windows-icon-light-dark-contact-sheet.png"
    with Image.open(sheet_path) as sheet:
        assert sheet.mode == "RGBA"
        assert sheet.size == (
            (max(ICON_SIZES) + 32) * len(BACKGROUNDS),
            (max(ICON_SIZES) + 32) * len(ICON_SIZES),
        )
        assert (
            manifest["contact_sheet"]["rgba_sha256"]
            == hashlib.sha256(sheet.tobytes()).hexdigest()
        )
    assert (
        manifest["contact_sheet"]["sha256"]
        == hashlib.sha256(sheet_path.read_bytes()).hexdigest()
    )


def test_contact_sheet_review_identity_ignores_png_compression(
    tmp_path: Path,
) -> None:
    manifest = create_icon_evidence(
        ICONS, tmp_path, source_sha=SOURCE_ID, source_tree=TREE_ID
    )
    sheet_path = tmp_path / "windows-icon-light-dark-contact-sheet.png"
    with Image.open(sheet_path) as source:
        sheet = source.convert("RGBA")

    alternate_path = tmp_path / "alternate-compression.png"
    sheet.save(alternate_path, format="PNG", compress_level=0)

    assert (
        hashlib.sha256(alternate_path.read_bytes()).hexdigest()
        != manifest["contact_sheet"]["sha256"]
    )
    with Image.open(alternate_path) as alternate:
        assert (
            hashlib.sha256(alternate.convert("RGBA").tobytes()).hexdigest()
            == manifest["contact_sheet"]["rgba_sha256"]
        )


def test_icon_evidence_rejects_png_and_ico_pixel_drift(tmp_path: Path) -> None:
    icon_dir = tmp_path / "icons"
    icon_dir.mkdir()
    for asset in ICONS.iterdir():
        if asset.is_file():
            (icon_dir / asset.name).write_bytes(asset.read_bytes())
    with Image.open(icon_dir / "16x16.png") as source:
        altered = source.convert("RGBA")
    altered.putpixel((8, 8), (255, 0, 255, 255))
    altered.save(icon_dir / "16x16.png")

    with pytest.raises(IconEvidenceError, match="not visually equivalent"):
        create_icon_evidence(
            icon_dir, tmp_path / "out", source_sha=SOURCE_ID, source_tree=TREE_ID
        )


def test_icon_evidence_rejects_unbound_source_identity(tmp_path: Path) -> None:
    with pytest.raises(IconEvidenceError, match="source SHA and tree"):
        create_icon_evidence(ICONS, tmp_path, source_sha="main", source_tree=TREE_ID)
