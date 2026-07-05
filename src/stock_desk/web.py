from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import stat
from types import MappingProxyType

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, Response


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@dataclass(frozen=True, slots=True)
class _StaticManifest:
    index: Path
    files: Mapping[str, Path]


def _validated_index(dist: Path) -> Path:
    index_path = dist / "index.html"
    try:
        index = index_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR must contain a resolvable index.html: {dist}"
        ) from error

    try:
        index.relative_to(dist)
    except ValueError:
        raise RuntimeError(
            "STOCK_DESK_WEB_DIST_DIR index.html must resolve inside "
            f"the configured directory: {dist}"
        ) from None

    try:
        index_mode = index_path.lstat().st_mode
        if stat.S_ISLNK(index_mode):
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR index.html must not be a symlink: {index_path}"
            )
        if not stat.S_ISREG(index_mode):
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR index.html must be a regular file: {index}"
            )
        with index.open("rb") as entrypoint:
            entrypoint.read(1)
    except OSError as error:
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR index.html must be readable: {index}"
        ) from error
    return index


def _validated_static_files(dist: Path, index: Path) -> Mapping[str, Path]:
    try:
        members = sorted(dist.rglob("*"))
    except OSError as error:
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR static tree must be readable: {dist}"
        ) from error

    static_files: dict[str, Path] = {}
    for member in members:
        try:
            member_mode = member.lstat().st_mode
        except OSError as error:
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR static member must be inspectable: {member}"
            ) from error

        if stat.S_ISLNK(member_mode):
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR static member must not be a symlink: {member}"
            )
        if stat.S_ISDIR(member_mode):
            continue
        if not stat.S_ISREG(member_mode):
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR static member must be a regular file: {member}"
            )

        try:
            resolved_member = member.resolve(strict=True)
            relative_url = resolved_member.relative_to(dist).as_posix()
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError(
                "STOCK_DESK_WEB_DIST_DIR static member must resolve inside "
                f"the configured directory: {member}"
            ) from error

        if resolved_member == index:
            continue

        try:
            with resolved_member.open("rb") as static_file:
                static_file.read(1)
        except OSError as error:
            raise RuntimeError(
                f"STOCK_DESK_WEB_DIST_DIR static member must be readable: {member}"
            ) from error
        static_files[relative_url] = resolved_member

    return MappingProxyType(static_files)


def _validated_dist(configured_dist: Path) -> _StaticManifest:
    dist = configured_dist.expanduser().resolve()
    if not dist.is_dir():
        raise RuntimeError(
            f"STOCK_DESK_WEB_DIST_DIR must be an existing directory: {dist}"
        )

    index = _validated_index(dist)
    return _StaticManifest(index=index, files=_validated_static_files(dist, index))


def _looks_like_file_request(requested_path: str) -> bool:
    final_segment = requested_path.rsplit("/", maxsplit=1)[-1]
    final_dot = final_segment.rfind(".")
    return 0 < final_dot < len(final_segment) - 1


def install_web_routes(application: FastAPI, configured_dist: Path) -> None:
    """Install static and SPA fallback routes for an explicitly configured build."""
    manifest = _validated_dist(configured_dist)

    @application.get("/{requested_path:path}", include_in_schema=False)
    def serve_web(requested_path: str) -> Response:
        if requested_path == "api" or requested_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        if any(segment in {".", ".."} for segment in requested_path.split("/")):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        static_file = manifest.files.get(requested_path)
        if static_file is not None:
            cache_headers = (
                _IMMUTABLE_HEADERS
                if requested_path.startswith("assets/")
                else _NO_CACHE_HEADERS
            )
            return FileResponse(static_file, headers=cache_headers)

        if requested_path == "index.html":
            return FileResponse(
                manifest.index,
                media_type="text/html",
                headers=_NO_CACHE_HEADERS,
            )

        if (
            requested_path == "assets"
            or requested_path.startswith("assets/")
            or _looks_like_file_request(requested_path)
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        return FileResponse(
            manifest.index,
            media_type="text/html",
            headers=_NO_CACHE_HEADERS,
        )
