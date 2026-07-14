from __future__ import annotations

import io
import json
from http.client import HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import Self
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pytest

from scripts import windows_vm_broker_client as broker


ENDPOINT = "https://broker.example"
CASE_ID = "win10-22h2-dpi-100"
LEASE_ID = "lease-1234567890ab"
TOKEN = "header.payload.signature"


class _Response:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int = 200,
    ) -> None:
        self._payload = payload
        self._url = url
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def read(self, amount: int = -1) -> bytes:
        del amount
        return self._payload

    def geturl(self) -> str:
        return self._url


def _json_response(value: object, *, url: str, status: int = 200) -> _Response:
    return _Response(json.dumps(value).encode(), url=url, status=status)


def _result_zip(files: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (files or {"raw-manifest.json": b"{}"}).items():
            archive.writestr(name, data)
    return output.getvalue()


class _BrokerTransport:
    def __init__(
        self,
        *,
        statuses: list[str] | None = None,
        start_status: str = "running",
        result_bytes: bytes | None = None,
        lease_overrides: dict[str, object] | None = None,
        status_case_id: str = CASE_ID,
        cancel_error: bool = False,
    ) -> None:
        self.statuses = list(
            statuses or ["queued", "running", "completed-restored-released"]
        )
        self.start_status = start_status
        self.result_bytes = result_bytes or _result_zip(
            {
                "raw-manifest.json": b'{"schema_version":2}\n',
                "controller/lifecycle-receipt.json": b"{}\n",
            }
        )
        self.lease_overrides = lease_overrides or {}
        self.status_case_id = status_case_id
        self.cancel_error = cancel_error
        self.requests: list[tuple[urllib.request.Request, int]] = []
        self.lease_request: dict[str, object] | None = None

    def open(self, request: urllib.request.Request, *, timeout: int) -> _Response:
        self.requests.append((request, timeout))
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        method = request.get_method()
        if parsed.netloc == "token.actions.githubusercontent.com":
            return _json_response({"value": TOKEN}, url=url)
        if parsed.netloc != "broker.example":
            raise AssertionError(f"unexpected request origin: {url}")
        if method == "POST" and parsed.path == "/v1/leases":
            assert request.data is not None
            self.lease_request = json.loads(request.data)
            lease: dict[str, object] = {
                "schema": "stock-desk-windows-vm-broker-lease-v1",
                "status": "upload-required",
                "case_id": CASE_ID,
                "nonce_sha256": self.lease_request["nonce_sha256"],
                "lease_id": LEASE_ID,
                "upload_url": f"{ENDPOINT}/v1/upload/{LEASE_ID}",
                "start_url": f"{ENDPOINT}/v1/start/{LEASE_ID}",
                "status_url": f"{ENDPOINT}/v1/status/{LEASE_ID}",
                "cancel_url": f"{ENDPOINT}/v1/cancel/{LEASE_ID}",
            }
            lease.update(self.lease_overrides)
            return _json_response(lease, url=url)
        if method == "PUT" and parsed.path == f"/v1/upload/{LEASE_ID}":
            return _Response(b"", url=url, status=204)
        if method == "POST" and parsed.path == f"/v1/start/{LEASE_ID}":
            return _json_response({"status": self.start_status}, url=url)
        if method == "GET" and parsed.path == f"/v1/status/{LEASE_ID}":
            if not self.statuses:
                raise AssertionError("status poll exceeded the fake state sequence")
            state = self.statuses.pop(0)
            value: dict[str, object] = {
                "lease_id": LEASE_ID,
                "case_id": self.status_case_id,
                "status": state,
            }
            if state == "completed-restored-released":
                value["result_url"] = f"{ENDPOINT}/v1/result/{LEASE_ID}"
            return _json_response(value, url=url)
        if method == "GET" and parsed.path == f"/v1/result/{LEASE_ID}":
            return _Response(self.result_bytes, url=url)
        if method == "POST" and parsed.path == f"/v1/cancel/{LEASE_ID}":
            if self.cancel_error:
                raise urllib.error.URLError("cancel unavailable")
            return _json_response({"status": "cancelled-restored-released"}, url=url)
        raise AssertionError(f"unexpected broker request: {method} {url}")

    def matching(self, method: str, path: str) -> list[urllib.request.Request]:
        return [
            request
            for request, _timeout in self.requests
            if request.get_method() == method
            and urllib.parse.urlsplit(request.full_url).path == path
        ]


def _headers(request: urllib.request.Request) -> dict[str, str]:
    return {name.casefold(): value for name, value in request.header_items()}


def _controller_root(tmp_path: Path) -> Path:
    root = tmp_path / "controller"
    (root / "reviewed").mkdir(parents=True)
    (root / "controller-request.json").write_text(
        '{"schema":"stock-desk-windows-installed-controller-request-v2"}\n',
        encoding="utf-8",
    )
    (root / "reviewed" / "guest.ps1").write_bytes(b"guest\n")
    return root


def _configure_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: _BrokerTransport,
) -> list[int]:
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "https://token.actions.githubusercontent.com/oidc?request=one",
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "oidc-authority")
    monkeypatch.setattr(broker, "_urlopen", transport.open)
    monkeypatch.setattr(broker.os, "urandom", lambda size: b"\x11" * size)
    sleeps: list[int] = []
    monkeypatch.setattr(broker.time, "sleep", sleeps.append)
    return sleeps


def _run_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: _BrokerTransport,
) -> Path:
    _configure_transport(monkeypatch, transport)
    output = tmp_path / "raw-result"
    broker.run_case(
        endpoint=ENDPOINT,
        controller_root=_controller_root(tmp_path),
        output_root=output,
        case_id=CASE_ID,
        source_sha="a" * 40,
        source_tree="b" * 40,
        policy_sha256="c" * 64,
        adapter_sha256="d" * 64,
        guest_harness_sha256="e" * 64,
        uia_driver_sha256="f" * 64,
        workflow_sha256="1" * 64,
        run_id=42,
        job_id=f"windows-installed-{CASE_ID}",
    )
    return output


def test_run_case_executes_content_bound_first_attempt_and_extracts_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _BrokerTransport()
    sleeps = _configure_transport(monkeypatch, transport)
    controller = _controller_root(tmp_path)
    output = tmp_path / "raw-result"

    broker.run_case(
        endpoint=ENDPOINT,
        controller_root=controller,
        output_root=output,
        case_id=CASE_ID,
        source_sha="a" * 40,
        source_tree="b" * 40,
        policy_sha256="c" * 64,
        adapter_sha256="d" * 64,
        guest_harness_sha256="e" * 64,
        uia_driver_sha256="f" * 64,
        workflow_sha256="1" * 64,
        run_id=42,
        job_id=f"windows-installed-{CASE_ID}",
    )

    assert (output / "raw-manifest.json").read_bytes() == (b'{"schema_version":2}\n')
    assert (output / "controller" / "lifecycle-receipt.json").read_bytes() == b"{}\n"
    assert sleeps == [5, 5]
    assert transport.lease_request is not None
    request = transport.lease_request
    assert request == {
        "schema": "stock-desk-windows-vm-broker-request-v1",
        "audience": broker.AUDIENCE,
        "case_id": CASE_ID,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "snapshot_policy_sha256": "c" * 64,
        "adapter_sha256": "d" * 64,
        "guest_harness_sha256": "e" * 64,
        "uia_driver_sha256": "f" * 64,
        "workflow_sha256": "1" * 64,
        "controller_request_sha256": broker._sha256(
            (controller / "controller-request.json").read_bytes()
        ),
        "run_id": 42,
        "run_attempt": 1,
        "request_job_id": f"windows-installed-{CASE_ID}",
        "case_attempt": 1,
        "bundle_sha256": request["bundle_sha256"],
        "bundle_size_bytes": request["bundle_size_bytes"],
        "nonce": "11" * 32,
        "nonce_sha256": broker._sha256(b"\x11" * 32),
        "raw_only": True,
    }

    oidc_request = transport.requests[0][0]
    oidc_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(oidc_request.full_url).query
    )
    assert oidc_query == {"request": ["one"], "audience": [broker.AUDIENCE]}
    assert _headers(oidc_request)["authorization"] == "Bearer oidc-authority"

    upload = transport.matching("PUT", f"/v1/upload/{LEASE_ID}")
    assert len(upload) == 1
    upload_request = upload[0]
    assert isinstance(upload_request.data, bytes)
    upload_headers = _headers(upload_request)
    assert upload_headers["authorization"] == f"Bearer {TOKEN}"
    assert upload_headers["digest"] == (
        f"sha-256={broker._sha256(upload_request.data)}"
    )
    assert int(upload_headers["content-length"]) == len(upload_request.data)
    assert request["bundle_sha256"] == broker._sha256(upload_request.data)
    assert request["bundle_size_bytes"] == len(upload_request.data)
    with zipfile.ZipFile(io.BytesIO(upload_request.data)) as archive:
        assert archive.namelist() == [
            "controller-request.json",
            "reviewed/guest.ps1",
        ]
        assert archive.read("reviewed/guest.ps1") == b"guest\n"
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100600

    start = transport.matching("POST", f"/v1/start/{LEASE_ID}")
    assert len(start) == 1
    assert json.loads(start[0].data or b"") == {
        "lease_id": LEASE_ID,
        "bundle_sha256": request["bundle_sha256"],
        "nonce_sha256": request["nonce_sha256"],
    }
    assert len(transport.matching("GET", f"/v1/status/{LEASE_ID}")) == 3
    assert len(transport.matching("GET", f"/v1/result/{LEASE_ID}")) == 1
    assert transport.matching("POST", f"/v1/cancel/{LEASE_ID}") == []


def test_start_failure_cancels_the_unreleased_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _BrokerTransport(start_status="rejected")
    with pytest.raises(broker.BrokerError, match="did not start"):
        _run_case(tmp_path, monkeypatch, transport)

    cancel = transport.matching("POST", f"/v1/cancel/{LEASE_ID}")
    assert len(cancel) == 1
    assert json.loads(cancel[0].data or b"") == {"reason": "client-finally"}
    assert not (tmp_path / "raw-result").exists()


def test_cancel_failure_does_not_mask_the_authoritative_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _BrokerTransport(start_status="rejected", cancel_error=True)
    with pytest.raises(broker.BrokerError, match="did not start"):
        _run_case(tmp_path, monkeypatch, transport)

    assert len(transport.matching("POST", f"/v1/cancel/{LEASE_ID}")) == 1
    assert "one-hour watchdog remains authoritative" in capsys.readouterr().err


def test_restored_failure_fails_closed_without_redundant_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _BrokerTransport(statuses=["failed-restored-released"])
    with pytest.raises(broker.BrokerError, match="failed after restoring"):
        _run_case(tmp_path, monkeypatch, transport)

    assert transport.matching("POST", f"/v1/cancel/{LEASE_ID}") == []


def test_status_identity_change_fails_closed_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _BrokerTransport(statuses=["running"], status_case_id="win11-dpi-100")
    with pytest.raises(broker.BrokerError, match="changed lease identity"):
        _run_case(tmp_path, monkeypatch, transport)

    assert len(transport.matching("POST", f"/v1/cancel/{LEASE_ID}")) == 1


def test_unknown_or_timed_out_lifecycle_fails_closed_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = _BrokerTransport(statuses=["unexpected"])
    with pytest.raises(broker.BrokerError, match="unknown lifecycle"):
        _run_case(tmp_path, monkeypatch, unknown)
    assert len(unknown.matching("POST", f"/v1/cancel/{LEASE_ID}")) == 1

    timed_out = _BrokerTransport(statuses=["running"])
    _configure_transport(monkeypatch, timed_out)
    ticks = iter((0.0, 2401.0))
    monkeypatch.setattr(broker.time, "monotonic", lambda: next(ticks))
    with pytest.raises(broker.BrokerError, match="bounded case deadline"):
        broker.run_case(
            endpoint=ENDPOINT,
            controller_root=_controller_root(tmp_path / "timeout"),
            output_root=tmp_path / "timeout-result",
            case_id=CASE_ID,
            source_sha="a" * 40,
            source_tree="b" * 40,
            policy_sha256="c" * 64,
            adapter_sha256="d" * 64,
            guest_harness_sha256="e" * 64,
            uia_driver_sha256="f" * 64,
            workflow_sha256="1" * 64,
            run_id=42,
            job_id=f"windows-installed-{CASE_ID}",
        )
    assert len(timed_out.matching("POST", f"/v1/cancel/{LEASE_ID}")) == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"nonce_sha256": "0" * 64}, "not bound"),
        ({"lease_id": "short"}, "lease identity"),
        ({"upload_url": "https://attacker.example/upload"}, "escapes"),
    ],
)
def test_unbound_or_cross_origin_lease_fails_closed_and_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    transport = _BrokerTransport(lease_overrides=overrides)
    with pytest.raises(broker.BrokerError, match=message):
        _run_case(tmp_path, monkeypatch, transport)

    assert len(transport.matching("POST", f"/v1/cancel/{LEASE_ID}")) == 1


@pytest.mark.parametrize(
    ("name", "external_attr", "message"),
    [
        ("../escape.json", 0o100600 << 16, "unsafe entry"),
        ("raw\\escape.json", 0o100600 << 16, "unsafe entry"),
        ("raw/link", 0o120777 << 16, "unsafe entry"),
        ("raw/empty.json", 0o100600 << 16, "closed limit"),
    ],
)
def test_result_extraction_rejects_unsafe_or_empty_entries(
    tmp_path: Path,
    name: str,
    external_attr: int,
    message: str,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = external_attr
        archive.writestr(info, b"" if "empty" in name else b"payload")

    with pytest.raises(broker.BrokerError, match=message):
        broker._extract_result(output.getvalue(), tmp_path / "result")


def test_result_extraction_rejects_existing_output_and_empty_archive(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(broker.BrokerError, match="must not already exist"):
        broker._extract_result(_result_zip(), existing)
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(broker.BrokerError, match="file count"):
        broker._extract_result(empty.getvalue(), tmp_path / "empty-result")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "invalid"),
        (b'{"value":"not-a-jwt"}', "malformed"),
        (json.dumps({"value": "a.b.c" * 6000}).encode(), "malformed"),
        (b"x" * (64 * 1024 + 1), "oversized"),
    ],
)
def test_oidc_token_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    monkeypatch.setattr(
        broker,
        "_urlopen",
        lambda request, *, timeout: _Response(
            payload, url=request.full_url, status=200
        ),
    )
    with pytest.raises(broker.BrokerError, match=message):
        broker._actions_oidc_token(
            "https://token.actions.githubusercontent.com/oidc", "authority"
        )


def test_oidc_and_json_network_errors_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(request: urllib.request.Request, *, timeout: int) -> _Response:
        del request, timeout
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(broker, "_urlopen", fail_open)
    with pytest.raises(broker.BrokerError, match="OIDC token request failed"):
        broker._actions_oidc_token(
            "https://token.actions.githubusercontent.com/oidc", "authority"
        )
    with pytest.raises(broker.BrokerError, match="broker request failed"):
        broker._request_json(
            f"{ENDPOINT}/v1/status", method="GET", token=TOKEN, endpoint=ENDPOINT
        )
    with pytest.raises(broker.BrokerError, match="must use HTTPS"):
        broker._actions_oidc_token("http://token.actions/oidc", "authority")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"x" * (1024 * 1024 + 1), "exceeds"),
        (b"not-json", "invalid JSON"),
        (b"[]", "not an object"),
    ],
)
def test_json_broker_response_rejects_oversized_or_unclosed_payload(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    monkeypatch.setattr(
        broker,
        "_urlopen",
        lambda request, *, timeout: _Response(payload, url=request.full_url),
    )
    with pytest.raises(broker.BrokerError, match=message):
        broker._request_json(
            f"{ENDPOINT}/v1/status", method="GET", token=TOKEN, endpoint=ENDPOINT
        )


def test_transport_rejects_redirects_bad_uploads_and_empty_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker,
        "_urlopen",
        lambda request, *, timeout: _Response(
            b"{}", url="https://attacker.example/result", status=200
        ),
    )
    with pytest.raises(broker.BrokerError, match="redirected"):
        broker._request_json(
            f"{ENDPOINT}/v1/status", method="GET", token=TOKEN, endpoint=ENDPOINT
        )
    with pytest.raises(broker.BrokerError, match="redirected"):
        broker._download(f"{ENDPOINT}/v1/result", endpoint=ENDPOINT, token=TOKEN)

    monkeypatch.setattr(
        broker,
        "_urlopen",
        lambda request, *, timeout: _Response(b"", url=request.full_url, status=500),
    )
    with pytest.raises(broker.BrokerError, match="upload was not accepted"):
        broker._put(
            f"{ENDPOINT}/v1/upload",
            endpoint=ENDPOINT,
            token=TOKEN,
            data=b"bundle",
            digest=broker._sha256(b"bundle"),
        )
    with pytest.raises(broker.BrokerError, match="closed size boundary"):
        broker._download(f"{ENDPOINT}/v1/result", endpoint=ENDPOINT, token=TOKEN)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://broker.example",
        "https://user@broker.example",
        "https://broker.example?query=1",
        "https://broker.example#fragment",
    ],
)
def test_broker_origin_rejects_non_closed_authority(endpoint: str) -> None:
    with pytest.raises(broker.BrokerError, match="closed HTTPS origin"):
        broker._broker_origin(endpoint)


def test_bundle_and_url_helpers_reject_unsafe_local_or_remote_inputs(
    tmp_path: Path,
) -> None:
    with pytest.raises(broker.BrokerError, match="bundle is empty"):
        broker._bundle(tmp_path / "empty")
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    (source / "link.txt").symlink_to(target)
    with pytest.raises(broker.BrokerError, match="symlink"):
        broker._bundle(source)
    with pytest.raises(broker.BrokerError, match="URL is invalid"):
        broker._same_broker_url(None, endpoint=ENDPOINT, label="result")
    with pytest.raises(broker.BrokerError, match="authority material"):
        broker._same_broker_url(
            f"{ENDPOINT}/result#secret", endpoint=ENDPOINT, label="result"
        )


def test_redirect_handler_and_default_urlopen_do_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = broker._NoRedirectHandler()
    assert (
        handler.redirect_request(
            urllib.request.Request(ENDPOINT),
            io.BytesIO(),
            302,
            "Found",
            HTTPMessage(),
            "https://attacker.example",
        )
        is None
    )

    class _Opener:
        def open(self, request: urllib.request.Request, *, timeout: int) -> str:
            assert request.full_url == ENDPOINT
            assert timeout == 17
            return "opened"

    monkeypatch.setattr(broker.urllib.request, "build_opener", lambda *args: _Opener())
    assert broker._urlopen(urllib.request.Request(ENDPOINT), timeout=17) == "opened"


def test_run_case_rejects_invalid_identity_and_missing_authority_before_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = {
        "endpoint": ENDPOINT,
        "controller_root": _controller_root(tmp_path),
        "output_root": tmp_path / "result",
        "case_id": CASE_ID,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "policy_sha256": "c" * 64,
        "adapter_sha256": "d" * 64,
        "guest_harness_sha256": "e" * 64,
        "uia_driver_sha256": "f" * 64,
        "workflow_sha256": "1" * 64,
        "run_id": 42,
        "job_id": f"windows-installed-{CASE_ID}",
    }
    with pytest.raises(broker.BrokerError, match="case identity"):
        broker.run_case(**{**common, "case_id": "attacker"})  # type: ignore[arg-type]
    with pytest.raises(broker.BrokerError, match="source SHA"):
        broker.run_case(**{**common, "source_sha": "bad"})  # type: ignore[arg-type]
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    with pytest.raises(broker.BrokerError, match="OIDC authority"):
        broker.run_case(**common)  # type: ignore[arg-type]


def test_main_returns_success_or_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "--endpoint",
        ENDPOINT,
        "--controller-root",
        str(tmp_path / "controller"),
        "--output-root",
        str(tmp_path / "output"),
        "--case-id",
        CASE_ID,
        "--source-sha",
        "a" * 40,
        "--source-tree",
        "b" * 40,
        "--snapshot-policy-sha256",
        "c" * 64,
        "--adapter-sha256",
        "d" * 64,
        "--guest-harness-sha256",
        "e" * 64,
        "--uia-driver-sha256",
        "f" * 64,
        "--workflow-sha256",
        "1" * 64,
        "--run-id",
        "42",
        "--job-id",
        f"windows-installed-{CASE_ID}",
    ]
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(broker, "run_case", lambda **kwargs: calls.append(kwargs))
    assert broker.main(arguments) == 0
    assert calls[0]["case_id"] == CASE_ID
    assert calls[0]["run_id"] == 42

    def reject(**kwargs: object) -> None:
        del kwargs
        raise broker.BrokerError("rejected")

    monkeypatch.setattr(broker, "run_case", reject)
    assert broker.main(arguments) == 1
    assert "failed closed: rejected" in capsys.readouterr().out
