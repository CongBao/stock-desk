from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.storage.database import create_engine_for_url, downgrade, migrate


NOW = "2026-07-07 09:00:00.000000"


def _config() -> tuple[str, str]:
    value = AnalysisModelPublicConfig(
        provider=ModelProviderKind.OLLAMA,
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
    )
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return payload, f"sha256:{hashlib.sha256(payload.encode('ascii')).hexdigest()}"


def _values() -> dict[str, object]:
    payload, digest = _config()
    return {
        "id": digest,
        "display_name": "Local model",
        "provider": "ollama",
        "model": "qwen3:8b",
        "public_config_json": payload,
        "public_config_hash": digest,
        "secret_reference_id": None,
        "supersedes_id": None,
        "status": "unverified",
        "revision": 0,
        "verified_at": None,
        "last_tested_at": None,
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


INSERT = """
INSERT INTO analysis_model_config (
    id, display_name, provider, model, public_config_json, public_config_hash,
    secret_reference_id, supersedes_id, status, revision, verified_at,
    last_tested_at, error_code, created_at, updated_at
) VALUES (
    :id, :display_name, :provider, :model, :public_config_json, :public_config_hash,
    :secret_reference_id, :supersedes_id, :status, :revision, :verified_at,
    :last_tested_at, :error_code, :created_at, :updated_at
)
"""


def test_upgrade_downgrade_and_exact_table_shape(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'shape.db'}"
    migrate(url, "0008_analysis_runs")
    engine = create_engine_for_url(url)
    try:
        assert "analysis_model_config" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("analysis_model_config")[
            "constrained_columns"
        ] == ["id"]
        assert {
            column["name"] for column in inspector.get_columns("analysis_model_config")
        } == {
            "id",
            "display_name",
            "provider",
            "model",
            "public_config_json",
            "public_config_hash",
            "secret_reference_id",
            "supersedes_id",
            "status",
            "revision",
            "verified_at",
            "last_tested_at",
            "error_code",
            "created_at",
            "updated_at",
        }
    finally:
        engine.dispose()

    downgrade(url, "0008_analysis_runs")
    engine = create_engine_for_url(url)
    try:
        assert "analysis_model_config" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert "analysis_model_config" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("id", "sha256:" + "A" * 64),
        ("public_config_hash", "sha256:" + "b" * 64),
        ("public_config_json", "[]"),
        ("public_config_json", "{}"),
        ("provider", "deepseek"),
        ("model", "different-model"),
        ("secret_reference_id", "analysis_model_api_key_UPPER"),
        ("status", "active"),
        ("revision", 1),
        ("error_code", "Bad-Code"),
    ],
)
def test_database_rejects_invalid_hash_json_secret_state_and_error_shapes(
    tmp_path: Path, column: str, value: object
) -> None:
    url = f"sqlite:///{tmp_path / f'invalid-{column}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    values[column] = value
    if column == "error_code":
        values.update(status="failed", last_tested_at=NOW)
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(INSERT), values)
    finally:
        engine.dispose()


def test_state_shape_foreign_key_and_immutable_execution_fields_are_enforced(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'constraints.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    with engine.begin() as connection:
        connection.execute(text(INSERT), values)

    bad_verified = {
        **values,
        "id": "sha256:" + "b" * 64,
        "public_config_hash": "sha256:" + "b" * 64,
        "status": "verified",
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(INSERT), bad_verified)
    bad_successor = {
        **values,
        "id": "sha256:" + "c" * 64,
        "public_config_hash": "sha256:" + "c" * 64,
        "supersedes_id": "sha256:" + "d" * 64,
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(INSERT), bad_successor)

    immutable = {
        "id": "sha256:" + "e" * 64,
        "provider": "deepseek",
        "model": "another-model",
        "public_config_json": "{}",
        "public_config_hash": "sha256:" + "e" * 64,
        "secret_reference_id": "analysis_model_api_key",
        "supersedes_id": values["id"],
        "created_at": datetime(2026, 7, 8, tzinfo=timezone.utc).isoformat(),
    }
    for column, replacement in immutable.items():
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE analysis_model_config SET {column}=:replacement "
                    "WHERE id=:id"
                ),
                {"id": values["id"], "replacement": replacement},
            )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR REPLACE INTO analysis_model_config ("
                "id, display_name, provider, model, public_config_json, "
                "public_config_hash, status, revision, created_at, updated_at) VALUES "
                "(:id, 'replace', 'ollama', 'qwen3:8b', :json, :id, "
                "'unverified', 0, :now, :now)"
            ),
            {"id": values["id"], "json": values["public_config_json"], "now": NOW},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("DELETE FROM analysis_model_config WHERE id=:id"),
            {"id": values["id"]},
        )
    engine.dispose()


def test_public_config_json_requires_provider_model_and_runtime_fields(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'public-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        for missing in (
            "provider",
            "model",
            "temperature",
            "timeout_seconds",
            "max_output_tokens",
        ):
            values = _values()
            payload = json.loads(str(values["public_config_json"]))
            del payload[missing]
            values["public_config_json"] = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            )
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(INSERT), values)
    finally:
        engine.dispose()


@pytest.mark.parametrize("codepoint", [*range(32), 127])
def test_database_rejects_every_c0_and_del_in_display_name(
    tmp_path: Path, codepoint: int
) -> None:
    url = f"sqlite:///{tmp_path / f'control-{codepoint}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    values["display_name"] = f"bad{chr(codepoint)}name"
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(INSERT), values)
    finally:
        engine.dispose()


def test_revision_updates_are_monotonic_audited_and_disabled_is_terminal(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'revision-guard.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    with engine.begin() as connection:
        connection.execute(text(INSERT), values)

    invalid_updates = (
        "UPDATE analysis_model_config SET display_name='renamed', revision=0 "
        "WHERE id=:id",
        "UPDATE analysis_model_config SET revision=1 WHERE id=:id",
        "UPDATE analysis_model_config SET revision=1, "
        "updated_at='2026-07-08T09:00:00+00:00' WHERE id=:id",
        "UPDATE analysis_model_config SET display_name='renamed', revision=2 "
        "WHERE id=:id",
    )
    for statement in invalid_updates:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement), {"id": values["id"]})

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_model_config SET display_name='renamed', "
                "updated_at='2026-07-07 09:00:00.000001', "
                "revision=1 WHERE id=:id"
            ),
            {"id": values["id"]},
        )
        connection.execute(
            text(
                "UPDATE analysis_model_config SET status='disabled', "
                "updated_at='2026-07-07 09:00:00.000002', "
                "revision=2 WHERE id=:id"
            ),
            {"id": values["id"]},
        )

    terminal_updates = (
        "UPDATE analysis_model_config SET status='unverified', revision=3 WHERE id=:id",
        "UPDATE analysis_model_config SET display_name='again', revision=3 "
        "WHERE id=:id",
    )
    for statement in terminal_updates:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement), {"id": values["id"]})
    engine.dispose()


def test_verified_row_lock_update_is_exact_noop_while_mutations_remain_audited(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'verified-row-lock.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    with engine.begin() as connection:
        connection.execute(text(INSERT), values)
        connection.execute(
            text(
                "UPDATE analysis_model_config SET status='verified', revision=1, "
                "verified_at='2026-07-07 09:00:00.000001', "
                "last_tested_at='2026-07-07 09:00:00.000001', "
                "updated_at='2026-07-07 09:00:00.000001' WHERE id=:id"
            ),
            {"id": values["id"]},
        )
        before = connection.execute(
            text("SELECT * FROM analysis_model_config WHERE id=:id"),
            {"id": values["id"]},
        ).one()
        result = connection.execute(
            text(
                "UPDATE analysis_model_config SET revision=revision "
                "WHERE id=:id AND status='verified'"
            ),
            {"id": values["id"]},
        )
        after = connection.execute(
            text("SELECT * FROM analysis_model_config WHERE id=:id"),
            {"id": values["id"]},
        ).one()

    assert result.rowcount == 1
    assert after == before
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_model_config SET display_name='partial', "
                "revision=revision WHERE id=:id"
            ),
            {"id": values["id"]},
        )
    engine.dispose()


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-07-07T09:00:00+00:00",
        "2026-07-07 17:00:00+08:00",
        "2026-07-07 09:00:00",
        "0000-01-01 00:00:00.000000",
        "2026-13-01 00:00:00.000000",
        "2026-07-07 24:00:00.000000",
        "2026-07-07 23:60:00.000000",
        "2026-07-07 23:59:60.000000",
        "2026-02-30 09:00:00.000000",
        "2025-02-29 09:00:00.000000",
        "not-a-timestamp",
    ],
)
def test_database_rejects_noncanonical_or_invalid_timestamp_formats(
    tmp_path: Path, invalid_timestamp: str
) -> None:
    url = f"sqlite:///{tmp_path / 'invalid-time.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    values.update(created_at=invalid_timestamp, updated_at=invalid_timestamp)
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(INSERT), values)
    finally:
        engine.dispose()


def test_database_accepts_valid_gregorian_leap_day(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'leap-day.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    values.update(
        created_at="2024-02-29 23:59:59.999999",
        updated_at="2024-02-29 23:59:59.999999",
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(INSERT), values)
    finally:
        engine.dispose()


def test_database_rejects_timestamp_order_and_raw_t_offset_updates(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'time-order.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    values = _values()
    earlier_update = {**values, "updated_at": "2026-07-07 08:59:59.999999"}
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(INSERT), earlier_update)

    with engine.begin() as connection:
        connection.execute(text(INSERT), values)
    for raw in ("2026-07-08T09:00:00+00:00", "2026-07-08 17:00:00+08:00"):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE analysis_model_config SET display_name='renamed', "
                    "revision=1, updated_at=:raw WHERE id=:id"
                ),
                {"id": values["id"], "raw": raw},
            )
    engine.dispose()
