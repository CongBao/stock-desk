from __future__ import annotations

from collections.abc import Callable
import re
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
    ProviderPermissionDenied,
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


class TushareResearchClient(Protocol):
    def income(self, **kwargs: object) -> object: ...

    def anns_d(self, **kwargs: object) -> object: ...


_SENSITIVE_ASSIGNMENT = re.compile(r"(?i)\b(token|api[_-]?key)\s*([:=])\s*[^\s,;]+")
_PERMISSION_MARKERS = (
    "没有访问该接口的权限",
    "无权访问该接口",
    "权限不足",
    "token无效",
    "invalid token",
    "permission denied",
    "insufficient permission",
)


def _default_permission_classifier(error: Exception) -> bool:
    try:
        raw = str(error)[:2_048]
    except Exception:
        return False
    redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", raw).casefold()
    return any(marker in redacted for marker in _PERMISSION_MARKERS)


class TushareResearchSdkFacade:
    """Minimal facade over the symbol-scoped Tushare Pro research APIs."""

    def __init__(
        self,
        *,
        pro: object,
        permission_classifier: Callable[[Exception], bool] | None = None,
    ) -> None:
        self._pro = pro
        self._permission_classifier = (
            permission_classifier
            if permission_classifier is not None
            else _default_permission_classifier
        )

    def _call(self, operation: str, **kwargs: object) -> object:
        safe_error: ProviderClientError | None = None
        try:
            return call_sdk(
                required_sdk_callable(self._pro, operation),
                **kwargs,
            )
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception as error:
            try:
                permission_denied = self._permission_classifier(error)
            except Exception:
                permission_denied = False
            safe_error = (
                ProviderPermissionDenied()
                if permission_denied
                else ProviderInvalidResponse()
            )
        raise safe_error

    def income(self, **kwargs: object) -> object:
        return self._call("income", **kwargs)

    def anns_d(self, **kwargs: object) -> object:
        return self._call("anns_d", **kwargs)


class TushareResearchSource:
    name = ProviderId.TUSHARE

    def __init__(self, *, client: TushareResearchClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(
        cls,
        *,
        token: str,
        clock: Clock,
        permission_classifier: Callable[[Exception], bool] | None = None,
    ) -> Self:
        if not isinstance(token, str) or not token:
            raise ProviderPermissionDenied()
        safe_error: ProviderClientError | None = None
        pro: object | None = None
        try:
            module = import_optional_sdk("tushare")
            pro_api = required_sdk_callable(module, "pro_api")
            pro = call_sdk(pro_api, token)
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception:
            safe_error = ProviderPermissionDenied()
        if safe_error is not None:
            raise safe_error
        if pro is None:
            raise ProviderPermissionDenied()
        return cls(
            client=TushareResearchSdkFacade(
                pro=pro,
                permission_classifier=permission_classifier,
            ),
            clock=clock,
        )

    def fetch(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> ResearchSection:
        safe_error: ProviderClientError | None = None
        try:
            if kind is ResearchSectionKind.FUNDAMENTALS:
                table = self._client.income(ts_code=symbol)
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    cutoff_fields=("ann_date", "f_ann_date", "end_date"),
                    default_source_url="https://tushare.pro/document/2?doc_id=33",
                )
            if kind is ResearchSectionKind.ANNOUNCEMENTS:
                table = self._client.anns_d(ts_code=symbol)
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    cutoff_fields=("ann_date",),
                    published_fields=("ann_date",),
                    url_fields=("url",),
                    default_source_url="https://tushare.pro/document/2?doc_id=176",
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
    "TushareResearchClient",
    "TushareResearchSdkFacade",
    "TushareResearchSource",
]
