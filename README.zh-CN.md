[English](README.md)

# Stock Desk

Stock Desk 是一个本地优先、面向个人 A 股研究工作台的开源基础项目。当前 `0.1.0` 属于 **Stage 0：基础阶段**，已包含 FastAPI 服务、SQLite 迁移、可持久化任务 API 与 worker、本地加密密钥存储、React 工作区外壳，以及原生和容器两套开发路径。

实时 A 股数据、公式执行、策略回测和智能分析代理 **尚未实现**。相关路由目前只是后续阶段的如实预览，详见[路线图](ROADMAP.md)。

## 环境要求

- Python `>=3.12,<3.13`（即 Python 3.12）
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 或 24 LTS，以及 pnpm 11
- Docker 与 Compose v2（仅容器方式需要）

## 原生启动

```bash
make bootstrap
make dev
```

浏览器访问 UI [http://localhost:5173](http://localhost:5173)、健康检查 [http://localhost:8000/api/health](http://localhost:8000/api/health) 和 API 文档 [http://localhost:8000/docs](http://localhost:8000/docs)。`make dev` 会同时监管 API、持久化任务 worker 与 Vite 服务；按 `Ctrl-C` 一并停止。

## Compose 启动

```bash
docker compose up --build --wait
```

构建后的 UI 与 API 会统一运行在 [http://localhost:8000](http://localhost:8000)。停止并清理容器：

```bash
docker compose down --volumes --remove-orphans
```

`make release-check` 包含容器 smoke 测试，因此必须先让本仓库的 Compose 服务保持运行中：

```bash
docker compose up --build --wait
make release-check
docker compose down --volumes --remove-orphans
```

运行 `make security` 可只审计锁定的生产依赖图。该命令会访问 OSV 查询 Python 依赖漏洞，并访问 npm registry 查询 Node 依赖漏洞，因此需要网络访问。

## 当前可用范围

- `/market` 是静态工作区/布局预览，其中图表明确不是实时数据。
- `/formulas`、`/backtests`、`/analysis` 仅说明规划能力。
- `/tasks`、`/settings` 仍是 UI 占位页，暂不能管理任务或密钥。
- `POST /api/tasks`、`GET /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel` 是 Stage 0 的持久化任务 API；worker 目前只处理 `demo.double` 演示任务。

原生或 Compose 服务运行后可执行：

```bash
curl -sS -X POST http://localhost:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"kind":"demo.double","payload":{"value":21}}'
curl -sS http://localhost:8000/api/tasks
```

第一个响应对应的任务会持久化到本地 SQLite；worker 领取任务后把 `{"value":42}` 写入结果。这只是基础设施演示，不是行情数据任务。

## 数据与安全边界

Stage 0 不内置也不会连接任何行情数据提供商。后续用户需要自行评估数据许可、可用性、质量和再分发条款。

在未来保存数据商凭据前，请生成 Fernet 密钥，并把它作为 `STOCK_DESK_MASTER_KEY` 写入不受版本控制的 `.env`：

```bash
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

本地密钥存储会加密数值；安全模块也提供日志脱敏 filter/formatter，但只有显式配置到 handler 后才会生效，Stage 0 并未全局安装。当前版本也没有认证、授权或 TLS。不要提交 `.env`、不要在 issue 中粘贴密钥，也不要把服务暴露到不可信网络。参阅[安全报告方式](SECURITY.md)和[架构信任边界](docs/architecture.md)。

## 项目信息

- [架构](docs/architecture.md)
- [参与贡献](CONTRIBUTING.md)与[行为准则](CODE_OF_CONDUCT.md)
- [安全](SECURITY.md)与[支持](SUPPORT.md)
- [路线图](ROADMAP.md)与[变更记录](CHANGELOG.md)
- [Apache-2.0 许可证](LICENSE)

Stock Desk 是研究软件，不构成投资建议。请独立核验数据和结论；任何金融行为均由使用者自行负责。
