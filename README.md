[English](README.en.md)

# Stock Desk

## 产品定位

Stock Desk v1.0.0 是一个本地优先的个人 A 股研究工作台，覆盖可追溯行情图、
通达信兼容公式、可复现历史回测和证据关联的多智能体研究。它不连接券商，也不会下单。

![带来源证据的 A 股行情图](docs/images/market-data-and-charts.png)

贵州茅台 `600519.SH`，BaoStock 日线/前复权，数据截至 `2026-07-08T07:00:00Z`。仅作功能演示，不构成投资建议。

## 核心功能

- 查看本地缓存的日线、周线和 60 分钟行情图，并核对来源、截止时间、复权、数据版本和路由证据。
- 在低代码的通达信兼容编辑器中构建和版本化公式，预览主图、副图与买卖信号。
- 使用已保存的公式版本执行可复现回测，明确 A 股 T+1、成本、手数、数据覆盖和不可变结果。
- 运行 DeepSeek、OpenAI 兼容接口或本地 Ollama 研究流程，让结论始终关联持久化证据。

| 真实公式预览 | 被阻断的真实回测预检 | 分析准备状态 |
| --- | --- | --- |
| ![宁德时代 MACD BUY/SELL 公式预览](docs/images/formula-studio.png)<br>宁德时代 `300750.SZ`；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`；显示 MACD BUY/SELL。仅作功能演示，不构成投资建议。 | ![平安银行 MACD 回测严格预检被阻断](docs/images/backtesting.png)<br>平安银行 `000001.SZ` 的真实 MACD 配置；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`。因没有合法的 Tushare execution-status 快照，严格预检被阻断；未创建任务或报告，不代表回测成功、结果或胜率。仅作功能演示，不构成投资建议。 | ![招商银行模型与证据准备状态](docs/images/multi-agent-research.png)<br>招商银行 `600036.SH` 的模型/证据准备状态：无已验证模型，未发起模型调用，也未生成报告。 |

## 下载安装

从 [Latest Release](https://github.com/CongBao/stock-desk/releases/latest) 下载对应平台的无需源码安装包；下载和真实性验证步骤见[下载说明](docs/download.md)：

- `stock-desk-<version>-windows-x86_64.exe`
- `stock-desk-<version>-macos-x86_64.dmg`
- `stock-desk-<version>-macos-arm64.dmg`

1. 选择平台和处理器架构对应的安装包。
2. Windows 运行 EXE；macOS 打开 DMG，并将应用复制到“应用程序”。
3. 首次启动 Stock Desk，等待内置服务就绪并自动打开应用。

普通用户无需 GitHub CLI、源码检出、Docker 或开发工具。校验和、构建证明及高级部署说明位于发布页和使用文档。

## 使用文档

默认入口是[简体中文 GitHub Wiki](https://github.com/CongBao/stock-desk/wiki)，可切换到
[English Wiki](https://github.com/CongBao/stock-desk/wiki/Home-en)。Wiki 提供安装、行情、公式、
回测、分析、任务、配置、备份和恢复的详细步骤。

仓库内参考：[架构](docs/architecture.md)、[配置](docs/configuration.md)、
[故障排查](docs/troubleshooting.md)、[备份与恢复](docs/backup-and-restore.md)和
[免责声明](docs/disclaimer.md)。

## 安全与范围

Stock Desk 是研究软件，不构成投资建议。数据可能延迟、不完整、经过复权或受许可限制；
公式、回测和模型输出也可能错误。请独立核验所有决策。

切勿公开凭据、`.env`、密钥、TDX 路径、数据库、备份或授权行情数据。
本地部署没有认证、授权或 TLS；请勿暴露到不受信任的网络。安全问题请通过
[GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 私下报告，
并参阅 [SECURITY.md](SECURITY.md)。

代码签名的当前状态、人工批准和可信构建边界见[代码签名政策](docs/code-signing-policy.md)，本地数据和网络行为见[隐私政策](docs/privacy.md)。
