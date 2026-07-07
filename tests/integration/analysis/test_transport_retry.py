from __future__ import annotations

from stock_desk.analysis.providers.base import ModelTransportError
from stock_desk.analysis.retry import classify_retry


def test_transport_connection_failure_is_not_retryable_or_secret_bearing() -> None:
    decision = classify_retry(ModelTransportError("secret-token-must-never-persist"))

    assert decision.retryable is False
    assert decision.code == "model_transport"
    assert decision.safe_message == "model provider transport failed"
    assert "secret" not in decision.safe_message
    assert "token" not in decision.safe_message
