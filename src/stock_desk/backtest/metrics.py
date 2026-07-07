from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from stock_desk.backtest.costs import divide_decimal, round_money
from stock_desk.backtest.events import OrderPending
from stock_desk.backtest.trades import TradeSample


INDEPENDENT_SAMPLE_LABEL = "independent trade samples, not portfolio return"
RATIO_QUANTUM = Decimal("0.000001")
HISTOGRAM_CONTRACT = (
    ("lt_neg_20pct", None, Decimal("-0.20"), False, False),
    ("neg_20_to_10pct", Decimal("-0.20"), Decimal("-0.10"), True, False),
    ("neg_10_to_5pct", Decimal("-0.10"), Decimal("-0.05"), True, False),
    ("neg_5_to_0pct", Decimal("-0.05"), Decimal("0"), True, False),
    ("zero", Decimal("0"), Decimal("0"), True, True),
    ("pos_0_to_5pct", Decimal("0"), Decimal("0.05"), False, True),
    ("pos_5_to_10pct", Decimal("0.05"), Decimal("0.10"), False, True),
    ("pos_10_to_20pct", Decimal("0.10"), Decimal("0.20"), False, True),
    ("gt_20pct", Decimal("0.20"), None, False, False),
)


@dataclass(frozen=True, slots=True)
class TradeIdentity:
    symbol: str
    entry_signal_at: datetime
    entry_fill_at: datetime
    entry_eligible_at: datetime
    formula_version_id: str
    signal_series_id: str
    market_manifest_ids: tuple[str, ...]
    status_manifest_ids: tuple[str, ...]
    quantity: int
    entry_reference_open: Decimal


@dataclass(frozen=True, slots=True)
class HistogramBin:
    code: str
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool
    count: int
    share: Decimal | None
    share_reason: str | None

    def __post_init__(self) -> None:
        _canonicalize_decimal_zeros(self, ("lower_bound", "upper_bound", "share"))
        _validate_identity(self.code, field_name="code")
        if type(self.count) is not int or self.count < 0:
            raise ValueError("count must be a nonnegative integer")
        if (
            type(self.lower_inclusive) is not bool
            or type(self.upper_inclusive) is not bool
        ):
            raise TypeError("histogram inclusivity flags must be bool")
        _validate_optional_finite(self.lower_bound, field_name="lower_bound")
        _validate_optional_finite(self.upper_bound, field_name="upper_bound")
        _validate_optional_metric(
            self.share, self.share_reason, field_name="share", nonnegative=True
        )
        if self.lower_bound is not None and self.upper_bound is not None:
            if self.lower_bound > self.upper_bound:
                raise ValueError("histogram bounds must be ordered")
            if self.lower_bound == self.upper_bound and not (
                self.lower_inclusive and self.upper_inclusive
            ):
                raise ValueError("equal histogram bounds must both be inclusive")
        if self.share is not None and self.share > 1:
            raise ValueError("histogram share must be between 0 and 1")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "lower_bound": _json_decimal(self.lower_bound),
            "upper_bound": _json_decimal(self.upper_bound),
            "lower_inclusive": self.lower_inclusive,
            "upper_inclusive": self.upper_inclusive,
            "count": self.count,
            "share": _json_decimal(self.share),
            "share_reason": self.share_reason,
        }


@dataclass(frozen=True, slots=True)
class OpenTradeMetrics:
    count: int
    floating_pnl_total: Decimal
    mean_floating_return: Decimal | None
    mean_floating_return_reason: str | None

    def __post_init__(self) -> None:
        _canonicalize_decimal_zeros(
            self, ("floating_pnl_total", "mean_floating_return")
        )
        if type(self.count) is not int or self.count < 0:
            raise ValueError("open count must be a nonnegative integer")
        _validate_finite(self.floating_pnl_total, field_name="floating_pnl_total")
        _validate_optional_metric(
            self.mean_floating_return,
            self.mean_floating_return_reason,
            field_name="mean_floating_return",
        )
        if self.count == 0 and self.mean_floating_return is not None:
            raise ValueError("empty open samples cannot have a mean return")
        if self.count > 0 and self.mean_floating_return is None:
            raise ValueError("open samples require a mean return")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "floating_pnl_total": _json_decimal(self.floating_pnl_total),
            "mean_floating_return": _json_decimal(self.mean_floating_return),
            "mean_floating_return_reason": self.mean_floating_return_reason,
        }


@dataclass(frozen=True, slots=True)
class Reliability:
    level: str
    reason: str | None
    realized_count: int
    largest_symbol_share: Decimal | None

    def __post_init__(self) -> None:
        _canonicalize_decimal_zeros(self, ("largest_symbol_share",))
        if self.level not in {"low", "medium", "high"}:
            raise ValueError("reliability level must be low, medium, or high")
        if type(self.realized_count) is not int or self.realized_count < 0:
            raise ValueError("reliability realized_count must be nonnegative")
        if self.reason is not None:
            _validate_identity(self.reason, field_name="reliability reason")
        _validate_optional_finite(
            self.largest_symbol_share, field_name="largest_symbol_share"
        )
        if self.realized_count == 0 and self.largest_symbol_share is not None:
            raise ValueError("empty reliability cannot have symbol concentration")
        if self.realized_count > 0 and self.largest_symbol_share is None:
            raise ValueError("nonempty reliability requires symbol concentration")
        if self.largest_symbol_share is not None and not (
            Decimal("0") <= self.largest_symbol_share <= Decimal("1")
        ):
            raise ValueError("largest symbol share must be between 0 and 1")
        expected_level, expected_reason = _reliability_semantics(
            self.realized_count, self.largest_symbol_share
        )
        if (self.level, self.reason) != (expected_level, expected_reason):
            expected = expected_reason or "high_reliability"
            raise ValueError(f"reliability must use {expected} semantics")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reason": self.reason,
            "realized_count": self.realized_count,
            "largest_symbol_share": _json_decimal(self.largest_symbol_share),
        }


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    label: str
    realized_count: int
    win_rate_denominator: int
    positive_count: int
    negative_count: int
    zero_count: int
    win_rate: Decimal | None
    win_rate_reason: str | None
    mean_net_return: Decimal | None
    mean_net_return_reason: str | None
    median_net_return: Decimal | None
    median_net_return_reason: str | None
    payoff_ratio: Decimal | None
    payoff_ratio_reason: str | None
    max_win_return: Decimal | None
    max_win_return_reason: str | None
    max_loss_return: Decimal | None
    max_loss_return_reason: str | None
    realized_net_pnl_total: Decimal
    average_holding_bars: Decimal | None
    average_holding_bars_reason: str | None
    average_holding_days: Decimal | None
    average_holding_days_reason: str | None
    histogram: tuple[HistogramBin, ...]
    open_trades: OpenTradeMetrics
    reliability: Reliability
    _realized_net_returns: tuple[Decimal, ...]
    _realized_net_pnls: tuple[Decimal, ...]
    _realized_holding_bars: tuple[int, ...]
    _realized_holding_days: tuple[int, ...]
    _realized_symbols: tuple[str, ...]
    _open_floating_pnls: tuple[Decimal, ...]
    _open_floating_returns: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        _canonicalize_decimal_tuple(self, "_realized_net_returns")
        _canonicalize_decimal_tuple(self, "_realized_net_pnls")
        _canonicalize_decimal_tuple(self, "_open_floating_pnls")
        _canonicalize_decimal_tuple(self, "_open_floating_returns")
        _canonicalize_decimal_zeros(
            self,
            (
                "win_rate",
                "mean_net_return",
                "median_net_return",
                "payoff_ratio",
                "max_win_return",
                "max_loss_return",
                "realized_net_pnl_total",
                "average_holding_bars",
                "average_holding_days",
            ),
        )
        if self.label != INDEPENDENT_SAMPLE_LABEL:
            raise ValueError("metrics must retain the independent-sample label")
        count_fields = (
            self.realized_count,
            self.win_rate_denominator,
            self.positive_count,
            self.negative_count,
            self.zero_count,
        )
        if any(type(value) is not int or value < 0 for value in count_fields):
            raise ValueError("metric counts must be nonnegative integers")
        if self.win_rate_denominator != self.realized_count:
            raise ValueError("win-rate denominator must equal realized_count")
        if (
            self.positive_count + self.negative_count + self.zero_count
            != self.realized_count
        ):
            raise ValueError("realized sign counts must reconcile")
        for field_name, value, reason in (
            ("win_rate", self.win_rate, self.win_rate_reason),
            ("mean_net_return", self.mean_net_return, self.mean_net_return_reason),
            (
                "median_net_return",
                self.median_net_return,
                self.median_net_return_reason,
            ),
            ("payoff_ratio", self.payoff_ratio, self.payoff_ratio_reason),
            ("max_win_return", self.max_win_return, self.max_win_return_reason),
            ("max_loss_return", self.max_loss_return, self.max_loss_return_reason),
            (
                "average_holding_bars",
                self.average_holding_bars,
                self.average_holding_bars_reason,
            ),
            (
                "average_holding_days",
                self.average_holding_days,
                self.average_holding_days_reason,
            ),
        ):
            _validate_optional_metric(value, reason, field_name=field_name)
        _validate_finite(
            self.realized_net_pnl_total, field_name="realized_net_pnl_total"
        )
        if type(self.histogram) is not tuple:
            raise TypeError("histogram must be an immutable tuple")
        if sum(bin_.count for bin_ in self.histogram) != self.realized_count:
            raise ValueError("histogram counts must reconcile to realized_count")
        if self.reliability.realized_count != self.realized_count:
            raise ValueError(
                "reliability realized_count must equal global realized_count"
            )
        actual_contract = tuple(
            (
                bin_.code,
                bin_.lower_bound,
                bin_.upper_bound,
                bin_.lower_inclusive,
                bin_.upper_inclusive,
            )
            for bin_ in self.histogram
        )
        if actual_contract != HISTOGRAM_CONTRACT:
            raise ValueError("histogram must use the exact fixed 9-bin contract")
        for bin_ in self.histogram:
            if self.realized_count == 0:
                if bin_.share is not None or bin_.share_reason != "no_realized_samples":
                    raise ValueError(
                        "empty histogram shares require an explicit reason"
                    )
            elif (
                bin_.share != _ratio(bin_.count, self.realized_count)
                or bin_.share_reason is not None
            ):
                raise ValueError("histogram share must equal count/global denominator")
        if self.realized_count:
            share_error = abs(
                sum((bin_.share or Decimal("0")) for bin_ in self.histogram)
                - Decimal("1")
            )
            tolerance = Decimal(len(self.histogram)) * RATIO_QUANTUM / Decimal("2")
            if share_error > tolerance:
                raise ValueError("histogram shares exceed the quantization error bound")
        _validate_global_derivations(self)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "realized_count": self.realized_count,
            "win_rate_denominator": self.win_rate_denominator,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "zero_count": self.zero_count,
            "win_rate": _json_decimal(self.win_rate),
            "win_rate_reason": self.win_rate_reason,
            "mean_net_return": _json_decimal(self.mean_net_return),
            "mean_net_return_reason": self.mean_net_return_reason,
            "median_net_return": _json_decimal(self.median_net_return),
            "median_net_return_reason": self.median_net_return_reason,
            "payoff_ratio": _json_decimal(self.payoff_ratio),
            "payoff_ratio_reason": self.payoff_ratio_reason,
            "max_win_return": _json_decimal(self.max_win_return),
            "max_win_return_reason": self.max_win_return_reason,
            "max_loss_return": _json_decimal(self.max_loss_return),
            "max_loss_return_reason": self.max_loss_return_reason,
            "realized_net_pnl_total": _json_decimal(self.realized_net_pnl_total),
            "average_holding_bars": _json_decimal(self.average_holding_bars),
            "average_holding_bars_reason": self.average_holding_bars_reason,
            "average_holding_days": _json_decimal(self.average_holding_days),
            "average_holding_days_reason": self.average_holding_days_reason,
            "histogram": [bin_.to_json_dict() for bin_ in self.histogram],
            "open_trades": self.open_trades.to_json_dict(),
            "reliability": self.reliability.to_json_dict(),
            "equity_curve": None,
        }


def summarize(samples: Iterable[TradeSample]) -> BacktestMetrics:
    frozen_samples = freeze_unique_samples(samples)
    realized = tuple(sample for sample in frozen_samples if sample.realized)
    opened = tuple(sample for sample in frozen_samples if not sample.realized)
    returns = tuple(_required_net_return(sample) for sample in realized)
    positive = tuple(value for value in returns if value > 0)
    negative = tuple(value for value in returns if value < 0)
    zero_count = sum(1 for value in returns if value == 0)
    count = len(realized)
    no_realized_reason = "no_realized_samples" if count == 0 else None

    win_rate = _ratio(len(positive), count) if count else None
    mean_return = _mean(returns) if returns else None
    median_return = _median(returns) if returns else None
    payoff, payoff_reason = _payoff(positive, negative)
    max_win = _round_ratio(max(positive)) if positive else None
    max_loss = _round_ratio(min(negative)) if negative else None
    average_bars = _mean(tuple(Decimal(sample.holding_bars) for sample in realized))
    average_days = _mean(tuple(Decimal(sample.holding_days) for sample in realized))
    pnl_total = round_money(
        sum((_required_net_pnl(sample) for sample in realized), Decimal("0"))
    )

    return BacktestMetrics(
        label=INDEPENDENT_SAMPLE_LABEL,
        realized_count=count,
        win_rate_denominator=count,
        positive_count=len(positive),
        negative_count=len(negative),
        zero_count=zero_count,
        win_rate=win_rate,
        win_rate_reason=no_realized_reason,
        mean_net_return=mean_return,
        mean_net_return_reason=no_realized_reason,
        median_net_return=median_return,
        median_net_return_reason=no_realized_reason,
        payoff_ratio=payoff,
        payoff_ratio_reason=payoff_reason,
        max_win_return=max_win,
        max_win_return_reason=None if positive else "no_positive_returns",
        max_loss_return=max_loss,
        max_loss_return_reason=None if negative else "no_negative_returns",
        realized_net_pnl_total=pnl_total,
        average_holding_bars=average_bars,
        average_holding_bars_reason=no_realized_reason,
        average_holding_days=average_days,
        average_holding_days_reason=no_realized_reason,
        histogram=_histogram(returns),
        open_trades=_summarize_open(opened),
        reliability=_reliability(realized),
        _realized_net_returns=returns,
        _realized_net_pnls=tuple(_required_net_pnl(sample) for sample in realized),
        _realized_holding_bars=tuple(sample.holding_bars for sample in realized),
        _realized_holding_days=tuple(sample.holding_days for sample in realized),
        _realized_symbols=tuple(sample.symbol for sample in realized),
        _open_floating_pnls=tuple(_required_floating_pnl(sample) for sample in opened),
        _open_floating_returns=tuple(
            _required_floating_return(sample) for sample in opened
        ),
    )


def _summarize_open(samples: tuple[TradeSample, ...]) -> OpenTradeMetrics:
    returns = tuple(_required_floating_return(sample) for sample in samples)
    pnls = tuple(_required_floating_pnl(sample) for sample in samples)
    return _open_metrics_from_values(pnls, returns)


def _open_metrics_from_values(
    pnls: tuple[Decimal, ...], returns: tuple[Decimal, ...]
) -> OpenTradeMetrics:
    pnl_total = round_money(sum(pnls, Decimal("0")))
    return OpenTradeMetrics(
        count=len(returns),
        floating_pnl_total=pnl_total,
        mean_floating_return=_mean(returns) if returns else None,
        mean_floating_return_reason=None if returns else "no_open_samples",
    )


def _reliability(samples: tuple[TradeSample, ...]) -> Reliability:
    return _reliability_from_symbols(tuple(sample.symbol for sample in samples))


def _reliability_from_symbols(symbols: tuple[str, ...]) -> Reliability:
    count = len(symbols)
    if count == 0:
        return Reliability(
            level="low",
            reason="no_realized_samples",
            realized_count=0,
            largest_symbol_share=None,
        )
    symbol_counts = Counter(symbols)
    largest_share = _ratio(max(symbol_counts.values()), count)
    level, reason = _reliability_semantics(count, largest_share)
    return Reliability(
        level=level,
        reason=reason,
        realized_count=count,
        largest_symbol_share=largest_share,
    )


def _histogram(returns: tuple[Decimal, ...]) -> tuple[HistogramBin, ...]:
    count = len(returns)
    counts = [0] * 9
    for value in returns:
        if value < Decimal("-0.20"):
            index = 0
        elif value < Decimal("-0.10"):
            index = 1
        elif value < Decimal("-0.05"):
            index = 2
        elif value < 0:
            index = 3
        elif value == 0:
            index = 4
        elif value <= Decimal("0.05"):
            index = 5
        elif value <= Decimal("0.10"):
            index = 6
        elif value <= Decimal("0.20"):
            index = 7
        else:
            index = 8
        counts[index] += 1
    return tuple(
        HistogramBin(
            code=code,
            lower_bound=lower,
            upper_bound=upper,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
            count=counts[index],
            share=_ratio(counts[index], count) if count else None,
            share_reason=None if count else "no_realized_samples",
        )
        for index, (code, lower, upper, lower_inclusive, upper_inclusive) in enumerate(
            HISTOGRAM_CONTRACT
        )
    )


def freeze_unique_samples(samples: Iterable[TradeSample]) -> tuple[TradeSample, ...]:
    frozen = tuple(samples)
    if any(not isinstance(sample, TradeSample) for sample in frozen):
        raise TypeError("samples must contain only TradeSample values")
    identities: set[TradeIdentity] = set()
    for sample in frozen:
        identity = trade_identity(sample)
        if identity in identities:
            raise ValueError("duplicate trade identity is not allowed")
        identities.add(identity)
    return frozen


def trade_identity(sample: TradeSample) -> TradeIdentity:
    matching_pending = tuple(
        event
        for event in sample.order_events
        if isinstance(event, OrderPending)
        and event.side == "buy"
        and event.signal_at == sample.entry_signal_at
    )
    if len(matching_pending) != 1:
        raise ValueError("trade sample is missing stable entry order identity")
    return TradeIdentity(
        symbol=sample.symbol,
        entry_signal_at=sample.entry_signal_at,
        entry_fill_at=sample.entry_fill_at,
        entry_eligible_at=matching_pending[0].eligible_at,
        formula_version_id=sample.formula_version_id,
        signal_series_id=sample.signal_series_id,
        market_manifest_ids=sample.market_manifest_ids,
        status_manifest_ids=sample.status_manifest_ids,
        quantity=sample.quantity,
        entry_reference_open=sample.entry_reference_open,
    )


def _payoff(
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


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return _round_ratio(divide_decimal(sum(values, Decimal("0")), Decimal(len(values))))


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return _round_ratio(ordered[midpoint])
    return _round_ratio(
        divide_decimal(ordered[midpoint - 1] + ordered[midpoint], Decimal("2"))
    )


def _ratio(numerator: int, denominator: int) -> Decimal:
    return _round_ratio(divide_decimal(Decimal(numerator), Decimal(denominator)))


def _round_ratio(value: Decimal) -> Decimal:
    rounded = value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP)
    return rounded.copy_abs() if rounded.is_zero() else rounded


def _required_net_return(sample: TradeSample) -> Decimal:
    if sample.net_return is None:
        raise ValueError("realized sample requires net_return")
    return sample.net_return


def _required_net_pnl(sample: TradeSample) -> Decimal:
    if sample.net_pnl is None:
        raise ValueError("realized sample requires net_pnl")
    return sample.net_pnl


def _required_floating_return(sample: TradeSample) -> Decimal:
    if sample.floating_return is None:
        raise ValueError("open sample requires floating_return")
    return sample.floating_return


def _required_floating_pnl(sample: TradeSample) -> Decimal:
    if sample.floating_pnl is None:
        raise ValueError("open sample requires floating_pnl")
    return sample.floating_pnl


def _validate_optional_metric(
    value: Decimal | None,
    reason: str | None,
    *,
    field_name: str,
    nonnegative: bool = False,
) -> None:
    _validate_optional_finite(value, field_name=field_name)
    if value is None:
        if reason is None:
            raise ValueError(f"undefined {field_name} requires a reason")
        _validate_identity(reason, field_name=f"{field_name}_reason")
    elif reason is not None:
        raise ValueError(f"defined {field_name} cannot have a reason")
    if nonnegative and value is not None and value < 0:
        raise ValueError(f"{field_name} cannot be negative")


def _reliability_semantics(
    realized_count: int, largest_symbol_share: Decimal | None
) -> tuple[str, str | None]:
    if realized_count == 0:
        return "low", "no_realized_samples"
    assert largest_symbol_share is not None
    if realized_count < 30:
        return "low", "small_sample"
    if largest_symbol_share > Decimal("0.500000"):
        return "low", "high_symbol_concentration"
    if realized_count < 100:
        return "medium", "moderate_sample"
    return "high", None


def _validate_global_derivations(metrics: BacktestMetrics) -> None:
    returns = metrics._realized_net_returns
    pnls = metrics._realized_net_pnls
    holding_bars = metrics._realized_holding_bars
    holding_days = metrics._realized_holding_days
    symbols = metrics._realized_symbols
    open_pnls = metrics._open_floating_pnls
    open_returns = metrics._open_floating_returns
    if any(
        type(values) is not tuple
        for values in (
            returns,
            pnls,
            holding_bars,
            holding_days,
            symbols,
            open_pnls,
            open_returns,
        )
    ):
        raise TypeError("global metric audit values must be immutable tuples")
    if not (
        len(returns)
        == len(pnls)
        == len(holding_bars)
        == len(holding_days)
        == len(symbols)
        == metrics.realized_count
    ):
        raise ValueError("global metric audit values must match realized_count")
    for value in (*returns, *pnls):
        _validate_finite(value, field_name="global metric audit Decimal")
    for value in (*open_pnls, *open_returns):
        _validate_finite(value, field_name="open metric audit Decimal")
    if len(open_pnls) != len(open_returns):
        raise ValueError("open metric audit values must have matching lengths")
    for symbol in symbols:
        _validate_identity(symbol, field_name="realized symbol audit")
    if any(
        type(value) is not int or value < 0 for value in (*holding_bars, *holding_days)
    ):
        raise ValueError("holding audit values must be nonnegative integers")

    positive = tuple(value for value in returns if value > 0)
    negative = tuple(value for value in returns if value < 0)
    zero_count = sum(1 for value in returns if value == 0)
    if (
        metrics.positive_count,
        metrics.negative_count,
        metrics.zero_count,
    ) != (len(positive), len(negative), zero_count):
        raise ValueError("global sign counts must match realized return audit values")

    count = metrics.realized_count
    no_realized_reason = "no_realized_samples" if count == 0 else None
    expected_values = (
        (
            "win_rate",
            metrics.win_rate,
            metrics.win_rate_reason,
            _ratio(len(positive), count) if count else None,
            no_realized_reason,
        ),
        (
            "mean_net_return",
            metrics.mean_net_return,
            metrics.mean_net_return_reason,
            _mean(returns),
            no_realized_reason,
        ),
        (
            "median_net_return",
            metrics.median_net_return,
            metrics.median_net_return_reason,
            _median(returns) if returns else None,
            no_realized_reason,
        ),
        (
            "average_holding_bars",
            metrics.average_holding_bars,
            metrics.average_holding_bars_reason,
            _mean(tuple(Decimal(value) for value in holding_bars)),
            no_realized_reason,
        ),
        (
            "average_holding_days",
            metrics.average_holding_days,
            metrics.average_holding_days_reason,
            _mean(tuple(Decimal(value) for value in holding_days)),
            no_realized_reason,
        ),
    )
    for field_name, actual, actual_reason, expected, expected_reason in expected_values:
        if (actual, actual_reason) != (expected, expected_reason):
            if count == 0:
                raise ValueError(f"{field_name} must be None with no_realized_samples")
            raise ValueError(f"{field_name} does not match global audit values")

    expected_payoff, expected_payoff_reason = _payoff(positive, negative)
    if (metrics.payoff_ratio, metrics.payoff_ratio_reason) != (
        expected_payoff,
        expected_payoff_reason,
    ):
        raise ValueError("payoff_ratio does not match global audit values")
    expected_max_win = _round_ratio(max(positive)) if positive else None
    expected_max_loss = _round_ratio(min(negative)) if negative else None
    if (metrics.max_win_return, metrics.max_win_return_reason) != (
        expected_max_win,
        None if positive else "no_positive_returns",
    ):
        raise ValueError("max_win_return does not match global audit values")
    if (metrics.max_loss_return, metrics.max_loss_return_reason) != (
        expected_max_loss,
        None if negative else "no_negative_returns",
    ):
        raise ValueError("max_loss_return does not match global audit values")
    if metrics.realized_net_pnl_total != round_money(sum(pnls, Decimal("0"))):
        raise ValueError("realized_net_pnl_total does not match global audit values")
    if metrics.histogram != _histogram(returns):
        raise ValueError("histogram does not match realized return audit values")
    if metrics.reliability != _reliability_from_symbols(symbols):
        raise ValueError("reliability does not match realized symbol audit values")
    if metrics.open_trades != _open_metrics_from_values(open_pnls, open_returns):
        raise ValueError("open metrics do not match open value audit inputs")


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


def _validate_optional_finite(value: Decimal | None, *, field_name: str) -> None:
    if value is not None:
        _validate_finite(value, field_name=field_name)


def _validate_finite(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def _validate_identity(value: object, *, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")


def _json_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized.copy_abs() if normalized.is_zero() else normalized, "f")
