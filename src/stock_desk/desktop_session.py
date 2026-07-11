from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import re
import secrets
from threading import Event, Lock
from typing import Final, Protocol

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware


TAURI_WINDOWS_ORIGIN: Final = "http://tauri.localhost"
_ALLOWED_METHODS: Final = frozenset(
    {"DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"}
)
_ALLOWED_HEADERS: Final = frozenset({"authorization", "content-type"})


@dataclass(frozen=True, slots=True)
class DesktopSession:
    """In-memory authority shared only by the Tauri host and Python sidecar."""

    origin: str
    secret: str = field(repr=False)
    host_version: str
    frontend_version: str
    sidecar_version: str
    source_revision: str

    def __post_init__(self) -> None:
        if self.origin != TAURI_WINDOWS_ORIGIN:
            raise ValueError("desktop origin is not the production Tauri origin")
        if len(self.secret.encode("utf-8")) < 32:
            raise ValueError("desktop session secret is too short")
        versions = (self.host_version, self.frontend_version, self.sidecar_version)
        if any(
            re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", item) is None for item in versions
        ):
            raise ValueError("desktop version identity is invalid")
        if len(set(versions)) != 1:
            raise ValueError("desktop version identity does not match")
        if re.fullmatch(r"[0-9a-f]{40}", self.source_revision) is None:
            raise ValueError("desktop source revision is invalid")

    def secret_for_host(self) -> str:
        return self.secret

    def authorizes(self, authorization: str | None) -> bool:
        if authorization is None or not authorization.startswith("Bearer "):
            return False
        candidate = authorization.removeprefix("Bearer ")
        return secrets.compare_digest(candidate, self.secret)

    def handshake(self) -> "DesktopHandshake":
        return DesktopHandshake(
            host_version=self.host_version,
            frontend_version=self.frontend_version,
            sidecar_version=self.sidecar_version,
            source_revision=self.source_revision,
        )


class DesktopHandshake(BaseModel):
    status: str = "ready"
    api_version: str = "v1"
    host_version: str
    frontend_version: str
    sidecar_version: str
    source_revision: str
    storage: str = "ready"


class _CooperativeServer(Protocol):
    should_exit: bool


class DesktopLifecycleController:
    """Thread-safe cooperative stop authority shared by API, worker and server."""

    def __init__(self) -> None:
        self._stop_event = Event()
        self._lock = Lock()
        self._server: _CooperativeServer | None = None

    @property
    def stop_event(self) -> Event:
        return self._stop_event

    @property
    def shutdown_requested(self) -> bool:
        return self._stop_event.is_set()

    def bind_server(self, server: _CooperativeServer) -> None:
        with self._lock:
            self._server = server
            if self._stop_event.is_set():
                server.should_exit = True

    def request_shutdown(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._server is not None:
                self._server.should_exit = True


CallNext = Callable[[Request], Awaitable[Response]]


class DesktopSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, session: DesktopSession) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._session = session

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if not (request.url.path == "/api" or request.url.path.startswith("/api/")):
            return await call_next(request)

        if request.headers.get("origin") != self._session.origin:
            return _desktop_error(403, "desktop_origin_forbidden")
        if request.method == "OPTIONS":
            return self._preflight(request)
        if not self._session.authorizes(request.headers.get("authorization")):
            return _desktop_error(401, "desktop_auth_required")

        response = await call_next(request)
        _add_cors_headers(response, self._session.origin)
        return response

    def _preflight(self, request: Request) -> Response:
        method = request.headers.get("access-control-request-method", "").upper()
        raw_headers = request.headers.get("access-control-request-headers", "")
        requested_headers = {
            item.strip().casefold() for item in raw_headers.split(",") if item.strip()
        }
        if method not in _ALLOWED_METHODS or not requested_headers <= _ALLOWED_HEADERS:
            return _desktop_error(403, "desktop_origin_forbidden")
        response = Response(status_code=204)
        _add_cors_headers(response, self._session.origin)
        response.headers["Access-Control-Allow-Methods"] = ", ".join(
            sorted(_ALLOWED_METHODS - {"OPTIONS"})
        )
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "300"
        return response


def _desktop_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})


def _add_cors_headers(response: Response, origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
