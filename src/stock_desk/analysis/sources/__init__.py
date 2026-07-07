"""Traceable, capability-aware research data sources."""

from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.base import (
    RESEARCH_SOURCE_CATEGORIES,
    ResearchSourceAdapter,
    ResearchSourceCapability,
)
from stock_desk.analysis.sources.market_cache import MarketCacheLoader
from stock_desk.analysis.sources.routing import (
    RESEARCH_SOURCE_CAPABILITIES,
    ResearchSourceRouter,
)
from stock_desk.analysis.sources.tushare import TushareResearchSource


__all__ = [
    "AkShareResearchSource",
    "MarketCacheLoader",
    "RESEARCH_SOURCE_CAPABILITIES",
    "RESEARCH_SOURCE_CATEGORIES",
    "ResearchSourceAdapter",
    "ResearchSourceCapability",
    "ResearchSourceRouter",
    "TushareResearchSource",
]
