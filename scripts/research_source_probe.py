"""Explicitly opt-in live probe for research source adapters."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import os

from stock_desk.analysis.snapshot import ResearchSectionKind
from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.base import ResearchSourceAdapter
from stock_desk.analysis.sources.tushare import TushareResearchSource
from stock_desk.market.types import ProviderId


_OPT_IN = "STOCK_DESK_RESEARCH_LIVE_PROBE"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("tushare", "akshare"))
    parser.add_argument(
        "category",
        choices=("fundamentals", "announcements", "news"),
    )
    parser.add_argument("symbol", help="canonical A-share symbol, e.g. 600000.SH")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    if environment.get(_OPT_IN) != "1":
        emit(f"live probe disabled; set {_OPT_IN}=1 to allow network access")
        return 2

    def clock() -> datetime:
        return datetime.now(timezone.utc)

    provider = ProviderId(args.provider)
    kind = ResearchSectionKind(args.category)
    if provider is ProviderId.TUSHARE and kind is ResearchSectionKind.NEWS:
        emit("tushare does not support news")
        return 2
    source: ResearchSourceAdapter
    if provider is ProviderId.TUSHARE:
        token = environment.get("TUSHARE_TOKEN")
        if token is None:
            emit("TUSHARE_TOKEN is required")
            return 2
        source = TushareResearchSource.from_sdk(token=token, clock=clock)
    else:
        source = AkShareResearchSource.from_sdk(clock=clock)
    section = source.fetch(args.symbol, kind)
    items = section.content.get("items")
    if not isinstance(items, list):
        raise RuntimeError("research source returned invalid content")
    emit(
        json.dumps(
            {
                "source": section.canonical_source,
                "category": section.kind.value,
                "items": len(items),
                "dataset_version": section.dataset_version,
                "fetched_at": section.fetched_at.isoformat(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
