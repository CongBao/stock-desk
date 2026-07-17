from pathlib import Path

from PIL import Image

from scripts.generate_desktop_icons import ICON_SIZES, generate_desktop_icons


ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "web" / "public" / "brand-icon.svg"


def test_generated_assets_are_bound_to_the_canonical_svg(tmp_path: Path) -> None:
    generate_desktop_icons(CANONICAL, tmp_path)

    assert (tmp_path / "icon.svg").read_bytes() == CANONICAL.read_bytes()
    assert (tmp_path / "icon.png").is_file()
    for size in ICON_SIZES:
        path = tmp_path / f"{size}x{size}.png"
        with Image.open(path) as image:
            assert image.size == (size, size)
            assert image.mode == "RGBA"
            assert image.getchannel("A").getextrema() == (0, 255)

    with Image.open(tmp_path / "icon.ico") as icon:
        assert icon.ico.sizes() == {(size, size) for size in ICON_SIZES}


def test_canonical_icon_has_only_one_stock_chart_visual_meaning() -> None:
    source = CANONICAL.read_text(encoding="utf-8")

    assert 'aria-label="Stock Desk"' in source
    assert 'data-identity="line-chart"' in source
    assert "candlestick" not in source
    assert "gear" not in source
    assert "<text" not in source
