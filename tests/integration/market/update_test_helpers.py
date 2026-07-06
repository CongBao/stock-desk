from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from stock_desk.market.lake import MarketLake
from stock_desk.market.provenance import (
    RoutedBarFailure,
    RoutedBarSuccess,
    RoutingManifest,
)
from stock_desk.market.types import BarQuery
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository


@dataclass(frozen=True, slots=True)
class UpdateHarness:
    engine: Engine
    tasks: TaskRepository
    lake: MarketLake


@contextmanager
def open_update_harness(tmp_path: Path) -> Iterator[UpdateHarness]:
    url = f"sqlite:///{tmp_path / 'market-update.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield UpdateHarness(
            engine=engine,
            tasks=TaskRepository(engine),
            lake=MarketLake(engine=engine, root=tmp_path / "market"),
        )
    finally:
        engine.dispose()


def update_payload(*symbols: str) -> dict[str, Any]:
    return {
        "symbols": list(symbols),
        "period": "1d",
        "adjustment": "qfq",
        "start": "2024-01-01T16:00:00Z",
        "end": "2024-01-02T16:00:00Z",
    }


class SpyRouter:
    def __init__(
        self,
        outcomes: dict[str, RoutedBarSuccess | RoutedBarFailure],
    ) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[BarQuery, RoutingManifest | None]] = []

    def fetch_bars(
        self,
        query: BarQuery,
        *,
        previous_manifest: RoutingManifest | None = None,
    ) -> RoutedBarSuccess | RoutedBarFailure:
        self.calls.append((query, previous_manifest))
        return self._outcomes[query.symbol]
