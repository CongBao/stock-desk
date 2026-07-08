from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.worker_runtime import ProductionMarketWorker
from tests.acceptance.test_market_flow import (
    FixtureCompositionProvider,
    FixtureProviderFactory,
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_module_inventory_and_heavy_work_use_independent_worker(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "stock_desk"
    capability_packages = {
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and not path.name.startswith("__") and any(path.glob("*.py"))
    }
    assert capability_packages == {
        "analysis",
        "api",
        "backtest",
        "formula",
        "market",
        "security",
        "storage",
        "tasks",
    }

    forbidden_api_imports = {
        "stock_desk.analysis.runner",
        "stock_desk.analysis.worker",
        "stock_desk.backtest.pool_runner",
        "stock_desk.market.worker_runtime",
    }
    for api_module in (source_root / "api").glob("*.py"):
        assert not (_imports(api_module) & forbidden_api_imports), api_module.name

    database_url = f"sqlite:///{tmp_path / 'architecture.db'}"
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    runtime = ProductionMarketWorker.open(
        settings,
        worker_id="architecture-contract-worker",
        provider_factory=FixtureProviderFactory(),
        composition_factory=FixtureCompositionProvider,
    )
    try:
        assert runtime.worker.registered_claimed_kinds == (
            "analysis.run",
            "backtest.run",
        )
        with TestClient(create_app(settings)) as client:
            queued = client.post("/api/market/catalog/updates")
            assert queued.status_code == 201
            assert queued.json()["status"] == "queued"
            assert runtime.tasks.get(queued.json()["id"]).status == "queued"

            completed = runtime.run_once()
            assert completed is not None
            assert completed.id == queued.json()["id"]
            assert completed.kind == "market.catalog.update"
            assert completed.status == "succeeded"
            assert client.get(f"/api/tasks/{completed.id}").json()["status"] == (
                "succeeded"
            )
    finally:
        runtime.close()
