#!/usr/bin/env python3
"""OIDC client for the protected external ephemeral Windows VM broker.

The broker API is deliberately small and content-addressed:

* ``POST /v1/leases`` verifies the GitHub OIDC token and returns one-time HTTPS
  upload/start/status/result URLs plus a bounded lease identity.
* the client uploads one deterministic controller bundle, starts exactly one
  first-attempt case, polls without extending the lease, downloads raw-only
  bytes, and cancels in ``finally`` unless the broker reports restored/released.

No OIDC token, signed URL, guest-private log, VM disk or derived pass/fail value
is written to the checkout or the public artifact.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
from http.client import HTTPMessage
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import sys
import time
from typing import Any, IO, cast
import urllib.error
import urllib.parse
import urllib.request
import zipfile


AUDIENCE = "stock-desk-windows-installed-acceptance"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_RE = re.compile(
    r"^(win10-22h2|win11)-dpi-(100|125|150|175|200)(-webview-offline)?$"
)
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_RESULT_FILES = 32


class BrokerError(RuntimeError):
    """Raised when the external VM broker contract fails closed."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def _urlopen(request: urllib.request.Request, *, timeout: int) -> Any:
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _broker_origin(endpoint: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BrokerError("protected VM broker endpoint must be a closed HTTPS origin")
    return parsed.scheme, parsed.netloc


def _assert_response_origin(response: object, *, endpoint: str, label: str) -> None:
    response_url = getattr(response, "geturl", lambda: "")()
    value = urllib.parse.urlsplit(response_url)
    if (value.scheme, value.netloc) != _broker_origin(endpoint):
        raise BrokerError(
            f"broker {label} response redirected outside the pinned HTTPS origin"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _request_json(
    url: str,
    *,
    method: str,
    token: str,
    body: object | None = None,
    timeout: int = 30,
    endpoint: str | None = None,
) -> dict[str, object]:
    data = None if body is None else _json_bytes(body)
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with _urlopen(request, timeout=timeout) as response:
            if endpoint is not None:
                _assert_response_origin(response, endpoint=endpoint, label="JSON")
            payload = response.read(1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise BrokerError("protected VM broker request failed") from error
    if len(payload) > 1024 * 1024:
        raise BrokerError("protected VM broker response exceeds its closed limit")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerError("protected VM broker returned invalid JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BrokerError("protected VM broker response is not an object")
    return value


def _actions_oidc_token(request_url: str, request_authority: str) -> str:
    parsed = urllib.parse.urlsplit(request_url)
    if parsed.scheme != "https":
        raise BrokerError("GitHub OIDC request URL must use HTTPS")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("audience", AUDIENCE))
    url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {request_authority}",
            "Accept": "application/json",
        },
    )
    try:
        with _urlopen(request, timeout=20) as response:
            payload = response.read(64 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise BrokerError("GitHub OIDC token request failed") from error
    if len(payload) > 64 * 1024:
        raise BrokerError("GitHub OIDC token response is oversized")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerError("GitHub OIDC token response is invalid") from error
    token = value.get("value") if isinstance(value, dict) else None
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16384:
        raise BrokerError("GitHub OIDC token is malformed")
    return token


def _same_broker_url(candidate: object, *, endpoint: str, label: str) -> str:
    if not isinstance(candidate, str) or len(candidate) > 2048:
        raise BrokerError(f"broker {label} URL is invalid")
    base_scheme, base_netloc = _broker_origin(endpoint)
    value = urllib.parse.urlsplit(candidate)
    if value.scheme != "https" or (value.scheme, value.netloc) != (
        base_scheme,
        base_netloc,
    ):
        raise BrokerError(f"broker {label} URL escapes the pinned HTTPS origin")
    if value.username is not None or value.password is not None or value.fragment:
        raise BrokerError(f"broker {label} URL contains forbidden authority material")
    return candidate


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BrokerError("controller bundle contains a symlink")
        if path.is_file():
            files.append(path)
    if not files:
        raise BrokerError("controller bundle is empty")
    return files


def _bundle(root: Path) -> tuple[bytes, str]:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in _regular_files(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, path.read_bytes())
    data = output.getvalue()
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise BrokerError("controller bundle exceeds its closed size boundary")
    return data, _sha256(data)


def _put(url: str, *, endpoint: str, token: str, data: bytes, digest: str) -> None:
    request = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
            "Content-Length": str(len(data)),
            "Digest": f"sha-256={digest}",
        },
    )
    try:
        with _urlopen(request, timeout=300) as response:
            _assert_response_origin(response, endpoint=endpoint, label="upload")
            if response.status not in (200, 201, 204):
                raise BrokerError("protected VM bundle upload was not accepted")
    except (urllib.error.URLError, TimeoutError) as error:
        raise BrokerError("protected VM bundle upload failed") from error


def _download(url: str, *, endpoint: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _urlopen(request, timeout=120) as response:
            _assert_response_origin(response, endpoint=endpoint, label="result")
            data = cast(bytes, response.read(MAX_RESULT_BYTES + 1))
    except (urllib.error.URLError, TimeoutError) as error:
        raise BrokerError("protected VM raw result download failed") from error
    if not data or len(data) > MAX_RESULT_BYTES:
        raise BrokerError("protected VM raw result exceeds its closed size boundary")
    return data


def _extract_result(data: bytes, output_root: Path) -> None:
    if output_root.exists():
        raise BrokerError("raw output root must not already exist")
    output_root.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= MAX_RESULT_FILES:
            raise BrokerError("raw result file count is outside its closed boundary")
        names: set[str] = set()
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                info.is_dir()
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in info.filename
                or info.filename in names
                or info.external_attr >> 16 & 0o170000 not in (0, 0o100000)
            ):
                raise BrokerError("raw result ZIP contains an unsafe entry")
            names.add(info.filename)
            total += info.file_size
            if (
                info.file_size < 1
                or info.file_size > 8 * 1024 * 1024
                or total > MAX_RESULT_BYTES
            ):
                raise BrokerError("raw result ZIP entry exceeds its closed limit")
        for info in infos:
            target = output_root.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))


def run_case(
    *,
    endpoint: str,
    controller_root: Path,
    output_root: Path,
    case_id: str,
    source_sha: str,
    source_tree: str,
    policy_sha256: str,
    adapter_sha256: str,
    guest_harness_sha256: str,
    uia_driver_sha256: str,
    workflow_sha256: str,
    run_id: int,
    job_id: str,
) -> None:
    _broker_origin(endpoint)
    if CASE_RE.fullmatch(case_id) is None:
        raise BrokerError("case identity is invalid")
    for label, value, pattern in (
        ("source SHA", source_sha, re.compile(r"^[0-9a-f]{40}$")),
        ("source tree", source_tree, re.compile(r"^[0-9a-f]{40}$")),
        ("policy digest", policy_sha256, SHA256_RE),
        ("adapter digest", adapter_sha256, SHA256_RE),
        ("guest harness digest", guest_harness_sha256, SHA256_RE),
        ("UIA driver digest", uia_driver_sha256, SHA256_RE),
        ("workflow digest", workflow_sha256, SHA256_RE),
    ):
        if pattern.fullmatch(value) is None:
            raise BrokerError(f"{label} is invalid")
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_authority = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_authority:
        raise BrokerError("GitHub Actions OIDC authority is unavailable")
    token = _actions_oidc_token(request_url, request_authority)
    controller_request = controller_root / "controller-request.json"
    if not controller_request.is_file() or controller_request.is_symlink():
        raise BrokerError("controller request is missing or unsafe")
    controller_request_sha256 = _sha256(controller_request.read_bytes())
    bundle, bundle_sha256 = _bundle(controller_root)
    nonce = os.urandom(32).hex()
    nonce_sha256 = _sha256(bytes.fromhex(nonce))
    lease: dict[str, object] | None = None
    released = False
    try:
        lease = _request_json(
            urllib.parse.urljoin(endpoint.rstrip("/") + "/", "v1/leases"),
            method="POST",
            token=token,
            body={
                "schema": "stock-desk-windows-vm-broker-request-v1",
                "audience": AUDIENCE,
                "case_id": case_id,
                "source_sha": source_sha,
                "source_tree": source_tree,
                "snapshot_policy_sha256": policy_sha256,
                "adapter_sha256": adapter_sha256,
                "guest_harness_sha256": guest_harness_sha256,
                "uia_driver_sha256": uia_driver_sha256,
                "workflow_sha256": workflow_sha256,
                "controller_request_sha256": controller_request_sha256,
                "run_id": run_id,
                "run_attempt": 1,
                # GitHub does not issue a job_id OIDC claim.  This is an
                # explicit broker-request field that the signed lifecycle
                # receipt must echo as request_job_id.
                "request_job_id": job_id,
                "case_attempt": 1,
                "bundle_sha256": bundle_sha256,
                "bundle_size_bytes": len(bundle),
                "nonce": nonce,
                "nonce_sha256": nonce_sha256,
                "raw_only": True,
            },
            endpoint=endpoint,
        )
        if (
            lease.get("schema") != "stock-desk-windows-vm-broker-lease-v1"
            or lease.get("status") != "upload-required"
        ):
            raise BrokerError(
                "protected VM broker did not create a closed upload lease"
            )
        if lease.get("case_id") != case_id or lease.get("nonce_sha256") != nonce_sha256:
            raise BrokerError(
                "protected VM broker lease is not bound to this one-time request"
            )
        lease_id = lease.get("lease_id")
        if not isinstance(lease_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{16,128}", lease_id
        ):
            raise BrokerError("protected VM broker lease identity is invalid")
        upload_url = _same_broker_url(
            lease.get("upload_url"), endpoint=endpoint, label="upload"
        )
        start_url = _same_broker_url(
            lease.get("start_url"), endpoint=endpoint, label="start"
        )
        status_url = _same_broker_url(
            lease.get("status_url"), endpoint=endpoint, label="status"
        )
        cancel_url = _same_broker_url(
            lease.get("cancel_url"), endpoint=endpoint, label="cancel"
        )
        _put(
            upload_url,
            endpoint=endpoint,
            token=token,
            data=bundle,
            digest=bundle_sha256,
        )
        started = _request_json(
            start_url,
            method="POST",
            token=token,
            body={
                "lease_id": lease_id,
                "bundle_sha256": bundle_sha256,
                "nonce_sha256": nonce_sha256,
            },
            endpoint=endpoint,
        )
        if started.get("status") not in ("queued", "running"):
            raise BrokerError("protected VM broker did not start the first attempt")
        deadline = time.monotonic() + 40 * 60
        result_url: str | None = None
        while time.monotonic() < deadline:
            status = _request_json(
                status_url, method="GET", token=token, endpoint=endpoint
            )
            if status.get("lease_id") != lease_id or status.get("case_id") != case_id:
                raise BrokerError("protected VM broker status changed lease identity")
            state = status.get("status")
            if state == "completed-restored-released":
                result_url = _same_broker_url(
                    status.get("result_url"), endpoint=endpoint, label="result"
                )
                released = True
                break
            if state in (
                "failed-restored-released",
                "cancelled-restored-released",
                "expired-restored-released",
            ):
                released = True
                raise BrokerError(
                    "protected VM broker case failed after restoring its snapshot"
                )
            if state not in ("queued", "running", "restoring"):
                raise BrokerError(
                    "protected VM broker returned an unknown lifecycle state"
                )
            time.sleep(5)
        if result_url is None:
            raise BrokerError("protected VM broker exceeded its bounded case deadline")
        _extract_result(
            _download(result_url, endpoint=endpoint, token=token), output_root
        )
    finally:
        if lease is not None and not released:
            try:
                cancel_url_value = lease.get("cancel_url")
                cancel_url = _same_broker_url(
                    cancel_url_value, endpoint=endpoint, label="cancel"
                )
                _request_json(
                    cancel_url,
                    method="POST",
                    token=token,
                    body={"reason": "client-finally"},
                    endpoint=endpoint,
                )
            except BrokerError:
                print(
                    "Windows VM broker cancellation was not acknowledged; the protected one-hour watchdog remains authoritative",
                    file=sys.stderr,
                )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--snapshot-policy-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--guest-harness-sha256", required=True)
    parser.add_argument("--uia-driver-sha256", required=True)
    parser.add_argument("--workflow-sha256", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--job-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        run_case(
            endpoint=arguments.endpoint,
            controller_root=arguments.controller_root,
            output_root=arguments.output_root,
            case_id=arguments.case_id,
            source_sha=arguments.source_sha,
            source_tree=arguments.source_tree,
            policy_sha256=arguments.snapshot_policy_sha256,
            adapter_sha256=arguments.adapter_sha256,
            guest_harness_sha256=arguments.guest_harness_sha256,
            uia_driver_sha256=arguments.uia_driver_sha256,
            workflow_sha256=arguments.workflow_sha256,
            run_id=arguments.run_id,
            job_id=arguments.job_id,
        )
    except (BrokerError, OSError, zipfile.BadZipFile) as error:
        print(f"Windows VM broker client failed closed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
