from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from scripts.verify_windows_packaged_icons import (
    PackagedIconError,
    verify_packaged_icons,
)


ROOT = Path(__file__).resolve().parents[2]


def _icon(path: Path, size: int, *, color: tuple[int, int, int, int]) -> None:
    image = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=3, fill=color)
    draw.line((size // 3, size - 5, size // 3, 5), fill="white", width=2)
    image.save(path)


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, dict[int, Path]]]:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    entries: dict[str, dict[int, Path]] = {"host": {}, "installer": {}}
    for size in (16, 32):
        canonical_path = canonical / f"{size}x{size}.png"
        _icon(canonical_path, size, color=(32, 91, 212, 255))
        for name in entries:
            path = tmp_path / f"{name}-{size}.png"
            with Image.open(canonical_path) as image:
                image.save(path)
            entries[name][size] = path
    return canonical, entries


def test_packaged_entries_each_match_reviewed_identity(tmp_path: Path) -> None:
    canonical, entries = _fixture(tmp_path)

    evidence = verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")

    assert evidence["packaged_entries_match_reviewed_identity"] is True
    assert set(evidence["entries"]) == {"host", "installer"}


def test_reviewed_ico_frames_pass_packaged_identity_thresholds(tmp_path: Path) -> None:
    canonical = ROOT / "src-tauri" / "icons"
    entries: dict[str, dict[int, Path]] = {"host": {}, "installer": {}}
    with Image.open(canonical / "icon.ico") as icon:
        for size in (16, 32):
            frame = icon.ico.getimage((size, size)).convert("RGBA")
            for name in entries:
                path = tmp_path / f"{name}-{size}.png"
                frame.save(path)
                entries[name][size] = path

    evidence = verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")

    assert evidence["packaged_entries_match_reviewed_identity"] is True


def test_low_alpha_extraction_fringe_is_not_a_visible_shape_change(
    tmp_path: Path,
) -> None:
    canonical, entries = _fixture(tmp_path)
    for paths in entries.values():
        for path in paths.values():
            with Image.open(path) as opened:
                image = opened.convert("RGBA")
            pixels = image.load()
            assert pixels is not None
            for point in ((0, 0), (image.width - 1, 0), (0, image.height - 1)):
                pixels[point] = (20, 20, 20, 1)
            image.save(path)

    evidence = verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")

    assert evidence["packaged_entries_match_reviewed_identity"] is True


def test_same_wrong_packaged_icons_cannot_validate_each_other(tmp_path: Path) -> None:
    canonical, entries = _fixture(tmp_path)
    for paths in entries.values():
        for size, path in paths.items():
            _icon(path, size, color=(220, 38, 38, 255))

    with pytest.raises(PackagedIconError, match="reviewed identity"):
        verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")


def test_empty_packaged_icon_fails_closed(tmp_path: Path) -> None:
    canonical, entries = _fixture(tmp_path)
    Image.new("RGBA", (16, 16)).save(entries["installer"][16])

    with pytest.raises(PackagedIconError, match="no visible pixels"):
        verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")


def test_materially_cropped_packaged_icon_fails_shape_gate(tmp_path: Path) -> None:
    canonical, entries = _fixture(tmp_path)
    source = entries["installer"][16]
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    draw = ImageDraw.Draw(image)
    draw.rectangle((image.width // 2, 0, image.width, image.height), fill=(0, 0, 0, 0))
    image.save(source)

    with pytest.raises(PackagedIconError, match="reviewed identity|alpha identity"):
        verify_packaged_icons(canonical, entries, tmp_path / "evidence.json")
