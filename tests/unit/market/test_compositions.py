from __future__ import annotations

from datetime import datetime, timezone

from stock_desk.market.compositions import AkShareCompositionProvider
from stock_desk.market.pools import PoolCategory
from stock_desk.market.types import FailureReason, ProviderId


class FixtureClient:
    def index_constituents(self, symbol: str) -> object:
        ranges = {
            "000300": range(1, 301),
            "000016": range(1, 51),
            "000905": range(301, 801),
        }
        return tuple(
            {
                "成分券代码": f"{index:06d}",
                "成分券名称": f"证券{index}",
                "日期": "2026-07-05",
            }
            for index in ranges[symbol]
        )

    def industry_names(self) -> object:
        return ({"板块名称": "银行"},)

    def industry_constituents(self, symbol: str) -> object:
        assert symbol == "银行"
        return (
            {"代码": "000001", "名称": "平安银行"},
            {"代码": "600000", "名称": "浦发银行"},
        )


def test_akshare_normalizes_current_index_and_industry_compositions() -> None:
    now = datetime(2026, 7, 6, 8, tzinfo=timezone.utc)
    provider = AkShareCompositionProvider(client=FixtureClient(), clock=lambda: now)

    known = frozenset({*(f"{index:06d}.SZ" for index in range(1, 801)), "600000.SH"})
    result = provider.fetch_presets(known)

    assert result.failures == ()
    assert [item.preset_key for item in result.compositions[:3]] == [
        "index-csi300",
        "index-sse50",
        "index-csi500",
    ]
    assert result.compositions[-1].category is PoolCategory.INDUSTRY
    assert all(item.source is ProviderId.AKSHARE for item in result.compositions)
    assert all(item.fetched_at == now for item in result.compositions)
    assert len(result.compositions[0].symbols) == 300
    assert result.compositions[0].data_cutoff == datetime(
        2026, 7, 5, tzinfo=timezone.utc
    )


class PartialFailureClient(FixtureClient):
    def industry_constituents(self, symbol: str) -> object:
        raise TimeoutError("secret path must not escape")


def test_composition_refresh_itemizes_partial_failure_without_dropping_index() -> None:
    provider = AkShareCompositionProvider(
        client=PartialFailureClient(),
        clock=lambda: datetime(2026, 7, 6, 8, tzinfo=timezone.utc),
    )

    known = frozenset({*(f"{index:06d}.SZ" for index in range(1, 801)), "600000.SH"})
    result = provider.fetch_presets(known)

    assert len(result.compositions) == 3
    assert len(result.failures) == 1
    assert result.failures[0].preset_key == "industry-bank"
    assert result.failures[0].reason is FailureReason.TIMEOUT
    assert "secret" not in repr(result.failures[0])
