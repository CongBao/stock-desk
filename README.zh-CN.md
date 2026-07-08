[English](README.md)

# Stock Desk

Stock Desk v1.0.0 是一个本地优先的个人 A 股研究工作台，覆盖可追溯行情图、通达信兼容公式、
可复现历史回测与证据关联的多智能体研究。它不连接券商，也不会下单。

![带本地来源证据的 A 股行情图](docs/images/market-data-and-charts.png)

## 核心工作流

- 查看只读本地缓存的日线、周线与 60 分钟图，并核对来源、截止时间、复权、数据版本与路由证据。
- 在低代码的通达信兼容编辑器中构建、校验和版本化指标或交易公式，预览 K 线主图、公式副图与
  BUY/SELL 信号。
- 使用已保存的公式版本回测，明确执行 A 股 T+1、成本、手数、数据覆盖、固定回放、导出与
  不可变结果语义。
- 运行面向 DeepSeek、OpenAI 兼容接口或本地 Ollama 的研究流程，结论始终关联持久化证据。

| 公式预览 | 回测结论 | 证据关联研究 |
| --- | --- | --- |
| ![公式编辑与预览](docs/images/formula-studio.png) | ![回测结果](docs/images/backtesting.png) | ![多智能体研究报告](docs/images/multi-agent-research.png) |

## 快速启动

Windows 与 macOS 用户应选择随版本发布、无需源码的构件：

- `stock-desk-<version>-windows-x86_64.exe`
- `stock-desk-<version>-macos-x86_64.dmg`
- `stock-desk-<version>-macos-arm64.dmg`

核对 `.sha256` 与目标清单后，使用已认证的 GitHub CLI 校验来源证明与 SPDX SBOM 证明：

```bash
gh attestation verify INSTALLER_PATH --repo CongBao/stock-desk --signer-workflow CongBao/stock-desk/.github/workflows/release.yml
gh attestation verify INSTALLER_PATH --repo CongBao/stock-desk --signer-workflow CongBao/stock-desk/.github/workflows/release.yml --predicate-type https://spdx.dev/Document/v2.3
```

运行安装包，或把对应架构的 macOS 应用从 DMG 复制到“应用程序”。启动后会在回环地址运行内置
服务并自动打开 Stock Desk。

Linux 或私有的仅回环容器部署可使用：

```bash
docker compose up --build --wait
# 打开 http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

源码贡献者按 [CONTRIBUTING.md](CONTRIBUTING.md) 操作，再打开
[http://localhost:5173/market](http://localhost:5173/market)。两种模式都必须保持私有：
Stock Desk 没有认证、授权或 TLS。

## 文档

双语 [GitHub Wiki](https://github.com/CongBao/stock-desk/wiki) 提供安装、行情、公式、回测、分析、
任务、配置、备份和恢复的详细操作步骤与候选版本真实截图。

仓库内参考：[架构](docs/architecture.md)、[配置](docs/configuration.md)、
[故障排查](docs/troubleshooting.md)、[备份与恢复](docs/backup-and-restore.md)、
[变更日志](CHANGELOG.md)与[路线图](ROADMAP.md)。

在本地校验公共文档契约：

```bash
uv run --frozen python scripts/verify_docs.py
```

## 安全与范围

Stock Desk 是研究软件，不构成投资建议。数据可能延迟、不完整、经过复权或受许可限制；公式、
回测和模型输出也可能错误。请独立核验所有决策，并阅读完整[免责声明](docs/disclaimer.md)。

切勿公开凭据、`.env`、`STOCK_DESK_MASTER_KEY`、TDX 路径、数据库、备份或授权行情数据。
安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 参与贡献

参阅 [CONTRIBUTING.md](CONTRIBUTING.md)、[支持方式](SUPPORT.md)与
[行为准则](CODE_OF_CONDUCT.md)。项目采用 [Apache-2.0](LICENSE) 许可证。
