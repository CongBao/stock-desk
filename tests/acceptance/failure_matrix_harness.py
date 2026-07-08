from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Callable

from stock_desk.analysis.data_service import ResearchDataService
from stock_desk.analysis.evidence import EvidenceGraph
from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelRateLimitError,
)
from stock_desk.analysis.report import ResearchReportBuilder
from stock_desk.analysis.retry import classify_retry
from stock_desk.backtest.types import FrozenSymbolGap
from stock_desk.formula.compiler import FormulaCompileError, compile_formula
from stock_desk.formula.errors import FormulaSyntaxError
from stock_desk.market.provenance import RoutedBarFailure
from stock_desk.market.providers.base import (
    ProviderPermissionDenied,
    ProviderTimeout,
)
from stock_desk.market.providers.tdx_local import (
    TdxInspectionFailure,
    TdxLocalProvider,
)
from stock_desk.market.routing import SourcePriorities, SourceRouter
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    CapabilityReport,
    CapabilityState,
    Exchange,
    MarketCapability,
    Period,
    ProviderId,
)
from tests.backtest_test_helpers import (
    BacktestHarness,
    OPEN_ONLY_FORMULA,
    local_time,
)


SECRET = "sk-release-matrix-NEVER-LEAK"


@dataclass(frozen=True, slots=True)
class FailureResult:
    user_message: str
    recovery_action: str
    typed_reason: str
    scope: str
    source: str
    safe_status: str = "failed"
    partial_preserved: bool = False
    rating: str | None = None

    @property
    def contains_secret(self) -> bool:
        return SECRET in json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class _FailingProvider:
    name = ProviderId.TUSHARE

    def __init__(self, error: Exception) -> None:
        self._error = error

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(MarketCapability),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            data_cutoff=datetime(2024, 7, 1, 15, tzinfo=timezone.utc),
            gaps=(),
        )

    def fetch_bars(self, _query: BarQuery) -> object:
        raise self._error

    def fetch_instruments(self) -> object:
        raise AssertionError("failure matrix does not request instruments")

    def fetch_calendar(self, _exchange: Exchange, _start: date, _end: date) -> object:
        raise AssertionError("failure matrix does not request calendars")


def _bar_query(period: Period = Period.DAY) -> BarQuery:
    return BarQuery(
        symbol="600000.SH",
        period=period,
        adjustment=Adjustment.NONE,
        start=datetime(2024, 7, 1, tzinfo=timezone.utc),
        end=datetime(2024, 7, 3, tzinfo=timezone.utc),
    )


class FailureHarness:
    def __init__(self, root: Path) -> None:
        self._root = root

    def trigger(self, failure: str) -> FailureResult:
        handlers: dict[str, Callable[[], FailureResult]] = {
            "data_permission": lambda: self._data_failure(
                ProviderPermissionDenied(SECRET),
                recovery="check provider token and data permissions",
            ),
            "data_timeout": lambda: self._data_failure(
                ProviderTimeout(SECRET),
                recovery="retry the provider connection",
            ),
            "corrupt_tdx": self._corrupt_tdx,
            "missing_60m": self._missing_60m,
            "formula_syntax": self._formula_syntax,
            "future_formula": self._future_formula,
            "pool_symbol_failure": self._pool_symbol_failure,
            "model_auth": lambda: self._model_failure(
                ModelAuthenticationError(SECRET),
                recovery="update the model credentials and test the connection",
            ),
            "model_rate_limit": lambda: self._model_failure(
                ModelRateLimitError(SECRET),
                recovery="retry after the provider rate-limit window",
            ),
            "critical_evidence_gap": self._critical_evidence_gap,
        }
        try:
            handler = handlers[failure]
        except KeyError as error:
            raise ValueError("unknown release failure scenario") from error
        return handler()

    @staticmethod
    def _data_failure(error: Exception, *, recovery: str) -> FailureResult:
        provider = _FailingProvider(error)
        routed = SourceRouter(
            ((ProviderId.TUSHARE, provider),),
            priorities=SourcePriorities(bars=(ProviderId.TUSHARE,)),
        ).fetch_bars(_bar_query())
        if not isinstance(routed, RoutedBarFailure) or not routed.audit.attempts:
            raise AssertionError("provider failure did not produce a routing audit")
        attempt = routed.audit.attempts[-1]
        return FailureResult(
            user_message=attempt.detail,
            recovery_action=recovery,
            typed_reason=attempt.reason.value,
            scope=routed.failure.query.symbol,
            source=attempt.source.value,
        )

    def _corrupt_tdx(self) -> FailureResult:
        root = (self._root / SECRET / "vipdoc").resolve()
        (root / "sh" / "lday").mkdir(parents=True)
        (root / "sz" / "lday").mkdir(parents=True)
        (root / "sh" / "lday" / "sh600000.day").write_bytes(b"corrupt")
        inspected = TdxLocalProvider(
            root=root,
            clock=lambda: datetime(2024, 7, 8, 16, tzinfo=timezone.utc),
        ).preflight()
        if not isinstance(inspected, TdxInspectionFailure):
            raise AssertionError("corrupt TDX fixture was unexpectedly accepted")
        return FailureResult(
            user_message=inspected.detail,
            recovery_action="repair the TDX day file or select another data source",
            typed_reason=inspected.reason.value,
            scope="TDX vipdoc",
            source=ProviderId.TDX_LOCAL.value,
        )

    def _missing_60m(self) -> FailureResult:
        outcome = TdxLocalProvider(
            root=self._root / "missing-minute-vipdoc",
            clock=lambda: datetime(2024, 7, 8, 16, tzinfo=timezone.utc),
        ).fetch_bars(_bar_query(Period.MIN60))
        if not isinstance(outcome, BarFailure):
            raise AssertionError("TDX unexpectedly returned 60-minute bars")
        return FailureResult(
            user_message=outcome.detail,
            recovery_action="configure a provider with 60-minute coverage",
            typed_reason=outcome.reason.value,
            scope="600000.SH/60m",
            source=ProviderId.TDX_LOCAL.value,
        )

    @staticmethod
    def _formula_syntax() -> FailureResult:
        try:
            compile_formula(f"BUY:CROSS(C,;{SECRET}")
        except FormulaSyntaxError as error:
            return FailureResult(
                user_message=str(error),
                recovery_action=f"edit formula at line {error.line}, column {error.column}",
                typed_reason=error.code,
                scope=f"line:{error.line}:column:{error.column}",
                source="formula_engine",
            )
        raise AssertionError("invalid formula syntax was unexpectedly accepted")

    @staticmethod
    def _future_formula() -> FailureResult:
        try:
            compile_formula("BUY:REF(CLOSE,-1)>0;SELL:CLOSE<0;")
        except FormulaCompileError as error:
            if error.code != "future_data":
                raise
            return FailureResult(
                user_message=str(error),
                recovery_action=f"remove future reference at line {error.line}",
                typed_reason=error.code,
                scope=f"line:{error.line}:column:{error.column}",
                source="formula_engine",
            )
        raise AssertionError("future-data formula was unexpectedly accepted")

    def _pool_symbol_failure(self) -> FailureResult:
        with BacktestHarness.create(self._root / "pool") as harness:
            harness.seed_instruments("000001.SZ", "600000.SH")
            days = tuple(date(2024, 1, day) for day in range(2, 9))
            harness.seed_symbol("600000.SH", Period.DAY, days)
            formula = harness.create_formula("failure-matrix", OPEN_ONLY_FORMULA)
            completed = harness.run_pool(
                formula.id,
                symbols=("000001.SZ", "600000.SH"),
                period=Period.DAY,
                scoring_start=local_time(date(2024, 1, 3)),
                scoring_end=local_time(date(2024, 1, 9)),
            )
            failed = next(
                item for item in completed.run.symbols if item.status == "failed"
            )
            if not isinstance(failed.reference, FrozenSymbolGap):
                raise AssertionError("pool failure did not retain its frozen gap")
            outcomes = completed.report.outcomes
            if outcomes.succeeded != 1 or outcomes.data_insufficient != 1:
                raise AssertionError("pool failure did not preserve valid partial work")
            return FailureResult(
                user_message=f"{failed.symbol}: {failed.failure_reason}",
                recovery_action="refresh the failed symbol data and rerun the pool",
                typed_reason=failed.reference.reason,
                scope=failed.symbol,
                source="backtest_pool",
                safe_status="partial",
                partial_preserved=True,
            )

    @staticmethod
    def _model_failure(error: BaseException, *, recovery: str) -> FailureResult:
        decision = classify_retry(error)
        return FailureResult(
            user_message=decision.safe_message,
            recovery_action=recovery,
            typed_reason=decision.code,
            scope="analysis_model",
            source="model_provider",
        )

    @staticmethod
    def _critical_evidence_gap() -> FailureResult:
        frozen_at = datetime(2024, 7, 8, 16, tzinfo=timezone.utc)
        snapshot, _diagnostics = ResearchDataService(
            loaders=(),
            clock=lambda: frozen_at,
        ).build_snapshot("600000.SH", frozen_at=frozen_at)
        report = ResearchReportBuilder().build_insufficient(
            snapshot=snapshot,
            evidence_graph=EvidenceGraph(
                snapshot=snapshot,
                evidence_items=(),
                claims=(),
            ),
        )
        return FailureResult(
            user_message=(
                "critical evidence is missing: "
                + ",".join(item.value for item in report.missing_sections)
            ),
            recovery_action=",".join(report.recovery_actions),
            typed_reason=report.status.value,
            scope="600000.SH",
            source="research_data",
            safe_status=report.status.value,
            rating=None if report.rating is None else report.rating.value,
        )
