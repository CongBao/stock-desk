from __future__ import annotations

import json
from pathlib import Path
import re
import asyncio
import time

from cryptography.fernet import Fernet
import httpx2
from pydantic import SecretStr
import pytest
from sqlalchemy import Engine, event, text

import stock_desk.analysis.model_settings as model_settings_module
from stock_desk.analysis.model_catalog import AnalysisModelCatalog, ModelConfigStatus
from stock_desk.analysis.model_config import ModelConfigUpdate, ModelProviderKind
from stock_desk.analysis.model_settings import (
    ConnectionTestResult,
    ModelSettingsConflict,
    ModelSettingsService,
    ModelSettingsStorageError,
    ModelSettingsSecureStorageError,
    ModelSettingsValidationError,
    ModelProviderFactory,
)
from stock_desk.analysis.providers.base import (
    ModelConnectionResult,
    ModelCredentialUnavailableError,
    ModelErrorCode,
)
from stock_desk.config import Settings
from stock_desk.security.secrets import SecretStore
from stock_desk.storage.database import create_engine_for_url, migrate


FIRST_KEY = "sk-first-plaintext-never-persist"
SECOND_KEY = "sk-second-plaintext-never-persist"
SECRET_REF = re.compile(r"analysis_model_api_key_[0-9a-f]{32}\Z")


def _remote_update(
    *,
    model: str = "vendor-chat",
    api_key: str | None = FIRST_KEY,
) -> ModelConfigUpdate:
    return ModelConfigUpdate(
        provider=ModelProviderKind.OPENAI_COMPATIBLE,
        base_url="https://models.example.com/v1",
        model=model,
        api_key=SecretStr(api_key) if api_key is not None else None,
    )


def _ollama_update(
    *, model: str = "qwen3:8b", timeout_seconds: float = 90.0
) -> ModelConfigUpdate:
    return ModelConfigUpdate(
        provider=ModelProviderKind.OLLAMA,
        model=model,
        api_key=None,
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture
def settings_service(
    tmp_path: Path,
) -> tuple[ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine]:
    url = f"sqlite:///{tmp_path / 'model-settings.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    catalog = AnalysisModelCatalog(engine)
    store = SecretStore(
        engine,
        Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
    )
    service = ModelSettingsService(catalog=catalog, secret_store=store)
    try:
        yield service, catalog, store, engine
    finally:
        catalog.close()


def _stored_public_config(engine: Engine, config_id: str) -> dict[str, object]:
    with engine.connect() as connection:
        payload = connection.execute(
            text("SELECT public_config_json FROM analysis_model_config WHERE id=:id"),
            {"id": config_id},
        ).scalar_one()
    return json.loads(str(payload))


def test_create_remote_writes_encrypted_secret_and_catalog_in_one_transaction(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, store, engine = settings_service

    saved = service.create("Primary remote", _remote_update())
    stored = _stored_public_config(engine, saved.id)
    secret_ref = str(stored["secret_reference_id"])

    assert SECRET_REF.fullmatch(secret_ref)
    assert saved.api_key_configured is True
    assert saved.masked_api_key == "sk-f•••••••sist"
    assert saved.status is ModelConfigStatus.UNVERIFIED
    assert not hasattr(saved, "secret_reference_id")
    assert FIRST_KEY not in repr(saved)
    assert store.read_secret_for_server_call(secret_ref) == FIRST_KEY
    with engine.connect() as connection:
        database_text = " ".join(
            str(value)
            for row in connection.exec_driver_sql(
                "SELECT key, encrypted_value FROM app_setting"
            )
            for value in row
        )
    assert FIRST_KEY not in database_text


def test_create_rolls_back_secret_when_catalog_insert_fails(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, catalog, _store, engine = settings_service

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(FIRST_KEY)

    monkeypatch.setattr(catalog, "create_in_transaction", explode)

    with pytest.raises(Exception) as captured:
        service.create("Will roll back", _remote_update())

    assert FIRST_KEY not in str(captured.value)
    assert FIRST_KEY not in repr(captured.value)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM app_setting").all() == []
        assert (
            connection.exec_driver_sql("SELECT * FROM analysis_model_config").all()
            == []
        )


def test_ollama_create_has_null_ref_and_never_reads_or_writes_secret_store(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _catalog, store, engine = settings_service
    monkeypatch.setattr(
        store,
        "save_secret_in_transaction",
        lambda *_args: (_ for _ in ()).throw(AssertionError("secret write")),
    )
    monkeypatch.setattr(
        store,
        "read_secret_for_server_call_in_transaction",
        lambda *_args: (_ for _ in ()).throw(AssertionError("secret read")),
    )

    saved = service.create("Local", _ollama_update())

    assert saved.api_key_configured is False
    assert saved.masked_api_key is None
    assert _stored_public_config(engine, saved.id)["secret_reference_id"] is None


def test_duplicate_immutable_config_is_reported_as_safe_conflict(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, _store, _engine = settings_service
    service.create("Local", _ollama_update())

    with pytest.raises(ModelSettingsConflict) as captured:
        service.create("Same immutable local config", _ollama_update())

    assert str(captured.value) == "Model settings state changed"


def test_successor_without_key_reuses_existing_secret_reference(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, store, engine = settings_service
    parent = service.create("Remote v1", _remote_update())
    parent_bytes = json.dumps(_stored_public_config(engine, parent.id), sort_keys=True)
    parent_ref = str(_stored_public_config(engine, parent.id)["secret_reference_id"])

    successor = service.create_successor(
        parent.id,
        "Remote v2",
        _remote_update(model="vendor-chat-v2", api_key=None),
    )

    assert successor.supersedes_id == parent.id
    assert successor.status is ModelConfigStatus.UNVERIFIED
    assert (
        _stored_public_config(engine, successor.id)["secret_reference_id"] == parent_ref
    )
    assert (
        json.dumps(_stored_public_config(engine, parent.id), sort_keys=True)
        == parent_bytes
    )
    assert store.read_secret_for_server_call(parent_ref) == FIRST_KEY


def test_successor_with_key_rotates_without_overwriting_old_secret(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, store, engine = settings_service
    parent = service.create("Remote v1", _remote_update())
    old_ref = str(_stored_public_config(engine, parent.id)["secret_reference_id"])

    successor = service.create_successor(
        parent.id,
        "Remote v2",
        _remote_update(model="vendor-chat-v2", api_key=SECOND_KEY),
    )
    new_ref = str(_stored_public_config(engine, successor.id)["secret_reference_id"])

    assert old_ref != new_ref
    assert store.read_secret_for_server_call(old_ref) == FIRST_KEY
    assert store.read_secret_for_server_call(new_ref) == SECOND_KEY


def test_secret_reference_collision_never_overwrites_existing_key(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _catalog, store, engine = settings_service
    parent = service.create("Remote v1", _remote_update())
    old_ref = str(_stored_public_config(engine, parent.id)["secret_reference_id"])
    monkeypatch.setattr(
        model_settings_module,
        "_new_secret_reference",
        lambda: old_ref,
    )
    monkeypatch.setattr(
        store,
        "has_secret_in_transaction",
        lambda *_args: False,
    )

    with pytest.raises(ModelSettingsConflict):
        service.create_successor(
            parent.id,
            "Colliding rotation",
            _remote_update(model="colliding-model", api_key=SECOND_KEY),
        )

    assert store.read_secret_for_server_call(old_ref) == FIRST_KEY


def test_ollama_to_remote_without_key_is_rejected_and_remote_to_ollama_clears_ref(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, _store, engine = settings_service
    local = service.create("Local", _ollama_update())

    with pytest.raises(ModelSettingsValidationError):
        service.create_successor(
            local.id,
            "Remote without credentials",
            _remote_update(api_key=None),
        )

    remote = service.create("Remote", _remote_update(model="another-model"))
    local_successor = service.create_successor(
        remote.id,
        "Local successor",
        _ollama_update(model="qwen3:14b"),
    )
    stored = _stored_public_config(engine, local_successor.id)
    assert stored["secret_reference_id"] is None
    assert stored["api_key_configured"] is False


def test_two_remote_configs_use_independent_secrets_and_safe_page_views(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, store, engine = settings_service
    first = service.create("First", _remote_update())
    second = service.create(
        "Second", _remote_update(model="second-model", api_key=SECOND_KEY)
    )
    first_ref = str(_stored_public_config(engine, first.id)["secret_reference_id"])
    second_ref = str(_stored_public_config(engine, second.id)["secret_reference_id"])

    page = service.list_page(limit=1)
    next_page = service.list_page(limit=1, after=page.next_key)
    rendered = repr((service.get(first.id), page, next_page))

    assert first_ref != second_ref
    assert store.read_secret_for_server_call(first_ref) == FIRST_KEY
    assert store.read_secret_for_server_call(second_ref) == SECOND_KEY
    assert "secret_reference" not in rendered
    assert FIRST_KEY not in rendered
    assert SECOND_KEY not in rendered
    assert {item.id for item in (*page.items, *next_page.items)} == {
        first.id,
        second.id,
    }


def test_get_and_hundred_item_page_use_bounded_consistent_selects(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, _catalog, _store, engine = settings_service
    created = tuple(
        service.create(
            f"Remote {index}",
            _remote_update(model=f"model-{index}", api_key=f"secret-key-{index}"),
        )
        for index in range(100)
    )
    selects: list[str] = []

    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        loaded = service.get(created[0].id)
        get_selects = len(selects)
        selects.clear()
        page = service.list_page(limit=100)
        page_selects = len(selects)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert loaded.masked_api_key is not None
    assert get_selects <= 2
    assert page_selects <= 2
    assert len(page.items) == 100
    assert all(item.masked_api_key is not None for item in page.items)


def test_list_page_returns_one_transaction_snapshot_during_concurrent_disable(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'consistent-page.db'}"
    migrate(url)
    owner_engine = create_engine_for_url(url)
    peer_engine = create_engine_for_url(url)
    owner_catalog = AnalysisModelCatalog(owner_engine)
    peer_catalog = AnalysisModelCatalog(peer_engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    service = ModelSettingsService(
        catalog=owner_catalog,
        secret_store=SecretStore(owner_engine, Settings(master_key=key)),
    )
    saved = service.create("Concurrent", _remote_update())
    disabled = False

    def disable_after_page_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal disabled
        if not disabled and "FROM analysis_model_config" in statement:
            disabled = True
            peer_catalog.disable(saved.id, expected_revision=saved.revision)

    event.listen(owner_engine, "after_cursor_execute", disable_after_page_query)
    try:
        page = service.list_page(limit=10)
    finally:
        event.remove(owner_engine, "after_cursor_execute", disable_after_page_query)
        peer_catalog.close()
        owner_catalog.close()

    assert disabled is True
    assert len(page.items) == 1
    assert page.items[0].status is ModelConfigStatus.UNVERIFIED


class _ConnectionProvider:
    provider = "openai_compatible"
    model = "vendor-chat"

    def __init__(self, result: ModelConnectionResult) -> None:
        self.result = result
        self.timeouts: list[float] = []

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        self.timeouts.append(timeout_seconds)
        return self.result


class _ConnectionFactory:
    def __init__(self, provider: _ConnectionProvider) -> None:
        self.provider = provider
        self.configs: list[object] = []

    def create(self, config: object) -> _ConnectionProvider:
        self.configs.append(config)
        return self.provider


def test_connection_calls_provider_with_bounded_timeout_and_cas_marks_verified(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    _service, catalog, store, _engine = settings_service
    provider = _ConnectionProvider(
        ModelConnectionResult(
            connected=True,
            provider="openai_compatible",
            model="vendor-chat",
        )
    )
    factory = _ConnectionFactory(provider)
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=factory,  # type: ignore[arg-type]
    )
    saved = service.create("Connection target", _remote_update())

    result = asyncio.run(
        service.test_connection(saved.id, expected_revision=saved.revision)
    )

    assert isinstance(result, ConnectionTestResult)
    assert result.connected is True
    assert result.error_code is None
    assert result.status is ModelConfigStatus.VERIFIED
    assert result.revision == 1
    assert result.tested_at == result.last_tested_at
    assert result.last_tested_at is not None
    assert provider.timeouts == [10.0]
    assert len(factory.configs) == 1
    assert "secret_reference" not in repr(result)
    assert FIRST_KEY not in repr(result)


def test_connection_failure_is_safe_and_cas_marks_failed(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    _service, catalog, store, _engine = settings_service
    provider = _ConnectionProvider(
        ModelConnectionResult(
            connected=False,
            provider="openai_compatible",
            model="vendor-chat",
            error_code=ModelErrorCode.AUTHENTICATION,
        )
    )
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=_ConnectionFactory(provider),  # type: ignore[arg-type]
    )
    saved = service.create("Connection target", _remote_update())

    result = asyncio.run(
        service.test_connection(saved.id, expected_revision=saved.revision)
    )

    assert result.connected is False
    assert result.error_code is ModelErrorCode.AUTHENTICATION
    assert result.status is ModelConfigStatus.FAILED
    assert catalog.get(saved.id).error_code == "authentication"


def test_connection_storage_failure_does_not_mutate_catalog(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    _service, catalog, store, _engine = settings_service
    provider = _ConnectionProvider(
        ModelConnectionResult(
            connected=False,
            provider="openai_compatible",
            model="vendor-chat",
            error_code=ModelErrorCode.STORAGE,
        )
    )
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=_ConnectionFactory(provider),  # type: ignore[arg-type]
    )
    saved = service.create("Storage target", _remote_update())

    with pytest.raises(ModelSettingsStorageError):
        asyncio.run(service.test_connection(saved.id, expected_revision=saved.revision))

    unchanged = catalog.get(saved.id)
    assert unchanged.status is ModelConfigStatus.UNVERIFIED
    assert unchanged.revision == saved.revision
    assert unchanged.last_tested_at is None


def test_connection_raised_credential_failure_does_not_mutate_catalog(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    _service, catalog, store, _engine = settings_service

    class RaisingProvider(_ConnectionProvider):
        async def test_connection(
            self, *, timeout_seconds: float = 10.0
        ) -> ModelConnectionResult:
            raise ModelCredentialUnavailableError("unsafe detail")

    service = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=_ConnectionFactory(
            RaisingProvider(
                ModelConnectionResult(
                    connected=True,
                    provider="openai_compatible",
                    model="vendor-chat",
                )
            )
        ),  # type: ignore[arg-type]
    )
    saved = service.create("Raised storage target", _remote_update())

    with pytest.raises(ModelSettingsStorageError):
        asyncio.run(service.test_connection(saved.id, expected_revision=saved.revision))

    unchanged = catalog.get(saved.id)
    assert unchanged.status is ModelConfigStatus.UNVERIFIED
    assert unchanged.revision == saved.revision
    assert unchanged.last_tested_at is None


def test_connection_with_damaged_ciphertext_fails_safe_without_network_or_leak(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    service, catalog, _store, engine = settings_service
    saved = service.create("Damaged secret", _remote_update())
    stored = _stored_public_config(engine, saved.id)
    secret_ref = str(stored["secret_reference_id"])
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE app_setting SET encrypted_value='damaged' WHERE key=:key"),
            {"key": f"secret.{secret_ref}"},
        )

    with pytest.raises(ModelSettingsStorageError):
        asyncio.run(service.test_connection(saved.id, expected_revision=saved.revision))

    unchanged = catalog.get(saved.id)
    assert unchanged.status is ModelConfigStatus.UNVERIFIED
    assert unchanged.revision == saved.revision
    assert unchanged.last_tested_at is None
    rendered = repr(unchanged)
    assert FIRST_KEY not in rendered
    assert "damaged" not in rendered


def test_connection_rejects_stale_and_disabled_before_provider_call(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    _service, catalog, store, _engine = settings_service
    provider = _ConnectionProvider(
        ModelConnectionResult(
            connected=True,
            provider="openai_compatible",
            model="vendor-chat",
        )
    )
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=_ConnectionFactory(provider),  # type: ignore[arg-type]
    )
    stale = service.create("Stale", _remote_update())

    with pytest.raises(ModelSettingsConflict):
        asyncio.run(service.test_connection(stale.id, expected_revision=99))

    disabled_target = service.create(
        "Disabled", _remote_update(model="disabled-model", api_key=SECOND_KEY)
    )
    catalog.disable(disabled_target.id, expected_revision=disabled_target.revision)
    with pytest.raises(ModelSettingsConflict):
        asyncio.run(
            service.test_connection(
                disabled_target.id,
                expected_revision=disabled_target.revision + 1,
            )
        )

    assert provider.timeouts == []


def test_ollama_full_settings_flow_works_without_master_key_or_secret_store(
    tmp_path: Path,
) -> None:
    settings = Settings(master_key=None)
    assert settings.master_key is None
    url = f"sqlite:///{tmp_path / 'no-master-key.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    catalog = AnalysisModelCatalog(engine)
    requested: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requested.append(request)
        return httpx2.Response(200, json={"models": [{"name": "qwen3:14b"}]})

    factory = ModelProviderFactory(
        secret_store=None,
        transport=httpx2.MockTransport(respond),
    )
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=None,
        provider_factory=factory,
    )
    try:
        parent = service.create("Local", _ollama_update())
        successor = service.create_successor(
            parent.id,
            "Local v2",
            _ollama_update(model="qwen3:14b"),
        )
        assert service.get(successor.id).masked_api_key is None
        assert {item.id for item in service.list_page(limit=10).items} == {
            parent.id,
            successor.id,
        }

        tested = asyncio.run(
            service.test_connection(
                successor.id,
                expected_revision=successor.revision,
            )
        )

        assert tested.connected is True
        assert tested.tested_at == tested.last_tested_at
        assert tested.last_tested_at is not None
        assert len(requested) == 1
        assert "authorization" not in requested[0].headers
    finally:
        catalog.close()


def test_remote_settings_operations_fail_closed_without_secret_store(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    owner, catalog, _store, _engine = settings_service
    remote = owner.create("Remote", _remote_update())
    local = owner.create("Local", _ollama_update())
    bypassing_factory = _ConnectionFactory(
        _ConnectionProvider(
            ModelConnectionResult(
                connected=True,
                provider="openai_compatible",
                model="vendor-chat",
            )
        )
    )
    service = ModelSettingsService(
        catalog=catalog,
        secret_store=None,
        provider_factory=bypassing_factory,  # type: ignore[arg-type]
    )

    operations = (
        lambda: service.create("No storage", _remote_update(model="new-model")),
        lambda: service.create(
            "No storage or key",
            _remote_update(model="new-model-without-key", api_key=None),
        ),
        lambda: service.create_successor(
            local.id,
            "Remote successor",
            _remote_update(model="successor-model"),
        ),
        lambda: service.get(remote.id),
        lambda: service.list_page(limit=10),
        lambda: asyncio.run(
            service.test_connection(remote.id, expected_revision=remote.revision)
        ),
    )
    for operation in operations:
        with pytest.raises(ModelSettingsStorageError) as captured:
            operation()
        assert str(captured.value) == "Model settings could not be saved"


def test_require_verified_execution_reads_remote_secret_and_allows_local_without_store(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    owner, catalog, _store, _engine = settings_service
    remote = owner.create("Remote", _remote_update())
    local = owner.create("Local", _ollama_update())
    for snapshot in (remote, local):
        catalog.mark_test_result(
            snapshot.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=snapshot.revision,
            succeeded=True,
        )

    assert owner.require_verified_execution(remote.id).model_config_id == remote.id
    local_only = ModelSettingsService(catalog=catalog, secret_store=None)
    assert local_only.require_verified_execution(local.id).model_config_id == local.id


def test_require_verified_execution_rejects_missing_or_wrong_remote_store(
    settings_service: tuple[
        ModelSettingsService, AnalysisModelCatalog, SecretStore, Engine
    ],
) -> None:
    owner, catalog, _store, engine = settings_service
    remote = owner.create("Remote", _remote_update())
    catalog.mark_test_result(
        remote.id,
        expected_status=ModelConfigStatus.UNVERIFIED,
        expected_revision=remote.revision,
        succeeded=True,
    )
    wrong_store = SecretStore(
        engine,
        Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
    )

    for service in (
        ModelSettingsService(catalog=catalog, secret_store=None),
        ModelSettingsService(catalog=catalog, secret_store=wrong_store),
    ):
        with pytest.raises(ModelSettingsSecureStorageError):
            service.require_verified_execution(remote.id)


def test_service_enforces_hard_deadline_and_records_timeout_test_timestamp(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'hard-deadline.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    catalog = AnalysisModelCatalog(engine)

    async def slow_response(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(2.0)
        return httpx2.Response(200, json={"models": [{"name": "qwen3:8b"}]})

    service = ModelSettingsService(
        catalog=catalog,
        secret_store=None,
        provider_factory=ModelProviderFactory(
            secret_store=None,
            transport=httpx2.MockTransport(slow_response),
        ),
    )
    try:
        saved = service.create(
            "Slow local",
            _ollama_update(timeout_seconds=1.0),
        )
        started = time.monotonic()
        result = asyncio.run(
            service.test_connection(saved.id, expected_revision=saved.revision)
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1.5
        assert result.connected is False
        assert result.error_code is ModelErrorCode.TIMEOUT
        assert result.status is ModelConfigStatus.FAILED
        assert result.tested_at == result.last_tested_at
        assert result.last_tested_at is not None
        assert catalog.get(saved.id).last_tested_at == result.last_tested_at
    finally:
        catalog.close()
