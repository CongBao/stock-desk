from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, Response


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


def _validated_dist(configured_dist: Path) -> tuple[Path, Path]:
    dist = configured_dist.expanduser().resolve()
    if not dist.is_dir():
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR must be an existing directory: {dist}"
        )

    index = dist / "index.html"
    if not index.is_file():
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR must contain a readable index.html: {dist}"
        )
    return dist, index


def install_web_routes(application: FastAPI, configured_dist: Path) -> None:
    """Install static and SPA fallback routes for an explicitly configured build."""
    dist, index = _validated_dist(configured_dist)

    @application.get("/{requested_path:path}", include_in_schema=False)
    def serve_web(requested_path: str) -> Response:
        if requested_path == "api" or requested_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        relative_path = PurePosixPath(requested_path)
        candidate = (dist / relative_path).resolve()
        try:
            candidate.relative_to(dist)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

        if candidate.is_file():
            cache_headers = (
                _IMMUTABLE_HEADERS
                if requested_path.startswith("assets/")
                else _NO_CACHE_HEADERS
            )
            return FileResponse(candidate, headers=cache_headers)

        if (
            requested_path == "assets"
            or requested_path.startswith("assets/")
            or relative_path.suffix
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        return FileResponse(index, media_type="text/html", headers=_NO_CACHE_HEADERS)
