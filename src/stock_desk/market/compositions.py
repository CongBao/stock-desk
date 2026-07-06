"""Normalized current major-index and industry preset compositions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
from typing import Protocol, Self

from stock_desk.market.pools import PoolCategory, PoolComposition
from stock_desk.market.providers.base import ProviderClientError
from stock_desk.market.providers.normalization import records_from_table
from stock_desk.market.providers.sdk import (
    call_sdk,
    import_optional_sdk,
    is_sdk_timeout,
    required_sdk_callable,
)
from stock_desk.market.types import FailureReason, ProviderId


class AkShareCompositionClient(Protocol):
    def index_constituents(self, symbol: str) -> object: ...

    def industry_constituents(self, symbol: str) -> object: ...

    def industry_names(self) -> object: ...


class CompositionProvider(Protocol):
    def fetch_presets(
        self, known_symbols: frozenset[str]
    ) -> PresetCompositionResult: ...


class AkShareCompositionSdkFacade:
    def __init__(self, module: object) -> None:
        self._module = module

    def index_constituents(self, symbol: str) -> object:
        return call_sdk(
            required_sdk_callable(self._module, "index_stock_cons_csindex"),
            symbol=symbol,
        )

    def industry_constituents(self, symbol: str) -> object:
        return call_sdk(
            required_sdk_callable(self._module, "stock_board_industry_cons_em"),
            symbol=symbol,
        )

    def industry_names(self) -> object:
        return call_sdk(
            required_sdk_callable(self._module, "stock_board_industry_name_em")
        )


@dataclass(frozen=True, slots=True)
class PresetCompositionFailure:
    preset_key: str
    category: PoolCategory
    reason: FailureReason


@dataclass(frozen=True, slots=True)
class PresetCompositionResult:
    compositions: tuple[PoolComposition, ...]
    failures: tuple[PresetCompositionFailure, ...]


@dataclass(frozen=True, slots=True)
class _PresetSpec:
    preset_key: str
    category: PoolCategory
    display_name: str
    provider_symbol: str
    code_fields: tuple[str, ...]
    expected_members: int | None


_PRESETS = (
    _PresetSpec(
        preset_key="index-csi300",
        category=PoolCategory.INDEX,
        display_name="沪深300",
        provider_symbol="000300",
        code_fields=("成分券代码", "品种代码", "代码"),
        expected_members=300,
    ),
    _PresetSpec(
        preset_key="index-sse50",
        category=PoolCategory.INDEX,
        display_name="上证50",
        provider_symbol="000016",
        code_fields=("成分券代码", "品种代码", "代码"),
        expected_members=50,
    ),
    _PresetSpec(
        preset_key="index-csi500",
        category=PoolCategory.INDEX,
        display_name="中证500",
        provider_symbol="000905",
        code_fields=("成分券代码", "品种代码", "代码"),
        expected_members=500,
    ),
)


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("composition clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _canonical_symbol(raw: object) -> str:
    if isinstance(raw, int) and not isinstance(raw, bool):
        code = f"{raw:06d}"
    elif isinstance(raw, str):
        code = raw.strip().split(".", 1)[0].zfill(6)
    else:
        raise ValueError("composition code is invalid")
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError("composition code is invalid")
    if code.startswith(("4", "8", "9")):
        exchange = "BJ"
    elif code.startswith(("5", "6", "7")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"{code}.{exchange}"


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _industry_key(name: str) -> str:
    if name == "银行":
        return "industry-bank"
    return f"industry-{_digest(name).removeprefix('sha256:')[:16]}"


def _source_cutoff(
    rows: tuple[dict[str, object], ...], fetched_at: datetime
) -> datetime:
    raw_dates = {row.get("日期") for row in rows if row.get("日期") not in {None, ""}}
    if not raw_dates:
        return fetched_at
    if len(raw_dates) != 1:
        raise ValueError("composition dates are inconsistent")
    raw = raw_dates.pop()
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, date):
        parsed = datetime.combine(raw, time(), tzinfo=timezone.utc)
    elif isinstance(raw, str):
        normalized = raw.strip()
        parsed = datetime.combine(
            datetime.strptime(
                normalized,
                "%Y%m%d" if normalized.isdigit() else "%Y-%m-%d",
            ).date(),
            time(),
            tzinfo=timezone.utc,
        )
    else:
        raise ValueError("composition date is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    cutoff = parsed.astimezone(timezone.utc)
    if cutoff > fetched_at:
        raise ValueError("composition date is in the future")
    return cutoff


def _reason(error: Exception) -> FailureReason:
    if is_sdk_timeout(error):
        return FailureReason.TIMEOUT
    if isinstance(error, ProviderClientError):
        return error.reason
    return FailureReason.INVALID_RESPONSE


class AkShareCompositionProvider:
    name = ProviderId.AKSHARE

    def __init__(
        self,
        *,
        client: AkShareCompositionClient,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(cls, *, clock: Callable[[], datetime]) -> Self:
        module = import_optional_sdk("akshare")
        return cls(client=AkShareCompositionSdkFacade(module), clock=clock)

    def fetch_presets(self, known_symbols: frozenset[str]) -> PresetCompositionResult:
        compositions: list[PoolComposition] = []
        failures: list[PresetCompositionFailure] = []
        specs = list(_PRESETS)
        try:
            names = records_from_table(
                self._client.industry_names(),
                required=frozenset({"板块名称"}),
            )
            industry_names: list[str] = []
            for row in names:
                raw_name = row["板块名称"]
                if (
                    not isinstance(raw_name, str)
                    or not raw_name.strip()
                    or raw_name != raw_name.strip()
                    or raw_name in industry_names
                ):
                    raise ValueError("industry name is invalid")
                industry_names.append(raw_name)
            if not industry_names:
                raise ValueError("industry catalog is empty")
            specs.extend(
                _PresetSpec(
                    preset_key=_industry_key(name),
                    category=PoolCategory.INDUSTRY,
                    display_name=f"{name}行业",
                    provider_symbol=name,
                    code_fields=("代码", "成分券代码"),
                    expected_members=None,
                )
                for name in industry_names
            )
        except Exception as error:
            failures.append(
                PresetCompositionFailure(
                    preset_key="industry-catalog",
                    category=PoolCategory.INDUSTRY,
                    reason=_reason(error),
                )
            )
        for spec in specs:
            try:
                table = (
                    self._client.index_constituents(spec.provider_symbol)
                    if spec.category is PoolCategory.INDEX
                    else self._client.industry_constituents(spec.provider_symbol)
                )
                rows = records_from_table(table, required=frozenset())
                symbols: list[str] = []
                for row in rows:
                    raw_code = next(
                        (row[field] for field in spec.code_fields if field in row),
                        None,
                    )
                    symbol = _canonical_symbol(raw_code)
                    if symbol not in known_symbols or symbol in symbols:
                        raise ValueError("composition is incomplete or duplicated")
                    symbols.append(symbol)
                ordered = tuple(sorted(symbols))
                if not ordered:
                    raise ValueError("composition has no catalog members")
                if (
                    spec.expected_members is not None
                    and len(ordered) != spec.expected_members
                ):
                    raise ValueError("index composition member count is incomplete")
                observed_at = _aware_now(self._clock)
                data_cutoff = _source_cutoff(rows, observed_at)
                dataset_version = _digest(
                    {
                        "source": self.name.value,
                        "preset_key": spec.preset_key,
                        "symbols": ordered,
                        "data_cutoff": data_cutoff.isoformat(),
                    }
                )
                route_version = _digest(
                    {
                        "source": self.name.value,
                        "preset_key": spec.preset_key,
                        "dataset_version": dataset_version,
                    }
                )
                compositions.append(
                    PoolComposition(
                        preset_key=spec.preset_key,
                        category=spec.category,
                        display_name=spec.display_name,
                        symbols=ordered,
                        source=self.name,
                        dataset_version=dataset_version,
                        route_version=route_version,
                        fetched_at=observed_at,
                        data_cutoff=data_cutoff,
                        complete=True,
                    )
                )
            except Exception as error:
                failures.append(
                    PresetCompositionFailure(
                        preset_key=spec.preset_key,
                        category=spec.category,
                        reason=_reason(error),
                    )
                )
        return PresetCompositionResult(tuple(compositions), tuple(failures))


__all__ = [
    "AkShareCompositionClient",
    "AkShareCompositionProvider",
    "CompositionProvider",
    "PresetCompositionFailure",
    "PresetCompositionResult",
]
