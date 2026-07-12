from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import secrets
from threading import RLock
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stock_desk.market.instruments import (
    InstrumentCorruption,
    InstrumentNotFound,
    InstrumentRepository,
    InstrumentRepositoryError,
)
from stock_desk.market.types import CanonicalSymbol, InstrumentKind, InstrumentName


_LOGGER = logging.getLogger(__name__)
_DEFAULT_INDEX = ("000001.SS", "上证指数", InstrumentKind.INDEX)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MarketNavigationInstrument(_FrozenModel):
    symbol: CanonicalSymbol
    name: InstrumentName
    instrument_kind: InstrumentKind


class MarketNavigationNotice(_FrozenModel):
    code: Literal["market_navigation_state_reset"] = "market_navigation_state_reset"
    reason: Literal["corrupt", "unsupported_schema"]


class MarketNavigationState(_FrozenModel):
    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(ge=0)] = 0
    watchlist: Annotated[tuple[MarketNavigationInstrument, ...], Field(max_length=100)]
    recent: Annotated[tuple[MarketNavigationInstrument, ...], Field(max_length=20)]

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> Self:
        for field_name, items in (
            ("watchlist", self.watchlist),
            ("recent", self.recent),
        ):
            symbols = tuple(item.symbol for item in items)
            if len(symbols) != len(set(symbols)):
                raise ValueError(f"{field_name} symbols must be unique")
        return self


class MarketNavigationSnapshot(MarketNavigationState):
    notice: MarketNavigationNotice | None = None


@dataclass(frozen=True, slots=True)
class MarketNavigationLoadResult:
    state: MarketNavigationState
    notice: MarketNavigationNotice | None


class MarketNavigationStorageError(RuntimeError):
    """Navigation state could not be read or atomically replaced."""


class MarketNavigationConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__()


class _InstrumentCatalog(Protocol):
    def current_catalog(self) -> object: ...


class MarketNavigationStateStore:
    """Small, versioned, crash-consistent state below the v1.1 data root."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("market navigation state path must be absolute")
        self._path = path
        self._lock = RLock()

    def load(self) -> MarketNavigationLoadResult:
        with self._lock:
            if not self._path.exists():
                return MarketNavigationLoadResult(self._empty(), None)
            try:
                raw = self._path.read_bytes()
            except OSError as error:
                raise MarketNavigationStorageError() from error
            reason: Literal["corrupt", "unsupported_schema"] = "corrupt"
            try:
                if len(raw) > 256 * 1024:
                    raise ValueError
                decoded = json.loads(raw)
                if (
                    not isinstance(decoded, dict)
                    or type(decoded.get("schema_version")) is not int
                    or decoded.get("schema_version") != 1
                ):
                    reason = "unsupported_schema"
                    raise ValueError
                state = MarketNavigationState.model_validate_json(raw, strict=True)
            except (UnicodeError, json.JSONDecodeError, ValueError, ValidationError):
                diagnostic_id = secrets.token_hex(8)
                _LOGGER.warning(
                    "market navigation state reset diagnostic_id=%s reason=%s",
                    diagnostic_id,
                    reason,
                )
                return MarketNavigationLoadResult(
                    self._empty(),
                    MarketNavigationNotice(reason=reason),
                )
            return MarketNavigationLoadResult(state, None)

    def save(self, state: MarketNavigationState) -> MarketNavigationState:
        validated = MarketNavigationState.model_validate(
            state.model_dump(mode="python"), strict=True
        )
        encoded = json.dumps(
            validated.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with self._lock:
            parent = self._path.parent
            temporary = parent / f".{self._path.name}.{os.getpid()}.tmp"
            descriptor: int | None = None
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    descriptor = None
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path)
                if os.name == "posix":
                    directory = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except OSError as error:
                raise MarketNavigationStorageError() from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return validated

    @staticmethod
    def _empty() -> MarketNavigationState:
        return MarketNavigationState(revision=0, watchlist=(), recent=())


class MarketNavigationService:
    def __init__(
        self,
        *,
        store: MarketNavigationStateStore,
        instruments: InstrumentRepository | _InstrumentCatalog,
    ) -> None:
        self._store = store
        self._instruments = instruments
        self._lock = RLock()

    @classmethod
    def open(
        cls,
        *,
        data_dir: Path,
        instruments: InstrumentRepository,
    ) -> MarketNavigationService:
        path = data_dir.resolve() / "market" / "navigation-v1.json"
        return cls(store=MarketNavigationStateStore(path), instruments=instruments)

    def state(self) -> MarketNavigationSnapshot:
        loaded = self._store.load()
        return self._snapshot(loaded.state, loaded.notice)

    def replace(
        self,
        *,
        expected_revision: int,
        watchlist: Sequence[MarketNavigationInstrument],
        recent: Sequence[MarketNavigationInstrument],
    ) -> MarketNavigationSnapshot:
        with self._lock:
            loaded = self._store.load()
            if loaded.state.revision != expected_revision:
                raise MarketNavigationConflict("market_navigation_revision_conflict")
            try:
                candidate = MarketNavigationState(
                    revision=expected_revision + 1,
                    watchlist=tuple(watchlist),
                    recent=tuple(recent),
                )
            except ValidationError as error:
                raise MarketNavigationConflict("invalid_request") from error
            self._validate_instruments((*candidate.watchlist, *candidate.recent))
            saved = self._store.save(candidate)
            return self._snapshot(saved, loaded.notice)

    def _validate_instruments(
        self, items: Sequence[MarketNavigationInstrument]
    ) -> None:
        if not items:
            return
        try:
            catalog = self._instruments.current_catalog()
            catalog_items = getattr(catalog, "instruments")
            by_symbol = {item.symbol: item for item in catalog_items}
        except InstrumentNotFound:
            by_symbol = None
        except (
            InstrumentCorruption,
            InstrumentRepositoryError,
            AttributeError,
        ) as error:
            raise MarketNavigationConflict(
                "market_navigation_catalog_unavailable"
            ) from error

        for item in items:
            if (
                item.symbol,
                item.name,
                item.instrument_kind,
            ) == _DEFAULT_INDEX:
                continue
            if by_symbol is None:
                raise MarketNavigationConflict("market_navigation_catalog_unavailable")
            actual = by_symbol.get(item.symbol)
            if actual is None or (
                actual.symbol,
                actual.name,
                actual.instrument_kind,
            ) != (item.symbol, item.name, item.instrument_kind):
                raise MarketNavigationConflict("invalid_market_navigation_instrument")

    @staticmethod
    def _snapshot(
        state: MarketNavigationState,
        notice: MarketNavigationNotice | None,
    ) -> MarketNavigationSnapshot:
        return MarketNavigationSnapshot(
            **state.model_dump(mode="python"),
            notice=notice,
        )
