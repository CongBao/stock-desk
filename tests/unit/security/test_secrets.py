from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import cast

from cryptography.fernet import Fernet
from pydantic import SecretStr
import pytest
from sqlalchemy import Engine, select, text

import stock_desk.security.secrets as secrets_module
from stock_desk.config import Settings
from stock_desk.security.secrets import (
    SecretConfigurationError,
    SecretDecryptionError,
    SecretNotFoundError,
    SecretStore,
    SecretValidationError,
    mask_secret,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import AppSetting


@pytest.fixture
def secret_database(tmp_path: Path) -> Iterator[tuple[Engine, str]]:
    url = f"sqlite:///{tmp_path / 'secrets.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    yield engine, url
    engine.dispose()


def _settings(key: bytes) -> Settings:
    return Settings(master_key=SecretStr(key.decode("ascii")))


def _stored_row(engine: Engine, name: str) -> tuple[str, str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT key, encrypted_value, updated_at "
                "FROM app_setting WHERE key = :key"
            ),
            {"key": f"secret.{name}"},
        ).one()
    return str(row.key), str(row.encrypted_value), str(row.updated_at)


def _stored_timestamp(engine: Engine, name: str) -> datetime:
    with engine.connect() as connection:
        stored = connection.execute(
            select(AppSetting.updated_at).where(AppSetting.key == f"secret.{name}")
        ).scalar_one()
    if stored.tzinfo is None:
        return stored.replace(tzinfo=timezone.utc)
    return stored


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "abc",
        "abcd",
        "abcde",
        "•••••••",
        "abcd•••••••efgh",
    ],
)
def test_mask_secret_never_returns_or_contains_plaintext(value: str) -> None:
    masked = mask_secret(value)

    assert masked != value
    assert value not in masked


def test_mask_secret_keeps_the_expected_long_value_hint() -> None:
    assert mask_secret("sk-123456789") == "sk-1•••••••6789"


def test_secret_store_encrypts_and_exposes_only_intended_views(
    secret_database: tuple[Engine, str],
) -> None:
    engine, _url = secret_database
    key = Fernet.generate_key()
    plaintext = "sk-private-provider-token"
    store = SecretStore(engine, _settings(key))

    store.save_secret("deepseek_api_key", plaintext)

    row_key, ciphertext, _updated_at = _stored_row(engine, "deepseek_api_key")
    assert row_key == "secret.deepseek_api_key"
    assert plaintext not in ciphertext
    assert key.decode("ascii") not in ciphertext
    assert ciphertext.isascii()
    assert store.has_secret("deepseek_api_key") is True
    assert store.has_secret("missing") is False
    assert store.read_secret_for_server_call("deepseek_api_key") == plaintext
    assert store.masked_secret("deepseek_api_key") != plaintext
    assert plaintext not in store.masked_secret("deepseek_api_key")
    assert plaintext not in repr(store)
    assert not hasattr(store, "list_secrets")
    assert not hasattr(store, "export_secrets")


def test_repeated_save_uses_fresh_ciphertext_and_updates_timestamp(
    secret_database: tuple[Engine, str],
) -> None:
    engine, _url = secret_database
    store = SecretStore(engine, _settings(Fernet.generate_key()))

    store.save_secret("provider_key", "same-value")
    _key, first_token, first_updated = _stored_row(engine, "provider_key")
    store.save_secret("provider_key", "same-value")
    _key, second_token, second_updated = _stored_row(engine, "provider_key")

    assert first_token != second_token
    assert second_updated >= first_updated
    assert store.read_secret_for_server_call("provider_key") == "same-value"


def test_concurrent_upsert_remains_readable(
    secret_database: tuple[Engine, str],
) -> None:
    engine, _url = secret_database
    store = SecretStore(engine, _settings(Fernet.generate_key()))
    values = [f"token-{index}" for index in range(8)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda value: store.save_secret("shared_key", value), values))

    assert store.read_secret_for_server_call("shared_key") in values
    assert _stored_row(engine, "shared_key")[1] not in values


def test_out_of_order_concurrent_upserts_never_regress_updated_at(
    secret_database: tuple[Engine, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _url = secret_database
    store = SecretStore(engine, _settings(Fernet.generate_key()))
    older = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    local = threading.local()
    older_sampled = threading.Event()
    allow_older_write = threading.Event()

    def controlled_clock() -> datetime:
        sampled = cast(datetime, getattr(local, "sampled"))
        if sampled == older:
            older_sampled.set()
            if not allow_older_write.wait(timeout=5):
                raise RuntimeError("Timed out coordinating secret upsert test")
        return sampled

    monkeypatch.setattr(secrets_module, "_utc_now", controlled_clock)

    def save_with_timestamp(value: str, sampled: datetime) -> None:
        local.sampled = sampled
        store.save_secret("ordered_key", value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        older_write = executor.submit(save_with_timestamp, "older", older)
        assert older_sampled.wait(timeout=1)
        newer_write = executor.submit(save_with_timestamp, "newer", newer)
        try:
            newer_write.result(timeout=5)
            timestamp_after_newer_write = _stored_timestamp(engine, "ordered_key")
        finally:
            allow_older_write.set()
        older_write.result(timeout=5)

    assert timestamp_after_newer_write == newer
    assert _stored_timestamp(engine, "ordered_key") >= timestamp_after_newer_write


@pytest.mark.parametrize(
    "name",
    ["", "Upper", "with-dash", "has space", "_leading", "x" * 65],
)
def test_secret_names_are_strictly_validated(
    secret_database: tuple[Engine, str], name: str
) -> None:
    engine, _url = secret_database
    store = SecretStore(engine, _settings(Fernet.generate_key()))

    with pytest.raises(SecretValidationError, match="Secret name is invalid"):
        store.save_secret(name, "value")


@pytest.mark.parametrize("value", ["", None, b"bytes"])
def test_secret_values_must_be_nonempty_strings(
    secret_database: tuple[Engine, str], value: object
) -> None:
    engine, _url = secret_database
    store = SecretStore(engine, _settings(Fernet.generate_key()))

    with pytest.raises(SecretValidationError, match="Secret value is invalid"):
        store.save_secret("provider_key", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("configured_key", [None, "", "invalid-fernet-key"])
def test_missing_or_invalid_master_key_raises_generic_configuration_error(
    secret_database: tuple[Engine, str], configured_key: str | None
) -> None:
    engine, _url = secret_database
    settings = Settings(
        master_key=SecretStr(configured_key) if configured_key is not None else None
    )

    with pytest.raises(SecretConfigurationError) as captured:
        SecretStore(engine, settings)

    message = str(captured.value)
    assert message == "STOCK_DESK_MASTER_KEY is missing or invalid"
    if configured_key:
        assert configured_key not in message


def test_missing_wrong_key_and_tampering_raise_generic_errors(
    secret_database: tuple[Engine, str],
) -> None:
    engine, _url = secret_database
    plaintext = "super-secret-value"
    writer = SecretStore(engine, _settings(Fernet.generate_key()))

    with pytest.raises(SecretNotFoundError) as missing:
        writer.read_secret_for_server_call("missing")
    assert str(missing.value) == "Secret is not configured"

    writer.save_secret("provider_key", plaintext)
    ciphertext = _stored_row(engine, "provider_key")[1]
    wrong_key_store = SecretStore(engine, _settings(Fernet.generate_key()))
    with pytest.raises(SecretDecryptionError) as wrong_key:
        wrong_key_store.read_secret_for_server_call("provider_key")

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE app_setting SET encrypted_value = :value WHERE key = :key"),
            {"value": f"{ciphertext[:-1]}x", "key": "secret.provider_key"},
        )
    with pytest.raises(SecretDecryptionError) as tampered:
        writer.read_secret_for_server_call("provider_key")

    for captured in (wrong_key, tampered):
        message = str(captured.value)
        assert message == "Stored secret could not be decrypted"
        assert plaintext not in message
        assert ciphertext not in message
        assert Fernet.generate_key().decode("ascii") not in message


def test_has_secret_does_not_decrypt(
    secret_database: tuple[Engine, str],
) -> None:
    engine, _url = secret_database
    writer = SecretStore(engine, _settings(Fernet.generate_key()))
    writer.save_secret("provider_key", "value")

    store_with_wrong_key = SecretStore(engine, _settings(Fernet.generate_key()))

    assert store_with_wrong_key.has_secret("provider_key") is True
