[English](README.en.md)

# Stock Desk

> 当前稳定版为 Windows x64 `v1.1.0`。它是明确标记的未签名桌面版本；范围与风险见 [v1.1.0 说明](docs/releases/v1.1.0.md)。

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

请从 [Latest Release](https://github.com/CongBao/stock-desk/releases/latest) 下载
`stock-desk-1.1.0-unsigned-x64-setup.exe`。它没有 Authenticode 签名，Windows 可能显示未知发布者或 SmartScreen 提示；请先按发布页的 `UNSIGNED-WINDOWS-SHA256SUMS` 核对文件。

安装 `v1.1.0`：

1. 使用普通 Windows 用户运行安装器，无需管理员权限。
2. 从开始菜单打开 Stock Desk；内置服务会随桌面窗口启动。
3. 按首次使用向导完成数据准备并选择股票；若暂不选择，默认打开上证指数 `000001.SS`。

普通用户无需 GitHub CLI、源码检出、Docker 或开发工具。SignPath Foundation 因项目曝光率不足拒绝了免费签名申请，因此 v1.1.0 按用户决定以 unsigned release 发布，而不是自签名版本。production updater 继续关闭；计划中的 v1.2 将转向 Microsoft Store / MSIX。详见[下载说明](docs/download.md)和[代码签名政策](docs/code-signing-policy.md)。

`v1.1.0` 不发布 macOS、Linux、Android 或 ARM64 安装包，也不会自动迁移或删除旧版 v1 数据。卸载器只允许用户明确选择是否删除 **仅 v1.1** 的本地数据；取消或删除失败都会保留数据。

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
