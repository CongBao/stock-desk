from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, text

import stock_desk.main as main_module
from stock_desk.analysis.model_catalog import AnalysisModelCatalog, ModelConfigStatus
from stock_desk.analysis.model_config import AnalysisModelPublicConfig
from stock_desk.analysis.model_settings import ModelSettingsService
from stock_desk.analysis.providers.base import ModelConnectionResult
from stock_desk.analysis.runtime import AnalysisPreflightService
from stock_desk.analysis.snapshot import ResearchSectionKind
from stock_desk.api.market import MarketServices
from stock_desk.api.settings import SourceSettingsServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.providers.base import ProviderUnavailable
from stock_desk.market.types import ProviderId
from stock_desk.security.secrets import SecretStorageError, SecretStore
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository


CONFIG_ID = "sha256:" + "a" * 64
RUN_ID = "11111111-1111-1111-1111-111111111111"


class _ConnectedRemoteProvider:
    def __init__(self, config: AnalysisModelPublicConfig) -> None:
        self.provider = config.provider.value
        self.model = config.model

    async def complete(self, _request: object) -> object:
        raise AssertionError("connection-only provider")

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        del timeout_seconds
        return ModelConnectionResult(
            connected=True,
            provider=self.provider,
            model=self.model,
        )


class _ConnectedRemoteFactory:
    def create(self, config: AnalysisModelPublicConfig) -> _ConnectedRemoteProvider:
        return _ConnectedRemoteProvider(config)


def _settings(tmp_path: Path, *, master_key: str | None = None) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'stock-desk.db'}",
        master_key=master_key,
    )


def _ollama_body() -> dict[str, object]:
    return {
        "display_name": "Local Qwen",
        "provider": "ollama",
        "model": "qwen3:8b",
        "temperature": 0.1,
        "timeout": 90.0,
        "max_output": 4096,
    }


def test_main_openapi_exposes_complete_model_and_analysis_contract(
    tmp_path: Path,
) -> None:
    application = create_app(_settings(tmp_path))

    document = application.openapi()

    assert document["info"]["version"] == "0.5.0"
    expected_operations = {
        "/api/settings/models": {"get", "post"},
        "/api/settings/models/{config_id}": {"get", "put"},
        "/api/settings/models/{config_id}/test": {"post"},
        "/api/settings/models/{config_id}/disable": {"post"},
        "/api/analysis": {"get", "post"},
        "/api/analysis/preflight": {"post"},
        "/api/analysis/{run_id}": {"get"},
        "/api/analysis/{run_id}/cancel": {"post"},
        "/api/analysis/{run_id}/report": {"get"},
        "/api/analysis/{run_id}/evidence/{evidence_id}": {"get"},
        "/api/analysis/{run_id}/stages/{stage}/retry": {"post"},
    }
    for path, methods in expected_operations.items():
        assert methods <= set(document["paths"][path])

    model_response = document["components"]["schemas"]["ModelSettingsResponse"]
    assert set(model_response["required"]) == set(model_response["properties"])
    rendered = json.dumps(document, ensure_ascii=False)
    assert "secret_reference" not in rendered
    assert "public_config_json" not in rendered
    assert "encrypted_value" not in rendered
    api_key = document["components"]["schemas"]["ModelSettingsCreateRequest"][
        "properties"
    ]["api_key"]
    assert api_key["writeOnly"] is True


def test_create_app_is_lazy_and_cursor_keys_are_stable_unique_secrets(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = create_app(_settings(first_root))
    second = create_app(_settings(second_root))

    assert not first_root.exists()
    assert not second_root.exists()
    assert type(first.state.model_settings_cursor_key) is bytes
    assert type(first.state.analysis_cursor_key) is bytes
    assert len(first.state.model_settings_cursor_key) == 32
    assert len(first.state.analysis_cursor_key) == 32
    assert (
        first.state.model_settings_cursor_key is first.state.model_settings_cursor_key
    )
    assert first.state.analysis_cursor_key is first.state.analysis_cursor_key
    assert (
        first.state.model_settings_cursor_key != second.state.model_settings_cursor_key
    )
    assert first.state.analysis_cursor_key != second.state.analysis_cursor_key
    assert first.state.model_settings_cursor_key != first.state.analysis_cursor_key
    assert first.state.model_settings_cursor_key.hex() not in repr(first.state)


def test_default_model_and_analysis_services_share_storage_and_empty_lists(
    tmp_path: Path,
) -> None:
    application = create_app(_settings(tmp_path))

    with TestClient(application) as client:
        models = client.get("/api/settings/models")
        history = client.get("/api/analysis")
        model_service = application.state.model_settings_services_provider()
        analysis_service = application.state.analysis_services_provider()
        identity = application.state.database_identity_provider()

    assert models.status_code == 200
    assert models.json() == {"items": [], "next_cursor": None}
    assert history.status_code == 200
    assert history.json() == {"items": [], "next_cursor": None}
    assert identity is not None
    assert model_service.database_identity == identity
    assert analysis_service.database_identity == identity
    assert analysis_service.analysis_repository_identity == identity
    assert analysis_service.task_repository_identity == identity
    assert analysis_service.model_catalog_identity == identity
    assert not (tmp_path / "data" / "market").exists()


def test_without_master_key_ollama_works_but_remote_secret_is_fail_closed(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        ollama = client.post("/api/settings/models", json=_ollama_body())
        remote = client.post(
            "/api/settings/models",
            json={
                **_ollama_body(),
                "display_name": "Remote",
                "provider": "openai_compatible",
                "base_url": "https://models.example.com/v1",
                "model": "vendor-chat",
                "api_key": "sk-never-return-this-value",
            },
        )

    assert ollama.status_code == 201
    assert ollama.json()["provider"] == "ollama"
    assert ollama.json()["api_key_configured"] is False
    assert remote.status_code == 503
    assert remote.json() == {"code": "secure_storage_unavailable"}
    assert "sk-never-return-this-value" not in remote.text


def test_valid_master_key_first_remote_and_ollama_requests_succeed(
    tmp_path: Path,
) -> None:
    plaintext = "sk-first-request-must-not-escape"
    remote_settings = _settings(
        tmp_path / "remote", master_key=Fernet.generate_key().decode("ascii")
    )
    ollama_settings = _settings(
        tmp_path / "ollama", master_key=Fernet.generate_key().decode("ascii")
    )

    with TestClient(create_app(remote_settings)) as client:
        remote = client.post(
            "/api/settings/models",
            json={
                **_ollama_body(),
                "display_name": "Remote first",
                "provider": "openai_compatible",
                "base_url": "https://models.example.com/v1",
                "model": "vendor-chat",
                "api_key": plaintext,
            },
        )
    with TestClient(create_app(ollama_settings)) as client:
        ollama = client.post("/api/settings/models", json=_ollama_body())

    assert remote.status_code == 201
    assert remote.json()["api_key_configured"] is True
    assert plaintext not in remote.text
    assert "secret_reference" not in remote.text
    assert ollama.status_code == 201
    assert ollama.json()["provider"] == "ollama"


def test_transient_secret_store_construction_failure_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        tmp_path,
        master_key=Fernet.generate_key().decode("ascii"),
    )
    real_constructor = SecretStore
    constructor_calls = 0

    class FlakySecretStore:
        def __new__(cls, *args: object, **kwargs: object) -> SecretStore:
            nonlocal constructor_calls
            constructor_calls += 1
            if constructor_calls == 1:
                raise SecretStorageError("transient unsafe detail")
            return real_constructor(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_module, "SecretStore", FlakySecretStore)
    application = create_app(settings)

    with TestClient(application, raise_server_exceptions=False) as client:
        first = client.get("/api/settings/models")
        second = client.get("/api/settings/models")

    assert first.status_code == 503
    assert first.json() == {"code": "storage_unavailable"}
    assert "transient unsafe detail" not in first.text
    assert second.status_code == 200
    assert second.json() == {"items": [], "next_cursor": None}
    assert constructor_calls == 2


@pytest.mark.parametrize("restart_key", [None, "wrong"])
def test_verified_remote_submit_after_restart_requires_readable_secret_before_enqueue(
    tmp_path: Path, restart_key: str | None
) -> None:
    database_url = f"sqlite:///{tmp_path / 'remote-restart.db'}"
    owner_key = Fernet.generate_key().decode("ascii")
    owner_settings = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        master_key=owner_key,
    )
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    tasks = TaskRepository(engine)
    catalog = AnalysisModelCatalog(engine, owns_engine=False)
    store = SecretStore(engine, owner_settings)
    models = ModelSettingsService(
        catalog=catalog,
        secret_store=store,
        provider_factory=_ConnectedRemoteFactory(),  # type: ignore[arg-type]
    )
    plaintext = "sk-restart-guard-never-return"
    try:
        with TestClient(
            create_app(
                owner_settings,
                task_repository=tasks,
                model_settings_service=models,
            )
        ) as client:
            created = client.post(
                "/api/settings/models",
                json={
                    **_ollama_body(),
                    "display_name": "Remote guarded",
                    "provider": "openai_compatible",
                    "base_url": "https://models.example.com/v1",
                    "model": "vendor-chat",
                    "api_key": plaintext,
                },
            )
            assert created.status_code == 201
            config_id = created.json()["id"]
            tested = client.post(
                f"/api/settings/models/{config_id}/test",
                json={"expected_revision": 0},
            )
            assert tested.status_code == 200
            assert tested.json()["status"] == "verified"
    finally:
        catalog.close()
        engine.dispose()

    restarted_master_key = (
        None if restart_key is None else Fernet.generate_key().decode("ascii")
    )
    restarted = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        master_key=restarted_master_key,
    )
    application = create_app(restarted)
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/analysis",
            json={
                "symbol": "600000.SH",
                "model_config_id": config_id,
                "retry": {"max_retries": 0},
            },
        )
        repository = application.state.task_repository_provider()
        with repository.engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM task_run), "
                    "(SELECT count(*) FROM analysis_run), "
                    "(SELECT count(*) FROM analysis_stage)"
                )
            ).one()

    assert response.status_code == 503
    assert response.json() == {"code": "secure_storage_unavailable"}
    assert plaintext not in response.text
    assert tuple(counts) == (0, 0, 0)


def test_real_preflight_composition_is_fresh_read_only_and_typed_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(_settings(tmp_path))

    class _UnavailableAkShare:
        name = ProviderId.AKSHARE

        def fetch(self, _symbol: str, _kind: ResearchSectionKind) -> object:
            raise ProviderUnavailable()

    with TestClient(application) as client:
        tasks = application.state.task_repository_provider()
        preflight = application.state.analysis_preflight_provider()
        market = application.state.market_services_provider()
        sources = application.state.source_settings_services_provider()
        factory = preflight._data_service_factory
        assert factory._market_lake is market.lake
        assert factory._source_settings is sources
        monkeypatch.setattr(factory, "_akshare_factory", _UnavailableAkShare)
        snapshot_calls: list[bool] = []
        real_snapshot = sources.runtime_snapshot

        def counted_snapshot() -> object:
            snapshot_calls.append(True)
            return real_snapshot()

        monkeypatch.setattr(sources, "runtime_snapshot", counted_snapshot)
        with tasks.engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM task_run), "
                    "(SELECT count(*) FROM analysis_run), "
                    "(SELECT count(*) FROM analysis_model_config)"
                )
            ).one()

        first = client.post("/api/analysis/preflight", json={"symbol": "600000.SH"})
        second = client.post("/api/analysis/preflight", json={"symbol": "600000.SH"})
        with tasks.engine.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM task_run), "
                    "(SELECT count(*) FROM analysis_run), "
                    "(SELECT count(*) FROM analysis_model_config)"
                )
            ).one()

    assert first.status_code == second.status_code == 200
    for response in (first, second):
        body = response.json()
        assert body["reservation"] is False
        assert body["rating_eligible"] is False
        assert [item["kind"] for item in body["categories"]] == [
            "market",
            "fundamentals",
            "announcements",
            "news",
        ]
        assert all(item["connection_state"] == "missing" for item in body["categories"])
        assert all(item["missing_reason"] for item in body["categories"])
    assert snapshot_calls == [True, True]
    assert tuple(before) == tuple(after) == (0, 0, 0)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/settings/models", {"provider": "ollama"}),
        ("post", "/api/analysis/preflight", {"symbol": "bad"}),
        ("post", "/api/analysis", {"symbol": "bad"}),
        ("post", "/api/tasks", {"kind": ""}),
    ],
)
def test_main_validation_errors_keep_route_specific_single_code(
    tmp_path: Path,
    method: str,
    path: str,
    body: dict[str, object],
) -> None:
    with TestClient(
        create_app(_settings(tmp_path)), raise_server_exceptions=False
    ) as client:
        response = client.request(method, path, json=body)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


def test_preflight_static_path_is_not_captured_by_run_detail(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path)), raise_server_exceptions=False
    ) as client:
        response = client.post("/api/analysis/preflight", json={"symbol": "bad"})

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


def test_owned_lifecycle_closes_all_initialized_resources_after_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ("test-database", "same")
    closes: list[str] = []

    class _Owned:
        database_identity = identity

        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            closes.append(self.name)
            if self.fail:
                raise RuntimeError("unsafe shutdown detail")

    task = _Owned("task", fail=True)
    market = _Owned("market")
    market.engine = SimpleNamespace()
    market.lake = SimpleNamespace(database_identity=identity)
    source = _Owned("source")
    monkeypatch.setattr(TaskRepository, "open", classmethod(lambda _cls, _url: task))
    monkeypatch.setattr(
        MarketServices,
        "open",
        classmethod(lambda _cls, **_kwargs: market),
    )
    monkeypatch.setattr(
        SourceSettingsServices,
        "open",
        classmethod(lambda _cls, **_kwargs: source),
    )
    application = create_app(_settings(tmp_path))

    with TestClient(application):
        application.state.task_repository_provider()
        application.state.market_services_provider()
        application.state.source_settings_services_provider()

    assert set(closes) == {"task", "market", "source"}
    assert len(closes) == 3


def test_shared_catalog_close_does_not_dispose_injected_task_engine(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'injected.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    repository = TaskRepository(engine)
    disposals: list[bool] = []
    event.listen(engine, "engine_disposed", lambda _engine: disposals.append(True))
    application = create_app(_settings(tmp_path), task_repository=repository)
    try:
        with TestClient(application) as client:
            assert client.get("/api/settings/models").status_code == 200
            model_service = application.state.model_settings_services_provider()
            assert model_service.database_identity == repository.database_identity

        assert disposals == []
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_default_shared_catalog_and_task_engine_dispose_exactly_once(
    tmp_path: Path,
) -> None:
    application = create_app(_settings(tmp_path))
    disposals: list[bool] = []

    with TestClient(application) as client:
        assert client.get("/api/analysis").status_code == 200
        repository = application.state.task_repository_provider()
        application.state.model_settings_services_provider()
        event.listen(
            repository.engine,
            "engine_disposed",
            lambda _engine: disposals.append(True),
        )

    assert disposals == [True]


def test_overlapping_lifespans_close_on_final_exit_and_rebuild_on_reentry(
    tmp_path: Path,
) -> None:
    application = create_app(_settings(tmp_path))
    model_cursor_key = application.state.model_settings_cursor_key
    analysis_cursor_key = application.state.analysis_cursor_key
    first_disposals: list[bool] = []

    with TestClient(application) as first_client:
        with TestClient(application) as second_client:
            assert first_client.get("/api/analysis").status_code == 200
            assert second_client.get("/api/settings/models").status_code == 200
            first_repository = application.state.task_repository_provider()
            event.listen(
                first_repository.engine,
                "engine_disposed",
                lambda _engine: first_disposals.append(True),
            )

        assert first_disposals == []
        assert first_client.get("/api/analysis").status_code == 200

    assert first_disposals == [True]

    second_disposals: list[bool] = []
    with TestClient(application) as reentered_client:
        response = reentered_client.get("/api/settings/models")
        rebuilt_repository = application.state.task_repository_provider()
        event.listen(
            rebuilt_repository.engine,
            "engine_disposed",
            lambda _engine: second_disposals.append(True),
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}
        assert rebuilt_repository is not first_repository
        assert application.state.model_settings_cursor_key is model_cursor_key
        assert application.state.analysis_cursor_key is analysis_cursor_key

    assert second_disposals == [True]


def test_injected_analysis_services_are_never_closed_by_application(
    tmp_path: Path,
) -> None:
    identity = ("test-database", "injected")
    closes: list[str] = []
    model = SimpleNamespace(
        database_identity=identity,
        close=lambda: closes.append("model"),
    )
    analysis = SimpleNamespace(
        database_identity=identity,
        analysis_repository_identity=identity,
        task_repository_identity=identity,
        model_catalog_identity=identity,
        close=lambda: closes.append("analysis"),
    )
    preflight = SimpleNamespace(
        database_identity=identity,
        close=lambda: closes.append("preflight"),
    )
    application = create_app(
        _settings(tmp_path),
        model_settings_service=model,  # type: ignore[arg-type]
        analysis_service=analysis,  # type: ignore[arg-type]
        analysis_preflight_service=preflight,  # type: ignore[arg-type]
    )

    with TestClient(application):
        application.state.model_settings_services_provider()
        application.state.analysis_services_provider()
        application.state.analysis_preflight_provider()

    assert closes == []


def test_injected_model_and_analysis_identity_mismatch_is_503(tmp_path: Path) -> None:
    first = ("test-database", "first")
    second = ("test-database", "second")

    class _ModelService:
        database_identity = first

        def get(self, _config_id: str) -> object:
            raise AssertionError("identity must be checked first")

    class _AnalysisService:
        database_identity = second
        analysis_repository_identity = second
        task_repository_identity = second
        model_catalog_identity = second

        def detail(self, _run_id: str) -> object:
            raise AssertionError("identity must be checked first")

    application = create_app(
        _settings(tmp_path),
        model_settings_service=_ModelService(),  # type: ignore[arg-type]
        analysis_service=_AnalysisService(),  # type: ignore[arg-type]
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        model_response = client.get(f"/api/settings/models/{CONFIG_ID}")
        response = client.get(f"/api/analysis/{RUN_ID}")

    assert model_response.status_code == 503
    assert model_response.json() == {"code": "storage_unavailable"}
    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


@pytest.mark.parametrize("first_path", ["/api/tasks", "/api/settings/models"])
def test_explicit_real_task_and_model_database_mismatch_is_503_on_first_access(
    tmp_path: Path, first_path: str
) -> None:
    first_url = f"sqlite:///{tmp_path / 'task-a.db'}"
    second_url = f"sqlite:///{tmp_path / 'model-b.db'}"
    migrate(first_url)
    migrate(second_url)
    first_engine = create_engine_for_url(first_url)
    second_engine = create_engine_for_url(second_url)
    tasks = TaskRepository(first_engine)
    catalog = AnalysisModelCatalog(second_engine, owns_engine=False)
    models = ModelSettingsService(catalog=catalog, secret_store=None)
    try:
        application = create_app(
            Settings(database_url=first_url, data_dir=tmp_path),
            task_repository=tasks,
            model_settings_service=models,
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get(first_path)

        assert response.status_code == 503
        assert response.json() == {"code": "storage_unavailable"}
    finally:
        catalog.close()
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.parametrize("first_path", ["/api/tasks", "/api/market/pools"])
def test_explicit_real_task_and_market_database_mismatch_is_503_on_first_access(
    tmp_path: Path, first_path: str
) -> None:
    first_url = f"sqlite:///{tmp_path / 'task-a.db'}"
    second_url = f"sqlite:///{tmp_path / 'market-b.db'}"
    migrate(first_url)
    migrate(second_url)
    first_engine = create_engine_for_url(first_url)
    second_engine = create_engine_for_url(second_url)
    tasks = TaskRepository(first_engine)
    market = MarketServices(
        engine=second_engine,
        lake_root=(tmp_path / "market-b").resolve(),
    )
    try:
        application = create_app(
            Settings(database_url=first_url, data_dir=tmp_path),
            task_repository=tasks,
            market_services=market,
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get(first_path)

        assert response.status_code == 503
        assert response.json() == {"code": "storage_unavailable"}
    finally:
        first_engine.dispose()
        market.close()


@pytest.mark.parametrize(
    ("method", "first_path", "body"),
    [
        ("get", "/api/settings/sources", None),
        ("post", "/api/analysis/preflight", {"symbol": "600000.SH"}),
    ],
)
def test_explicit_real_source_and_preflight_mismatch_is_503_on_first_access(
    tmp_path: Path,
    method: str,
    first_path: str,
    body: dict[str, object] | None,
) -> None:
    first_url = f"sqlite:///{tmp_path / 'source-a.db'}"
    second_url = f"sqlite:///{tmp_path / 'preflight-b.db'}"
    migrate(first_url)
    migrate(second_url)
    first_engine = create_engine_for_url(first_url)
    second_engine = create_engine_for_url(second_url)
    settings = Settings(database_url=first_url, data_dir=tmp_path)
    sources = SourceSettingsServices(engine=first_engine, settings=settings)

    class _NeverBuildDataService:
        database_identity = TaskRepository(second_engine).database_identity

        def __call__(self) -> object:
            raise AssertionError("compromised DI must fail before execution")

    preflight = AnalysisPreflightService(
        data_service_factory=_NeverBuildDataService()  # type: ignore[arg-type]
    )
    try:
        application = create_app(
            settings,
            source_settings_services=sources,
            analysis_preflight_service=preflight,
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.request(method, first_path, json=body)

        assert response.status_code == 503
        assert response.json() == {"code": "storage_unavailable"}
    finally:
        sources.close()
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.parametrize("identity", [None, ("test-database", "changed")])
def test_injected_task_missing_or_changed_identity_is_stable_503(
    tmp_path: Path, identity: object
) -> None:
    repository = SimpleNamespace(database_identity=("test-database", "original"))
    application = create_app(
        _settings(tmp_path),
        task_repository=repository,  # type: ignore[arg-type]
    )
    application.state.task_repository_provider()
    repository.database_identity = identity

    with TestClient(application, raise_server_exceptions=False) as client:
        first = client.get("/api/tasks")
        second = client.get("/api/tasks")

    assert first.status_code == second.status_code == 503
    assert first.json() == second.json() == {"code": "storage_unavailable"}


def test_explicit_dependency_missing_identity_is_compromised_before_first_access(
    tmp_path: Path,
) -> None:
    repository = SimpleNamespace(database_identity=None)
    application = create_app(
        _settings(tmp_path),
        task_repository=repository,  # type: ignore[arg-type]
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/api/tasks")

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


def test_model_status_is_public_enum_not_internal_secret(tmp_path: Path) -> None:
    assert ModelConfigStatus.UNVERIFIED.value == "unverified"
    rendered = json.dumps(create_app(_settings(tmp_path)).openapi())
    assert "api_key_configured" in rendered
    assert 'api_key"' in rendered
