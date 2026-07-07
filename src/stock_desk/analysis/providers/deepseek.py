from __future__ import annotations

from typing import ClassVar

import httpx2

from stock_desk.analysis.model_config import (
    DEEPSEEK_BASE_URL,
    MODEL_API_KEY_SECRET_NAME,
    ModelProviderKind,
)
from stock_desk.analysis.providers.base import ModelSecretReader
from stock_desk.analysis.providers.openai_compatible import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """Named adapter that isolates DeepSeek defaults from generic providers."""

    provider: ClassVar[str] = ModelProviderKind.DEEPSEEK.value

    def __init__(
        self,
        *,
        model: str,
        secret_store: ModelSecretReader,
        base_url: str = DEEPSEEK_BASE_URL,
        secret_name: str = MODEL_API_KEY_SECRET_NAME,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            model=model,
            secret_store=secret_store,
            secret_name=secret_name,
            transport=transport,
        )
