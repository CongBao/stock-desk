"""Pluggable model providers for structured analysis."""

from stock_desk.analysis.providers.base import (
    ModelConnectionResult,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.providers.deepseek import DeepSeekProvider
from stock_desk.analysis.providers.ollama import OllamaProvider
from stock_desk.analysis.providers.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "DeepSeekProvider",
    "ModelConnectionResult",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "OllamaProvider",
    "OpenAICompatibleProvider",
]
