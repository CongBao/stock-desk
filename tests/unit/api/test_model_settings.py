from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import text

from stock_desk.analysis.model_catalog import (
    AnalysisModelCatalog,
    ModelConfigListKey,
    ModelConfigStatus,
    ModelNotFound,
    ModelNotVerified,
)
from stock_desk.analysis.model_config import ModelConfigUpdate, ModelProviderKind
from stock_desk.analysis.model_settings import (
    ConnectionTestResult,
    ModelSettingsConflict,
    ModelSettingsPage,
    ModelSettingsSecureStorageError,
    ModelSettingsService,
    ModelSettingsSnapshot,
    ModelSettingsStorageError,
    ModelSettingsValidationError,
)
from stock_desk.analysis.providers.base import ModelErrorCode
from stock_desk.api.models import (
    ModelConnectionTestResponse,
    ModelSettingsResponse,
    router,
)
from stock_desk.config import Settings
from stock_desk.security.secrets import SecretStore
from stock_desk.storage.database import create_engine_for_url, migrate


CONFIG_ID = "sha256:" + "a" * 64
NEXT_ID = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 7, 8, 30, tzinfo=timezone.utc)
PLAINTEXT = "sk-plaintext-must-never-escape"
TEST_CURSOR_KEY = b"k" * 32
OTHER_CURSOR_KEY = b"z" * 32
_DEFAULT_STATE = object()
_OMIT_STATE = object()

assert len(TEST_CURSOR_KEY) == 32
assert len(OTHER_CURSOR_KEY) == 32


def _snapshot(**changes: object) -> ModelSettingsSnapshot:
    base = ModelSettingsSnapshot(
        id=CONFIG_ID,
        public_config_hash=CONFIG_ID,
        display_name="Local model",
        provider=ModelProviderKind.OLLAMA,
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
        api_key_configured=False,
        masked_api_key=None,
        supersedes_id=None,
        status=ModelConfigStatus.UNVERIFIED,
        revision=0,
        verified_at=None,
        last_tested_at=None,
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(base, **cast(Any, changes))


class _ModelSettingsServices:
    database_identity = SimpleNamespace(kind="test", value="same")

    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.calls: list[tuple[object, ...]] = []
        self.list_next_key: ModelConfigListKey | None = None
        self.error: Exception | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    def create(
        self, display_name: str, update: ModelConfigUpdate
    ) -> ModelSettingsSnapshot:
        self._raise()
        self.calls.append(("create", display_name, update))
        return replace(
            self.snapshot,
            display_name=display_name,
            provider=update.provider,
            model=update.model,
        )

    def create_successor(
        self,
        parent_id: str,
        display_name: str,
        update: ModelConfigUpdate,
    ) -> ModelSettingsSnapshot:
        self._raise()
        self.calls.append(("successor", parent_id, display_name, update))
        return replace(
            self.snapshot,
            id=NEXT_ID,
            public_config_hash=NEXT_ID,
            display_name=display_name,
            model=update.model,
            supersedes_id=parent_id,
        )

    def get(self, config_id: str) -> ModelSettingsSnapshot:
        self._raise()
        self.calls.append(("get", config_id))
        return self.snapshot

    def list_page(
        self,
        *,
        limit: int,
        after: ModelConfigListKey | None = None,
        include_disabled: bool = False,
    ) -> ModelSettingsPage:
        self._raise()
        self.calls.append(("list", limit, after, include_disabled))
        return ModelSettingsPage(items=(self.snapshot,), next_key=self.list_next_key)

    async def test_connection(
        self, config_id: str, *, expected_revision: int
    ) -> ConnectionTestResult:
        self._raise()
        self.calls.append(("test", config_id, expected_revision))
        return ConnectionTestResult(
            config_id=config_id,
            connected=False,
            provider=self.snapshot.provider,
            model=self.snapshot.model,
            error_code=ModelErrorCode.INVALID_RESPONSE,
            status=ModelConfigStatus.FAILED,
            revision=expected_revision + 1,
            tested_at=NOW,
            last_tested_at=NOW,
        )

    def disable(
        self, config_id: str, *, expected_revision: int
    ) -> ModelSettingsSnapshot:
        self._raise()
        self.calls.append(("disable", config_id, expected_revision))
        return replace(
            self.snapshot,
            status=ModelConfigStatus.DISABLED,
            revision=expected_revision + 1,
        )


def _client(
    services: object,
    *,
    database_identity: object = _DEFAULT_STATE,
    cursor_key: object = TEST_CURSOR_KEY,
) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.state.model_settings_services_provider = lambda: services
    if database_identity is _DEFAULT_STATE:
        service_identity = getattr(services, "database_identity", None)
        if service_identity is not None:
            application.state.database_identity = service_identity
    elif database_identity is not _OMIT_STATE:
        application.state.database_identity = database_identity
    if cursor_key is not _OMIT_STATE:
        application.state.model_settings_cursor_key = cursor_key
    return TestClient(application, raise_server_exceptions=False)


def _ollama_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "display_name": "Local model",
        "provider": "ollama",
        "model": "qwen3:8b",
        "temperature": 0.1,
        "timeout": 90.0,
        "max_output": 4096,
    }
    body.update(changes)
    return body


def test_create_get_update_test_and_disable_routes_delegate_to_service() -> None:
    services = _ModelSettingsServices()
    client = _client(services)

    created = client.post("/settings/models", json=_ollama_body())
    loaded = client.get(f"/settings/models/{CONFIG_ID}")
    updated = client.put(
        f"/settings/models/{CONFIG_ID}",
        json=_ollama_body(display_name="Successor", model="qwen3:14b"),
    )
    tested = client.post(
        f"/settings/models/{CONFIG_ID}/test", json={"expected_revision": 0}
    )
    disabled = client.post(
        f"/settings/models/{CONFIG_ID}/disable",
        json={"expected_revision": 1},
    )

    assert created.status_code == 201
    assert loaded.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["supersedes_id"] == CONFIG_ID
    assert tested.status_code == 200
    assert tested.json() == {
        "config_id": CONFIG_ID,
        "connected": False,
        "provider": "ollama",
        "model": "qwen3:8b",
        "error_code": "invalid_response",
        "status": "failed",
        "revision": 1,
        "tested_at": "2026-07-07T08:30:00Z",
        "last_tested_at": "2026-07-07T08:30:00Z",
    }
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert [call[0] for call in services.calls] == [
        "create",
        "get",
        "successor",
        "test",
        "disable",
    ]
    update = cast(ModelConfigUpdate, services.calls[0][2])
    assert update.timeout_seconds == 90.0
    assert update.max_output_tokens == 4096


def test_remote_create_passes_secret_without_exposing_it_in_response_or_repr() -> None:
    services = _ModelSettingsServices()
    services.snapshot = _snapshot(
        provider=ModelProviderKind.OPENAI_COMPATIBLE,
        base_url="https://models.example.com/v1",
        api_key_configured=True,
        masked_api_key="sk-p•••••••cape",
    )
    client = _client(services)
    body = {
        **_ollama_body(
            provider="openai_compatible",
            base_url="https://models.example.com/v1",
            model="vendor-chat",
        ),
        "api_key": PLAINTEXT,
    }

    response = client.post("/settings/models", json=body)

    assert response.status_code == 201
    rendered = response.text + repr(services.calls)
    assert PLAINTEXT not in response.text
    assert "secret_reference" not in response.text
    assert "public_config_json" not in response.text
    assert "encrypted" not in response.text
    update = cast(ModelConfigUpdate, services.calls[0][2])
    assert update.api_key == SecretStr(PLAINTEXT)
    assert PLAINTEXT not in rendered


def test_list_cursor_is_canonical_filter_bound_and_not_silently_truncated() -> None:
    services = _ModelSettingsServices()
    services.list_next_key = ModelConfigListKey(id=CONFIG_ID)
    client = _client(services)

    first = client.get(
        "/settings/models", params={"limit": 1, "include_disabled": True}
    )
    cursor = first.json()["next_cursor"]
    second = client.get(
        "/settings/models",
        params={"limit": 100, "include_disabled": True, "cursor": cursor},
    )
    wrong_filter = client.get(
        "/settings/models",
        params={"limit": 1, "include_disabled": False, "cursor": cursor},
    )
    tampered = client.get(
        "/settings/models",
        params={
            "limit": 1,
            "include_disabled": True,
            "cursor": cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
        },
    )
    oversized = client.get("/settings/models", params={"cursor": "A" * 2049})

    assert first.status_code == 200
    assert first.json()["items"][0]["id"] == CONFIG_ID
    assert second.status_code == 200
    assert services.calls[-1] == (
        "list",
        100,
        ModelConfigListKey(id=CONFIG_ID),
        True,
    )
    assert wrong_filter.status_code == 422
    assert wrong_filter.json() == {"code": "invalid_cursor"}
    assert tampered.status_code == 422
    assert tampered.json() == {"code": "invalid_cursor"}
    assert oversized.status_code == 422
    assert oversized.json() == {"code": "invalid_cursor"}


@pytest.mark.parametrize(
    "cursor_key",
    [_OMIT_STATE, None, "not-bytes", b"short", b"x" * 33],
)
def test_list_requires_strict_server_cursor_key(cursor_key: object) -> None:
    response = _client(_ModelSettingsServices(), cursor_key=cursor_key).get(
        "/settings/models"
    )

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


def test_cursor_signed_with_different_server_key_is_rejected() -> None:
    services = _ModelSettingsServices()
    services.list_next_key = ModelConfigListKey(id=CONFIG_ID)
    first = _client(services, cursor_key=TEST_CURSOR_KEY).get("/settings/models")
    cursor = first.json()["next_cursor"]

    response = _client(services, cursor_key=OTHER_CURSOR_KEY).get(
        "/settings/models", params={"cursor": cursor}
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


def test_public_sha256_cursor_recomputation_cannot_forge_id() -> None:
    services = _ModelSettingsServices()
    services.list_next_key = ModelConfigListKey(id=CONFIG_ID)
    client = _client(services)
    first = client.get("/settings/models")
    token = first.json()["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    payload["id"] = NEXT_ID
    body = {key: value for key, value in payload.items() if key != "checksum"}
    canonical_body = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    payload["checksum"] = hashlib.sha256(
        b"stock-desk:model-settings-cursor:v1\x00" + canonical_body
    ).hexdigest()
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        .decode("ascii")
        .rstrip("=")
    )

    response = client.get("/settings/models", params={"cursor": forged})

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ModelNotFound("unsafe"), 404, "not_found"),
        (ModelSettingsConflict("unsafe"), 409, "state_conflict"),
        (ModelNotVerified("unsafe"), 409, "model_not_verified"),
        (ModelSettingsValidationError(PLAINTEXT), 422, "invalid_request"),
        (
            ModelSettingsSecureStorageError(PLAINTEXT),
            503,
            "secure_storage_unavailable",
        ),
        (ModelSettingsStorageError(PLAINTEXT), 503, "storage_unavailable"),
    ],
)
def test_domain_errors_have_fixed_safe_responses(
    error: Exception, status_code: int, code: str
) -> None:
    services = _ModelSettingsServices()
    services.error = error
    response = _client(services).get(f"/settings/models/{CONFIG_ID}")

    assert response.status_code == status_code
    assert response.json() == {"code": code}
    assert PLAINTEXT not in response.text
    assert "unsafe" not in response.text


def test_validation_errors_never_echo_secret_or_authorization() -> None:
    services = _ModelSettingsServices()
    client = _client(services)

    extra = client.post(
        "/settings/models",
        headers={"Authorization": "Bearer plaintext-authorization"},
        json={**_ollama_body(), "api_key": PLAINTEXT, "unexpected": PLAINTEXT},
    )
    malformed = client.post(
        "/settings/models",
        content=(
            f'{{"display_name":"Local","provider":"ollama","api_key":"{PLAINTEXT}"'
        ),
        headers={
            "content-type": "application/json",
            "Authorization": "Bearer plaintext-authorization",
        },
    )

    assert extra.status_code == 422
    assert extra.json() == {"code": "invalid_request"}
    assert malformed.status_code == 422
    assert malformed.json() == {"code": "invalid_request"}
    rendered = extra.text + malformed.text
    assert PLAINTEXT not in rendered
    assert "plaintext-authorization" not in rendered


def test_openapi_marks_api_key_write_only_and_excludes_unrelated_schemas() -> None:
    schema = _client(_ModelSettingsServices()).get("/openapi.json").json()
    serialized = str(schema)
    create_schema = schema["components"]["schemas"]["ModelSettingsCreateRequest"]

    assert create_schema["properties"]["api_key"]["writeOnly"] is True
    assert "example" not in create_schema["properties"]["api_key"]
    assert "default" not in create_schema["properties"]["api_key"]
    assert "secret_reference" not in serialized
    assert PLAINTEXT not in serialized
    assert "formula" not in serialized.lower()
    assert "backtest" not in serialized.lower()


def test_optional_database_identity_boundary_rejects_mismatch() -> None:
    services = _ModelSettingsServices()
    response = _client(
        services,
        database_identity=SimpleNamespace(kind="test", value="different"),
    ).get(f"/settings/models/{CONFIG_ID}")

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert services.calls == []


def test_configured_database_identity_boundary_rejects_unbound_service() -> None:
    services = SimpleNamespace(get=lambda _config_id: _snapshot())
    response = _client(
        services,
        database_identity=SimpleNamespace(kind="test", value="expected"),
    ).get(f"/settings/models/{CONFIG_ID}")

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


def test_missing_application_database_identity_is_fail_closed() -> None:
    services = _ModelSettingsServices()
    response = _client(services, database_identity=_OMIT_STATE).get(
        f"/settings/models/{CONFIG_ID}"
    )

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert services.calls == []


def test_list_bounds_and_remote_without_key_are_invalid_requests() -> None:
    client = _client(_ModelSettingsServices())

    too_small = client.get("/settings/models", params={"limit": 0})
    too_large = client.get("/settings/models", params={"limit": 101})
    remote_without_key = client.post(
        "/settings/models",
        json=_ollama_body(
            provider="openai_compatible",
            base_url="https://models.example.com/v1",
            model="vendor-chat",
        ),
    )

    assert too_small.status_code == 422
    assert too_small.json() == {"code": "invalid_request"}
    assert too_large.status_code == 422
    assert too_large.json() == {"code": "invalid_request"}
    assert remote_without_key.status_code == 422
    assert remote_without_key.json() == {"code": "invalid_request"}


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature": 1},
        {"temperature": True},
        {"timeout": 90},
        {"timeout": True},
        {"max_output": 4096.0},
        {"max_output": True},
    ],
)
def test_model_write_numbers_require_exact_json_types(
    changes: dict[str, object],
) -> None:
    response = _client(_ModelSettingsServices()).post(
        "/settings/models", json=_ollama_body(**changes)
    )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


def test_real_service_supports_ollama_without_master_key_and_cas_disable(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'model-api.db'}"
    migrate(database_url)
    catalog = AnalysisModelCatalog(create_engine_for_url(database_url))
    service = ModelSettingsService(catalog=catalog, secret_store=None)
    client = _client(
        service,
        database_identity=catalog.database_identity,
    )
    try:
        created = client.post("/settings/models", json=_ollama_body())
        disabled = client.post(
            f"/settings/models/{created.json()['id']}/disable",
            json={"expected_revision": created.json()["revision"]},
        )
        stale = client.post(
            f"/settings/models/{created.json()['id']}/disable",
            json={"expected_revision": created.json()["revision"]},
        )
    finally:
        catalog.close()

    assert service.database_identity == catalog.database_identity
    assert created.status_code == 201
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert stale.status_code == 409
    assert stale.json() == {"code": "state_conflict"}


def test_real_remote_service_without_secure_store_is_503(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'remote-model-api.db'}"
    migrate(database_url)
    catalog = AnalysisModelCatalog(create_engine_for_url(database_url))
    service = ModelSettingsService(catalog=catalog, secret_store=None)
    try:
        response = _client(service).post(
            "/settings/models",
            json={
                **_ollama_body(
                    provider="openai_compatible",
                    base_url="https://models.example.com/v1",
                    model="vendor-chat",
                ),
                "api_key": PLAINTEXT,
            },
        )
    finally:
        catalog.close()

    assert response.status_code == 503
    assert response.json() == {"code": "secure_storage_unavailable"}
    assert PLAINTEXT not in response.text


def _secure_model_service(
    tmp_path: Path, filename: str
) -> tuple[ModelSettingsService, AnalysisModelCatalog]:
    database_url = f"sqlite:///{tmp_path / filename}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    catalog = AnalysisModelCatalog(engine)
    store = SecretStore(
        engine,
        Settings(master_key=SecretStr(Fernet.generate_key().decode("ascii"))),
    )
    return ModelSettingsService(catalog=catalog, secret_store=store), catalog


def _remote_update(
    *,
    provider: ModelProviderKind = ModelProviderKind.OPENAI_COMPATIBLE,
    base_url: str = "https://models.example.com/v1",
    model: str = "vendor-chat",
    api_key: str | None = PLAINTEXT,
) -> ModelConfigUpdate:
    return ModelConfigUpdate(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=None if api_key is None else SecretStr(api_key),
    )


def test_successor_without_key_reuses_only_same_provider_and_base_url(
    tmp_path: Path,
) -> None:
    service, catalog = _secure_model_service(tmp_path, "same-scope.db")
    try:
        parent = service.create("Remote", _remote_update())
        successor = service.create_successor(
            parent.id,
            "Remote successor",
            _remote_update(model="vendor-chat-v2", api_key=None),
        )
    finally:
        catalog.close()

    assert successor.supersedes_id == parent.id
    assert successor.api_key_configured is True


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        (ModelProviderKind.DEEPSEEK, "https://api.deepseek.com"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://other.example.com/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://models.example.com/v2"),
    ],
)
def test_successor_without_key_rejects_cross_scope_credential_reuse(
    tmp_path: Path,
    provider: ModelProviderKind,
    base_url: str,
) -> None:
    service, catalog = _secure_model_service(
        tmp_path, f"cross-scope-{provider.value}-{base_url[-1]}.db"
    )
    try:
        parent = service.create("Remote", _remote_update())
        with pytest.raises(ModelSettingsValidationError):
            service.create_successor(
                parent.id,
                "Cross scope",
                _remote_update(
                    provider=provider,
                    base_url=base_url,
                    model="other-model",
                    api_key=None,
                ),
            )
    finally:
        catalog.close()


def test_successor_missing_parent_secret_row_is_secure_storage_error(
    tmp_path: Path,
) -> None:
    service, catalog = _secure_model_service(tmp_path, "missing-secret.db")
    try:
        parent = service.create("Remote", _remote_update())
        with catalog.engine.begin() as connection:
            connection.execute(text("DELETE FROM app_setting"))
        with pytest.raises(ModelSettingsSecureStorageError):
            service.create_successor(
                parent.id,
                "Missing secret",
                _remote_update(model="vendor-chat-v2", api_key=None),
            )
    finally:
        catalog.close()


def test_ollama_to_remote_successor_without_key_is_invalid_request(
    tmp_path: Path,
) -> None:
    service, catalog = _secure_model_service(tmp_path, "local-to-remote.db")
    try:
        parent = service.create(
            "Local",
            ModelConfigUpdate(
                provider=ModelProviderKind.OLLAMA,
                model="qwen3:8b",
            ),
        )
        with pytest.raises(ModelSettingsValidationError):
            service.create_successor(
                parent.id,
                "Remote",
                _remote_update(api_key=None),
            )
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": -1},
        {"revision": True},
        {"connected": False, "error_code": None},
        {"error_code": "invalid_response"},
        {
            "connected": True,
            "error_code": ModelErrorCode.INVALID_RESPONSE,
            "status": ModelConfigStatus.VERIFIED,
        },
        {
            "connected": True,
            "error_code": None,
            "status": ModelConfigStatus.FAILED,
        },
        {
            "connected": False,
            "error_code": ModelErrorCode.INVALID_RESPONSE,
            "status": ModelConfigStatus.VERIFIED,
        },
        {"tested_at": NOW.replace(tzinfo=None)},
        {"last_tested_at": NOW.replace(tzinfo=None)},
        {"last_tested_at": NOW.replace(microsecond=1)},
    ],
)
def test_connection_test_result_rejects_invalid_state(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "config_id": CONFIG_ID,
        "connected": False,
        "provider": ModelProviderKind.OLLAMA,
        "model": "qwen3:8b",
        "error_code": ModelErrorCode.INVALID_RESPONSE,
        "status": ModelConfigStatus.FAILED,
        "revision": 1,
        "tested_at": NOW,
        "last_tested_at": NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError):
        ConnectionTestResult(**cast(Any, values))


def test_connection_test_response_repeats_domain_invariants() -> None:
    with pytest.raises(ValidationError):
        ModelConnectionTestResponse(
            config_id=CONFIG_ID,
            connected=False,
            provider=ModelProviderKind.OLLAMA,
            model="qwen3:8b",
            error_code=None,
            status=ModelConfigStatus.FAILED,
            revision=1,
            tested_at=NOW,
            last_tested_at=NOW,
        )
    with pytest.raises(ValidationError):
        ModelConnectionTestResponse(
            config_id=CONFIG_ID,
            connected=False,
            provider=ModelProviderKind.OLLAMA,
            model="qwen3:8b",
            error_code="unknown_error",
            status=ModelConfigStatus.FAILED,
            revision=1,
            tested_at=NOW,
            last_tested_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"public_config_hash": NEXT_ID},
        {"revision": -1},
        {"revision": True},
        {"api_key_configured": True, "masked_api_key": None},
        {"masked_api_key": "plaintext"},
        {
            "provider": ModelProviderKind.OPENAI_COMPATIBLE,
            "base_url": "https://models.example.com/v1",
            "api_key_configured": False,
            "masked_api_key": None,
        },
        {
            "provider": ModelProviderKind.OLLAMA,
            "api_key_configured": True,
            "masked_api_key": "[MASKED]",
        },
        {"status": ModelConfigStatus.VERIFIED},
        {
            "status": ModelConfigStatus.FAILED,
            "last_tested_at": NOW,
            "error_code": "INVALID CODE",
        },
        {"created_at": NOW.replace(year=2027)},
        {"supersedes_id": CONFIG_ID},
    ],
)
def test_model_settings_response_rejects_invalid_internal_snapshot(
    changes: dict[str, object],
) -> None:
    services = _ModelSettingsServices()
    services.snapshot = _snapshot(**changes)

    response = _client(services).get(f"/settings/models/{CONFIG_ID}")

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


def test_model_settings_response_constructor_enforces_snapshot_invariants() -> None:
    with pytest.raises(ValidationError):
        ModelSettingsResponse(
            id=CONFIG_ID,
            public_config_hash=NEXT_ID,
            display_name="Local",
            provider=ModelProviderKind.OLLAMA,
            base_url="http://127.0.0.1:11434",
            model="qwen3:8b",
            temperature=0.1,
            timeout=90.0,
            max_output=4096,
            api_key_configured=False,
            masked_api_key=None,
            status=ModelConfigStatus.UNVERIFIED,
            revision=0,
            verified_at=None,
            last_tested_at=None,
            error_code=None,
            supersedes_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
