from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _catalog_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return tuple(
            int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in (
                "market_dataset",
                "market_dataset_partition",
                "market_routing_manifest",
            )
        )


@pytest.mark.parametrize("replacement_kind", ["corrupt", "valid-copy"])
def test_write_revalidates_published_binding_immediately_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        relative = lake._partition_relative_path(routed, 2024)
        target = root / relative
        original_parent = target.parent
        replacement_parent = tmp_path / f"replacement-{replacement_kind}"
        replacement_parent.mkdir(mode=0o700)
        replacement_target = replacement_parent / target.name
        displaced_parent = tmp_path / f"displaced-{replacement_kind}"
        original_verify = lake._verify_operation_context
        replacement_bytes: list[bytes] = []
        swapped = False

        def swap_parent_then_verify(context: object) -> None:
            nonlocal swapped
            if not swapped:
                if replacement_kind == "valid-copy":
                    shutil.copyfile(target, replacement_target)
                else:
                    replacement_target.write_bytes(b"replacement partition")
                replacement_target.chmod(0o600)
                replacement_bytes.append(replacement_target.read_bytes())
                os.replace(original_parent, displaced_parent)
                os.replace(replacement_parent, original_parent)
                swapped = True
            original_verify(context)  # type: ignore[arg-type]

        monkeypatch.setattr(
            lake,
            "_verify_operation_context",
            swap_parent_then_verify,
        )

        with pytest.raises(MarketLakeCorruptionError, match="partition"):
            lake.write(routed)

        assert swapped
        assert _catalog_counts(engine) == (0, 0, 0)
        assert target.read_bytes() == replacement_bytes[0]
        assert not (displaced_parent / target.name).exists()


def test_write_revalidates_binding_after_catalog_sql_before_transaction_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        relative = lake._partition_relative_path(routed, 2024)
        target = root / relative
        original_parent = target.parent
        replacement_parent = tmp_path / "transaction-replacement"
        replacement_parent.mkdir(mode=0o700)
        replacement_target = replacement_parent / target.name
        displaced_parent = tmp_path / "transaction-displaced"
        swapped = False

        def swap_after_manifest_insert(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal swapped
            if not swapped and statement.startswith(
                "INSERT INTO market_routing_manifest"
            ):
                replacement_target.write_bytes(b"transaction replacement")
                replacement_target.chmod(0o600)
                os.replace(original_parent, displaced_parent)
                os.replace(replacement_parent, original_parent)
                swapped = True

        event.listen(engine, "after_cursor_execute", swap_after_manifest_insert)
        try:
            with pytest.raises(MarketLakeCorruptionError, match="partition"):
                lake.write(routed)
        finally:
            event.remove(engine, "after_cursor_execute", swap_after_manifest_insert)

        assert swapped
        assert _catalog_counts(engine) == (0, 0, 0)
        assert target.read_bytes() == b"transaction replacement"
        assert not (displaced_parent / target.name).exists()
