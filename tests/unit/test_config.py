from collections.abc import Iterator
from pathlib import Path

import pytest

from stock_desk.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_settings_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_defaults() -> None:
    settings = Settings()

    assert settings.app_name == "stock-desk"
    assert settings.data_dir == Path("data")
    assert settings.database_url == "sqlite:///data/stock-desk.db"


def test_settings_use_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOCK_DESK_APP_NAME", "Personal Desk")
    monkeypatch.setenv("STOCK_DESK_DATA_DIR", "/tmp/stock-desk-data")
    monkeypatch.setenv(
        "STOCK_DESK_DATABASE_URL",
        "sqlite:////tmp/personal-stock-desk.db",
    )

    settings = Settings()

    assert settings.app_name == "Personal Desk"
    assert settings.data_dir == Path("/tmp/stock-desk-data")
    assert settings.database_url == "sqlite:////tmp/personal-stock-desk.db"


def test_get_settings_caches_until_explicitly_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOCK_DESK_APP_NAME", "First Desk")
    initial = get_settings()

    monkeypatch.setenv("STOCK_DESK_APP_NAME", "Second Desk")

    assert get_settings() is initial
    assert get_settings().app_name == "First Desk"

    get_settings.cache_clear()
    assert get_settings().app_name == "Second Desk"
