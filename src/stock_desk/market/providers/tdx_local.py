from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import errno
import os
from pathlib import Path
import re
import stat
import sys
from typing import NoReturn, TypeAlias

from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
    ProviderCorrupt,
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderNoData,
    ProviderOperation,
    ProviderPermissionDenied,
    ProviderTransientFailure,
    ProviderUnsupported,
    ProviderUnavailable,
)
from stock_desk.market.providers.normalization import (
    MARKET_TIMEZONE,
    aware_now,
    bar_failure,
    batch_failure,
    dataset_version,
)
from stock_desk.market.providers.tdx_binary import (
    DAY_RECORD_STRUCT,
    MAX_DAY_BYTES,
    parse_day_bytes,
)
from stock_desk.market.providers.tdx_windows import (
    WindowsBackend,
    create_windows_backend,
)
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.providers.execution_status import (
    ExecutionStatusFailure,
    ExecutionStatusFetchOutcome,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFetchOutcome,
    BarQuery,
    BarResult,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)


MAX_DIRECTORY_ENTRIES = 10_000
_REPARSE_POINT = 0x400
_MARKET_DIRECTORY = {Exchange.SH: "sh", Exchange.SZ: "sz"}
_TDX_FILE_PATTERNS = {
    exchange: re.compile(rf"^{directory}[0-9]{{6}}\.day$")
    for exchange, directory in _MARKET_DIRECTORY.items()
}
_INSPECTION_DETAILS = {
    FailureReason.PERMISSION_DENIED: "TDX vipdoc access was denied",
    FailureReason.MISSING: "TDX vipdoc layout is missing",
    FailureReason.CORRUPT: "TDX vipdoc contents are corrupt",
    FailureReason.INVALID_RESPONSE: "TDX vipdoc layout is invalid",
    FailureReason.TRANSIENT_FAILURE: "TDX vipdoc changed during inspection",
    FailureReason.PROVIDER_UNAVAILABLE: "TDX vipdoc inspection is unavailable",
}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LEAF_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOINHERIT", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_USE_POSIX_DESCRIPTOR_IO = (
    os.name == "posix"
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and bool(getattr(os, "O_DIRECTORY", 0))
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
)
_PLATFORM = os.name
_WINDOWS_BACKEND_FACTORY: Callable[[], WindowsBackend] = create_windows_backend


@dataclass(frozen=True, slots=True)
class TdxMarketFileCount:
    exchange: Exchange
    count: int


@dataclass(frozen=True, slots=True)
class TdxInspectionSuccess:
    markets: frozenset[Exchange]
    file_counts: tuple[TdxMarketFileCount, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class TdxInspectionFailure:
    reason: FailureReason
    detail: str


TdxInspectionOutcome: TypeAlias = TdxInspectionSuccess | TdxInspectionFailure


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _validate_day_file_metadata(metadata: os.stat_result) -> None:
    if _is_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ProviderInvalidResponse()
    if (
        metadata.st_size <= 0
        or metadata.st_size > MAX_DAY_BYTES
        or metadata.st_size % DAY_RECORD_STRUCT.size != 0
    ):
        raise ProviderCorrupt()


def _raise_open_error(error: OSError) -> NoReturn:
    if isinstance(error, FileNotFoundError):
        raise ProviderMissingCoverage() from None
    if isinstance(error, PermissionError):
        raise ProviderPermissionDenied() from None
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise ProviderInvalidResponse() from None
    raise ProviderUnavailable() from None


def _open_directory_fd(path: str | Path, *, parent_fd: int | None = None) -> int:
    try:
        if parent_fd is None:
            descriptor = os.open(os.fspath(path), _DIRECTORY_FLAGS)
        else:
            descriptor = os.open(os.fspath(path), _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        _raise_open_error(error)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(metadata):
            raise ProviderInvalidResponse()
    except Exception:
        try:
            os.close(descriptor)
        except Exception:
            pass
        raise
    return descriptor


def _open_leaf_fd(name: str, *, parent_fd: int) -> int:
    try:
        descriptor = os.open(name, _LEAF_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        _raise_open_error(error)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
            raise ProviderInvalidResponse()
    except Exception:
        try:
            os.close(descriptor)
        except Exception:
            pass
        raise
    return descriptor


def _close_descriptors(descriptors: tuple[int, ...]) -> None:
    primary_error = sys.exception()
    cleanup_error: Exception | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except Exception as error:
            if cleanup_error is None:
                cleanup_error = error
    if cleanup_error is not None and primary_error is None:
        raise cleanup_error


def _open_directory_chain(root: Path, exchange: Exchange) -> tuple[int, ...]:
    descriptors: list[int] = []
    try:
        if root.anchor != os.sep or not root.is_absolute():
            raise ProviderInvalidResponse()
        components = root.parts[1:]
        if any(not component or component in {".", ".."} for component in components):
            raise ProviderInvalidResponse()
        descriptors.append(_open_directory_fd(os.sep))
        for component in components:
            child = _open_directory_fd(component, parent_fd=descriptors[-1])
            try:
                os.close(descriptors[-1])
            except Exception:
                opened = tuple(descriptors) + (child,)
                descriptors.clear()
                _close_descriptors(opened)
                raise
            descriptors[:] = [child]
        descriptors.append(
            _open_directory_fd(
                _MARKET_DIRECTORY[exchange],
                parent_fd=descriptors[-1],
            )
        )
        descriptors.append(_open_directory_fd("lday", parent_fd=descriptors[-1]))
    except Exception:
        _close_descriptors(tuple(descriptors))
        raise
    return tuple(descriptors)


def _stat_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _chain_signatures(
    descriptors: tuple[int, ...],
) -> tuple[tuple[int, int, int, int, int], ...]:
    return tuple(_stat_signature(os.fstat(descriptor)) for descriptor in descriptors)


def _verify_directory_chain(
    root: Path,
    exchange: Exchange,
    expected: tuple[tuple[int, int, int, int, int], ...],
) -> None:
    try:
        current = _open_directory_chain(root, exchange)
    except (OSError, ProviderClientError):
        raise ProviderTransientFailure() from None
    try:
        if _chain_signatures(current) != expected:
            raise ProviderTransientFailure()
    finally:
        _close_descriptors(current)


def _count_market_files_fd(descriptor: int, exchange: Exchange) -> int:
    pattern = _TDX_FILE_PATTERNS[exchange]
    count = 0
    entries_seen = 0
    with os.scandir(descriptor) as entries:
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_DIRECTORY_ENTRIES:
                raise ProviderCorrupt()
            if not entry.name.endswith(".day"):
                continue
            if pattern.fullmatch(entry.name) is None:
                raise ProviderCorrupt()
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                raise ProviderInvalidResponse()
            _validate_day_file_metadata(metadata)
            count += 1
    return count


def _inspect_market_posix(root: Path, exchange: Exchange) -> int:
    descriptors = _open_directory_chain(root, exchange)
    before = _chain_signatures(descriptors)
    try:
        count = _count_market_files_fd(descriptors[-1], exchange)
        after = _chain_signatures(descriptors)
        if after != before:
            raise ProviderTransientFailure()
        _verify_directory_chain(root, exchange, before)
        return count
    finally:
        _close_descriptors(descriptors)


def _inspection_failure(error: Exception) -> TdxInspectionFailure:
    if isinstance(error, ProviderPermissionDenied):
        reason = FailureReason.PERMISSION_DENIED
    elif isinstance(error, ProviderMissingCoverage):
        reason = FailureReason.MISSING
    elif isinstance(error, ProviderCorrupt):
        reason = FailureReason.CORRUPT
    elif isinstance(error, ProviderInvalidResponse):
        reason = FailureReason.INVALID_RESPONSE
    elif isinstance(error, ProviderTransientFailure):
        reason = FailureReason.TRANSIENT_FAILURE
    else:
        reason = FailureReason.PROVIDER_UNAVAILABLE
    return TdxInspectionFailure(
        reason=reason,
        detail=_INSPECTION_DETAILS[reason],
    )


def _local_midnight(value: date) -> datetime:
    return datetime.combine(value, time(), tzinfo=MARKET_TIMEZONE).astimezone(
        timezone.utc
    )


def _local_cutoff(value: date) -> datetime:
    return datetime.combine(value, time(15), tzinfo=MARKET_TIMEZONE).astimezone(
        timezone.utc
    )


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise ProviderTransientFailure()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_leaf_path(
    root: Path,
    exchange: Exchange,
    name: str,
    expected_chain: tuple[tuple[int, int, int, int, int], ...],
    expected_leaf: tuple[int, int, int, int, int],
) -> None:
    descriptors: tuple[int, ...] | None = None
    leaf: int | None = None
    try:
        descriptors = _open_directory_chain(root, exchange)
        leaf = _open_leaf_fd(name, parent_fd=descriptors[-1])
    except (OSError, ProviderClientError):
        opened: tuple[int, ...] = descriptors or ()
        if leaf is not None:
            opened += (leaf,)
        _close_descriptors(opened)
        raise ProviderTransientFailure() from None
    try:
        if (
            _chain_signatures(descriptors) != expected_chain
            or _stat_signature(os.fstat(leaf)) != expected_leaf
        ):
            raise ProviderTransientFailure()
    finally:
        _close_descriptors(descriptors + (leaf,))


def _read_posix_snapshot_once(root: Path, exchange: Exchange, name: str) -> bytes:
    descriptors = _open_directory_chain(root, exchange)
    leaf: int | None = None
    try:
        chain_before = _chain_signatures(descriptors)
        leaf = _open_leaf_fd(name, parent_fd=descriptors[-1])
        leaf_before = _stat_signature(os.fstat(leaf))
        if leaf_before[2] > MAX_DAY_BYTES:
            raise ProviderCorrupt()
        payload = _read_exact(leaf, leaf_before[2])
        if len(payload) != leaf_before[2]:
            raise ProviderTransientFailure()
        if (
            _stat_signature(os.fstat(leaf)) != leaf_before
            or _chain_signatures(descriptors) != chain_before
        ):
            raise ProviderTransientFailure()
        _verify_leaf_path(root, exchange, name, chain_before, leaf_before)
        return payload
    finally:
        opened: tuple[int, ...] = descriptors
        if leaf is not None:
            opened += (leaf,)
        _close_descriptors(opened)


def _read_stable_snapshot(root: Path, exchange: Exchange, name: str) -> bytes:
    saw_transient = False
    for attempt in range(2):
        try:
            if _USE_POSIX_DESCRIPTOR_IO:
                return _read_posix_snapshot_once(root, exchange, name)
            if _PLATFORM == "nt":
                return _WINDOWS_BACKEND_FACTORY().read_snapshot(root, exchange, name)
            raise ProviderUnavailable()
        except ProviderTransientFailure:
            saw_transient = True
            if attempt == 1:
                raise
        except (OSError, ProviderClientError):
            if saw_transient:
                raise ProviderTransientFailure() from None
            raise
    raise AssertionError("snapshot retry loop did not terminate")


class TdxLocalProvider:
    name = ProviderId.TDX_LOCAL

    def __init__(self, *, root: str | os.PathLike[str], clock: Clock) -> None:
        raw_root = os.fspath(root)
        self._root = Path(raw_root)
        self._has_forbidden_posix_component = os.name == "posix" and any(
            component in {".", ".."} for component in raw_root.split(os.sep)
        )
        self._clock = clock

    def _validate_root(self) -> None:
        if not self._root.is_absolute() or self._has_forbidden_posix_component:
            raise ProviderInvalidResponse()

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset({MarketCapability.BARS}),
            available_periods=frozenset({Period.DAY}),
            available_adjustments=frozenset({Adjustment.NONE}),
            markets=frozenset({Exchange.SH, Exchange.SZ}),
            data_cutoff=None,
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.EXECUTION_STATUS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="Local TDX files do not prove historical suspension and limits",
                ),
                CapabilityGap(
                    capability=MarketCapability.INSTRUMENTS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="Local TDX files do not provide instrument metadata",
                ),
                CapabilityGap(
                    capability=MarketCapability.TRADING_CALENDAR,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="Local TDX files do not prove calendar completeness",
                ),
            ),
        )

    def fetch_execution_status(
        self, query: ExecutionStatusQuery
    ) -> ExecutionStatusFetchOutcome:
        return ExecutionStatusFailure(
            query=query,
            source=self.name,
            reason=FailureReason.UNSUPPORTED,
            detail="provider does not support authoritative execution status",
        )

    def preflight(self) -> TdxInspectionOutcome:
        try:
            self._validate_root()
            windows_backend = (
                _WINDOWS_BACKEND_FACTORY()
                if not _USE_POSIX_DESCRIPTOR_IO and _PLATFORM == "nt"
                else None
            )
            counts: list[TdxMarketFileCount] = []
            for exchange in _MARKET_DIRECTORY:
                if _USE_POSIX_DESCRIPTOR_IO:
                    count = _inspect_market_posix(self._root, exchange)
                elif windows_backend is not None:
                    count = windows_backend.inspect_market(self._root, exchange)
                else:
                    raise ProviderUnavailable()
                counts.append(
                    TdxMarketFileCount(
                        exchange=exchange,
                        count=count,
                    )
                )
            markets = frozenset(item.exchange for item in counts if item.count > 0)
            if not markets:
                raise ProviderMissingCoverage()
            return TdxInspectionSuccess(
                markets=markets,
                file_counts=tuple(counts),
                detail="TDX vipdoc layout validated",
            )
        except FileNotFoundError as error:
            return _inspection_failure(ProviderMissingCoverage(error))
        except PermissionError as error:
            return _inspection_failure(ProviderPermissionDenied(error))
        except Exception as error:
            return _inspection_failure(error)

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        exchange = Exchange(query.symbol[-2:])
        if (
            query.period is not Period.DAY
            or query.adjustment is not Adjustment.NONE
            or exchange not in _MARKET_DIRECTORY
        ):
            return bar_failure(
                source=self.name,
                query=query,
                error=ProviderUnsupported(),
            )
        try:
            self._validate_root()
            directory = _MARKET_DIRECTORY[exchange]
            payload = _read_stable_snapshot(
                self._root,
                exchange,
                f"{directory}{query.symbol[:6]}.day",
            )
            if not payload:
                raise ProviderNoData()
            fetched_at = aware_now(self._clock)
            records = parse_day_bytes(
                payload,
                observed_on=fetched_at.astimezone(MARKET_TIMEZONE).date(),
            )
            coverage_start = _local_midnight(records[0].day)
            coverage_end = _local_midnight(records[-1].day + timedelta(days=1))
            if query.start < coverage_start or query.end > coverage_end:
                raise ProviderMissingCoverage()
            bars = tuple(
                Bar(
                    symbol=query.symbol,
                    timestamp=_local_midnight(record.day),
                    period=Period.DAY,
                    adjustment=Adjustment.NONE,
                    open=record.open,
                    high=record.high,
                    low=record.low,
                    close=record.close,
                    volume=record.volume,
                    status=TradingStatus.UNKNOWN,
                )
                for record in records
                if query.start <= _local_midnight(record.day) < query.end
            )
            if not bars:
                raise ProviderNoData()
            cutoff = _local_cutoff(
                bars[-1].timestamp.astimezone(MARKET_TIMEZONE).date()
            )
            if cutoff > fetched_at:
                raise ProviderMissingCoverage()
            version = dataset_version(
                source=self.name,
                operation="bars",
                request={"query": query},
                data_cutoff=cutoff,
                items=bars,
            )
            return BarResult(
                query=query,
                bars=bars,
                coverage_start=query.start,
                coverage_end=query.end,
                provenance=Provenance(
                    source=self.name,
                    fetched_at=fetched_at,
                    data_cutoff=cutoff,
                    adjustment=query.adjustment,
                    dataset_version=version,
                ),
            )
        except FileNotFoundError as error:
            failure: Exception = ProviderMissingCoverage(error)
        except PermissionError as error:
            failure = ProviderPermissionDenied(error)
        except OSError as error:
            failure = ProviderUnavailable(error)
        except Exception as error:
            failure = error
        return bar_failure(source=self.name, query=query, error=failure)

    def fetch_instruments(self) -> InstrumentFetchOutcome:
        return batch_failure(
            source=self.name,
            operation=ProviderOperation.INSTRUMENTS,
            error=ProviderUnsupported(),
        )

    def fetch_calendar(
        self,
        exchange: Exchange,
        start: date,
        end: date,
    ) -> CalendarFetchOutcome:
        if start >= end:
            raise ValueError("calendar range must be nonempty")
        return batch_failure(
            source=self.name,
            operation=ProviderOperation.CALENDAR,
            error=ProviderUnsupported(),
            exchange=exchange,
            start=start,
            end=end,
        )
