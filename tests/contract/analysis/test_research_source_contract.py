from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import cast

from pydantic import ValidationError
import pytest
from requests.exceptions import Timeout as RequestsTimeout

from scripts.research_source_probe import main as live_probe_main
import stock_desk.analysis.sources.tushare as tushare_source_module
from stock_desk.analysis.data_service import ResearchDataUnavailable
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.analysis.sources.akshare import (
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
    ProviderInvalidResponse,
    ProviderNoData,
    ProviderPermissionDenied,
    ProviderTimeout,
)
from stock_desk.market.types import ProviderId


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
