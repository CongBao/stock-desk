from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "docs/model-providers.md"


def test_model_provider_guide_is_bilingual_public_and_complete() -> None:
    content = DOCUMENT.read_text(encoding="utf-8")
    lowered = content.lower()

    assert "# Model providers" in content
    assert "# 模型提供商" in content
    assert "DeepSeek" in content
    assert "OpenAI-compatible" in content
    assert "Ollama" in content
    assert "https://api-docs.deepseek.com/" in content
    assert "https://platform.openai.com/docs/api-reference" in content
    assert "https://docs.ollama.com/api" in content
    for english in (
        "base URL",
        "model name",
        "connection test",
        "verified-only",
        "successor version",
        "disable",
        "master key",
        "encrypted",
        "masked",
        "secure_storage_unavailable",
    ):
        assert english.lower() in lowered
    for chinese in (
        "服务地址",
        "模型名",
        "连接测试",
        "仅已验证",
        "后继版本",
        "禁用",
        "主密钥",
        "加密",
        "掩码",
        "本地部署",
    ):
        assert chinese in content


def test_model_provider_guide_contains_no_real_or_secret_like_key() -> None:
    content = DOCUMENT.read_text(encoding="utf-8")

    assert "OpenSpec" not in content
    assert "sk-never-return" not in content
    assert re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", content) is None
    assert re.search(r"\b[A-Za-z0-9_-]{32,}\b", content) is None
    assert "${MODEL_API_KEY}" in content


def test_model_provider_guide_matches_remote_submission_and_worker_failure_semantics() -> (
    None
):
    content = DOCUMENT.read_text(encoding="utf-8")

    assert (
        "remote model create, update, connection test, and analysis submission"
        in content
    )
    assert "远程模型的创建、更新、连接测试和分析提交" in content
    assert "already queued" in content
    assert "已经排队" in content
    assert content.count("`analysis_worker_failed`") >= 2
    assert "`authentication`" in content
    assert "authentication_failed" not in content
