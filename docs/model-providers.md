# Model providers

Stock Desk supports DeepSeek, OpenAI-compatible services, and Ollama. A model setting contains a display name, provider, base URL, model name, temperature, request timeout, and maximum output tokens. Remote providers also require an API key.

| Provider | Base URL | Model name | Credentials |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | A model listed by your DeepSeek account | API key |
| OpenAI-compatible | The service's HTTPS API root, commonly ending in `/v1` | The exact model identifier published by that service | API key |
| Ollama | `http://127.0.0.1:11434` | A locally installed tag such as `qwen3:8b` | None |

Provider references: [DeepSeek API](https://api-docs.deepseek.com/), [OpenAI API reference](https://platform.openai.com/docs/api-reference), and [Ollama API](https://docs.ollama.com/api).

## Safe workflow

1. Create a setting under `/api/settings/models` with the provider parameters. Supply remote credentials through a secret variable such as `${MODEL_API_KEY}`; never place a key in source control, logs, screenshots, or support reports.
2. Run the connection test. The test records a bounded public error code and never returns the credential.
3. Analysis is verified-only: only an enabled setting whose latest connection test succeeded can be selected. An unverified setting is rejected safely.
4. Settings are immutable. To change a base URL, model name, or runtime parameter, create a successor version with `PUT /api/settings/models/{config_id}` and verify it. Disable an obsolete version with `POST /api/settings/models/{config_id}/disable`.

API keys are encrypted at rest with `STOCK_DESK_MASTER_KEY`. Public responses expose only an `api_key_configured` flag and a masked value. The write schema marks `api_key` as write-only. If no master key is configured, the application, Ollama settings, and local analysis remain available, but remote model create, update, connection test, and analysis submission return `secure_storage_unavailable`. A damaged key or storage/database identity mismatch fails closed with a storage error; restore the correct key and database instead of replacing either blindly. If a remote analysis is already queued and credential storage becomes unreadable before worker execution, the task terminates safely with `analysis_worker_failed` rather than exposing a credential or provider response.

Common safe errors include `model_not_verified` (verify the active version), `secure_storage_unavailable` (configure or restore the master key), `storage_unavailable` (check the database and its identity), and provider connection codes such as `timeout`, `authentication`, or `invalid_response`. Error bodies do not include raw provider responses or credentials.

## Local Ollama example

Install Ollama using its official instructions, then run:

```console
ollama pull qwen3:8b
ollama serve
```

Create an Ollama setting with base URL `http://127.0.0.1:11434`, model name `qwen3:8b`, temperature `0.1`, timeout `90.0`, and a maximum output of `4096`. Run the connection test before selecting it for analysis. Local availability and model behavior depend on the installed Ollama version, model, and hardware.

# 模型提供商

Stock Desk 支持 DeepSeek、OpenAI-compatible 服务和 Ollama。每项模型设置包含显示名称、提供商、服务地址（base URL）、模型名、温度、请求超时和最大输出 token 数；远程提供商还需要 API 密钥。

| 提供商 | 服务地址 | 模型名 | 凭据 |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | DeepSeek 账户可用的准确模型标识 | API 密钥 |
| OpenAI-compatible | 服务商公布的 HTTPS API 根地址，通常以 `/v1` 结尾 | 服务商公布的准确模型标识 | API 密钥 |
| Ollama | `http://127.0.0.1:11434` | 已在本地安装的标签，例如 `qwen3:8b` | 无 |

官方参考：[DeepSeek API](https://api-docs.deepseek.com/)、[OpenAI API 参考](https://platform.openai.com/docs/api-reference) 和 [Ollama API](https://docs.ollama.com/api)。

## 安全使用流程

1. 在 `/api/settings/models` 创建设置并填写参数。远程密钥应从 `${MODEL_API_KEY}` 等秘密变量传入，不得写入源码、日志、截图或支持报告。
2. 执行连接测试。测试只保存受限的公开错误码，不返回凭据。
3. 分析采用仅已验证（verified-only）规则：只有最近连接测试成功且未禁用的设置才能被选择；未验证设置会被安全拒绝。
4. 模型设置不可原地修改。变更服务地址、模型名或运行参数时，通过 `PUT /api/settings/models/{config_id}` 创建后继版本，完成验证后再使用；通过 `POST /api/settings/models/{config_id}/disable` 禁用旧版本。

API 密钥使用 `STOCK_DESK_MASTER_KEY` 加密落盘；公开响应只显示“已配置”状态和掩码值，写入 schema 将 `api_key` 标记为 write-only。未配置主密钥时，应用、Ollama 设置和本地分析仍可用，但远程模型的创建、更新、连接测试和分析提交会返回 `secure_storage_unavailable`。主密钥损坏或存储/数据库身份不匹配时系统会 fail closed；应恢复正确的密钥和数据库，不要盲目替换。如果远程分析已经排队，但凭据存储在 worker 执行前失效，任务会安全结束并记录 `analysis_worker_failed`，不会暴露凭据或提供商原始响应。

常见安全错误包括：`model_not_verified`（验证当前版本）、`secure_storage_unavailable`（配置或恢复主密钥）、`storage_unavailable`（检查数据库及其身份），以及 `timeout`、`authentication`、`invalid_response` 等连接错误。错误响应不会包含提供商原始响应或密钥。

## 本地部署示例

按照 Ollama 官方文档安装后运行：

```console
ollama pull qwen3:8b
ollama serve
```

创建 Ollama 设置时，服务地址填 `http://127.0.0.1:11434`，模型名填 `qwen3:8b`，温度填 `0.1`，超时填 `90.0`，最大输出填 `4096`。选择该模型开始分析前先执行连接测试。本地可用性和模型表现取决于实际安装的 Ollama 版本、模型和硬件。
