from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx2
from pydantic import SecretStr
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.model_catalog import AnalysisModelCatalog
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    MODEL_API_KEY_SECRET_NAME,
    ModelConfigUpdate,
    ModelProviderKind,
)
from stock_desk.analysis.model_settings import (
    ModelProviderFactory,
    ModelSettingsService,
)
from stock_desk.api.tasks import TaskEventResponse, TaskResponse
from stock_desk.api.settings import SourceSettingsServices, TushareSourceUpdateRequest
from stock_desk.backtest.models import (
    BacktestFailureRow,
    BacktestGroupMetricRow,
    BacktestLogRow,
    BacktestRunRow,
    BacktestSymbolRow,
    BacktestTradeRow,
)
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.security.redaction import scoped_log_redaction
from stock_desk.security.secrets import SecretStore
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import TaskEvent
from stock_desk.tasks.repository import TaskRepository
from tests.integration.backtest.test_export import (
    FINISHED,
    RUN_ID,
    _bytes,
    _completed_repository,
)


ACTIVE_SECRET = "active-provider-secret-value"
ROTATED_SECRET = "rotated-provider-secret-value"
MODEL_ACTIVE_SECRET = "configured-model-active-secret-value"
MODEL_ROTATED_SECRET = "configured-model-rotated-secret-value"
MARKET_ACTIVE_SECRET = "configured-market-active-secret-value"
MARKET_ROTATED_SECRET = "configured-market-rotated-secret-value"


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'secret-surfaces.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def test_active_and_rotated_secret_values_are_redacted_before_task_persistence(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        with scoped_log_redaction(ACTIVE_SECRET, ROTATED_SECRET):
            succeeded = repository.create(
                "secret.surface",
                {"message": f"payload {ACTIVE_SECRET}", "ordinary": "kept"},
            )
            assert repository.claim_next("worker-success") is not None
            repository.set_progress(
                succeeded.id,
                0.5,
                {"message": f"event {ROTATED_SECRET}", "ordinary": "kept"},
            )
            repository.complete(
                succeeded.id,
                {"message": f"result {ACTIVE_SECRET}", "ordinary": "kept"},
            )

            failed = repository.create("secret.failure", {})
            assert repository.claim_next("worker-failure") is not None
            repository.fail(
                failed.id,
                {"message": f"error {ROTATED_SECRET}", "ordinary": "kept"},
            )

        stored = repr(
            (
                repository.get(succeeded.id),
                repository.list_events(succeeded.id),
                repository.get(failed.id),
            )
        )
        assert ACTIVE_SECRET not in stored
        assert ROTATED_SECRET not in stored
        assert "ordinary" in stored
        assert "kept" in stored
    finally:
        repository.close()


def test_active_secret_is_redacted_again_at_legacy_task_response_boundaries(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        task = repository.create("legacy.surface", {"ordinary": "kept"})
        event = repository.list_events(task.id)[0]
        dirty_task = replace(
            task,
            payload={"message": f"legacy {ACTIVE_SECRET}", "ordinary": "kept"},
        )
        dirty_event = replace(
            event,
            detail={"message": f"legacy {ACTIVE_SECRET}", "ordinary": "kept"},
        )

        with scoped_log_redaction(ACTIVE_SECRET):
            task_json = TaskResponse.from_snapshot(dirty_task).model_dump_json()
            event_json = TaskEventResponse.from_snapshot(dirty_event).model_dump_json()

        assert ACTIVE_SECRET not in task_json
        assert ACTIVE_SECRET not in event_json
        assert "ordinary" in task_json
        assert "ordinary" in event_json
    finally:
        repository.close()


def test_active_secret_is_redacted_from_every_backtest_export_format(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path, complete=False)
    with repository._engine.begin() as connection:
        connection.execute(
            update(BacktestLogRow)
            .where(BacktestLogRow.run_id == RUN_ID)
            .values(
                message=f"completed {MODEL_ACTIVE_SECRET}",
                detail_json={
                    "status": f"succeeded {MODEL_ROTATED_SECRET}",
                    "symbol": "600000.SH",
                    "reason": "ordinary configured-model-active",
                },
            )
        )
        connection.execute(
            update(BacktestSymbolRow)
            .where(BacktestSymbolRow.run_id == RUN_ID)
            .values(status="succeeded")
        )
        connection.execute(
            update(BacktestRunRow)
            .where(BacktestRunRow.id == RUN_ID)
            .values(
                status="succeeded",
                stage="completed",
                processed=1,
                finished_at=FINISHED,
                updated_at=FINISHED,
            )
        )

    catalog = AnalysisModelCatalog(repository._engine)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=SecretStore(
            repository._engine,
            Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
        ),
    )
    try:
        parent = service.create(
            "Export remote v1",
            ModelConfigUpdate(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://models.example.com/v1",
                model="export-model-v1",
                api_key=SecretStr(MODEL_ACTIVE_SECRET),
            ),
        )
        service.create_successor(
            parent.id,
            "Export remote v2",
            ModelConfigUpdate(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://models.example.com/v1",
                model="export-model-v2",
                api_key=SecretStr(MODEL_ROTATED_SECRET),
            ),
        )
        payloads = [
            _bytes(repository, section, format_)
            for section in ("trades", "open", "groups", "failures", "logs")
            for format_ in ("json", "csv")
        ]
    finally:
        service.close()
        catalog.close()

    assert all(MODEL_ACTIVE_SECRET.encode() not in payload for payload in payloads)
    assert all(MODEL_ROTATED_SECRET.encode() not in payload for payload in payloads)
    assert any(b"ordinary C: relative text" in payload for payload in payloads)


def test_configured_and_rotated_model_keys_register_for_all_process_boundaries(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'configured-model-secret.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    catalog = AnalysisModelCatalog(engine)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=SecretStore(
            engine,
            Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
        ),
    )
    tasks = TaskRepository(engine)
    try:
        legacy = tasks.create(
            "legacy.configured.secret",
            {
                "active": MODEL_ACTIVE_SECRET,
                "ordinary": "ordinary configured-model-active",
            },
        )
        parent = service.create(
            "Remote v1",
            ModelConfigUpdate(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://models.example.com/v1",
                model="vendor-chat-v1",
                api_key=SecretStr(MODEL_ACTIVE_SECRET),
            ),
        )
        service.create_successor(
            parent.id,
            "Remote v2",
            ModelConfigUpdate(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://models.example.com/v1",
                model="vendor-chat-v2",
                api_key=SecretStr(MODEL_ROTATED_SECRET),
            ),
        )

        task = tasks.create(
            "configured.secret.surface",
            {
                "active": MODEL_ACTIVE_SECRET,
                "rotated": MODEL_ROTATED_SECRET,
                "ordinary": "kept",
            },
        )
        rendered = repr(task)
        legacy_response = TaskResponse.from_snapshot(
            tasks.get(legacy.id)
        ).model_dump_json()

        assert MODEL_ACTIVE_SECRET not in rendered
        assert MODEL_ROTATED_SECRET not in rendered
        assert MODEL_ACTIVE_SECRET not in legacy_response
        assert "ordinary" in rendered
        assert "kept" in rendered
        assert "ordinary configured-model-active" in legacy_response
    finally:
        service.close()
        catalog.close()
        engine.dispose()


def test_market_token_never_leaves_masked_state_across_legacy_and_new_tasks(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'configured-market-secret.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    settings = Settings(
        database_url=url,
        data_dir=tmp_path,
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    service = SourceSettingsServices(engine=engine, settings=settings)
    tasks = TaskRepository(engine)
    try:
        legacy = tasks.create(
            "legacy.market.secret",
            {
                "token": MARKET_ACTIVE_SECRET,
                "ordinary": "ordinary configured-market-active",
            },
        )
        service.update_tushare(
            TushareSourceUpdateRequest(token=SecretStr(MARKET_ACTIVE_SECRET))
        )
        service.update_tushare(
            TushareSourceUpdateRequest(token=SecretStr(MARKET_ROTATED_SECRET))
        )

        new = tasks.create(
            "new.market.secret",
            {
                "active": MARKET_ACTIVE_SECRET,
                "rotated": MARKET_ROTATED_SECRET,
                "ordinary": "kept",
            },
        )
        legacy_response = TaskResponse.from_snapshot(
            tasks.get(legacy.id)
        ).model_dump_json()
        rendered = repr(new)

        assert MARKET_ACTIVE_SECRET not in legacy_response
        assert MARKET_ACTIVE_SECRET not in rendered
        assert MARKET_ROTATED_SECRET not in rendered
        assert "ordinary configured-market-active" in legacy_response
        assert "ordinary" in rendered
        assert "kept" in rendered
    finally:
        service.close()
        engine.dispose()


def test_worker_model_provider_factory_holds_secret_redaction_until_close(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'worker-model-secret.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    store = SecretStore(
        engine,
        Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
    )
    store.save_secret(MODEL_API_KEY_SECRET_NAME, MODEL_ACTIVE_SECRET)

    async def resolve_public(_hostname: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    factory = ModelProviderFactory(
        secret_store=store,
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200, json={"data": [{"id": "worker-model"}]}
            )
        ),
        resolver=resolve_public,
    )
    tasks = TaskRepository(engine)
    try:
        provider = factory.create(
            AnalysisModelPublicConfig(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://models.example.com/v1",
                model="worker-model",
                temperature=0.1,
                timeout_seconds=30.0,
                max_output_tokens=2_048,
                secret_reference_id=MODEL_API_KEY_SECRET_NAME,
                api_key_configured=True,
            )
        )
        result = asyncio.run(provider.test_connection(timeout_seconds=1.0))
        assert result.connected is True
        task = tasks.create(
            "worker.model.output",
            {"model_output": f"echoed {MODEL_ACTIVE_SECRET}", "ordinary": "kept"},
        )

        rendered = repr(task)
        assert MODEL_ACTIVE_SECRET not in rendered
        assert "ordinary" in rendered
        assert "kept" in rendered
    finally:
        factory.close()
        engine.dispose()


def _pollute_every_backtest_export_surface(
    repository: object,
    *,
    active: str,
    rotated: str,
    ordinary: str,
) -> None:
    engine = repository._engine
    with engine.begin() as connection:
        trade = dict(
            connection.execute(
                select(BacktestTradeRow.payload_json).where(
                    BacktestTradeRow.run_id == RUN_ID
                )
            )
            .scalars()
            .first()
        )
        group = dict(
            connection.execute(
                select(BacktestGroupMetricRow.payload_json).where(
                    BacktestGroupMetricRow.run_id == RUN_ID
                )
            )
            .scalars()
            .first()
        )
        failure = dict(
            connection.execute(
                select(BacktestFailureRow.detail_json).where(
                    BacktestFailureRow.run_id == RUN_ID
                )
            ).scalar_one()
        )
        log = dict(
            connection.execute(
                select(BacktestLogRow.detail_json).where(
                    BacktestLogRow.run_id == RUN_ID
                )
            )
            .scalars()
            .first()
        )
        connection.execute(
            update(BacktestTradeRow)
            .where(BacktestTradeRow.run_id == RUN_ID)
            .values(
                payload_json={
                    **trade,
                    "credential": active,
                    "rotated": rotated,
                    "ordinary": ordinary,
                }
            )
        )
        connection.execute(
            update(BacktestGroupMetricRow)
            .where(BacktestGroupMetricRow.run_id == RUN_ID)
            .values(
                payload_json={
                    **group,
                    "credential": active,
                    "rotated": rotated,
                    "ordinary": ordinary,
                }
            )
        )
        connection.execute(
            update(BacktestFailureRow)
            .where(BacktestFailureRow.run_id == RUN_ID)
            .values(
                detail_json={
                    **failure,
                    "credential": active,
                    "rotated": rotated,
                    "ordinary": ordinary,
                }
            )
        )
        connection.execute(
            update(BacktestLogRow)
            .where(BacktestLogRow.run_id == RUN_ID)
            .values(
                message=f"logged {active}",
                detail_json={
                    **log,
                    "rotated": rotated,
                    "ordinary": ordinary,
                },
            )
        )
        connection.execute(
            update(BacktestSymbolRow)
            .where(BacktestSymbolRow.run_id == RUN_ID)
            .values(status="succeeded")
        )
        connection.execute(
            update(BacktestRunRow)
            .where(BacktestRunRow.id == RUN_ID)
            .values(
                status="succeeded",
                stage="completed",
                processed=1,
                finished_at=FINISHED,
                updated_at=FINISHED,
            )
        )


def _raw_export_rows(repository: object) -> str:
    engine = repository._engine
    with engine.connect() as connection:
        return repr(
            (
                tuple(
                    connection.execute(
                        select(BacktestTradeRow.payload_json).where(
                            BacktestTradeRow.run_id == RUN_ID
                        )
                    ).scalars()
                ),
                tuple(
                    connection.execute(
                        select(BacktestGroupMetricRow.payload_json).where(
                            BacktestGroupMetricRow.run_id == RUN_ID
                        )
                    ).scalars()
                ),
                tuple(
                    connection.execute(
                        select(BacktestFailureRow.detail_json).where(
                            BacktestFailureRow.run_id == RUN_ID
                        )
                    ).scalars()
                ),
                tuple(
                    connection.execute(
                        select(
                            BacktestLogRow.message, BacktestLogRow.detail_json
                        ).where(BacktestLogRow.run_id == RUN_ID)
                    )
                ),
            )
        )


def test_model_restart_hydrates_and_scrubs_legacy_tasks_and_export_rows(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path, complete=False)
    _pollute_every_backtest_export_surface(
        repository,
        active=MODEL_ACTIVE_SECRET,
        rotated=MODEL_ROTATED_SECRET,
        ordinary="ordinary configured-model-active",
    )
    tasks = TaskRepository(repository._engine)
    legacy = tasks.create(
        "legacy.model.restart",
        {
            "active": MODEL_ACTIVE_SECRET,
            "rotated": MODEL_ROTATED_SECRET,
            "ordinary": "ordinary configured-model-active",
        },
    )
    with repository._engine.begin() as connection:
        connection.execute(
            update(TaskEvent)
            .where(TaskEvent.task_id == legacy.id)
            .values(
                detail_json={
                    "active": MODEL_ACTIVE_SECRET,
                    "rotated": MODEL_ROTATED_SECRET,
                    "ordinary": "ordinary configured-model-active",
                }
            )
        )
    settings = Settings(
        database_url=str(repository._engine.url),
        data_dir=tmp_path,
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    catalog = AnalysisModelCatalog(repository._engine)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=SecretStore(repository._engine, settings),
    )
    parent = service.create(
        "Restart remote v1",
        ModelConfigUpdate(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            base_url="https://models.example.com/v1",
            model="restart-model-v1",
            api_key=SecretStr(MODEL_ACTIVE_SECRET),
        ),
    )
    service.create_successor(
        parent.id,
        "Restart remote v2",
        ModelConfigUpdate(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            base_url="https://models.example.com/v1",
            model="restart-model-v2",
            api_key=SecretStr(MODEL_ROTATED_SECRET),
        ),
    )
    service.close()
    catalog.close()

    restarted_catalog = AnalysisModelCatalog(repository._engine)
    restarted = ModelSettingsService(
        catalog=restarted_catalog,
        secret_store=SecretStore(repository._engine, settings),
    )
    try:
        raw = (
            repr(tasks.get(legacy.id))
            + repr(tasks.list_events(legacy.id))
            + _raw_export_rows(repository)
        )
        exports = b"\n".join(
            _bytes(repository, section, format_)
            for section in ("trades", "open", "groups", "failures", "logs")
            for format_ in ("json", "csv")
        )
        with pytest.raises(IntegrityError, match="immutable"):
            with repository._engine.begin() as connection:
                connection.execute(
                    update(BacktestTradeRow)
                    .where(BacktestTradeRow.run_id == RUN_ID)
                    .values(payload_json={"unsafe": "post-scrub mutation"})
                )
    finally:
        restarted.close()
        restarted_catalog.close()
        repository._engine.dispose()

    assert MODEL_ACTIVE_SECRET not in raw
    assert MODEL_ROTATED_SECRET not in raw
    assert MODEL_ACTIVE_SECRET.encode() not in exports
    assert MODEL_ROTATED_SECRET.encode() not in exports
    assert "ordinary configured-model-active" in raw


def test_source_rotation_scrubs_old_token_before_close_and_restart(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path, complete=False)
    _pollute_every_backtest_export_surface(
        repository,
        active=MARKET_ACTIVE_SECRET,
        rotated=MARKET_ROTATED_SECRET,
        ordinary="ordinary configured-market-active",
    )
    tasks = TaskRepository(repository._engine)
    legacy = tasks.create(
        "legacy.source.rotation",
        {
            "old": MARKET_ACTIVE_SECRET,
            "new": MARKET_ROTATED_SECRET,
            "ordinary": "ordinary configured-market-active",
        },
    )
    settings = Settings(
        database_url=str(repository._engine.url),
        data_dir=tmp_path,
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    source = SourceSettingsServices(engine=repository._engine, settings=settings)
    source.update_tushare(
        TushareSourceUpdateRequest(token=SecretStr(MARKET_ACTIVE_SECRET))
    )
    source.update_tushare(
        TushareSourceUpdateRequest(token=SecretStr(MARKET_ROTATED_SECRET))
    )
    source.close()

    restarted = SourceSettingsServices(engine=repository._engine, settings=settings)
    try:
        raw_task = repr(tasks.get(legacy.id)) + _raw_export_rows(repository)
        exports = b"\n".join(
            _bytes(repository, section, format_)
            for section in ("trades", "open", "groups", "failures", "logs")
            for format_ in ("json", "csv")
        )
    finally:
        restarted.close()
        repository._engine.dispose()

    assert MARKET_ACTIVE_SECRET not in raw_task
    assert MARKET_ROTATED_SECRET not in raw_task
    assert MARKET_ACTIVE_SECRET.encode() not in exports
    assert MARKET_ROTATED_SECRET.encode() not in exports
    assert "ordinary configured-market-active" in raw_task


def test_application_startup_hydrates_model_secret_before_first_task_read(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'startup-secret.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        database_url=url,
        data_dir=tmp_path,
        master_key=SecretStr(key),
    )
    catalog = AnalysisModelCatalog(engine)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=SecretStore(engine, settings),
    )
    service.create(
        "Startup remote",
        ModelConfigUpdate(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            base_url="https://models.example.com/v1",
            model="startup-model",
            api_key=SecretStr(MODEL_ACTIVE_SECRET),
        ),
    )
    service.close()
    catalog.close()
    tasks = TaskRepository(engine)
    legacy = tasks.create(
        "legacy.startup.secret",
        {
            "secret": MODEL_ACTIVE_SECRET,
            "ordinary": "ordinary configured-model-active",
        },
    )
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/tasks/{legacy.id}")

    assert response.status_code == 200
    assert MODEL_ACTIVE_SECRET not in response.text
    assert "ordinary configured-model-active" in response.text


def test_worker_startup_hydrates_model_secret_before_first_task_claim(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'worker-startup-secret.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    settings = Settings(
        database_url=url,
        data_dir=tmp_path,
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    catalog = AnalysisModelCatalog(engine)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=SecretStore(engine, settings),
    )
    service.create(
        "Worker startup remote",
        ModelConfigUpdate(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            base_url="https://models.example.com/v1",
            model="worker-startup-model",
            api_key=SecretStr(MODEL_ACTIVE_SECRET),
        ),
    )
    service.close()
    catalog.close()
    tasks = TaskRepository(engine)
    legacy = tasks.create(
        "legacy.worker.startup",
        {
            "secret": MODEL_ACTIVE_SECRET,
            "ordinary": "ordinary configured-model-active",
        },
    )
    engine.dispose()

    worker = ProductionMarketWorker.open(settings)
    try:
        persisted = repr(worker.tasks.get(legacy.id))
    finally:
        worker.close()

    assert MODEL_ACTIVE_SECRET not in persisted
    assert "ordinary configured-model-active" in persisted
