from __future__ import annotations

from typing import Protocol, Self

from stock_desk.analysis.snapshot import ResearchSection, ResearchSectionKind
from stock_desk.analysis.sources.base import (
    clean_provider_error,
    Clock,
    research_section_from_table,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderTimeout,
    ProviderUnsupported,
)
from stock_desk.market.providers.sdk import (
    call_sdk,
    import_optional_sdk,
    is_sdk_timeout,
    required_sdk_callable,
)
from stock_desk.market.types import CanonicalSymbol, ProviderId


class AkShareResearchClient(Protocol):
    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object: ...

    def stock_individual_notice_report(self, **kwargs: object) -> object: ...

    def stock_news_em(self, **kwargs: object) -> object: ...


class AkShareResearchSdkFacade:
    """Minimal facade over the three symbol-scoped AKShare research APIs."""

    def __init__(self, *, module: object) -> None:
        self._module = module

    def _call(self, operation: str, **kwargs: object) -> object:
        safe_error: ProviderClientError | None = None
        try:
            return call_sdk(
                required_sdk_callable(self._module, operation),
                **kwargs,
            )
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception:
            safe_error = ProviderInvalidResponse()
        raise safe_error

    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object:
        return self._call(
            "stock_financial_analysis_indicator_em",
            **kwargs,
        )

    def stock_individual_notice_report(self, **kwargs: object) -> object:
        return self._call(
            "stock_individual_notice_report",
            **kwargs,
        )

    def stock_news_em(self, **kwargs: object) -> object:
        return self._call("stock_news_em", **kwargs)


class AkShareResearchSource:
    name = ProviderId.AKSHARE

    def __init__(self, *, client: AkShareResearchClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(cls, *, clock: Clock) -> Self:
        safe_error: ProviderClientError | None = None
        module: object | None = None
        try:
            module = import_optional_sdk("akshare")
            for operation in (
                "stock_financial_analysis_indicator_em",
                "stock_individual_notice_report",
                "stock_news_em",
            ):
                required_sdk_callable(module, operation)
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception:
            safe_error = ProviderInvalidResponse()
        if safe_error is not None:
            raise safe_error
        if module is None:
            raise ProviderInvalidResponse()
        return cls(client=AkShareResearchSdkFacade(module=module), clock=clock)

    def fetch(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> ResearchSection:
        code = symbol[:6]
        safe_error: ProviderClientError | None = None
        try:
            if kind is ResearchSectionKind.FUNDAMENTALS:
                table = self._client.stock_financial_analysis_indicator_em(
                    symbol=symbol,
                    indicator="按报告期",
                )
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    cutoff_fields=("REPORT_DATE",),
                    default_source_url=(
                        "https://emweb.securities.eastmoney.com/pc_hsf10/"
                        f"pages/index.html?type=web&code={symbol[-2:]}{code}#/cwfx"
                    ),
                )
            if kind is ResearchSectionKind.ANNOUNCEMENTS:
                table = self._client.stock_individual_notice_report(
                    security=code,
                    symbol="全部",
                )
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    cutoff_fields=("公告日期",),
                    published_fields=("公告日期",),
                    url_fields=("网址", "公告链接"),
                    default_source_url=f"https://data.eastmoney.com/notices/stock/{code}.html",
                )
            if kind is ResearchSectionKind.NEWS:
                table = self._client.stock_news_em(symbol=code)
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    cutoff_fields=("发布时间",),
                    published_fields=("发布时间",),
                    url_fields=("新闻链接",),
                    default_source_url=f"https://so.eastmoney.com/news/s?keyword={code}",
                )
            raise ProviderUnsupported()
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception as error:
            safe_error = (
                ProviderTimeout()
                if is_sdk_timeout(error)
                else ProviderInvalidResponse()
            )
        raise safe_error


__all__ = [
    "AkShareResearchClient",
    "AkShareResearchSdkFacade",
    "AkShareResearchSource",
]
