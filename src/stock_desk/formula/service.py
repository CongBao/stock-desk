from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import multiprocessing
from multiprocessing.connection import Connection
import time
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import Any, Protocol, cast

from pydantic import ValidationError

from stock_desk.formula.compiler import compile_formula
from stock_desk.formula.context import EvaluationContext, MAX_PARAMETERS
from stock_desk.formula.evaluator import FormulaEvaluator
from stock_desk.formula.models import (
    Formula,
    FormulaDraft,
    FormulaType,
    FormulaVersion,
)
from stock_desk.formula.repository import (
    FormulaRepository,
    FormulaValidationError,
    normalize_parameter_schema,
)
from stock_desk.formula.signal_series import (
    COMPATIBILITY_VERSION,
    ENGINE_VERSION,
    MAX_SIGNAL_SERIES_BYTES,
    FormulaReference,
    SignalSeries,
)
from stock_desk.formula.validator import FormulaValidator
from stock_desk.formula.values import IntegerScalar, NumberScalar, ScalarValue
from stock_desk.market.lake import MarketLake
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.market.types import BarQuery
from stock_desk.storage.database import DatabaseIdentity, connection_database_identity


MAX_FORMULA_WORKER_REQUEST_BYTES = 64 * 1024 * 1024
_CACHE_CAPACITY = 64
_DEFAULT_CACHE_BYTES = 64 * 1024 * 1024
MACD_TEMPLATE_SOURCE = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)


class FormulaServiceError(Exception):
    """Stable formula service failure without implementation details."""


class FormulaPreviewNotFound(FormulaServiceError):
    pass


class FormulaPreviewValidationError(FormulaServiceError, ValueError):
    pass


class FormulaPreviewResourceError(FormulaServiceError):
    pass


class FormulaPreviewTimeout(FormulaServiceError):
    pass


class FormulaPreviewWorkerError(FormulaServiceError):
    pass


class FormulaPreviewUnsupportedVersion(FormulaServiceError):
    pass


class FormulaServiceDatabaseMismatch(FormulaServiceError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _worker_parameters(items: object) -> dict[str, ScalarValue]:
    if not isinstance(items, list) or len(items) > MAX_PARAMETERS:
        raise ValueError
    result: dict[str, ScalarValue] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"kind", "name", "value"}:
            raise ValueError
        name, kind, value = item["name"], item["kind"], item["value"]
        if type(name) is not str or name in result:
            raise ValueError
        if kind == "integer" and type(value) is int:
            result[name] = IntegerScalar(value)
        elif kind == "number" and type(value) is float and math.isfinite(value):
            result[name] = NumberScalar(value)
        else:
            raise ValueError
    return dict(sorted(result.items()))


def _formula_worker(connection: Connection, request_bytes: bytes) -> None:
    try:
        if not 0 < len(request_bytes) <= MAX_FORMULA_WORKER_REQUEST_BYTES:
            raise ValueError
        request = json.loads(request_bytes)
        if not isinstance(request, dict) or set(request) != {
            "formula",
            "parameters",
            "routed",
            "source",
        }:
            raise ValueError
        routed = RoutedBarSuccess.model_validate_json(
            _canonical_bytes(request["routed"]), strict=False
        )
        parameters = _worker_parameters(request["parameters"])
        context = EvaluationContext.from_routed(routed, parameters=parameters)
        formula = FormulaReference.model_validate(request["formula"])
        result = FormulaEvaluator().evaluate(request["source"], context, formula)
        payload = result.canonical_json_bytes()
        if len(payload) > MAX_SIGNAL_SERIES_BYTES:
            raise ValueError
        connection.send_bytes(b"\x00" + payload)
    except BaseException:
        try:
            connection.send_bytes(b"\x01worker_failed")
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


WorkerTarget = Callable[[Connection, bytes], None]


class IsolatedFormulaExecutor:
    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        worker_target: WorkerTarget = _formula_worker,
        start_method: str = "spawn",
        max_request_bytes: int = MAX_FORMULA_WORKER_REQUEST_BYTES,
        max_response_bytes: int = MAX_SIGNAL_SERIES_BYTES,
        max_workers: int = 4,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("formula worker timeout is invalid")
        if not 0 < max_request_bytes <= MAX_FORMULA_WORKER_REQUEST_BYTES:
            raise ValueError("formula worker request bound is invalid")
        if not 0 < max_response_bytes <= MAX_SIGNAL_SERIES_BYTES:
            raise ValueError("formula worker response bound is invalid")
        if type(max_workers) is not int or not 0 < max_workers <= 64:
            raise ValueError("formula worker concurrency bound is invalid")
        self.timeout_seconds = timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._worker_target = worker_target
        self._context = cast(Any, multiprocessing.get_context(start_method))
        self._worker_slots = BoundedSemaphore(max_workers)

    def execute(self, request_bytes: bytes) -> bytes:
        started = time.monotonic()
        if type(request_bytes) is not bytes or not (
            0 < len(request_bytes) <= self.max_request_bytes
        ):
            raise FormulaPreviewResourceError("formula request exceeds resource limits")
        deadline = started + self.timeout_seconds
        if not self._worker_slots.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            raise FormulaPreviewTimeout("formula preview timed out")
        try:
            return self._execute_in_slot(request_bytes, deadline)
        finally:
            self._worker_slots.release()

    def _execute_in_slot(self, request_bytes: bytes, deadline: float) -> bytes:
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=self._worker_target,
            args=(sender, request_bytes),
            daemon=True,
        )
        received = Event()
        outcome: list[bytes | BaseException] = []

        def receive_message() -> None:
            try:
                outcome.append(receiver.recv_bytes(self.max_response_bytes + 1))
            except BaseException as error:
                outcome.append(error)
            finally:
                received.set()

        receiver_thread: Thread | None = None
        try:
            process.start()
            sender.close()
            receiver_thread = Thread(
                target=receive_message,
                name="formula-preview-receiver",
                daemon=False,
            )
            receiver_thread.start()
            if not received.wait(max(0.0, deadline - time.monotonic())):
                self._stop(process)
                receiver.close()
                receiver_thread.join()
                raise FormulaPreviewTimeout("formula preview timed out")
            result = outcome[0]
            if isinstance(result, EOFError):
                raise FormulaPreviewWorkerError(
                    "formula preview worker failed"
                ) from None
            if isinstance(result, OSError):
                raise FormulaPreviewResourceError(
                    "formula response exceeds resource limits"
                ) from None
            if isinstance(result, BaseException):
                raise FormulaPreviewWorkerError(
                    "formula preview worker failed"
                ) from None
            message = result
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                self._stop(process)
                raise FormulaPreviewTimeout("formula preview timed out")
            if not message or message[0] != 0:
                raise FormulaPreviewWorkerError("formula preview worker failed")
            payload = message[1:]
            if not payload or len(payload) > self.max_response_bytes:
                raise FormulaPreviewResourceError(
                    "formula response exceeds resource limits"
                )
            return payload
        finally:
            sender.close()
            if process.is_alive():
                self._stop(process)
            receiver.close()
            if receiver_thread is not None and receiver_thread.is_alive():
                receiver_thread.join()
            process.close()

    @staticmethod
    def _stop(process: Any) -> None:
        process.terminate()
        process.join(timeout=0.2)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.2)


@dataclass(frozen=True, slots=True)
class _PreviewCacheKey:
    formula_id: str
    formula_version_id: str
    formula_checksum: str
    formula_version: int
    engine_version: str
    compatibility_version: str
    parameters: tuple[tuple[str, str, str], ...]
    symbol: str
    period: str
    adjustment: str
    source: str
    query_start: str
    query_end: str
    dataset_version: str
    route_version: str
    manifest_record_id: str
    data_cutoff: str


@dataclass(slots=True)
class _PreviewFlight:
    completed: Event
    payload: bytes | None = None
    failure: BaseException | None = None


class FormulaExecutor(Protocol):
    def execute(self, request_bytes: bytes) -> bytes: ...


def _bind_parameter_schema(
    schema: Mapping[str, Any], overrides: Mapping[str, int | float]
) -> tuple[dict[str, ScalarValue], tuple[tuple[str, str, str], ...]]:
    if not isinstance(overrides, Mapping) or len(overrides) > MAX_PARAMETERS:
        raise FormulaPreviewValidationError("formula parameters are invalid")
    if set(overrides) - set(schema):
        raise FormulaPreviewValidationError("formula parameters are invalid")
    bound: dict[str, ScalarValue] = {}
    canonical: list[tuple[str, str, str]] = []
    for name in sorted(schema):
        declaration = schema[name]
        if not isinstance(declaration, Mapping):
            raise FormulaPreviewValidationError("formula parameters are invalid")
        kind = declaration.get("kind")
        value = overrides.get(name, declaration.get("default"))
        if kind == "integer" and type(value) is int:
            try:
                integer_scalar = IntegerScalar(value)
            except ValueError:
                raise FormulaPreviewValidationError(
                    "formula parameters are invalid"
                ) from None
            bound[name] = integer_scalar
            canonical.append((name, "integer", str(value)))
        elif kind == "number" and type(value) in {int, float}:
            try:
                number = float(cast(int | float, value))
                if not math.isfinite(number):
                    raise ValueError
                number_scalar = NumberScalar(number)
            except (OverflowError, ValueError):
                raise FormulaPreviewValidationError("formula parameters are invalid")
            bound[name] = number_scalar
            canonical.append((name, "number", repr(number_scalar.value)))
        else:
            raise FormulaPreviewValidationError("formula parameters are invalid")
    return bound, tuple(canonical)


def _bind_parameters(
    version: FormulaVersion, overrides: Mapping[str, int | float]
) -> tuple[dict[str, ScalarValue], tuple[tuple[str, str, str], ...]]:
    return _bind_parameter_schema(version.parameter_schema, overrides)


def _diagnostic_payload(item: Any) -> dict[str, object]:
    return {
        "blocks_backtest": item.blocks_backtest,
        "blocks_preview": item.blocks_preview,
        "blocks_save": item.blocks_save,
        "code": item.code,
        "explanation": item.explanation,
        "function": item.function,
        "span": {
            "column": item.span.column,
            "end_column": item.span.end_column,
            "end_line": item.span.end_line,
            "line": item.span.line,
        },
    }


class FormulaService:
    def __init__(
        self,
        *,
        repository: FormulaRepository,
        lake: MarketLake,
        executor: FormulaExecutor | None = None,
        max_cache_bytes: int = _DEFAULT_CACHE_BYTES,
    ) -> None:
        if not 0 < max_cache_bytes <= MAX_SIGNAL_SERIES_BYTES:
            raise ValueError("formula cache byte budget is invalid")
        self.repository = repository
        self.lake = lake
        with repository.engine.connect() as connection:
            database_identity = connection_database_identity(connection)
        if database_identity != lake.database_identity:
            raise ValueError("formula service database identities do not match")
        self._database_identity = database_identity
        self.executor = executor or IsolatedFormulaExecutor()
        self._max_cache_bytes = max_cache_bytes
        self._cache_size_bytes = 0
        self._cache: OrderedDict[_PreviewCacheKey, bytes] = OrderedDict()
        self._flights: dict[_PreviewCacheKey, _PreviewFlight] = {}
        self._cache_lock = Lock()

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def templates(self) -> tuple[dict[str, object], ...]:
        return (
            {
                "template_id": "macd-cross-v1",
                "name": "MACD 金叉/死叉",
                "formula_type": "trading",
                "placement": "subchart",
                "source": MACD_TEMPLATE_SOURCE,
                "parameter_schema": {},
            },
        )

    def validate(
        self,
        *,
        source: str,
        parameter_schema: Mapping[str, Any],
        formula_type: FormulaType,
    ) -> tuple[dict[str, object], ...]:
        try:
            _schema, parameters = normalize_parameter_schema(parameter_schema)
        except FormulaValidationError:
            return (
                {
                    "blocks_backtest": True,
                    "blocks_preview": True,
                    "blocks_save": True,
                    "code": "invalid_parameter_schema",
                    "explanation": "Formula parameter schema is invalid.",
                    "function": None,
                    "span": {"column": 1, "end_column": 1, "end_line": 1, "line": 1},
                },
            )
        diagnostics = FormulaValidator().validate(source, parameters=parameters)
        if diagnostics:
            return tuple(_diagnostic_payload(item) for item in diagnostics)
        compiled = compile_formula(source, parameters=parameters)
        if formula_type == "trading" and compiled.signal_outputs != ("BUY", "SELL"):
            return (
                {
                    "blocks_backtest": True,
                    "blocks_preview": True,
                    "blocks_save": True,
                    "code": "missing_trading_signals",
                    "explanation": "trading formula requires BUY and SELL outputs",
                    "function": None,
                    "span": {"column": 1, "end_column": 1, "end_line": 1, "line": 1},
                },
            )
        return ()

    def create(
        self,
        *,
        name: str,
        formula_type: str,
        placement: str,
        source: str,
        parameter_schema: Mapping[str, Any],
    ) -> FormulaVersion:
        return self.repository.create(
            name,
            formula_type,
            source,
            parameter_schema,
            placement=placement,
        )

    def list_formulas(self) -> tuple[Formula, ...]:
        return self.repository.list_formulas()

    def list_formula_page(
        self, *, limit: int, cursor: str | None
    ) -> tuple[tuple[Formula, ...], str | None]:
        return self.repository.list_formula_page(limit=limit, cursor=cursor)

    def get_formula(self, formula_id: str) -> tuple[Formula, FormulaDraft]:
        return (
            self.repository.get_formula(formula_id),
            self.repository.get_draft(formula_id),
        )

    def update_draft(
        self,
        formula_id: str,
        *,
        source: str,
        parameter_schema: Mapping[str, Any],
        expected_revision: int,
    ) -> FormulaDraft:
        return self.repository.update_draft(
            formula_id,
            source,
            parameter_schema,
            expected_revision=expected_revision,
        )

    def save(
        self,
        formula_id: str,
        *,
        source: str,
        parameter_schema: Mapping[str, Any],
        expected_revision: int,
    ) -> FormulaVersion:
        return self.repository.save(
            formula_id,
            source,
            parameter_schema,
            expected_revision=expected_revision,
        )

    def copy(
        self,
        formula_id: str,
        *,
        name: str,
        source_version_id: str | None,
    ) -> FormulaVersion:
        return self.repository.copy(
            formula_id, name, source_version_id=source_version_id
        )

    def list_versions(self, formula_id: str) -> tuple[FormulaVersion, ...]:
        return self.repository.list_versions(formula_id)

    def list_version_page(
        self, formula_id: str, *, limit: int, cursor: str | None
    ) -> tuple[tuple[FormulaVersion, ...], str | None]:
        return self.repository.list_version_page(formula_id, limit=limit, cursor=cursor)

    def preview(
        self,
        version_id: str,
        query: BarQuery,
        parameters: Mapping[str, int | float],
    ) -> SignalSeries:
        routed = self.lake.read_latest_exact(query)
        if routed is None:
            raise FormulaPreviewNotFound("formula preview data was not found")
        return self.preview_routed(version_id, routed, parameters)

    def preview_routed(
        self,
        version_id: str,
        routed: RoutedBarSuccess,
        parameters: Mapping[str, int | float],
    ) -> SignalSeries:
        version = self.repository.get_version(version_id)
        if (
            version.engine_version != ENGINE_VERSION
            or version.compatibility_version != COMPATIBILITY_VERSION
        ):
            raise FormulaPreviewUnsupportedVersion("formula version is unsupported")
        bound, canonical_parameters = _bind_parameters(version, parameters)
        compiled = compile_formula(version.source, parameters=bound)
        diagnostics = FormulaValidator().validate_execution_budget(
            compiled, row_count=len(routed.result.bars)
        )
        if diagnostics:
            raise FormulaPreviewResourceError("formula request exceeds resource limits")
        context = EvaluationContext.from_routed(routed, parameters=bound)
        key = _PreviewCacheKey(
            formula_id=version.formula_id,
            formula_version_id=version.id,
            formula_checksum=version.checksum,
            formula_version=version.version,
            engine_version=version.engine_version,
            compatibility_version=version.compatibility_version,
            parameters=canonical_parameters,
            symbol=context.symbol,
            period=context.period.value,
            adjustment=context.adjustment.value,
            source=context.source.value,
            query_start=context.query_start.isoformat(),
            query_end=context.query_end.isoformat(),
            dataset_version=context.dataset_version,
            route_version=context.route_version,
            manifest_record_id=context.manifest_record_id,
            data_cutoff=context.data_cutoff.isoformat(),
        )
        cached, flight, is_leader = self._cache_or_flight(key)
        if cached is not None:
            return SignalSeries.from_canonical_json_bytes(cached)
        try:
            assert flight is not None
            if not is_leader:
                flight.completed.wait()
                if flight.failure is not None:
                    if isinstance(flight.failure, FormulaServiceError):
                        raise type(flight.failure)(*flight.failure.args)
                    raise FormulaPreviewWorkerError(
                        "formula preview worker failed"
                    ) from None
                assert flight.payload is not None
                return SignalSeries.from_canonical_json_bytes(flight.payload)
            request = {
                "formula": {
                    "formula_id": version.formula_id,
                    "formula_version_id": version.id,
                    "version": version.version,
                    "checksum": version.checksum,
                },
                "parameters": [
                    {
                        "kind": kind,
                        "name": name,
                        "value": bound[name].value,
                    }
                    for name, kind, _value in canonical_parameters
                ],
                "routed": routed.model_dump(mode="json"),
                "source": version.source,
            }
            request_bytes = _canonical_bytes(request)
            if len(request_bytes) > MAX_FORMULA_WORKER_REQUEST_BYTES:
                raise FormulaPreviewResourceError(
                    "formula request exceeds resource limits"
                )
            payload = self.executor.execute(request_bytes)
            try:
                result = SignalSeries.from_canonical_json_bytes(payload)
            except (TypeError, ValueError, ValidationError):
                raise FormulaPreviewWorkerError(
                    "formula preview worker failed"
                ) from None
            result_parameters = tuple(
                (
                    item.name,
                    item.kind,
                    int(item.value) if item.kind == "integer" else float(item.value),
                )
                for item in result.parameters
            )
            expected_parameters = tuple(
                (name, kind, bound[name].value)
                for name, kind, _value in canonical_parameters
            )
            if (
                result.formula_id != version.formula_id
                or result.formula_version_id != version.id
                or result.formula_version != version.version
                or result.formula_checksum != version.checksum
                or result.engine_version != version.engine_version
                or result.compatibility_version != version.compatibility_version
                or result.symbol != context.symbol
                or result.source != context.source
                or result.period != context.period
                or result.adjustment != context.adjustment
                or result.dataset_version != context.dataset_version
                or result.route_version != context.route_version
                or result.manifest_record_id != context.manifest_record_id
                or result.data_cutoff != context.data_cutoff
                or result.query_start != context.query_start
                or result.query_end != context.query_end
                or result_parameters != expected_parameters
                or result.timestamps != context.timestamps
            ):
                raise FormulaPreviewWorkerError("formula preview worker failed")
            self._complete_flight(key, flight, payload=payload)
            return result
        except BaseException as error:
            if is_leader and flight is not None:
                self._complete_flight(key, flight, failure=error)
            raise

    def _cache_or_flight(
        self, key: _PreviewCacheKey
    ) -> tuple[bytes | None, _PreviewFlight | None, bool]:
        with self._cache_lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
                return value, None, False
            flight = self._flights.get(key)
            if flight is not None:
                return None, flight, False
            flight = _PreviewFlight(completed=Event())
            self._flights[key] = flight
            return None, flight, True

    def _complete_flight(
        self,
        key: _PreviewCacheKey,
        flight: _PreviewFlight,
        *,
        payload: bytes | None = None,
        failure: BaseException | None = None,
    ) -> None:
        with self._cache_lock:
            if payload is not None:
                self._put_cached_locked(key, payload)
            flight.payload = payload
            flight.failure = failure
            self._flights.pop(key, None)
            flight.completed.set()

    def _put_cached_locked(self, key: _PreviewCacheKey, value: bytes) -> None:
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_size_bytes -= len(previous)
        if len(value) > self._max_cache_bytes:
            return
        self._cache[key] = value
        self._cache_size_bytes += len(value)
        self._cache.move_to_end(key)
        while (
            len(self._cache) > _CACHE_CAPACITY
            or self._cache_size_bytes > self._max_cache_bytes
        ):
            _old_key, old_value = self._cache.popitem(last=False)
            self._cache_size_bytes -= len(old_value)

    @property
    def cache_size_bytes(self) -> int:
        with self._cache_lock:
            return self._cache_size_bytes
