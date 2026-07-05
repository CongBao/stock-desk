from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from Stock Desk environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="STOCK_DESK_",
        env_file=".env",
    )

    app_name: str = "stock-desk"
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/stock-desk.db"
    master_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
