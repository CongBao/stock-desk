import os
from pathlib import Path
import subprocess
import sys
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_installed_wheel_can_upgrade_and_downgrade_from_foreign_cwd(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--out-dir",
            str(dist_dir),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert build.returncode == 0, build.stderr

    wheel_path = next(dist_dir.glob("stock_desk-*.whl"))
    installed_dir = tmp_path / "installed"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(installed_dir)

    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    foreign_cwd = tmp_path / "foreign" / "nested"
    foreign_cwd.mkdir(parents=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed_dir)
    migration = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from sqlalchemy import inspect
from stock_desk.storage.database import create_engine_for_url, downgrade, migrate

url = "sqlite:///wheel.db"
migrate(url)
engine = create_engine_for_url(url)
try:
    assert {
        "app_setting",
        "instrument_dataset",
        "instrument_dataset_item",
        "instrument_routing_manifest",
        "preset_pool_snapshot",
        "preset_pool_member",
        "custom_pool",
        "custom_pool_member",
        "formula",
        "formula_draft",
        "formula_version",
        "execution_status_dataset",
        "execution_status_routing_manifest",
        "market_dataset",
        "market_dataset_partition",
        "market_dataset_timestamp",
        "market_dataset_timestamp_seal",
        "market_routing_manifest",
        "market_update_item",
        "market_update_occurrence",
        "market_update_schedule",
        "task_event",
        "task_run",
        "analysis_run",
        "analysis_stage",
        "analysis_attempt",
        "analysis_report",
        "analysis_model_config",
    } <= set(inspect(engine).get_table_names())
finally:
    engine.dispose()
downgrade(url, "base")
engine = create_engine_for_url(url)
try:
    assert {
        "app_setting",
        "instrument_dataset",
        "instrument_dataset_item",
        "instrument_routing_manifest",
        "preset_pool_snapshot",
        "preset_pool_member",
        "custom_pool",
        "custom_pool_member",
        "formula",
        "formula_draft",
        "formula_version",
        "execution_status_dataset",
        "execution_status_routing_manifest",
        "market_dataset",
        "market_dataset_partition",
        "market_dataset_timestamp",
        "market_dataset_timestamp_seal",
        "market_routing_manifest",
        "market_update_item",
        "market_update_occurrence",
        "market_update_schedule",
        "task_event",
        "task_run",
    }.isdisjoint(inspect(engine).get_table_names())
finally:
    engine.dispose()
""",
        ],
        cwd=foreign_cwd,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert migration.returncode == 0, migration.stderr

    packaged_files = {
        path.relative_to(installed_dir).as_posix()
        for path in installed_dir.rglob("*")
        if path.is_file()
    }
    assert "stock_desk/alembic.ini" in packaged_files
    assert "stock_desk/migrations/env.py" in packaged_files
    assert "stock_desk/migrations/versions/0001_core_tables.py" in packaged_files
    assert "stock_desk/migrations/versions/0002_task_observability.py" in packaged_files
    assert "stock_desk/migrations/versions/0003_market_catalog.py" in packaged_files
    assert (
        "stock_desk/migrations/versions/0004_instruments_and_pools.py" in packaged_files
    )
    assert "stock_desk/migrations/versions/0005_formula_catalog.py" in packaged_files
    assert "stock_desk/migrations/versions/0006_execution_status.py" in packaged_files
    assert "stock_desk/migrations/versions/0007_backtest_runs.py" in packaged_files
    assert "stock_desk/migrations/versions/0008_analysis_runs.py" in packaged_files
    assert (
        "stock_desk/migrations/versions/0009_analysis_model_configs.py"
        in packaged_files
    )
    assert (
        "stock_desk/migrations/versions/0010_parent_active_retry.py" in packaged_files
    )
