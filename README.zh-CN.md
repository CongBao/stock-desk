[English](README.md)

# Stock Desk

Stock Desk `v0.5.0` 是一个本地优先的 A 股研究工作台，覆盖行情数据、通达信兼容公式、
可复现历史回测和证据关联的多智能体分析。它面向单个可信本地用户，不会下单，也不连接券商。

## 快速启动

Windows 与 macOS 用户应优先使用随版本发布、无需源码的原生安装包。已验证的构件命名契约为：

- Windows x64：`stock-desk-<version>-windows-x86_64.exe`
- macOS Intel：`stock-desk-<version>-macos-x86_64.dmg`
- macOS Apple 芯片：`stock-desk-<version>-macos-arm64.dmg`

可下载的发布构件包括安装包、对应 `.sha256` 校验文件、目标 `.json` 清单和目标
`.sbom.spdx.json` SBOM。来源证明是通过 GitHub API 查询的 GitHub attestation，不是另一个
可下载的发布文件。核对哈希与清单后，使用已认证的 GitHub CLI 校验安装包证明：

```bash
gh attestation verify INSTALLER_PATH \
  --repo CongBao/stock-desk \
  --signer-workflow CongBao/stock-desk/.github/workflows/release.yml
gh attestation verify INSTALLER_PATH \
  --repo CongBao/stock-desk \
  --signer-workflow CongBao/stock-desk/.github/workflows/release.yml \
  --predicate-type https://spdx.dev/Document/v2.3
```

第一条命令校验 SLSA 来源证明，第二条校验同一安装包关联的 SPDX SBOM 证明。

本文不会链接尚未发布的版本。然后运行 Windows 当前用户安装包，或从 DMG 把 macOS 应用复制到
“应用程序”。首次启动会在随机回环端口运行内置 API 与 worker，并打开浏览器；不需要源码仓库、
Python、Node.js 或 pnpm。

Linux 或私有服务器可使用只绑定回环地址的容器方案。端口 8000 必须保持私有；远程访问应使用
可信隧道，不要直接暴露这个没有认证的服务：

```bash
docker compose up --build --wait
# 打开 http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

从源码参与开发需要 Python `>=3.12,<3.13`、[uv](https://docs.astral.sh/uv/)、
Node.js 22 或 24 LTS 与 pnpm 11：

```bash
make bootstrap
make dev
```

打开 [http://localhost:5173/market](http://localhost:5173/market)。`make dev`
会启动 API、任务 worker 和 Vite 开发服务器；按 `Ctrl-C` 停止。

API 健康检查位于
[http://localhost:8000/api/health](http://localhost:8000/api/health)。添加数据源或模型凭据前，
请先阅读[配置指南](docs/configuration.md)。

## 核心工作流

已发布路径依次为 Stage 0 基础、Stage 1 行情、Stage 2 公式、Stage 3 回测和 Stage 4 证据关联分析。

- **任务：** `/tasks` 展示行情、回测和分析任务的持久化进度、事件、取消、失败与恢复诊断；
  `demo.double` 可用于轻量 API/worker 诊断。
- **行情：** 在 `/settings` 配置数据源，刷新证券目录，更新单只证券或固定股票池，再查看带来源证明的
  本地缓存日线、周线或 60 分钟图。参阅[数据源说明](docs/data-sources.md)。
- **公式：** 在 `/formulas` 校验受控的通达信兼容表达式并保存不可变版本，再对固定行情快照运行预览。
  参阅[兼容清单](docs/formula-compatibility.md)。
- **回测：** 在 `/backtests` 选择不可变公式版本和数据范围；检查预检覆盖、明确的 A 股成交规则、
  成本、部分失败、导出与固定回放。参阅[回测语义](docs/backtesting-semantics.md)。
- **研究：** 在 `/analysis` 配置 DeepSeek、OpenAI 兼容或本地 Ollama 模型；启动九阶段分析前先预检证据。
  关键证据不足时不输出评级。参阅[模型提供方](docs/model-providers.md)。
- **备份与恢复：** 文档中的 CLI 仅用于源码或容器 POSIX 运维，不包含在原生安装包中；当前版本
  不支持在原生 Windows 上完成完整流程。升级前请先阅读[备份与恢复](docs/backup-and-restore.md)。

## 文档

- [架构与信任边界](docs/architecture.md)
- [配置与密钥](docs/configuration.md)
- [故障排查与恢复](docs/troubleshooting.md)
- [备份、恢复、升级与回滚](docs/backup-and-restore.md)
- [无障碍](docs/accessibility.md)与[性能方法](docs/performance.md)
- [变更日志](CHANGELOG.md)、[路线图](ROADMAP.md)与[支持方式](SUPPORT.md)

API 运行时可打开交互文档：
[http://localhost:8000/docs](http://localhost:8000/docs)。

在本地运行公共文档契约：

```bash
uv run --frozen python scripts/verify_docs.py
```

聚焦的验收、性能回归、浏览器、安全和完整发布命令如下：

```bash
make acceptance
make acceptance-formula
make acceptance-backtest
make benchmark
make benchmark-formula
make benchmark-backtest
make e2e-market
make e2e-formula
make e2e-backtest
make e2e-analysis
make e2e-task-center
make security
make release-check
```

`make security` 需要网络访问：它通过 OSV 审计锁定的 Python 依赖，并通过 npm registry
审计 JavaScript 生产依赖，且会先确认清单与锁文件一致。`make release-check` 还需要 Docker，
并会运行更完整的发布门禁；执行前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

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
