from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from stock_desk.backtest.costs import divide_decimal, round_money
from stock_desk.backtest.metrics import (
    INDEPENDENT_SAMPLE_LABEL,
    RATIO_QUANTUM,
    freeze_unique_samples,
    summarize,
)
from stock_desk.backtest.trades import TradeSample


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class GroupSummary:
    key: str
    realized_denominator: int
    realized_count: int
    positive_count: int
    negative_count: int
    zero_count: int
    share_of_all: Decimal
    win_rate: Decimal
    mean_net_return: Decimal
    median_net_return: Decimal
    payoff_ratio: Decimal | None
    payoff_ratio_reason: str | None
    net_pnl_total: Decimal
    average_holding_days: Decimal
    _net_returns: tuple[Decimal, ...]
    _net_pnls: tuple[Decimal, ...]
    _holding_days: tuple[int, ...]

    def __post_init__(self) -> None:
        _canonicalize_decimal_tuple(self, "_net_returns")
        _canonicalize_decimal_tuple(self, "_net_pnls")
        _canonicalize_decimal_zeros(
            self,
            (
                "share_of_all",
                "win_rate",
                "mean_net_return",
                "median_net_return",
                "payoff_ratio",
                "net_pnl_total",
                "average_holding_days",
            ),
        )
        _validate_identity(self.key, field_name="group key")
        for field_name, count_value in (
            ("realized_denominator", self.realized_denominator),
            ("realized_count", self.realized_count),
            ("positive_count", self.positive_count),
            ("negative_count", self.negative_count),
            ("zero_count", self.zero_count),
        ):
            if type(count_value) is not int or count_value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if self.realized_count == 0:
            raise ValueError("persisted groups must contain realized samples")
        if self.realized_denominator == 0:
            raise ValueError("group denominator must be positive")
        if self.realized_count > self.realized_denominator:
            raise ValueError("group count cannot exceed the global denominator")
        if (
            self.positive_count + self.negative_count + self.zero_count
            != self.realized_count
        ):
            raise ValueError("group sign counts must reconcile")
        for field_name, decimal_value in (
            ("share_of_all", self.share_of_all),
            ("win_rate", self.win_rate),
            ("mean_net_return", self.mean_net_return),
            ("median_net_return", self.median_net_return),
            ("net_pnl_total", self.net_pnl_total),
            ("average_holding_days", self.average_holding_days),
        ):
            _validate_finite(decimal_value, field_name=field_name)
        if self.payoff_ratio is None:
            if self.payoff_ratio_reason is None:
                raise ValueError("undefined group payoff requires a reason")
        else:
            _validate_finite(self.payoff_ratio, field_name="payoff_ratio")
            if self.payoff_ratio_reason is not None:
                raise ValueError("defined group payoff cannot include a reason")
            if self.payoff_ratio < 0:
                raise ValueError("group payoff cannot be negative")
        if not Decimal("0") <= self.share_of_all <= Decimal("1"):
            raise ValueError("group share must be between 0 and 1")
        if not Decimal("0") <= self.win_rate <= Decimal("1"):
            raise ValueError("group win_rate must be between 0 and 1")
        if self.share_of_all != _ratio(self.realized_count, self.realized_denominator):
            raise ValueError("group share must equal count/global denominator")
        if self.win_rate != _ratio(self.positive_count, self.realized_count):
            raise ValueError("group win_rate must equal positive/count")
        if self.average_holding_days < 0:
            raise ValueError("group average holding days cannot be negative")
        _validate_group_derivations(self)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "realized_denominator": self.realized_denominator,
            "realized_count": self.realized_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "zero_count": self.zero_count,
            "share_of_all": _json_decimal(self.share_of_all),
            "win_rate": _json_decimal(self.win_rate),
            "mean_net_return": _json_decimal(self.mean_net_return),
            "median_net_return": _json_decimal(self.median_net_return),
            "payoff_ratio": _json_decimal(self.payoff_ratio),
            "payoff_ratio_reason": self.payoff_ratio_reason,
            "net_pnl_total": _json_decimal(self.net_pnl_total),
            "average_holding_days": _json_decimal(self.average_holding_days),
        }


@dataclass(frozen=True, slots=True)
class GroupedMetrics:
    dimension: str
    label: str
    realized_denominator: int
    groups: tuple[GroupSummary, ...]
    reason: str | None

    def __post_init__(self) -> None:
        if self.dimension not in {"symbol", "entry_month", "entry_year"}:
            raise ValueError("unsupported grouping dimension")
        if self.label != INDEPENDENT_SAMPLE_LABEL:
            raise ValueError("groups must retain the independent-sample label")
        if type(self.realized_denominator) is not int or self.realized_denominator < 0:
            raise ValueError("realized_denominator must be a nonnegative integer")
        if type(self.groups) is not tuple:
            raise TypeError("groups must be an immutable tuple")
        if tuple(group.key for group in self.groups) != tuple(
            sorted(group.key for group in self.groups)
        ):
            raise ValueError("group keys must use deterministic sorted order")
        if len({group.key for group in self.groups}) != len(self.groups):
            raise ValueError("group keys must be unique")
        if (
            sum(group.realized_count for group in self.groups)
            != self.realized_denominator
        ):
            raise ValueError("group counts must reconcile to the global denominator")
        if any(
            group.realized_denominator != self.realized_denominator
            for group in self.groups
        ):
            raise ValueError("every group must disclose the same global denominator")
        if self.realized_denominator:
            share_error = abs(
                sum((group.share_of_all for group in self.groups), Decimal("0"))
                - Decimal("1")
            )
            tolerance = Decimal(len(self.groups)) * RATIO_QUANTUM / Decimal("2")
            if share_error > tolerance:
                raise ValueError("group shares exceed the quantization error bound")
        if self.realized_denominator == 0:
            if self.groups or self.reason != "no_realized_samples":
                raise ValueError("empty groups require no_realized_samples")
        elif self.reason is not None:
            raise ValueError("nonempty groups cannot include an empty reason")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "label": self.label,
            "realized_denominator": self.realized_denominator,
            "groups": [group.to_json_dict() for group in self.groups],
            "reason": self.reason,
            "equity_curve": None,
        }


def group_by_symbol(samples: Iterable[TradeSample]) -> GroupedMetrics:
    return _group(samples, dimension="symbol", key=lambda sample: sample.symbol)


def group_by_entry_month(samples: Iterable[TradeSample]) -> GroupedMetrics:
    return _group(
        samples,
        dimension="entry_month",
        key=lambda sample: _shanghai_entry(sample.entry_fill_at).strftime("%Y-%m"),
    )


def group_by_entry_year(samples: Iterable[TradeSample]) -> GroupedMetrics:
    return _group(
        samples,
        dimension="entry_year",
        key=lambda sample: _shanghai_entry(sample.entry_fill_at).strftime("%Y"),
    )


def _group(
    samples: Iterable[TradeSample],
    *,
    dimension: str,
    key: Callable[[TradeSample], str],
) -> GroupedMetrics:
    frozen = freeze_unique_samples(samples)
    realized = tuple(sample for sample in frozen if sample.realized)
    denominator = len(realized)
    buckets: dict[str, list[TradeSample]] = {}
    for sample in realized:
        buckets.setdefault(key(sample), []).append(sample)
    groups = tuple(
        _summarize_group(group_key, tuple(buckets[group_key]), denominator)
        for group_key in sorted(buckets)
    )
    return GroupedMetrics(
        dimension=dimension,
        label=INDEPENDENT_SAMPLE_LABEL,
        realized_denominator=denominator,
        groups=groups,
        reason=None if denominator else "no_realized_samples",
    )


def _summarize_group(
    key: str, samples: tuple[TradeSample, ...], denominator: int
) -> GroupSummary:
    metrics = summarize(samples)
    assert metrics.win_rate is not None
    assert metrics.mean_net_return is not None
    assert metrics.median_net_return is not None
    assert metrics.average_holding_days is not None
    return GroupSummary(
        key=key,
        realized_denominator=denominator,
        realized_count=metrics.realized_count,
        positive_count=metrics.positive_count,
        negative_count=metrics.negative_count,
        zero_count=metrics.zero_count,
        share_of_all=_ratio(metrics.realized_count, denominator),
        win_rate=metrics.win_rate,
        mean_net_return=metrics.mean_net_return,
        median_net_return=metrics.median_net_return,
        payoff_ratio=metrics.payoff_ratio,
        payoff_ratio_reason=metrics.payoff_ratio_reason,
        net_pnl_total=metrics.realized_net_pnl_total,
        average_holding_days=metrics.average_holding_days,
        _net_returns=tuple(_required_net_return(sample) for sample in samples),
        _net_pnls=tuple(_required_net_pnl(sample) for sample in samples),
        _holding_days=tuple(sample.holding_days for sample in samples),
    )


def _shanghai_entry(value: datetime) -> datetime:
    return value.astimezone(SHANGHAI)


def _ratio(numerator: int, denominator: int) -> Decimal:
    value = divide_decimal(Decimal(numerator), Decimal(denominator)).quantize(
        RATIO_QUANTUM, rounding=ROUND_HALF_UP
    )
    return value.copy_abs() if value.is_zero() else value


def _validate_finite(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def _validate_identity(value: object, *, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")


def _validate_group_derivations(group: GroupSummary) -> None:
    returns = group._net_returns
    pnls = group._net_pnls
    holding_days = group._holding_days
    if any(type(values) is not tuple for values in (returns, pnls, holding_days)):
        raise TypeError("group audit values must be immutable tuples")
    if not len(returns) == len(pnls) == len(holding_days) == group.realized_count:
        raise ValueError("group audit values must match realized_count")
    for value in (*returns, *pnls):
        _validate_finite(value, field_name="group audit Decimal")
    if any(type(value) is not int or value < 0 for value in holding_days):
        raise ValueError("group holding audit must contain nonnegative integers")
    positive = tuple(value for value in returns if value > 0)
    negative = tuple(value for value in returns if value < 0)
    zero_count = sum(1 for value in returns if value == 0)
    if (group.positive_count, group.negative_count, group.zero_count) != (
        len(positive),
        len(negative),
        zero_count,
    ):
        raise ValueError("group sign counts must match return audit values")
    expected_mean = _mean_values(returns)
    expected_median = _median_values(returns)
    if group.mean_net_return != expected_mean:
        raise ValueError("mean_net_return does not match group audit values")
    if group.median_net_return != expected_median:
        raise ValueError("median_net_return does not match group audit values")
    expected_payoff, expected_reason = _payoff_values(positive, negative)
    if (group.payoff_ratio, group.payoff_ratio_reason) != (
        expected_payoff,
        expected_reason,
    ):
        raise ValueError("payoff_ratio does not match group audit values")
    if group.net_pnl_total != round_money(sum(pnls, Decimal("0"))):
        raise ValueError("net_pnl_total does not match group audit values")
    if group.average_holding_days != _mean_values(
        tuple(Decimal(value) for value in holding_days)
    ):
        raise ValueError("average_holding_days does not match group audit values")


def _mean_values(values: tuple[Decimal, ...]) -> Decimal:
    return _round_ratio(divide_decimal(sum(values, Decimal("0")), Decimal(len(values))))


def _median_values(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return _round_ratio(ordered[midpoint])
    return _round_ratio(
        divide_decimal(ordered[midpoint - 1] + ordered[midpoint], Decimal("2"))
    )


def _payoff_values(
    positive: tuple[Decimal, ...], negative: tuple[Decimal, ...]
) -> tuple[Decimal | None, str | None]:
    if not positive and not negative:
        return None, "no_positive_or_negative_returns"
    if not positive:
        return None, "no_positive_returns"
    if not negative:
        return None, "no_negative_returns"
    positive_mean = divide_decimal(sum(positive, Decimal("0")), Decimal(len(positive)))
    negative_mean = divide_decimal(sum(negative, Decimal("0")), Decimal(len(negative)))
    return _round_ratio(divide_decimal(positive_mean, abs(negative_mean))), None


def _round_ratio(value: Decimal) -> Decimal:
    rounded = value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP)
    return rounded.copy_abs() if rounded.is_zero() else rounded


def _required_net_return(sample: TradeSample) -> Decimal:
    if sample.net_return is None:
        raise ValueError("group realized sample requires net_return")
    return sample.net_return


def _required_net_pnl(sample: TradeSample) -> Decimal:
    if sample.net_pnl is None:
        raise ValueError("group realized sample requires net_pnl")
    return sample.net_pnl


def _canonicalize_decimal_zeros(instance: object, field_names: tuple[str, ...]) -> None:
    for field_name in field_names:
        value = getattr(instance, field_name)
        if isinstance(value, Decimal) and value.is_zero():
            object.__setattr__(instance, field_name, value.copy_abs())


def _canonicalize_decimal_tuple(instance: object, field_name: str) -> None:
    value = getattr(instance, field_name)
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an immutable tuple")
    canonical = tuple(
        item.copy_abs() if isinstance(item, Decimal) and item.is_zero() else item
        for item in value
    )
    object.__setattr__(instance, field_name, canonical)


def _json_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized.copy_abs() if normalized.is_zero() else normalized, "f")
