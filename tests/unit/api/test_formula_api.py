from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from multiprocessing.connection import Connection
import multiprocessing
from datetime import date
import json
import os
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import (
    FormulaService,
    FormulaPreviewResourceError,
    FormulaPreviewTimeout,
    FormulaPreviewWorkerError,
    IsolatedFormulaExecutor,
)
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.formula_worker_helpers import echo_formula_worker, posix_partial_frame_worker
from tests.integration.market.lake_test_helpers import routed_daily_bars


class NeverExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _request: bytes) -> bytes:
        self.calls += 1
        raise AssertionError("unsupported formula version reached executor")


def _controllable_worker(connection: Connection, request: bytes) -> None:
    if request.startswith(b"slow:"):
        marker = Path(request.removeprefix(b"slow:").decode("utf-8"))
        time.sleep(1.2)
        marker.write_text("still-running", encoding="utf-8")
        return
    if request == b"crash":
        os._exit(17)
    connection.send_bytes(b"\x00recovered")
    connection.close()


def _slow_formula_worker(_connection: Connection, _request: bytes) -> None:
    time.sleep(2.0)


def _crashing_formula_worker(_connection: Connection, _request: bytes) -> None:
    os._exit(23)


def _echo_formula_worker(connection: Connection, request: bytes) -> None:
    connection.send_bytes(b"\x00" + request)
    connection.close()


def _tracked_formula_worker(
    active: object,
    maximum: object,
    lock: object,
    connection: Connection,
    request: bytes,
) -> None:
    with lock:  # type: ignore[attr-defined]
        active.value += 1  # type: ignore[attr-defined]
        maximum.value = max(maximum.value, active.value)  # type: ignore[attr-defined]
    try:
        time.sleep(0.15)
        connection.send_bytes(b"\x00" + request)
    finally:
        with lock:  # type: ignore[attr-defined]
            active.value -= 1  # type: ignore[attr-defined]
        connection.close()


def test_formula_catalog_and_preview_routes_are_exposed(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-api.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    services = MarketServices(engine=engine, lake_root=(tmp_path / "market").resolve())
    formulas = FormulaRepository(engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = formulas.create("Close", "indicator", "X:C;", {})
    query = routed.result.query
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            functions = client.get("/api/formulas/functions")
            preview = client.post(
                f"/api/formulas/{version.id}/preview",
                json={
                    "symbol": query.symbol,
                    "period": query.period.value,
                    "adjustment": query.adjustment.value,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "parameters": {},
                },
            )
    finally:
        services.close()

    assert functions.status_code == 200
    assert functions.json()["compatibility_version"] == "tdx-v1"
    assert preview.status_code == 200
    assert preview.json()["formula_version_id"] == version.id


def test_formula_catalog_validation_save_copy_and_version_routes(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-crud.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            templates = client.get("/api/formulas/templates")
            invalid = client.post(
                "/api/formulas/validate",
                json={
                    "formula_type": "indicator",
                    "source": "X:UNKNOWN(C);",
                    "parameter_schema": {},
                },
            )
            created = client.post(
                "/api/formulas",
                json={
                    "name": "Close",
                    "formula_type": "indicator",
                    "placement": "subchart",
                    "source": "X:C;",
                    "parameter_schema": {},
                },
            )
            created_body = created.json()
            formula_id = created_body["id"]
            listed = client.get("/api/formulas")
            detail = client.get(f"/api/formulas/{formula_id}")
            draft = client.put(
                f"/api/formulas/{formula_id}/draft",
                json={
                    "expected_revision": created_body["draft"]["revision"],
                    "source": "X:O;",
                    "parameter_schema": {},
                },
            )
            saved = client.post(
                f"/api/formulas/{formula_id}/save",
                json={
                    "expected_revision": draft.json()["revision"],
                    "source": "X:O;",
                    "parameter_schema": {},
                },
            )
            versions = client.get(f"/api/formulas/{formula_id}/versions")
            copied = client.post(
                f"/api/formulas/{formula_id}/copy",
                json={"name": "Close Copy", "source_version_id": saved.json()["id"]},
            )

        assert templates.status_code == 200
        assert templates.json()["items"][0]["template_id"] == "macd-cross-v1"
        assert invalid.status_code == 200
        assert invalid.json()["valid"] is False
        assert invalid.json()["diagnostics"][0]["code"] == "unsupported_function"
        assert created.status_code == 201
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [formula_id]
        assert detail.status_code == 200
        assert (
            detail.json()["draft"]["executable_version_id"]
            == created_body["draft"]["executable_version_id"]
        )
        assert draft.status_code == 200
        assert draft.json()["executable_version_id"] is None
        assert saved.status_code == 201
        assert saved.json()["version"] == 2
        assert versions.status_code == 200
        assert [item["version"] for item in versions.json()["items"]] == [1, 2]
        assert copied.status_code == 201
        assert copied.json()["formula_id"] != formula_id
        assert copied.json()["copied_from_version_id"] == saved.json()["id"]
    finally:
        services.close()


def test_formula_and_version_routes_apply_stable_cursor_pagination(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-pages.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    repository.save_draft("First", "X:C;")
    repository.save_draft("Second", "X:C;")
    repository.save_draft("Third", "X:C;")
    first_version = repository.create("Versions", "indicator", "X:C;", {})
    for source in ("X:O;", "X:H;"):
        draft = repository.get_draft(first_version.formula_id)
        repository.save(
            first_version.formula_id,
            source,
            {},
            expected_revision=draft.revision,
        )
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            formulas_first = client.get("/api/formulas", params={"limit": 2})
            formulas_second = client.get(
                "/api/formulas",
                params={
                    "limit": 2,
                    "cursor": formulas_first.json().get("next_cursor"),
                },
            )
            versions_first = client.get(
                f"/api/formulas/{first_version.formula_id}/versions",
                params={"limit": 2},
            )
            versions_second = client.get(
                f"/api/formulas/{first_version.formula_id}/versions",
                params={
                    "limit": 2,
                    "cursor": versions_first.json().get("next_cursor"),
                },
            )
            invalid = client.get(
                "/api/formulas", params={"limit": 2, "cursor": "missing"}
            )
    finally:
        services.close()

    assert formulas_first.status_code == 200
    assert len(formulas_first.json()["items"]) == 2
    assert (
        formulas_first.json()["next_cursor"] == formulas_first.json()["items"][-1]["id"]
    )
    assert formulas_second.status_code == 200
    assert len(formulas_second.json()["items"]) == 2
    assert formulas_second.json()["next_cursor"] is None
    assert versions_first.status_code == 200
    assert [item["version"] for item in versions_first.json()["items"]] == [1, 2]
    assert versions_second.status_code == 200
    assert [item["version"] for item in versions_second.json()["items"]] == [3]
    assert versions_second.json()["next_cursor"] is None
    assert invalid.status_code == 422
    assert invalid.json() == {"code": "invalid_cursor"}


def test_invalid_future_unpublished_and_stale_requests_use_safe_errors(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-errors.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    formulas = FormulaRepository(services.engine)
    unpublished = formulas.save_draft("Future", "X:REF(C,-1);")
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            rejected = client.post(
                "/api/formulas",
                json={
                    "name": "Future",
                    "formula_type": "indicator",
                    "placement": "subchart",
                    "source": "X:REF(C,-1);",
                    "parameter_schema": {},
                },
            )
            preview = client.post(
                f"/api/formulas/{unpublished.formula_id}/preview",
                json={
                    "symbol": "600000.SH",
                    "period": "1d",
                    "adjustment": "qfq",
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-02T00:00:00Z",
                    "parameters": {},
                },
            )
            stale = client.put(
                f"/api/formulas/{unpublished.formula_id}/draft",
                json={
                    "expected_revision": 99,
                    "source": "X:C;",
                    "parameter_schema": {},
                },
            )
            oversized = client.post(
                f"/api/formulas/{unpublished.formula_id}/preview",
                json={
                    "symbol": "600000.SH",
                    "period": "1d",
                    "adjustment": "qfq",
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-02T00:00:00Z",
                    "parameters": {f"P{index}": index for index in range(65)},
                },
            )
    finally:
        services.close()

    assert rejected.status_code == 422
    assert rejected.json() == {"code": "formula_invalid"}
    assert preview.status_code == 404
    assert preview.json() == {"code": "not_found"}
    assert stale.status_code == 409
    assert stale.json() == {"code": "revision_conflict"}
    assert oversized.status_code == 422
    assert oversized.json() == {"code": "invalid_request"}
    assert (
        str(tmp_path) not in rejected.text + preview.text + stale.text + oversized.text
    )


def test_timeout_terminates_worker_and_next_request_recovers(tmp_path: Path) -> None:
    children_before = {child.pid for child in multiprocessing.active_children()}
    executor = IsolatedFormulaExecutor(
        timeout_seconds=0.75,
        worker_target=_controllable_worker,
    )
    marker = tmp_path / "late-side-effect"

    with pytest.raises(FormulaPreviewTimeout, match="timed out"):
        executor.execute(f"slow:{marker}".encode())
    time.sleep(1.3)

    assert not marker.exists()
    # Recovery proves executor reuse, not a hosted-runner spawn deadline.
    executor.timeout_seconds = 30.0
    assert executor.execute(b"fast") == b"recovered"
    assert {child.pid for child in multiprocessing.active_children()} == children_before


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows message-mode pipes have no POSIX stream frame semantics",
)
def test_partial_frame_cannot_block_past_deadline_and_next_request_recovers() -> None:
    children_before = {child.pid for child in multiprocessing.active_children()}
    receiver_threads_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("formula-preview-receiver")
    }
    context = multiprocessing.get_context("spawn")
    partial_sent = context.Event()
    executor = IsolatedFormulaExecutor(
        # Leave enough time for a cold hosted-runner spawn so this test reaches
        # the partial-frame state it is intended to classify.
        timeout_seconds=5.0,
        worker_target=partial(posix_partial_frame_worker, partial_sent),
    )

    started = time.monotonic()
    with pytest.raises(FormulaPreviewTimeout, match="timed out"):
        executor.execute(b"partial")
    elapsed = time.monotonic() - started

    assert partial_sent.is_set()
    executor._worker_target = echo_formula_worker
    # The timed path above retains its 0.75s assertion; recovery is not a
    # process-startup benchmark.
    executor.timeout_seconds = 30.0
    assert executor.execute(b"recovered") == b"recovered"
    assert elapsed < 5.75
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("formula-preview-receiver")
    } == receiver_threads_before


def test_executor_globally_bounds_distinct_concurrent_workers() -> None:
    context = multiprocessing.get_context("spawn")
    active = context.Value("i", 0)
    maximum = context.Value("i", 0)
    lock = context.Lock()
    # This test classifies the global concurrency bound, not queued spawn latency.
    # Eight spawn-mode requests can exceed 10s on a loaded hosted runner.
    executor = IsolatedFormulaExecutor(
        timeout_seconds=30.0,
        max_workers=2,
        worker_target=partial(_tracked_formula_worker, active, maximum, lock),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(executor.execute, [bytes([index]) for index in range(8)])
        )

    assert results == [bytes([index]) for index in range(8)]
    assert maximum.value == 2


def test_executor_slot_wait_counts_against_request_deadline_and_recovers() -> None:
    executor = IsolatedFormulaExecutor(
        timeout_seconds=0.2,
        max_workers=1,
        worker_target=_slow_formula_worker,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executor.execute, b"first")
        time.sleep(0.05)
        second = pool.submit(executor.execute, b"second")
        for future in (first, second):
            with pytest.raises(FormulaPreviewTimeout, match="timed out"):
                future.result(timeout=1.0)

    executor._worker_target = _echo_formula_worker
    # Keep the 0.2s slot deadline above; recovery is not a spawn benchmark.
    executor.timeout_seconds = 30.0
    assert executor.execute(b"recovered") == b"recovered"


def test_worker_crash_is_safe_and_next_request_recovers() -> None:
    children_before = {child.pid for child in multiprocessing.active_children()}
    # This test classifies a completed crash rather than measuring spawn latency.
    executor = IsolatedFormulaExecutor(
        timeout_seconds=30.0,
        worker_target=_controllable_worker,
    )

    with pytest.raises(FormulaPreviewWorkerError, match="worker failed"):
        executor.execute(b"crash")

    assert executor.execute(b"fast") == b"recovered"
    assert {child.pid for child in multiprocessing.active_children()} == children_before


def test_worker_request_bytes_are_bounded() -> None:
    executor = IsolatedFormulaExecutor(
        timeout_seconds=1.0,
        worker_target=_controllable_worker,
        max_request_bytes=8,
        max_response_bytes=8,
    )

    with pytest.raises(FormulaPreviewResourceError, match="resource limits"):
        executor.execute(b"123456789")


def test_worker_response_bytes_are_bounded() -> None:
    # This test classifies an oversized response rather than measuring spawn latency.
    executor = IsolatedFormulaExecutor(
        timeout_seconds=30.0,
        worker_target=_controllable_worker,
        max_request_bytes=8,
        max_response_bytes=4,
    )

    with pytest.raises(FormulaPreviewResourceError, match="resource limits"):
        executor.execute(b"fast")


def test_worker_receive_cap_uses_configured_bound_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test classifies the receive cap and recovery, not process spawn latency.
    observed_limits: list[int | None] = []
    original_recv_bytes = Connection.recv_bytes

    def tracked_recv_bytes(
        connection: Connection, maxlength: int | None = None
    ) -> bytes:
        observed_limits.append(maxlength)
        return original_recv_bytes(connection, maxlength)

    monkeypatch.setattr(Connection, "recv_bytes", tracked_recv_bytes)
    executor = IsolatedFormulaExecutor(
        # This test classifies the receive bound, so use the maximum supported
        # deadline rather than making hosted-runner spawn latency part of it.
        timeout_seconds=30.0,
        worker_target=_echo_formula_worker,
        max_request_bytes=8,
        max_response_bytes=4,
    )

    with pytest.raises(FormulaPreviewResourceError, match="resource limits"):
        executor.execute(b"12345")

    assert executor.execute(b"ok") == b"ok"
    assert observed_limits == [5, 5]


def test_rapid_worker_completion_does_not_race_with_process_exit() -> None:
    # This test classifies process-exit ordering rather than spawn latency.
    executor = IsolatedFormulaExecutor(
        timeout_seconds=30.0,
        worker_target=_controllable_worker,
    )

    assert [executor.execute(b"fast") for _ in range(10)] == [b"recovered"] * 10


@pytest.mark.parametrize(
    ("worker", "timeout_seconds", "status_code", "code"),
    [
        (_slow_formula_worker, 0.5, 504, "preview_timeout"),
        (_crashing_formula_worker, 30.0, 503, "preview_worker_failed"),
    ],
)
def test_api_maps_isolated_worker_failures_to_safe_errors(
    tmp_path: Path,
    worker,
    timeout_seconds: float,
    status_code: int,
    code: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'{code}.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create("Failure", "indicator", "X:C;", {})
    formula_service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=IsolatedFormulaExecutor(
            timeout_seconds=timeout_seconds,
            worker_target=worker,
        ),
    )
    query = routed.result.query
    children_before = {child.pid for child in multiprocessing.active_children()}
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
                formula_service=formula_service,
            )
        ) as client:
            response = client.post(
                f"/api/formulas/{version.id}/preview",
                json={
                    "symbol": query.symbol,
                    "period": query.period.value,
                    "adjustment": query.adjustment.value,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "parameters": {},
                },
            )
    finally:
        services.close()

    assert response.status_code == status_code
    assert response.json() == {"code": code}
    assert str(tmp_path) not in response.text
    assert {child.pid for child in multiprocessing.active_children()} == children_before


@pytest.mark.parametrize(
    ("version_field", "unsupported_value"),
    [
        ("engine_version", "formula-engine-v2"),
        ("compatibility_version", "tdx-v2"),
    ],
)
def test_preview_fails_closed_for_unsupported_persisted_formula_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_field: str,
    unsupported_value: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'unsupported-{version_field}.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create("Unsupported", "indicator", "X:C;", {})
    report = dict(version.validation_result[0])
    report[version_field] = unsupported_value
    with repository.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_formula_version_immutable_update"))
        connection.execute(
            text(
                f"UPDATE formula_version SET {version_field} = :version_value, "
                "validation_result_json = :report WHERE id = :version_id"
            ),
            {
                "report": json.dumps([report], sort_keys=True, separators=(",", ":")),
                "version_id": version.id,
                "version_value": unsupported_value,
            },
        )

    def reject_current_engine_recompute(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsupported persisted version was recompiled")

    monkeypatch.setattr(
        "stock_desk.formula.repository._formula_diagnostics",
        reject_current_engine_recompute,
    )
    executor = NeverExecutor()
    formula_service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,
    )
    query = routed.result.query
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
                formula_service=formula_service,
            )
        ) as client:
            response = client.post(
                f"/api/formulas/{version.id}/preview",
                json={
                    "symbol": query.symbol,
                    "period": query.period.value,
                    "adjustment": query.adjustment.value,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "parameters": {},
                },
            )
    finally:
        services.close()

    assert executor.calls == 0
    assert response.status_code == 422
    assert response.json() == {"code": "unsupported_formula_version"}
    assert unsupported_value not in response.text


def test_validate_and_publish_share_parameter_schema_semantics(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-schema-validation.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    invalid_schemas = ({"N": {"kind": "integer", "default": 2**53}},)
    valid_schemas = (
        {"N": {"kind": "integer", "default": 1}},
        {"N": {"kind": "integer", "default": 2**53 - 1}},
        {"N": {"kind": "integer", "default": -(2**53 - 1)}},
        {"N": {"kind": "number", "default": 1.0}},
        # JSON has one numeric type. Browsers serialize 1.0 as 1, so a number
        # declaration must preserve its kind instead of depending on JSON text.
        {"N": {"kind": "number", "default": 1}},
    )
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            invalid = [
                client.post(
                    "/api/formulas/validate",
                    json={
                        "formula_type": "indicator",
                        "source": "X:C+N;",
                        "parameter_schema": schema,
                    },
                )
                for schema in invalid_schemas
            ]
            invalid_published = [
                client.post(
                    "/api/formulas",
                    json={
                        "name": f"Invalid Schema {index}",
                        "formula_type": "indicator",
                        "placement": "subchart",
                        "source": "X:C+N;",
                        "parameter_schema": schema,
                    },
                )
                for index, schema in enumerate(invalid_schemas)
            ]
            validated_and_published = []
            for index, schema in enumerate(valid_schemas):
                validated = client.post(
                    "/api/formulas/validate",
                    json={
                        "formula_type": "indicator",
                        "source": "X:C+N;",
                        "parameter_schema": schema,
                    },
                )
                published = client.post(
                    "/api/formulas",
                    json={
                        "name": f"Schema {index}",
                        "formula_type": "indicator",
                        "placement": "subchart",
                        "source": "X:C+N;",
                        "parameter_schema": schema,
                    },
                )
                validated_and_published.append((validated, published))
    finally:
        services.close()

    for response in invalid:
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
    for response in invalid_published:
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
    for validated, published in validated_and_published:
        assert validated.status_code == 200
        assert validated.json() == {"valid": True, "diagnostics": []}
        assert published.status_code == 201
        declaration = published.json()["draft"]["parameter_schema"]["N"]
        if declaration["kind"] == "number":
            assert isinstance(declaration["default"], float)


def test_number_defaults_round_trip_across_draft_and_save_requests(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-number-round-trip.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    schema = {"N": {"kind": "number", "default": 1}}
    boolean_schema = {"N": {"kind": "number", "default": True}}
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            created = client.post(
                "/api/formulas",
                json={
                    "name": "Browser Number",
                    "formula_type": "indicator",
                    "placement": "subchart",
                    "source": "X:C+N;",
                    "parameter_schema": schema,
                },
            )
            formula_id = created.json()["id"]
            draft = client.put(
                f"/api/formulas/{formula_id}/draft",
                json={
                    "source": "X:C+N+1;",
                    "parameter_schema": schema,
                    "expected_revision": 1,
                },
            )
            saved = client.post(
                f"/api/formulas/{formula_id}/save",
                json={
                    "source": "X:C+N+2;",
                    "parameter_schema": schema,
                    "expected_revision": 2,
                },
            )
            rejected = (
                client.post(
                    "/api/formulas/validate",
                    json={
                        "formula_type": "indicator",
                        "source": "X:C+N;",
                        "parameter_schema": boolean_schema,
                    },
                ),
                client.put(
                    f"/api/formulas/{formula_id}/draft",
                    json={
                        "source": "X:C+N;",
                        "parameter_schema": boolean_schema,
                        "expected_revision": 3,
                    },
                ),
                client.post(
                    f"/api/formulas/{formula_id}/save",
                    json={
                        "source": "X:C+N;",
                        "parameter_schema": boolean_schema,
                        "expected_revision": 3,
                    },
                ),
            )
    finally:
        services.close()

    assert created.status_code == 201
    assert draft.status_code == 200
    assert draft.json()["parameter_schema"]["N"]["default"] == 1.0
    assert saved.status_code == 201
    assert saved.json()["parameter_schema"]["N"]["default"] == 1.0
    assert all(response.status_code == 422 for response in rejected)


def test_public_integer_parameters_use_javascript_safe_bounds(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-safe-integer.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    maximum = 2**53 - 1
    schema = {"N": {"kind": "integer", "default": maximum}}
    unsafe_schema = {"N": {"kind": "integer", "default": 2**53}}
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            created = client.post(
                "/api/formulas",
                json={
                    "name": "Safe Integer",
                    "formula_type": "indicator",
                    "placement": "subchart",
                    "source": "X:C+N;",
                    "parameter_schema": schema,
                },
            )
            formula_id = created.json()["id"]
            saved = client.post(
                f"/api/formulas/{formula_id}/save",
                json={
                    "source": "X:C+N+1;",
                    "parameter_schema": schema,
                    "expected_revision": 1,
                },
            )
            rejected_create = client.post(
                "/api/formulas",
                json={
                    "name": "Unsafe Integer",
                    "formula_type": "indicator",
                    "placement": "subchart",
                    "source": "X:C+N;",
                    "parameter_schema": unsafe_schema,
                },
            )
            rejected_save = client.post(
                f"/api/formulas/{formula_id}/save",
                json={
                    "source": "X:C+N+2;",
                    "parameter_schema": unsafe_schema,
                    "expected_revision": 2,
                },
            )
    finally:
        services.close()

    assert created.status_code == 201
    assert created.json()["draft"]["parameter_schema"]["N"]["default"] == maximum
    assert saved.status_code == 201
    assert saved.json()["parameter_schema"]["N"]["default"] == maximum
    assert rejected_create.status_code == 422
    assert rejected_save.status_code == 422


def test_formula_openapi_has_bounded_requests_and_error_schemas(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-openapi.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            document = client.get("/openapi.json").json()
    finally:
        services.close()

    expected = {
        ("/api/formulas/functions", "get"),
        ("/api/formulas/templates", "get"),
        ("/api/formulas/validate", "post"),
        ("/api/formulas", "get"),
        ("/api/formulas", "post"),
        ("/api/formulas/{formula_id}", "get"),
        ("/api/formulas/{formula_id}/draft", "put"),
        ("/api/formulas/{formula_id}/save", "post"),
        ("/api/formulas/{formula_id}/copy", "post"),
        ("/api/formulas/{formula_id}/versions", "get"),
        ("/api/formulas/{version_id}/preview", "post"),
    }
    for path, method in expected:
        operation = document["paths"][path][method]
        assert operation["responses"]
        if method in {"post", "put"}:
            assert operation["requestBody"]["content"]["application/json"]["schema"]
        for error_code in set(operation["responses"]) & {
            "404",
            "409",
            "422",
            "503",
            "504",
        }:
            schema = operation["responses"][error_code]["content"]["application/json"][
                "schema"
            ]
            assert schema["$ref"].endswith("/FormulaErrorResponse")
    preview_schema = document["components"]["schemas"]["FormulaPreviewRequest"]
    assert preview_schema["properties"]["parameters"]["maxProperties"] == 64
    preview_parameter = preview_schema["properties"]["parameters"]["patternProperties"][
        "^[A-Z][A-Z0-9_]{0,63}$"
    ]
    preview_integer = next(
        item for item in preview_parameter["anyOf"] if item["type"] == "integer"
    )
    assert "minimum" not in preview_integer
    assert "maximum" not in preview_integer
    components = document["components"]["schemas"]
    signal_series = components["SignalSeries"]["properties"]
    assert signal_series["parameters"]["maxItems"] == 64
    assert signal_series["timestamps"]["maxItems"] == 100_000
    assert signal_series["numeric_outputs"]["maxItems"] == 32
    assert signal_series["signals"]["maxItems"] == 2
    assert signal_series["runtime_diagnostics"]["maxItems"] == 32
    assert components["NumericOutput"]["properties"]["values"]["maxItems"] == 100_000
    assert components["BooleanSignal"]["properties"]["values"]["maxItems"] == 100_000
    parameter_schema = components["FormulaCreateRequest"]["properties"][
        "parameter_schema"
    ]
    assert parameter_schema["maxProperties"] == 64
    declaration_ref = parameter_schema["patternProperties"]["^[A-Z][A-Z0-9_]{0,63}$"][
        "$ref"
    ]
    assert declaration_ref.endswith("/FormulaParameterDeclaration")
    declaration = components["FormulaParameterDeclaration"]
    assert {item["$ref"].rsplit("/", 1)[-1] for item in declaration["oneOf"]} == {
        "FormulaIntegerParameterDeclaration",
        "FormulaNumberParameterDeclaration",
    }
    integer_declaration = components["FormulaIntegerParameterDeclaration"]
    assert integer_declaration["additionalProperties"] is False
    integer_default = integer_declaration["properties"]["default"]
    assert integer_default["minimum"] == -(2**53 - 1)
    assert integer_default["maximum"] == 2**53 - 1
    assert integer_declaration["properties"]["label"]["anyOf"][0]["maxLength"] == 256
    assert components["FormulaDiagnosticSpanResponse"]["additionalProperties"] is False
    assert (
        components["FormulaDiagnosticResponse"]["properties"]["code"]["maxLength"] == 64
    )
    assert (
        components["FormulaValidationResponse"]["properties"]["diagnostics"]["maxItems"]
        == 64
    )
    assert (
        components["FormulaDraftResponse"]["properties"]["source"]["maxLength"]
        == 64_000
    )
    assert (
        components["FormulaVersionResponse"]["properties"]["validation_result"][
            "maxItems"
        ]
        == 64
    )
    assert components["FormulaListResponse"]["properties"]["items"]["maxItems"] == 100
    assert (
        components["FormulaListResponse"]["properties"]["next_cursor"]["anyOf"][0][
            "maxLength"
        ]
        == 128
    )
    assert (
        components["FormulaVersionListResponse"]["properties"]["items"]["maxItems"]
        == 100
    )
    functions_schema = document["paths"]["/api/formulas/functions"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert functions_schema["$ref"].endswith("/FormulaCompatibilityResponse")
    assert (
        components["FormulaCompatibilityResponse"]["properties"]["functions"][
            "maxItems"
        ]
        == 128
    )
    list_parameters = {
        item["name"]: item["schema"]
        for item in document["paths"]["/api/formulas"]["get"]["parameters"]
    }
    assert list_parameters["limit"]["maximum"] == 100
    assert list_parameters["cursor"]["anyOf"][0]["maxLength"] == 128
    version_parameters = {
        item["name"]: item["schema"]
        for item in document["paths"]["/api/formulas/{formula_id}/versions"]["get"][
            "parameters"
        ]
    }
    assert version_parameters["limit"]["maximum"] == 100
    bar_responses = document["paths"]["/api/market/bars"]["get"]["responses"]
    for code in ("503", "504"):
        assert bar_responses[code]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/FormulaErrorResponse")


def test_default_formula_service_is_lazy_singleton_on_market_database(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-lifecycle.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    application = create_app(
        Settings(database_url=database_url, data_dir=tmp_path),
        market_services=services,
    )
    try:
        with TestClient(application):
            first = application.state.formula_service_provider()
            second = application.state.formula_service_provider()
            assert first is second
            assert first.database_identity == services.database_identity
    finally:
        services.close()


def test_formula_service_rejects_repository_lake_database_mismatch(
    tmp_path: Path,
) -> None:
    first_url = f"sqlite:///{tmp_path / 'formula-first.db'}"
    second_url = f"sqlite:///{tmp_path / 'formula-second.db'}"
    migrate(first_url)
    migrate(second_url)
    first_engine = create_engine_for_url(first_url)
    second_services = MarketServices(
        engine=create_engine_for_url(second_url),
        lake_root=(tmp_path / "second-market").resolve(),
    )
    try:
        with pytest.raises(ValueError, match="database identities do not match"):
            FormulaService(
                repository=FormulaRepository(first_engine),
                lake=second_services.lake,
            )
    finally:
        first_engine.dispose()
        second_services.close()


def test_injected_formula_service_database_mismatch_fails_before_execution(
    tmp_path: Path,
) -> None:
    formula_url = f"sqlite:///{tmp_path / 'formula-owner.db'}"
    market_url = f"sqlite:///{tmp_path / 'market-owner.db'}"
    migrate(formula_url)
    migrate(market_url)
    formula_services = MarketServices(
        engine=create_engine_for_url(formula_url),
        lake_root=(tmp_path / "formula-market").resolve(),
    )
    market_services = MarketServices(
        engine=create_engine_for_url(market_url),
        lake_root=(tmp_path / "chart-market").resolve(),
    )
    executor = NeverExecutor()
    formula_repository = FormulaRepository(formula_services.engine)
    version = formula_repository.create("Wrong DB", "indicator", "X:C;", {})
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    market_services.lake.write(routed)
    formula_service = FormulaService(
        repository=formula_repository,
        lake=formula_services.lake,
        executor=executor,
    )
    query = routed.result.query
    try:
        with TestClient(
            create_app(
                Settings(database_url=market_url, data_dir=tmp_path),
                market_services=market_services,
                formula_service=formula_service,
            )
        ) as client:
            response = client.get(
                "/api/market/bars",
                params={
                    "symbol": query.symbol,
                    "period": query.period.value,
                    "adjustment": query.adjustment.value,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "formula_version_id": version.id,
                },
            )
    finally:
        formula_services.close()
        market_services.close()

    assert executor.calls == 0
    assert response.status_code == 503
    assert response.json() == {"code": "storage_mismatch"}
    assert str(tmp_path) not in response.text
