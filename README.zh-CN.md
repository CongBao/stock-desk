[English](README.md)

# Stock Desk

Stock Desk `v0.5.0` 是一个本地优先的 A 股研究工作台，覆盖行情数据、通达信兼容公式、
可复现历史回测和证据关联的多智能体分析。它面向单个可信本地用户，不会下单，也不连接券商。

## 快速启动

原生开发需要 Python `>=3.12,<3.13`、[uv](https://docs.astral.sh/uv/)、
Node.js 22 或 24 LTS 与 pnpm 11：

```bash
make bootstrap
make dev
```

打开 [http://localhost:5173/market](http://localhost:5173/market)。`make dev`
会启动 API、任务 worker 和 Vite 开发服务器；按 `Ctrl-C` 停止。

使用只绑定本机回环地址的容器部署：

```bash
docker compose up --build --wait
# 打开 http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

API 健康检查位于
[http://localhost:8000/api/health](http://localhost:8000/api/health)。添加数据源或模型凭据前，
请先阅读[配置指南](docs/configuration.md)。

## 核心工作流

- **任务：** `/tasks` 展示行情、回测和分析任务的持久化进度、事件、取消、失败与恢复诊断。
- **行情：** 在 `/settings` 配置数据源，刷新证券目录，更新单只证券或固定股票池，再查看带来源证明的
  本地缓存日线、周线或 60 分钟图。参阅[数据源说明](docs/data-sources.md)。
- **公式：** 在 `/formulas` 校验受控的通达信兼容表达式并保存不可变版本，再对固定行情快照运行预览。
  参阅[兼容清单](docs/formula-compatibility.md)。
- **回测：** 在 `/backtests` 选择不可变公式版本和数据范围；检查预检覆盖、明确的 A 股成交规则、
  成本、部分失败、导出与固定回放。参阅[回测语义](docs/backtesting-semantics.md)。
- **研究：** 在 `/analysis` 配置 DeepSeek、OpenAI 兼容或本地 Ollama 模型；启动九阶段分析前先预检证据。
  关键证据不足时不输出评级。参阅[模型提供方](docs/model-providers.md)。
- **备份与恢复：** 升级前创建经过校验的应用快照；恢复时必须协调停止相关进程。参阅
  [备份与恢复](docs/backup-and-restore.md)。

## 文档

- [架构与信任边界](docs/architecture.md)
- [配置与密钥](docs/configuration.md)
- [故障排查与恢复](docs/troubleshooting.md)
- [备份、恢复、升级与回滚](docs/backup-and-restore.md)
- [无障碍](docs/accessibility.md)与[性能方法](docs/performance.md)
- [变更日志](CHANGELOG.md)、[路线图](ROADMAP.md)与[支持方式](SUPPORT.md)

在本地运行公共文档契约：

```bash
uv run --frozen python scripts/verify_docs.py
```

## 安全与范围

Stock Desk 是研究软件，不构成投资建议。行情数据可能延迟、不完整、经过复权，或受上游条款限制；
模型输出也可能错误。请独立核验数据、公式、假设和结论。完整说明见[免责声明](docs/disclaimer.md)。

服务没有认证、授权或 TLS，只应绑定本机回环地址。切勿提交或分享 `.env`、token、模型密钥、
`STOCK_DESK_MASTER_KEY`、本地 TDX 路径、数据库、备份或下载的行情数据。报告漏洞前请阅读
[SECURITY.md](SECURITY.md)。

当前版本不提供实时行情、动态选股、共享资金组合模拟、个性化建议、目标价、仓位比例、券商连接或
实盘/自动交易。

## 参与贡献

请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与
[行为准则](CODE_OF_CONDUCT.md)。变更应包含聚焦的测试和同步更新的公共文档。项目采用
[Apache-2.0](LICENSE) 许可证。
