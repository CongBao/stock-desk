from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import threading
import time
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient
import pytest

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.formula.compiler import compile_formula
from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.evaluator import FormulaEvaluator
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import (
    MACD_TEMPLATE_SOURCE,
    FormulaPreviewValidationError,
    FormulaPreviewWorkerError,
    FormulaService,
    IsolatedFormulaExecutor,
)
from stock_desk.formula.signal_series import (
    FormulaReference,
    NormalizedParameter,
    SignalSeries,
)
from stock_desk.formula.values import IntegerScalar
from stock_desk.main import create_app
from stock_desk.market.types import Adjustment, Period, ProviderId
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = IsolatedFormulaExecutor()

    def execute(self, request: bytes) -> bytes:
        self.calls += 1
        return self.delegate.execute(request)


class StaticExecutor:
    def __init__(self, payload: bytes) -> None:
        self.calls = 0
        self.payload = payload

    def execute(self, _request: bytes) -> bytes:
        self.calls += 1
        return self.payload


class BlockingExecutor:
    def __init__(self, payload: bytes) -> None:
        self.calls = 0
        self.payload = payload
        self.failure: Exception | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def execute(self, _request: bytes) -> bytes:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=3.0):
            raise AssertionError("test executor was not released")
        if self.failure is not None:
            raise self.failure
        return self.payload


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _forge_series(result: SignalSeries, updates: dict[str, Any]) -> bytes:
    fields = {
        name: getattr(result, name)
        for name in type(result).model_fields
        if name != "signal_series_id"
    }
    fields.update(updates)
    provisional = SignalSeries.model_construct(
        signal_series_id="sha256:" + "0" * 64,
        **fields,
    )
    identity_payload = provisional.model_dump(mode="json", exclude={"signal_series_id"})
    identity = (
        f"sha256:{hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest()}"
    )
    return SignalSeries(signal_series_id=identity, **fields).canonical_json_bytes()


def _forge_raw_version(result: SignalSeries, field: str, value: str) -> bytes:
    payload = result.model_dump(mode="json")
    payload[field] = value
    identity_payload = {
        key: item for key, item in payload.items() if key != "signal_series_id"
    }
    payload["signal_series_id"] = (
        f"sha256:{hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest()}"
    )
    return _canonical_bytes(payload)


def test_preview_and_chart_signal_payload_match(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-consistency.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    services = MarketServices(engine=engine, lake_root=(tmp_path / "market").resolve())
    formulas = FormulaRepository(engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = formulas.create(
        "Cross",
        "trading",
        "BUY:CROSS(C,REF(C,1));SELL:CROSS(REF(C,1),C);",
        {},
        placement="main",
    )
    query = routed.result.query
    params = {
        "symbol": query.symbol,
        "period": query.period.value,
        "adjustment": query.adjustment.value,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
    }
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            preview = client.post(
                f"/api/formulas/{version.id}/preview",
                json={**params, "parameters": {}},
            )
            chart = client.get(
                "/api/market/bars",
                params={**params, "formula_version_id": version.id},
            )
            plain_chart = client.get("/api/market/bars", params=params)
    finally:
        services.close()

    assert preview.status_code == 200
    assert chart.status_code == 200
    assert plain_chart.status_code == 200
    assert "formula" not in plain_chart.json()
    assert preview.json()["signals"] == chart.json()["formula"]["signals"]
    assert preview.json()["engine_version"] == chart.json()["formula"]["engine_version"]
    assert json.dumps(
        preview.json(), sort_keys=True, separators=(",", ":")
    ) == json.dumps(chart.json()["formula"], sort_keys=True, separators=(",", ":"))


def test_preview_cache_hits_and_invalidates_on_dataset_provenance(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-cache.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    executor = CountingExecutor()
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,  # type: ignore[arg-type]
    )
    first_data = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(first_data)
    version = repository.create("Cache", "indicator", "X:C;", {})
    try:
        first = service.preview(version.id, first_data.result.query, {})
        repeated = service.preview(version.id, first_data.result.query, {})
        changed_data = routed_daily_bars(
            (date(2024, 1, 2), date(2024, 1, 3)), volume_delta=-1
        )
        changed = service.preview_routed(version.id, changed_data, {})
    finally:
        services.close()

    assert repeated.canonical_json_bytes() == first.canonical_json_bytes()
    assert executor.calls == 2
    assert changed.dataset_version != first.dataset_version


def test_preview_cache_has_a_total_byte_budget(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-cache-budget.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    executor = CountingExecutor()
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,  # type: ignore[arg-type]
        max_cache_bytes=1,
    )
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create("Budget", "indicator", "X:C;", {})
    try:
        service.preview(version.id, routed.result.query, {})
        service.preview(version.id, routed.result.query, {})
    finally:
        services.close()

    assert executor.calls == 2
    assert service.cache_size_bytes == 0


def test_preview_cache_key_memory_is_constant_in_market_row_count(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-cache-key.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    version = repository.create("Key Memory", "indicator", "X:C;", {})
    service = FormulaService(repository=repository, lake=services.lake)
    try:
        result = service.preview_routed(version.id, routed, {})
        key = next(iter(service._cache))
    finally:
        services.close()

    assert "timestamps" not in key.__slots__
    assert service.cache_size_bytes == len(result.canonical_json_bytes())


def test_identical_concurrent_preview_misses_execute_once(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-single-flight.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    version = repository.create("Single Flight", "indicator", "X:C;", {})
    baseline = FormulaService(repository=repository, lake=services.lake).preview_routed(
        version.id, routed, {}
    )
    executor = BlockingExecutor(baseline.canonical_json_bytes())
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(service.preview_routed, version.id, routed, {})
                for _ in range(8)
            ]
            assert executor.entered.wait(timeout=2.0)
            time.sleep(0.2)
            executor.release.set()
            results = [future.result(timeout=2.0) for future in futures]
    finally:
        services.close()

    assert executor.calls == 1
    assert {result.canonical_json_bytes() for result in results} == {
        baseline.canonical_json_bytes()
    }


def test_single_flight_failure_wakes_waiters_and_recovers(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-single-flight-failure.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    version = repository.create("Single Flight Failure", "indicator", "X:C;", {})
    baseline = FormulaService(repository=repository, lake=services.lake).preview_routed(
        version.id, routed, {}
    )
    executor = BlockingExecutor(baseline.canonical_json_bytes())
    executor.failure = FormulaPreviewWorkerError("formula preview worker failed")
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(service.preview_routed, version.id, routed, {})
                for _ in range(8)
            ]
            assert executor.entered.wait(timeout=2.0)
            time.sleep(0.2)
            executor.release.set()
            errors = []
            for future in futures:
                with pytest.raises(FormulaPreviewWorkerError) as captured:
                    future.result(timeout=2.0)
                errors.append(str(captured.value))

        executor.failure = None
        recovered = service.preview_routed(version.id, routed, {})
    finally:
        services.close()

    assert executor.calls == 2
    assert errors == ["formula preview worker failed"] * 8
    assert recovered.canonical_json_bytes() == baseline.canonical_json_bytes()


def test_preview_cache_binds_parameters_and_exact_formula_version(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-cache-identity.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    executor = CountingExecutor()
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,
    )
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    schema = {"N": {"kind": "integer", "default": 1}}
    services.lake.write(routed)
    first_version = repository.create("Parameters", "indicator", "X:C+N;", schema)
    try:
        first = service.preview(first_version.id, routed.result.query, {"N": 1})
        repeated = service.preview(first_version.id, routed.result.query, {"N": 1})
        changed_parameter = service.preview(
            first_version.id, routed.result.query, {"N": 2}
        )
        draft = repository.get_draft(first_version.formula_id)
        second_version = repository.save(
            first_version.formula_id,
            "X:C+N+1;",
            schema,
            expected_revision=draft.revision,
        )
        changed_version = service.preview(
            second_version.id, routed.result.query, {"N": 2}
        )
    finally:
        services.close()

    assert first.signal_series_id == repeated.signal_series_id
    assert changed_parameter.signal_series_id != first.signal_series_id
    assert changed_version.formula_version_id == second_version.id
    assert changed_version.signal_series_id != changed_parameter.signal_series_id
    assert executor.calls == 3


def test_preview_cache_canonicalizes_signed_zero_number_overrides(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-signed-zero-cache.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    executor = CountingExecutor()
    service = FormulaService(
        repository=repository,
        lake=services.lake,
        executor=executor,
    )
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create(
        "Signed Zero",
        "indicator",
        "X:C+N;",
        {"N": {"kind": "number", "default": 0.0}},
    )
    try:
        negative = service.preview(version.id, routed.result.query, {"N": -0.0})
        positive = service.preview(version.id, routed.result.query, {"N": 0.0})
    finally:
        services.close()

    assert negative.signal_series_id == positive.signal_series_id
    assert executor.calls == 1


def test_preview_parameter_numeric_limits_are_stable_and_match_chart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-parameter-limits.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create(
        "Parameter Limits",
        "indicator",
        "X:C+N+F;",
        {
            "F": {"kind": "number", "default": 0.0},
            "N": {"kind": "integer", "default": 0},
        },
    )
    service = FormulaService(repository=repository, lake=services.lake)
    query = routed.result.query
    request = {
        "symbol": query.symbol,
        "period": query.period.value,
        "adjustment": query.adjustment.value,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
    }
    try:
        with pytest.raises(
            FormulaPreviewValidationError,
            match=r"^formula parameters are invalid$",
        ):
            service.preview_routed(version.id, routed, {"N": 2**53 + 1})

        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
                formula_service=service,
            )
        ) as client:
            responses = []
            for parameters in ({"N": 2**53 + 1}, {"F": float("nan")}):
                responses.extend(
                    (
                        client.post(
                            f"/api/formulas/{version.id}/preview",
                            content=json.dumps(
                                {**request, "parameters": parameters},
                                allow_nan=True,
                            ),
                            headers={"content-type": "application/json"},
                        ),
                        client.get(
                            "/api/market/bars",
                            params={
                                **request,
                                "formula_version_id": version.id,
                                "formula_parameters": json.dumps(
                                    parameters, allow_nan=True
                                ),
                            },
                        ),
                    )
                )
    finally:
        services.close()

    assert [response.status_code for response in responses] == [422] * 4
    assert responses[0].json() == {"code": "invalid_request"}
    assert responses[1].json() == {"code": "invalid_request", "issues": []}
    assert responses[2].json() == {"code": "invalid_request"}
    assert responses[3].json() == {"code": "invalid_request", "issues": []}


def test_chart_formula_parameters_reject_python_huge_integer_valueerror(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-huge-json-integer.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create(
        "Huge JSON",
        "indicator",
        "X:C+N;",
        {"N": {"kind": "integer", "default": 1}},
    )
    service = FormulaService(repository=repository, lake=services.lake)
    query = routed.result.query
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
                formula_service=service,
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
                    "formula_parameters": '{"N":' + "9" * 5_000 + "}",
                },
            )
    finally:
        services.close()

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "issues": []}


def test_chart_formula_service_valueerror_is_safe_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-internal-valueerror.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    service = FormulaService(repository=repository, lake=services.lake)
    query = routed.result.query
    calls: list[tuple[str, Mapping[str, int | float]]] = []

    def fail_after_parameter_parsing(
        version_id: str,
        _routed: object,
        parameters: Mapping[str, int | float],
    ) -> SignalSeries:
        calls.append((version_id, parameters))
        raise ValueError("unexpected formula service failure")

    monkeypatch.setattr(service, "preview_routed", fail_after_parameter_parsing)
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
                formula_service=service,
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
                    "formula_version_id": "formula-version",
                    "formula_parameters": '{"N":2}',
                },
            )
    finally:
        services.close()

    assert calls == [("formula-version", {"N": 2})]
    assert response.status_code == 503
    assert response.json() == {"code": "internal_error"}
    assert "unexpected formula service failure" not in response.text


def test_preview_and_chart_share_parameter_binding(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-parameter-consistency.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    services.lake.write(routed)
    version = repository.create(
        "Parameter",
        "indicator",
        "X:C+N;",
        {"N": {"kind": "integer", "default": 1}},
    )
    query = routed.result.query
    params = {
        "symbol": query.symbol,
        "period": query.period.value,
        "adjustment": query.adjustment.value,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
    }
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            preview = client.post(
                f"/api/formulas/{version.id}/preview",
                json={**params, "parameters": {"N": 2}},
            )
            chart = client.get(
                "/api/market/bars",
                params={
                    **params,
                    "formula_version_id": version.id,
                    "formula_parameters": '{"N":2}',
                },
            )
    finally:
        services.close()

    assert preview.status_code == 200
    assert chart.status_code == 200
    assert preview.json() == chart.json()["formula"]


def test_parent_rejects_every_forged_worker_input_identity_without_caching(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-forged-worker.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    routed = routed_daily_bars((date(2024, 1, 1), date(2024, 1, 8)))
    schema = {"N": {"kind": "integer", "default": 1}}
    version = repository.create("Forged", "indicator", "X:C+N;", schema)
    context = EvaluationContext.from_routed(routed, parameters={"N": IntegerScalar(1)})
    result = FormulaEvaluator().evaluate(
        version.source,
        context,
        FormulaReference(
            formula_id=version.formula_id,
            formula_version_id=version.id,
            version=version.version,
            checksum=version.checksum,
        ),
    )
    changed_timestamp = result.timestamps[-1] - timedelta(days=1)
    digest = "sha256:" + "f" * 64
    mutations: tuple[tuple[str, bytes], ...] = (
        ("formula_id", _forge_series(result, {"formula_id": "forged"})),
        (
            "formula_version_id",
            _forge_series(result, {"formula_version_id": "forged-v2"}),
        ),
        ("formula_version", _forge_series(result, {"formula_version": 2})),
        ("formula_checksum", _forge_series(result, {"formula_checksum": digest})),
        (
            "engine_version",
            _forge_raw_version(result, "engine_version", "formula-engine-v2"),
        ),
        (
            "compatibility_version",
            _forge_raw_version(result, "compatibility_version", "tdx-v2"),
        ),
        ("symbol", _forge_series(result, {"symbol": "000001.SZ"})),
        ("source", _forge_series(result, {"source": ProviderId.BAOSTOCK})),
        ("period", _forge_series(result, {"period": Period.WEEK})),
        (
            "adjustment",
            _forge_series(result, {"adjustment": Adjustment.HFQ}),
        ),
        ("dataset_version", _forge_series(result, {"dataset_version": digest})),
        ("route_version", _forge_series(result, {"route_version": digest})),
        (
            "manifest_record_id",
            _forge_series(result, {"manifest_record_id": digest}),
        ),
        (
            "data_cutoff",
            _forge_series(
                result, {"data_cutoff": result.data_cutoff + timedelta(days=1)}
            ),
        ),
        (
            "query_start",
            _forge_series(
                result, {"query_start": result.query_start - timedelta(days=1)}
            ),
        ),
        (
            "query_end",
            _forge_series(result, {"query_end": result.query_end + timedelta(days=1)}),
        ),
        (
            "parameters",
            _forge_series(
                result,
                {
                    "parameters": (
                        NormalizedParameter(name="N", kind="integer", value="2"),
                    )
                },
            ),
        ),
        (
            "timestamps",
            _forge_series(
                result,
                {"timestamps": (result.timestamps[0], changed_timestamp)},
            ),
        ),
        (
            "multiple",
            _forge_series(
                result,
                {
                    "formula_id": "forged",
                    "symbol": "000001.SZ",
                    "dataset_version": digest,
                },
            ),
        ),
    )
    try:
        for label, payload in mutations:
            executor = StaticExecutor(payload)
            service = FormulaService(
                repository=repository,
                lake=services.lake,
                executor=executor,
            )
            for _ in range(2):
                try:
                    service.preview_routed(version.id, routed, {"N": 1})
                except FormulaPreviewWorkerError as error:
                    assert str(error) == "formula preview worker failed"
                else:
                    pytest.fail(f"forged {label} identity was accepted")
            assert executor.calls == 2, label
            assert service.cache_size_bytes == 0, label
    finally:
        services.close()


def test_macd_template_has_public_outputs_signals_and_subchart_evaluation(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-macd-template.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    repository = FormulaRepository(services.engine)
    service = FormulaService(repository=repository, lake=services.lake)
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    try:
        template = service.templates()[0]
        compiled = compile_formula(MACD_TEMPLATE_SOURCE)
        version = service.create(
            name="Template MACD",
            formula_type="trading",
            placement=str(template["placement"]),
            source=MACD_TEMPLATE_SOURCE,
            parameter_schema={},
        )
        evaluated = service.preview_routed(version.id, routed, {})
    finally:
        services.close()

    assert template["placement"] == "subchart"
    assert compiled.numeric_outputs == ("DIF", "DEA", "MACD")
    assert compiled.signal_outputs == ("BUY", "SELL")
    assert tuple(output.name for output in evaluated.numeric_outputs) == (
        "DIF",
        "DEA",
        "MACD",
    )
    assert tuple(signal.name for signal in evaluated.signals) == ("BUY", "SELL")
