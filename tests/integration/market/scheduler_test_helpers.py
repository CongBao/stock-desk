from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from stock_desk.market.scheduler import MarketUpdateScheduleRepository
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository


def valid_update_payload(
    *,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "symbols": symbols if symbols is not None else ["600000.SH"],
        "period": "1d",
        "adjustment": "none",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-02-01T00:00:00Z",
    }


def scheduler_database(
    tmp_path: Path,
    name: str = "scheduler.db",
) -> tuple[str, Engine, MarketUpdateScheduleRepository, TaskRepository]:
    url = f"sqlite:///{tmp_path / name}"
    migrate(url)
    engine = create_engine_for_url(url)
    return url, engine, MarketUpdateScheduleRepository(engine), TaskRepository(engine)


FIXED_NOW = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
