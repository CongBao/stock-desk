import os
from pathlib import Path
import stat

import pytest
from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app


def _web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>stock-desk test</title>",
        encoding="utf-8",
    )
    (assets / "app-abc123.js").write_text(
        "console.log('stock-desk')",
        encoding="utf-8",
    )
    return dist


def test_web_serving_is_disabled_by_default() -> None:
    with TestClient(create_app(Settings())) as client:
        response = client.get("/")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_configured_web_dist_serves_root_and_spa_deep_links(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)

    with TestClient(create_app(Settings(web_dist_dir=dist))) as client:
        root = client.get("/")
        deep_link = client.get("/market/watchlist")

    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert root.text == "<!doctype html><title>stock-desk test</title>"
    assert deep_link.status_code == 200
    assert deep_link.text == root.text
    assert root.headers["cache-control"] == "no-cache"


def test_configured_web_dist_serves_assets_with_safe_cache_headers(
    tmp_path: Path,
) -> None:
    dist = _web_dist(tmp_path)

    with TestClient(create_app(Settings(web_dist_dir=dist))) as client:
        response = client.get("/assets/app-abc123.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.text == "console.log('stock-desk')"


def test_static_allowlist_is_frozen_when_the_app_is_created(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)
    app = create_app(Settings(web_dist_dir=dist))
    late_asset = dist / "assets" / "late.js"
    late_asset.write_text("console.log('too late')", encoding="utf-8")

    with TestClient(app) as client:
        response = client.get("/assets/late.js")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_encoded_traversal_cannot_escape_the_static_allowlist(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)
    outside_secret = tmp_path / "secret.txt"
    outside_secret.write_text("not public", encoding="utf-8")

    with TestClient(create_app(Settings(web_dist_dir=dist))) as client:
        response = client.get("/assets/%2e%2e/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "not public" not in response.text


def test_missing_asset_and_unknown_api_remain_json_404(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)

    with TestClient(create_app(Settings(web_dist_dir=dist))) as client:
        missing_asset = client.get("/assets/missing.js")
        unknown_api = client.get("/api/does-not-exist")

    assert missing_asset.status_code == 404
    assert missing_asset.headers["content-type"].startswith("application/json")
    assert missing_asset.json() == {"detail": "Not Found"}
    assert unknown_api.status_code == 404
    assert unknown_api.headers["content-type"].startswith("application/json")
    assert unknown_api.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("contents", [None, "directory-without-index"])
def test_invalid_configured_web_dist_fails_clearly(
    tmp_path: Path,
    contents: str | None,
) -> None:
    configured = tmp_path / "configured-dist"
    if contents is not None:
        configured.mkdir()

    with pytest.raises(RuntimeError, match="STOCK_DESK_WEB_DIST_DIR"):
        create_app(Settings(web_dist_dir=configured))


def test_index_symlink_resolving_outside_dist_fails_clearly(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    outside_index = tmp_path / "outside.html"
    outside_index.write_text("outside", encoding="utf-8")
    try:
        (dist / "index.html").symlink_to(outside_index)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(RuntimeError, match="index.html must resolve inside"):
        create_app(Settings(web_dist_dir=dist))


def test_static_symlink_resolving_outside_dist_fails_at_app_creation(
    tmp_path: Path,
) -> None:
    dist = _web_dist(tmp_path)
    outside_asset = tmp_path / "outside.js"
    outside_asset.write_text("not public", encoding="utf-8")
    try:
        (dist / "assets" / "leak.js").symlink_to(outside_asset)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(RuntimeError, match="static member must not be a symlink"):
        create_app(Settings(web_dist_dir=dist))


def test_static_special_member_fails_at_app_creation(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)
    special_member = dist / "assets" / "events"
    try:
        os.mkfifo(special_member)
    except (AttributeError, NotImplementedError, OSError) as error:
        pytest.skip(f"FIFO creation is unavailable: {error}")

    with pytest.raises(RuntimeError, match="static member must be a regular file"):
        create_app(Settings(web_dist_dir=dist))


def test_unreadable_static_member_fails_at_app_creation(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)
    asset = dist / "assets" / "app-abc123.js"
    original_mode = stat.S_IMODE(asset.stat().st_mode)
    try:
        try:
            asset.chmod(0)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"file permission changes are unavailable: {error}")
        if os.access(asset, os.R_OK):
            pytest.skip("platform does not enforce owner read permissions")

        with pytest.raises(RuntimeError, match="static member must be readable"):
            create_app(Settings(web_dist_dir=dist))
    finally:
        asset.chmod(original_mode)


def test_unreadable_index_fails_at_app_creation(tmp_path: Path) -> None:
    dist = _web_dist(tmp_path)
    index = dist / "index.html"
    original_mode = stat.S_IMODE(index.stat().st_mode)
    try:
        try:
            index.chmod(0)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"file permission changes are unavailable: {error}")
        if os.access(index, os.R_OK):
            pytest.skip("platform does not enforce owner read permissions")

        with pytest.raises(RuntimeError, match="index.html must be readable"):
            create_app(Settings(web_dist_dir=dist))
    finally:
        index.chmod(original_mode)
