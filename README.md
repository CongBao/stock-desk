[English](README.en.md)

# Stock Desk

> 当前稳定版为 `v1.0.0`。当前开发候选 `v1.1.0-beta.2` 是 Windows x64 桌面体验预发布版；测试资产不替代稳定版，范围与限制见 [beta.2 说明](docs/releases/v1.1.0-beta.2.md)。

## 产品定位

Stock Desk 是一个本地优先的个人 A 股研究桌面软件，覆盖可追溯行情图、
通达信兼容公式、可复现历史回测和证据关联的多智能体研究。它不连接券商，也不会下单。

![带来源证据的 A 股行情图](docs/images/market-data-and-charts.png)

贵州茅台 `600519.SH`，BaoStock 日线/前复权，数据截至 `2026-07-08T07:00:00Z`。仅作功能演示，不构成投资建议。

## 核心功能

- 查看本地缓存的日线、周线和 60 分钟行情图，并核对来源、截止时间、复权、数据版本和路由证据。
- 在低代码的通达信兼容编辑器中构建和版本化公式，预览主图、副图与买卖信号。
- 使用已保存的公式版本执行可复现回测，明确 A 股 T+1、成本、手数、数据覆盖和不可变结果。
- 运行 DeepSeek、OpenAI 兼容接口或本地 Ollama 研究流程，让结论始终关联持久化证据。

回测兼容性由离线、不可变的 `v1.0.0` 基准保护：它绑定发布提交与 Git tree，覆盖
MACD/参数化自定义公式、单股/股票池、日线/周线/60 分钟的 12 种组合，以及 A 股约束、
持仓成本和部分数据缺口。CI 会校验基准、输入和生成器的摘要，并拒绝未明确列入白名单的漂移。

| 真实公式预览 | 被阻断的真实回测预检 | 分析准备状态 |
| --- | --- | --- |
| ![宁德时代 MACD BUY/SELL 公式预览](docs/images/formula-studio.png)<br>宁德时代 `300750.SZ`；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`；显示 MACD BUY/SELL。仅作功能演示，不构成投资建议。 | ![平安银行 MACD 回测严格预检被阻断](docs/images/backtesting.png)<br>平安银行 `000001.SZ` 的真实 MACD 配置；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`。因没有合法的 Tushare execution-status 快照，严格预检被阻断；未创建任务或报告，不代表回测成功、结果或胜率。仅作功能演示，不构成投资建议。 | ![招商银行模型与证据准备状态](docs/images/multi-agent-research.png)<br>招商银行 `600036.SH` 的模型/证据准备状态：无已验证模型，未发起模型调用，也未生成报告。 |

## 下载安装

稳定使用请从 [Latest Release](https://github.com/CongBao/stock-desk/releases/latest) 下载
`v1.0.0`。测试 Windows x64 桌面候选请使用单独的
[`v1.1.0-beta.2` 预发布页](https://github.com/CongBao/stock-desk/releases/tag/v1.1.0-beta.2)，
下载 `stock-desk-1.1.0-beta.2-unsigned-x64-setup.exe`；它未签名，Windows 可能显示未知发布者或 SmartScreen 提示。

测试 `v1.1.0-beta.2`：

1. 使用普通 Windows 用户运行安装器，无需管理员权限。
2. 从开始菜单打开 Stock Desk；内置服务会随桌面窗口启动。
3. 按首次使用向导完成数据准备并选择股票；若暂不选择，默认打开上证指数 `000001.SS`。

普通用户无需 GitHub CLI、源码检出、Docker 或开发工具。稳定版下载真实性验证见[下载说明](docs/download.md)；beta.2 的校验和与不可变构建证明位于其预发布页。

当前源码已包含**默认关闭**的可信更新运行链，以及**硬禁用的正式签名发布控制面骨架**。
骨架描述同一受保护 `main` 的 proof/candidate、人工批准的 SignPath、Windows 10/11 普通用户
证据和可信更新依赖，但签名 job 使用不可由配置或密钥解除的字面关闭门禁。只有后续受审变更
补齐 NSIS 安装控制语义等价证明与真实 SmartScreen/MOTW 证据后，才能移除该门禁。
因此 production updater 仍保持关闭；本阶段不会签名、正式发布、后台检查、下载或安装更新。

`v1.1.0` 不发布 macOS、Linux、Android 或 ARM64 安装包，也不会迁移或删除 v1 的本地数据。正式版签名状态以发布页和[代码签名政策](docs/code-signing-policy.md)为准。

当前源码中的下一候选卸载器可由用户明确选择是否删除 **仅 v1.1** 的本地数据；取消删除或删除失败都会继续保留数据，旧版 v1 数据不在该操作范围内。已发布的 beta.2 不因源码更新而被重新声明为具备此行为。

当前源码还提供真实 Windows 10/11 普通用户安装的[原始证据 schema、独立验证器与隔离控制器参考合约](docs/windows-installed-evidence.md)。仓库仍禁止持久化 self-hosted runner；workflow 已接入 fail-closed 的外部短生命周期 VM broker/JIT adapter 接口和十一项矩阵聚合，但外部 broker/HSM、受保护环境、生产凭据及真实 VM 运行证据尚未配置，因此不能生成通过回执。这不代表安装或完整旅程验收已通过，也不改变 beta.2 的测试版状态。

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
构建与不可变证明契约见 [CI 文档](docs/ci.md)。
