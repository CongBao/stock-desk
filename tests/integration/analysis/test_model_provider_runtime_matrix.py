from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text

from stock_desk.analysis.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from stock_desk.analysis.repository import AnalysisExecutionConfig
from tests.acceptance.test_analysis_flow import (
    DeterministicProvider,
    SYMBOL,
    _harness,
)


class CapturingDeterministicProvider(DeterministicProvider):
    def __init__(self, *, provider: str, model: str) -> None:
        super().__init__(provider=provider, model=model)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


@pytest.mark.parametrize(
    ("provider", "base_url", "model", "api_key", "runtime"),
    (
        (
            "deepseek",
            "https://api.deepseek.com",
            "deepseek-v4",
            "deepseek-runtime-matrix-secret",
            (0.2, 61.0, 5001),
        ),
        (
            "openai_compatible",
            "https://llm.example.cn/v1",
            "qwen-max",
            "compatible-runtime-matrix-secret",
            (0.3, 62.0, 5002),
        ),
        (
            "ollama",
            "http://127.0.0.1:11434",
            "qwen3:8b",
            None,
            (0.4, 63.0, 5003),
        ),
    ),
    ids=("deepseek", "openai-compatible", "ollama"),
)
def test_real_model_provider_run_freezes_endpoint_secret_reference_and_runtime(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None,
    runtime: tuple[float, float, int],
) -> None:
    captured: list[AnalysisExecutionConfig] = []
    providers: list[CapturingDeterministicProvider] = []

    def capture_execution(execution: AnalysisExecutionConfig) -> ModelProvider:
        captured.append(execution)
        provider_adapter = CapturingDeterministicProvider(
            provider=execution.provider,
            model=execution.model,
        )
        providers.append(provider_adapter)
        return cast(ModelProvider, provider_adapter)

    with _harness(tmp_path, provider_builder=capture_execution) as harness:
        temperature, timeout, max_output = runtime
        body: dict[str, Any] = {
            "display_name": f"Runtime matrix {provider}",
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
            "timeout": timeout,
            "max_output": max_output,
        }
        if api_key is not None:
            body["api_key"] = api_key

        created = harness.client.post("/api/settings/models", json=body)
        assert created.status_code == 201
        config_id = cast(str, created.json()["id"])

        reloaded = harness.client.get(f"/api/settings/models/{config_id}")
        assert reloaded.status_code == 200
        safe_config = reloaded.json()
        assert safe_config["provider"] == provider
        assert safe_config["base_url"] == base_url
        assert safe_config["model"] == model
        assert safe_config["temperature"] == temperature
        assert safe_config["timeout"] == timeout
        assert safe_config["max_output"] == max_output
        assert safe_config["api_key_configured"] is (api_key is not None)
        assert (safe_config["masked_api_key"] is not None) is (api_key is not None)

        tested = harness.client.post(
            f"/api/settings/models/{config_id}/test",
            json={"expected_revision": safe_config["revision"]},
        )
        assert tested.status_code == 200
        assert tested.json()["status"] == "verified"

        submitted = harness.client.post(
            "/api/analysis",
            json={
                "symbol": SYMBOL,
                "model_config_id": config_id,
                "retry": {"max_retries": 0},
            },
        )
        assert submitted.status_code == 202
        run_id = cast(str, submitted.json()["run_id"])

        rotated_key = None if api_key is None else f"rotated-{provider}-runtime-secret"
        successor_body: dict[str, Any] = {
            "display_name": f"Runtime matrix {provider} successor",
            "provider": provider,
            "base_url": (
                "https://rotated.example.cn/v1"
                if provider == "openai_compatible"
                else "http://127.0.0.1:11435"
                if provider == "ollama"
                else base_url
            ),
            "model": f"{model}-successor",
            "temperature": 0.9,
            "timeout": 99.0,
            "max_output": 9000,
        }
        if rotated_key is not None:
            successor_body["api_key"] = rotated_key
        successor = harness.client.put(
            f"/api/settings/models/{config_id}", json=successor_body
        )
        assert successor.status_code == 200
        assert successor.json()["id"] != config_id
        assert successor.json()["supersedes_id"] == config_id

        completed = harness.run_worker()
        assert getattr(completed, "status") == "succeeded"
        detail = harness.client.get(f"/api/analysis/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "succeeded"

        execution = harness.repository.load_execution_config(run_id)
        with harness.engine.connect() as connection:
            public_config_json = connection.execute(
                text(
                    "SELECT public_config_json FROM analysis_model_config "
                    "WHERE id=:config_id"
                ),
                {"config_id": config_id},
            ).scalar_one()
            run_config_json = connection.execute(
                text("SELECT model_config_json FROM analysis_run WHERE id=:run_id"),
                {"run_id": run_id},
            ).scalar_one()
            database_text = " ".join(
                str(value)
                for row in connection.exec_driver_sql(
                    "SELECT key, encrypted_value FROM app_setting"
                )
                for value in row
            )

        persisted_public = json.loads(str(public_config_json))
        persisted_run = json.loads(str(run_config_json))
        assert persisted_run == persisted_public
        assert execution.model_config_id == config_id
        assert execution.public_config.model_dump(mode="json") == persisted_public
        assert persisted_run == {
            "api_key_configured": api_key is not None,
            "base_url": base_url,
            "max_output_tokens": max_output,
            "model": model,
            "provider": provider,
            "schema_version": "analysis-model-public-v1",
            "secret_reference_id": persisted_public["secret_reference_id"],
            "temperature": temperature,
            "timeout_seconds": timeout,
        }
        if api_key is None:
            assert persisted_run["secret_reference_id"] is None
        else:
            assert str(persisted_run["secret_reference_id"]).startswith(
                "analysis_model_api_key_"
            )

        assert len(captured) == 1
        assert captured[0].public_config == execution.public_config
        assert len(providers) == 1
        assert providers[0].requests
        assert all(
            request.temperature == temperature for request in providers[0].requests
        )
        assert all(
            request.timeout_seconds == timeout for request in providers[0].requests
        )
        assert all(
            request.max_output_tokens == max_output for request in providers[0].requests
        )

        with caplog.at_level(logging.WARNING):
            for secret in (api_key, rotated_key):
                if secret is not None:
                    logging.getLogger("stock_desk.test.runtime_matrix").warning(
                        "configured secret probe=%s", secret
                    )

        public_boundary = "\n".join(
            (
                created.text,
                reloaded.text,
                tested.text,
                submitted.text,
                successor.text,
                detail.text,
                caplog.text,
                database_text,
            )
        )
        for secret in (api_key, rotated_key):
            if secret is not None:
                assert secret not in public_boundary
