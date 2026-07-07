from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import cast

from pydantic import ValidationError
import pytest
from requests.exceptions import Timeout as RequestsTimeout

from scripts.research_source_probe import main as live_probe_main
import stock_desk.analysis.sources.akshare as akshare_source_module
import stock_desk.analysis.sources.tushare as tushare_source_module
from stock_desk.analysis.sources import _akshare_worker
from stock_desk.analysis.data_service import ResearchDataUnavailable
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.analysis.sources.akshare import (
    AkShareIsolatedSdkFacade,
    AkShareResearchSdkFacade,
    AkShareResearchSource,
)
from stock_desk.analysis.sources.base import (
    MAX_RESEARCH_ITEM_BYTES,
    MAX_RESEARCH_ITEMS,
    MAX_RESEARCH_NODES,
    MAX_RESEARCH_TOTAL_BYTES,
    RESEARCH_SOURCE_CATEGORIES,
    ResearchSourceCapability,
    normalize_research_table,
    research_section_from_table,
)
from stock_desk.analysis.sources.routing import (
    RESEARCH_SOURCE_CAPABILITIES,
    ResearchSourceRouter,
)
from stock_desk.analysis.sources.tushare import (
    TushareResearchSdkFacade,
    TushareResearchSource,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderNoData,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderUnavailable,
)
from stock_desk.market.types import FailureReason, ProviderId


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/analysis/research_sources.json").read_text(encoding="utf-8")
)
NOW = datetime(2025, 7, 6, 9, tzinfo=timezone.utc)
SYMBOL = "600000.SH"


class FakeTusharePro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def income(self, **kwargs: object) -> object:
        self.calls.append(("income", kwargs))
        return FIXTURE["tushare"]["fundamentals"]

    def anns_d(self, **kwargs: object) -> object:
        self.calls.append(("anns_d", kwargs))
        return FIXTURE["tushare"]["announcements"]


class FakeAkShareModule:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object:
        self.calls.append(("stock_financial_analysis_indicator_em", kwargs))
        return FIXTURE["akshare"]["fundamentals"]

    def stock_individual_notice_report(self, **kwargs: object) -> object:
        self.calls.append(("stock_individual_notice_report", kwargs))
        return FIXTURE["akshare"]["announcements"]

    def stock_news_em(self, **kwargs: object) -> object:
        self.calls.append(("stock_news_em", kwargs))
        return FIXTURE["akshare"]["news"]


class StubSource:
    def __init__(
        self,
        name: ProviderId,
        outcomes: dict[ResearchSectionKind, ResearchSection | Exception],
    ) -> None:
        self.name = name
        self._outcomes = outcomes
        self.calls: list[tuple[str, ResearchSectionKind]] = []

    def fetch(self, symbol: str, kind: ResearchSectionKind) -> ResearchSection:
        self.calls.append((symbol, kind))
        outcome = self._outcomes[kind]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def section(
    source: ProviderId,
    kind: ResearchSectionKind,
    *,
    marker: str | None = None,
) -> ResearchSection:
    return ResearchSection(
        kind=kind,
        canonical_source=source.value,
        source_record=f"{source.value}:{kind.value}:fixture",
        source_url="https://example.com/source",
        published_at=(
            NOW
            if kind in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}
            else None
        ),
        data_cutoff=NOW,
        fetched_at=NOW,
        dataset_version="sha256:" + "a" * 64,
        content={"items": [{"marker": marker or source.value}]},
    )


def exception_chain_text(error: BaseException) -> str:
    rendered: list[str] = []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend((str(current), repr(current)))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(rendered)


def test_capability_matrix_is_explicit_and_never_fakes_baostock_or_tdx() -> None:
    assert RESEARCH_SOURCE_CAPABILITIES == {
        ProviderId.TUSHARE: ResearchSourceCapability(
            source=ProviderId.TUSHARE,
            categories=frozenset(
                {
                    ResearchSectionKind.FUNDAMENTALS,
                    ResearchSectionKind.ANNOUNCEMENTS,
                }
            ),
        ),
        ProviderId.AKSHARE: ResearchSourceCapability(
            source=ProviderId.AKSHARE,
            categories=RESEARCH_SOURCE_CATEGORIES,
        ),
        ProviderId.BAOSTOCK: ResearchSourceCapability(
            source=ProviderId.BAOSTOCK, categories=frozenset()
        ),
        ProviderId.TDX_LOCAL: ResearchSourceCapability(
            source=ProviderId.TDX_LOCAL, categories=frozenset()
        ),
        ProviderId.EASTMONEY: ResearchSourceCapability(
            source=ProviderId.EASTMONEY, categories=frozenset()
        ),
    }


def test_sdk_facades_call_real_research_seams_with_symbol_scoped_parameters() -> None:
    pro = FakeTusharePro()
    tushare = TushareResearchSdkFacade(pro=pro)
    ak_module = FakeAkShareModule()
    akshare = AkShareResearchSdkFacade(module=ak_module)

    assert tushare.income(ts_code=SYMBOL) == FIXTURE["tushare"]["fundamentals"]
    assert tushare.anns_d(ts_code=SYMBOL) == FIXTURE["tushare"]["announcements"]
    assert (
        akshare.stock_financial_analysis_indicator_em(
            symbol=SYMBOL, indicator="按报告期"
        )
        == FIXTURE["akshare"]["fundamentals"]
    )
    assert (
        akshare.stock_individual_notice_report(security="600000", symbol="全部")
        == FIXTURE["akshare"]["announcements"]
    )
    assert akshare.stock_news_em(symbol="600000") == FIXTURE["akshare"]["news"]
    assert [name for name, _kwargs in pro.calls] == ["income", "anns_d"]
    assert [name for name, _kwargs in ak_module.calls] == [
        "stock_financial_analysis_indicator_em",
        "stock_individual_notice_report",
        "stock_news_em",
    ]


@pytest.mark.parametrize(
    ("factory", "kind", "expected_source"),
    [
        (
            lambda: TushareResearchSource(
                client=TushareResearchSdkFacade(pro=FakeTusharePro()), clock=lambda: NOW
            ),
            ResearchSectionKind.FUNDAMENTALS,
            "tushare",
        ),
        (
            lambda: TushareResearchSource(
                client=TushareResearchSdkFacade(pro=FakeTusharePro()), clock=lambda: NOW
            ),
            ResearchSectionKind.ANNOUNCEMENTS,
            "tushare",
        ),
        (
            lambda: AkShareResearchSource(
                client=AkShareResearchSdkFacade(module=FakeAkShareModule()),
                clock=lambda: NOW,
            ),
            ResearchSectionKind.FUNDAMENTALS,
            "akshare",
        ),
        (
            lambda: AkShareResearchSource(
                client=AkShareResearchSdkFacade(module=FakeAkShareModule()),
                clock=lambda: NOW,
            ),
            ResearchSectionKind.ANNOUNCEMENTS,
            "akshare",
        ),
        (
            lambda: AkShareResearchSource(
                client=AkShareResearchSdkFacade(module=FakeAkShareModule()),
                clock=lambda: NOW,
            ),
            ResearchSectionKind.NEWS,
            "akshare",
        ),
    ],
)
def test_fixed_sdk_fixtures_normalize_to_nonempty_traceable_sections(
    factory: Callable[[], object],
    kind: ResearchSectionKind,
    expected_source: str,
) -> None:
    source = cast(object, factory())
    result = source.fetch(SYMBOL, kind)  # type: ignore[attr-defined]

    assert result.kind is kind
    assert result.canonical_source == expected_source
    assert result.source_record.startswith(f"{expected_source}:{kind.value}:")
    assert result.dataset_version.startswith("sha256:")
    assert result.data_cutoff <= result.fetched_at
    assert result.content["items"]
    assert "placeholder" not in result.content
    if expected_source == "akshare" and kind is ResearchSectionKind.FUNDAMENTALS:
        assert result.data_cutoff == datetime(2024, 12, 30, 16, tzinfo=timezone.utc)
    if expected_source == "akshare" and kind is ResearchSectionKind.NEWS:
        assert result.published_at == datetime(2025, 7, 5, 0, 30, tzinfo=timezone.utc)


def test_tushare_rejects_missing_or_conflicting_row_identity() -> None:
    class Client:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def income(self, **_kwargs: object) -> object:
            return [self.row]

        def anns_d(self, **_kwargs: object) -> object:
            return [self.row]

    cases = (
        (ResearchSectionKind.FUNDAMENTALS, {"ann_date": "20250430"}),
        (
            ResearchSectionKind.FUNDAMENTALS,
            {"ts_code": "000001.SZ", "ann_date": "20250430"},
        ),
        (ResearchSectionKind.ANNOUNCEMENTS, {"ann_date": "20250430"}),
        (
            ResearchSectionKind.ANNOUNCEMENTS,
            {"ts_code": "000001.SZ", "ann_date": "20250430"},
        ),
    )
    for kind, row in cases:
        with pytest.raises(ProviderInvalidResponse):
            TushareResearchSource(client=Client(row), clock=lambda: NOW).fetch(
                SYMBOL, kind
            )


def test_akshare_rejects_missing_or_conflicting_row_identity() -> None:
    class Client:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def stock_financial_analysis_indicator_em(self, **_kwargs: object) -> object:
            return [self.row]

        def stock_individual_notice_report(self, **_kwargs: object) -> object:
            return [self.row]

        def stock_news_em(self, **_kwargs: object) -> object:
            return [self.row]

    cases = (
        (
            ResearchSectionKind.FUNDAMENTALS,
            {"REPORT_DATE": "2024-12-31 00:00:00"},
        ),
        (
            ResearchSectionKind.FUNDAMENTALS,
            {"SECUCODE": "000001.SZ", "REPORT_DATE": "2024-12-31 00:00:00"},
        ),
        (ResearchSectionKind.ANNOUNCEMENTS, {"公告日期": "2025-07-04"}),
        (
            ResearchSectionKind.ANNOUNCEMENTS,
            {"代码": "000001", "公告日期": "2025-07-04"},
        ),
        (ResearchSectionKind.NEWS, {"发布时间": "2025-07-05 08:30:00"}),
        (
            ResearchSectionKind.NEWS,
            {"关键词": "000001", "发布时间": "2025-07-05 08:30:00"},
        ),
    )
    for kind, row in cases:
        with pytest.raises(ProviderInvalidResponse):
            AkShareResearchSource(client=Client(row), clock=lambda: NOW).fetch(
                SYMBOL, kind
            )


def test_nonempty_fundamentals_without_real_cutoff_are_typed_invalid() -> None:
    class ClientWithoutCutoff:
        def income(self, **_kwargs: object) -> object:
            return [{"ts_code": SYMBOL, "basic_eps": 1.0}]

        def anns_d(self, **_kwargs: object) -> object:
            return []

    source = TushareResearchSource(
        client=ClientWithoutCutoff(),
        clock=lambda: NOW,
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE,),
        sources=(source,),
    )

    with pytest.raises(ProviderInvalidResponse):
        source.fetch(SYMBOL, ResearchSectionKind.FUNDAMENTALS)
    with pytest.raises(ResearchDataUnavailable) as captured:
        router.load(SYMBOL)

    assert captured.value.reason is ResearchMissingReason.INVALID_RESPONSE


@pytest.mark.parametrize("symbol", ["600000.SH", "000001.SZ", "920001.BJ"])
def test_akshare_fundamentals_passes_the_canonical_exchange_to_the_sdk(
    symbol: str,
) -> None:
    class CapturingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return [
                {
                    "SECUCODE": symbol,
                    "REPORT_DATE": "2024-12-31 00:00:00",
                    "BASIC_EPS": 1.0,
                }
            ]

        def stock_individual_notice_report(self, **_kwargs: object) -> object:
            return []

        def stock_news_em(self, **_kwargs: object) -> object:
            return []

    client = CapturingClient()

    AkShareResearchSource(client=client, clock=lambda: NOW).fetch(
        symbol, ResearchSectionKind.FUNDAMENTALS
    )

    assert client.calls == [{"symbol": symbol, "indicator": "按报告期"}]


def test_router_skips_unsupported_sources_and_never_calls_them() -> None:
    tushare = StubSource(
        ProviderId.TUSHARE,
        {ResearchSectionKind.NEWS: AssertionError("must not be called")},
    )
    baostock = StubSource(
        ProviderId.BAOSTOCK,
        {ResearchSectionKind.NEWS: AssertionError("must not be called")},
    )
    akshare = StubSource(
        ProviderId.AKSHARE,
        {
            ResearchSectionKind.NEWS: section(
                ProviderId.AKSHARE, ResearchSectionKind.NEWS
            )
        },
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.NEWS,
        priority=(
            ProviderId.TUSHARE,
            ProviderId.BAOSTOCK,
            ProviderId.TDX_LOCAL,
            ProviderId.AKSHARE,
        ),
        sources=(tushare, baostock, akshare),
    )

    loaded = router.load(SYMBOL)

    assert loaded.canonical_source == "akshare"
    assert loaded.route is None
    assert tushare.calls == []
    assert baostock.calls == []
    assert akshare.calls == [(SYMBOL, ResearchSectionKind.NEWS)]


def test_router_falls_back_without_merging_and_marks_degraded_source() -> None:
    tushare = StubSource(
        ProviderId.TUSHARE,
        {ResearchSectionKind.FUNDAMENTALS: ProviderPermissionDenied("token=secret")},
    )
    akshare = StubSource(
        ProviderId.AKSHARE,
        {
            ResearchSectionKind.FUNDAMENTALS: section(
                ProviderId.AKSHARE,
                ResearchSectionKind.FUNDAMENTALS,
                marker="fallback-only",
            )
        },
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        sources=(tushare, akshare),
    )

    loaded = router.load(SYMBOL)

    assert loaded.canonical_source == "akshare"
    assert loaded.content == {"items": [{"marker": "fallback-only"}]}
    assert loaded.quality_flags == (ResearchQualityFlag.DEGRADED_SOURCE,)
    assert loaded.route is not None
    assert loaded.route.selected_source == "akshare"
    assert loaded.route.attempted_sources == ("tushare",)
    assert loaded.route.failure_reasons == (ResearchMissingReason.PERMISSION_DENIED,)
    assert (
        loaded.route.primary_failure_reason is ResearchMissingReason.PERMISSION_DENIED
    )
    assert loaded.route.degraded_from == "tushare"
    assert (
        ResearchSection.model_validate_json(loaded.model_dump_json(by_alias=True)).route
        == loaded.route
    )
    assert (
        loaded.section_id
        != section(
            ProviderId.AKSHARE,
            ResearchSectionKind.FUNDAMENTALS,
            marker="fallback-only",
        ).section_id
    )


def test_router_preserves_multiple_failure_order_in_terminal_missing() -> None:
    tushare = StubSource(
        ProviderId.TUSHARE,
        {ResearchSectionKind.FUNDAMENTALS: ProviderPermissionDenied()},
    )
    akshare = StubSource(
        ProviderId.AKSHARE,
        {ResearchSectionKind.FUNDAMENTALS: ProviderTimeout()},
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        sources=(tushare, akshare),
    )

    with pytest.raises(ResearchDataUnavailable) as captured:
        router.load(SYMBOL)

    assert captured.value.reason is ResearchMissingReason.PERMISSION_DENIED
    assert captured.value.attempted_sources == ("tushare", "akshare")


def test_malformed_fallback_section_cannot_escape_the_router_secret_boundary() -> None:
    primary = StubSource(
        ProviderId.TUSHARE,
        {ResearchSectionKind.FUNDAMENTALS: ProviderNoData()},
    )
    malformed = ResearchSection.model_construct(
        kind=ResearchSectionKind.FUNDAMENTALS,
        canonical_source="akshare",
        source_record="akshare:fundamentals:malformed",
        source_url=None,
        published_at=None,
        data_cutoff=NOW,
        fetched_at=NOW,
        dataset_version="sha256:" + "a" * 64,
        quality_flags=(),
        content_json=b'{"token":"TOP-SECRET"',
    )
    fallback = StubSource(
        ProviderId.AKSHARE,
        {ResearchSectionKind.FUNDAMENTALS: malformed},
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        sources=(primary, fallback),
    )

    with pytest.raises(ResearchDataUnavailable) as captured:
        router.load(SYMBOL)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(captured.value)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ProviderPermissionDenied("token=TOP-SECRET"),
            ResearchMissingReason.PERMISSION_DENIED,
        ),
        (ProviderTimeout("token=TOP-SECRET"), ResearchMissingReason.TIMEOUT),
        (ProviderNoData("token=TOP-SECRET"), ResearchMissingReason.NO_DATA),
        (RequestsTimeout("token=TOP-SECRET"), ResearchMissingReason.TIMEOUT),
        (RuntimeError("token=TOP-SECRET"), ResearchMissingReason.INVALID_RESPONSE),
    ],
)
def test_router_maps_failures_to_typed_secret_safe_missing(
    error: Exception, reason: ResearchMissingReason
) -> None:
    source = StubSource(
        ProviderId.AKSHARE,
        {ResearchSectionKind.NEWS: error},
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.NEWS,
        priority=(ProviderId.AKSHARE,),
        sources=(source,),
    )

    with pytest.raises(ResearchDataUnavailable) as captured:
        router.load(SYMBOL)

    assert captured.value.kind is ResearchSectionKind.NEWS
    assert captured.value.reason is reason
    assert captured.value.attempted_sources == ("akshare",)
    assert "TOP-SECRET" not in str(captured.value)
    assert "TOP-SECRET" not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_router_fail_closes_malformed_provider_failure_reasons() -> None:
    class MalformedProviderError(ProviderClientError):
        reason = cast(FailureReason, "TOP-SECRET")

    failures = (
        MalformedProviderError(),
        ResearchDataUnavailable(
            kind=ResearchSectionKind.NEWS,
            reason=cast(ResearchMissingReason, "TOP-SECRET"),
            attempted_sources=("unsafe",),
        ),
    )
    for failure in failures:
        router = ResearchSourceRouter(
            kind=ResearchSectionKind.NEWS,
            priority=(ProviderId.AKSHARE,),
            sources=(
                StubSource(
                    ProviderId.AKSHARE,
                    {ResearchSectionKind.NEWS: failure},
                ),
            ),
        )

        with pytest.raises(ResearchDataUnavailable) as captured:
            router.load(SYMBOL)

        assert captured.value.reason is ResearchMissingReason.INVALID_RESPONSE
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert "TOP-SECRET" not in exception_chain_text(captured.value)


def test_tushare_default_sdk_path_classifies_permission_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PermissionPro:
        def income(self, **_kwargs: object) -> object:
            raise Exception("抱歉，您没有访问该接口的权限 token=TOP-SECRET")

        def anns_d(self, **_kwargs: object) -> object:
            return []

    class FakeModule:
        def pro_api(self, token: str) -> PermissionPro:
            assert token == "TOP-SECRET"
            return PermissionPro()

    monkeypatch.setattr(
        tushare_source_module,
        "import_optional_sdk",
        lambda name: FakeModule() if name == "tushare" else None,
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE,),
        sources=(
            TushareResearchSource.from_sdk(
                token="TOP-SECRET",
                clock=lambda: NOW,
            ),
        ),
    )

    with pytest.raises(ResearchDataUnavailable) as captured:
        router.load(SYMBOL)

    assert captured.value.reason is ResearchMissingReason.PERMISSION_DENIED
    assert "TOP-SECRET" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_tushare_facade_and_source_remove_secret_exception_contexts() -> None:
    class PermissionPro:
        def income(self, **_kwargs: object) -> object:
            raise Exception("没有访问该接口的权限 token=TOP-SECRET")

    facade = TushareResearchSdkFacade(pro=PermissionPro())

    with pytest.raises(ProviderPermissionDenied) as facade_error:
        facade.income(ts_code=SYMBOL)

    assert facade_error.value.__cause__ is None
    assert facade_error.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(facade_error.value)

    class ContextBearingClient:
        def income(self, **_kwargs: object) -> object:
            try:
                raise RuntimeError("token=TOP-SECRET")
            except RuntimeError:
                raise ProviderTimeout() from None

        def anns_d(self, **_kwargs: object) -> object:
            return []

    source = TushareResearchSource(
        client=ContextBearingClient(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderTimeout) as source_error:
        source.fetch(SYMBOL, ResearchSectionKind.FUNDAMENTALS)

    assert source_error.value.__cause__ is None
    assert source_error.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(source_error.value)


def test_akshare_facade_source_and_normalizer_remove_secret_contexts() -> None:
    class FailingModule:
        def stock_news_em(self, **_kwargs: object) -> object:
            raise RequestsTimeout("api-key=TOP-SECRET")

    facade = AkShareResearchSdkFacade(module=FailingModule())

    with pytest.raises(ProviderTimeout) as facade_error:
        facade.stock_news_em(symbol="600000")
    assert facade_error.value.__cause__ is None
    assert facade_error.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(facade_error.value)

    source = AkShareResearchSource(client=facade, clock=lambda: NOW)
    with pytest.raises(ProviderTimeout) as source_error:
        source.fetch(SYMBOL, ResearchSectionKind.NEWS)
    assert source_error.value.__cause__ is None
    assert source_error.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(source_error.value)

    class FailingTable:
        columns = ("value",)

        def to_dict(self, **_kwargs: object) -> object:
            raise RuntimeError("database-url=TOP-SECRET")

    with pytest.raises(ProviderInvalidResponse) as normalization_error:
        normalize_research_table(FailingTable())
    assert normalization_error.value.__cause__ is None
    assert normalization_error.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(normalization_error.value)


def test_source_normalization_enforces_item_count_and_item_byte_budgets() -> None:
    class OversizedClient:
        def __init__(self) -> None:
            self.fundamentals_calls = 0
            self.announcement_calls = 0

        def stock_financial_analysis_indicator_em(self, **_kwargs: object) -> object:
            self.fundamentals_calls += 1
            return [{"value": index} for index in range(MAX_RESEARCH_ITEMS + 1)]

        def stock_individual_notice_report(self, **_kwargs: object) -> object:
            self.announcement_calls += 1
            return [{"value": "x" * MAX_RESEARCH_ITEM_BYTES}]

        def stock_news_em(self, **_kwargs: object) -> object:
            return []

    client = OversizedClient()
    source = AkShareResearchSource(client=client, clock=lambda: NOW)

    for kind in (
        ResearchSectionKind.FUNDAMENTALS,
        ResearchSectionKind.ANNOUNCEMENTS,
    ):
        with pytest.raises(ProviderInvalidResponse):
            source.fetch(SYMBOL, kind)
    assert client.fundamentals_calls == 1
    assert client.announcement_calls == 1


@pytest.mark.parametrize(
    "rows",
    [
        [{"value": "x" * (MAX_RESEARCH_TOTAL_BYTES // 8)} for _ in range(9)],
        [{"value": [[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]}],
        [
            {"values": list(range(MAX_RESEARCH_NODES // MAX_RESEARCH_ITEMS + 2))}
            for _ in range(MAX_RESEARCH_ITEMS)
        ],
    ],
)
def test_source_normalization_enforces_total_depth_and_node_budgets(
    rows: list[dict[str, object]],
) -> None:
    with pytest.raises(ProviderInvalidResponse):
        normalize_research_table(rows)


def test_source_normalization_preserves_provider_missing_cells_as_json_null() -> None:
    assert normalize_research_table([{"metric": float("nan")}]) == ({"metric": None},)


def test_dataframe_row_budget_is_checked_before_materialization() -> None:
    class OversizedFrame:
        shape = (MAX_RESEARCH_ITEMS + 1, 3)
        columns = ("SECUCODE", "REPORT_DATE", "BASIC_EPS")

        def to_dict(self, **_kwargs: object) -> object:
            pytest.fail("oversized DataFrame was materialized")

    with pytest.raises(ProviderInvalidResponse):
        normalize_research_table(OversizedFrame())


def test_every_configured_identity_field_is_required_on_every_row() -> None:
    with pytest.raises(ProviderInvalidResponse):
        research_section_from_table(
            source=ProviderId.AKSHARE,
            kind=ResearchSectionKind.FUNDAMENTALS,
            symbol=SYMBOL,
            table=[
                {
                    "id_a": SYMBOL,
                    "REPORT_DATE": "2024-12-31 00:00:00",
                }
            ],
            fetched_at=NOW,
            identity_fields=("id_a", "id_b"),
            expected_identity=SYMBOL,
            cutoff_fields=("REPORT_DATE",),
        )


def test_akshare_isolated_facade_kills_and_reaps_timed_out_process() -> None:
    class BlockingProcess:
        def __init__(self) -> None:
            self.killed = False
            self.reaped = False
            self.events: list[str] = []

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input
            self.events.append("communicate")
            if not self.killed:
                raise subprocess.TimeoutExpired("akshare-worker", timeout)
            self.reaped = True
            return b"", b""

        def kill(self) -> None:
            self.events.append("kill")
            self.killed = True

        def read_result(self, maximum_bytes: int) -> bytes:
            del maximum_bytes
            pytest.fail("timed out worker result must not be read")

        def close_result(self) -> None:
            self.events.append("close_result")

    process = BlockingProcess()
    launched: list[tuple[str, dict[str, object]]] = []

    def launch(operation: str, kwargs: dict[str, object]) -> BlockingProcess:
        launched.append((operation, kwargs))
        return process

    facade = AkShareIsolatedSdkFacade(
        launcher=launch,
        timeout_seconds=0.01,
    )

    with pytest.raises(ProviderTimeout) as captured:
        facade.stock_news_em(symbol="600000")

    assert launched == [("stock_news_em", {"symbol": "600000"})]
    assert process.killed is True
    assert process.reaped is True
    assert process.events == ["communicate", "kill", "communicate", "close_result"]
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_akshare_isolated_facade_rejects_noncanonical_worker_output_safely() -> None:
    class ResultProcess:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.read_limits: list[int] = []
            self.closed = False

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            return b"", b""

        def kill(self) -> None:
            pytest.fail("completed worker must not be killed")

        def read_result(self, maximum_bytes: int) -> bytes:
            self.read_limits.append(maximum_bytes)
            return self.payload[:maximum_bytes]

        def close_result(self) -> None:
            self.closed = True

    payloads = (
        b'noise{"status":"no_data"}',
        b'{"status":"no_data"}\n',
        b'{"status":"no_data"}\n{"status":"no_data"}',
        b'{"status":"no_data","secret":"TOP-SECRET"}',
        b'{"status":"ok","rows":[],"extra":true}',
        b'{"status":"ok","rows":"TOP-SECRET"}',
    )
    for payload in payloads:
        process = ResultProcess(payload)
        facade = AkShareIsolatedSdkFacade(
            launcher=lambda _operation, _kwargs, current=process: current,
        )

        with pytest.raises(ProviderInvalidResponse) as captured:
            facade.stock_news_em(symbol="600000")

        assert process.read_limits == [262_145]
        assert process.closed is True
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert "TOP-SECRET" not in exception_chain_text(captured.value)


def test_akshare_isolated_facade_preserves_typed_no_data() -> None:
    class EmptyProcess:
        def __init__(self) -> None:
            self.closed = False

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            return b"", b""

        def kill(self) -> None:
            pytest.fail("completed worker must not be killed")

        def read_result(self, maximum_bytes: int) -> bytes:
            assert maximum_bytes == 262_145
            return b'{"status":"no_data"}'

        def close_result(self) -> None:
            self.closed = True

    process = EmptyProcess()
    source = AkShareResearchSource(
        client=AkShareIsolatedSdkFacade(launcher=lambda _operation, _kwargs: process),
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderNoData) as captured:
        source.fetch(SYMBOL, ResearchSectionKind.NEWS)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert process.closed is True


def test_akshare_isolated_facade_cleans_up_non_timeout_communicate_failure() -> None:
    class FailingProcess:
        def __init__(self) -> None:
            self.killed = False
            self.events: list[str] = []

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            self.events.append("communicate")
            if not self.killed:
                raise RuntimeError("TOP-SECRET")
            return b"", b""

        def kill(self) -> None:
            self.events.append("kill")
            self.killed = True

        def read_result(self, maximum_bytes: int) -> bytes:
            del maximum_bytes
            pytest.fail("failed worker result must not be read")

        def close_result(self) -> None:
            self.events.append("close_result")

    process = FailingProcess()
    facade = AkShareIsolatedSdkFacade(
        launcher=lambda _operation, _kwargs: process,
    )

    with pytest.raises(ProviderInvalidResponse) as captured:
        facade.stock_news_em(symbol="600000")

    assert process.events == ["communicate", "kill", "communicate", "close_result"]
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(captured.value)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(130)])
def test_akshare_isolated_facade_cleans_up_then_reraises_user_interrupt(
    interrupt: BaseException,
) -> None:
    class InterruptedProcess:
        def __init__(self) -> None:
            self.killed = False
            self.events: list[str] = []

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            self.events.append("communicate")
            if not self.killed:
                raise interrupt
            return b"", b""

        def kill(self) -> None:
            self.events.append("kill")
            self.killed = True

        def read_result(self, maximum_bytes: int) -> bytes:
            del maximum_bytes
            pytest.fail("interrupted worker result must not be read")

        def close_result(self) -> None:
            self.events.append("close_result")

    process = InterruptedProcess()
    facade = AkShareIsolatedSdkFacade(
        launcher=lambda _operation, _kwargs: process,
    )

    with pytest.raises(type(interrupt)) as captured:
        facade.stock_news_em(symbol="600000")

    assert captured.value is interrupt
    assert process.events == ["communicate", "kill", "communicate", "close_result"]


def test_akshare_isolated_facade_keeps_original_interrupt_when_cleanup_interrupts() -> (
    None
):
    original = KeyboardInterrupt("original")

    class DoubleInterruptedProcess:
        def __init__(self) -> None:
            self.events: list[str] = []

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            self.events.append("communicate")
            raise original

        def kill(self) -> None:
            self.events.append("kill")
            raise SystemExit("cleanup")

        def read_result(self, maximum_bytes: int) -> bytes:
            del maximum_bytes
            pytest.fail("interrupted worker result must not be read")

        def close_result(self) -> None:
            self.events.append("close_result")
            raise SystemExit("close")

    process = DoubleInterruptedProcess()
    facade = AkShareIsolatedSdkFacade(
        launcher=lambda _operation, _kwargs: process,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        facade.stock_news_em(symbol="600000")

    assert captured.value is original
    assert process.events == ["communicate", "kill", "communicate", "close_result"]


def test_akshare_isolated_facade_reports_cleanup_failure_safely() -> None:
    class BrokenCleanupProcess:
        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            raise RuntimeError("TOP-SECRET")

        def kill(self) -> None:
            raise RuntimeError("TOP-SECRET")

        def read_result(self, maximum_bytes: int) -> bytes:
            del maximum_bytes
            pytest.fail("failed worker result must not be read")

        def close_result(self) -> None:
            return None

    facade = AkShareIsolatedSdkFacade(
        launcher=lambda _operation, _kwargs: BrokenCleanupProcess(),
    )

    with pytest.raises(ProviderUnavailable) as captured:
        facade.stock_news_em(symbol="600000")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOP-SECRET" not in exception_chain_text(captured.value)


def test_akshare_worker_emits_fixed_no_data_code_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class EmptyModule:
        def stock_news_em(self, **_kwargs: object) -> object:
            return []

    def noisy_import(name: str) -> object:
        print("TOP-SECRET")
        return EmptyModule() if name == "akshare" else None

    monkeypatch.setattr(_akshare_worker, "import_optional_sdk", noisy_import)
    result_path = tmp_path / "worker-result.json"

    result = _akshare_worker.main(
        ["stock_news_em", '{"symbol":"600000"}', str(result_path)]
    )

    assert result == 1
    assert capsys.readouterr().out == ""
    assert result_path.read_bytes() == b'{"status":"no_data"}'


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("typed_timeout", "timeout"),
        ("requests_timeout", "timeout"),
        ("unavailable", "provider_unavailable"),
    ],
)
def test_akshare_worker_preserves_safe_failure_status_without_raw_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    expected_status: str,
) -> None:
    class FailingModule:
        def stock_news_em(self, **_kwargs: object) -> object:
            if failure == "typed_timeout":
                raise ProviderTimeout("TOP-SECRET")
            if failure == "unavailable":
                raise ProviderUnavailable("TOP-SECRET")
            try:
                raise RuntimeError("TOP-SECRET")
            except RuntimeError:
                raise RequestsTimeout("TOP-SECRET")

    monkeypatch.setattr(
        _akshare_worker,
        "import_optional_sdk",
        lambda _name: FailingModule(),
    )
    result_path = tmp_path / f"{failure}.json"

    result = _akshare_worker.main(
        ["stock_news_em", '{"symbol":"600000"}', str(result_path)]
    )

    assert result == 1
    assert result_path.read_text(encoding="utf-8") == json.dumps(
        {"status": expected_status},
        separators=(",", ":"),
        sort_keys=True,
    )
    assert "TOP-SECRET" not in result_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        ("timeout", ProviderTimeout),
        ("provider_unavailable", ProviderUnavailable),
    ],
)
def test_akshare_isolated_facade_maps_safe_worker_failure_status(
    status: str,
    expected_error: type[ProviderClientError],
) -> None:
    class StatusProcess:
        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            del input, timeout
            return b"", b""

        def kill(self) -> None:
            pytest.fail("completed worker must not be killed")

        def read_result(self, maximum_bytes: int) -> bytes:
            assert maximum_bytes == 262_145
            return json.dumps(
                {"status": status},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

        def close_result(self) -> None:
            return None

    facade = AkShareIsolatedSdkFacade(
        launcher=lambda _operation, _kwargs: StatusProcess(),
    )

    with pytest.raises(expected_error) as captured:
        facade.stock_news_em(symbol="600000")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(130)])
def test_launch_worker_unlinks_temporary_result_when_popen_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interrupt: BaseException,
) -> None:
    result_path = tmp_path / "worker-result.json"

    class Temporary:
        name = str(result_path)

        def close(self) -> None:
            result_path.write_bytes(b"")

    monkeypatch.setattr(
        akshare_source_module.tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: Temporary(),
    )

    def interrupt_popen(*_args: object, **_kwargs: object) -> object:
        raise interrupt

    monkeypatch.setattr(akshare_source_module.subprocess, "Popen", interrupt_popen)

    with pytest.raises(type(interrupt)) as captured:
        akshare_source_module._launch_worker("stock_news_em", {"symbol": "600000"})

    assert captured.value is interrupt
    assert not result_path.exists()


def test_capability_model_rejects_market_as_a_network_research_category() -> None:
    with pytest.raises(ValidationError):
        ResearchSourceCapability(
            source=ProviderId.TUSHARE,
            categories=frozenset({ResearchSectionKind.MARKET}),
        )


def test_live_probe_is_network_disabled_without_explicit_opt_in() -> None:
    messages: list[str] = []

    result = live_probe_main(
        ("akshare", "news", SYMBOL),
        environ={},
        emit=messages.append,
    )

    assert result == 2
    assert messages == [
        "live probe disabled; set STOCK_DESK_RESEARCH_LIVE_PROBE=1 to allow network access"
    ]


def test_live_probe_rejects_unsupported_provider_category_before_sdk_loading() -> None:
    messages: list[str] = []

    result = live_probe_main(
        ("tushare", "news", SYMBOL),
        environ={"STOCK_DESK_RESEARCH_LIVE_PROBE": "1", "TUSHARE_TOKEN": "secret"},
        emit=messages.append,
    )

    assert result == 2
    assert messages == ["tushare does not support news"]


def test_live_probe_validates_symbol_before_sdk_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_sdk(cls: type[object], **_kwargs: object) -> object:
        pytest.fail("invalid symbol reached SDK initialization")

    monkeypatch.setattr(AkShareResearchSource, "from_sdk", classmethod(fail_sdk))
    messages: list[str] = []

    result = live_probe_main(
        ("akshare", "news", "not-a-symbol"),
        environ={"STOCK_DESK_RESEARCH_LIVE_PROBE": "1"},
        emit=messages.append,
    )

    assert result == 2
    assert messages == ["symbol is invalid"]
