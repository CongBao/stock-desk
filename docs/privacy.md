# 隐私政策 / Privacy policy

## 默认原则

Stock Desk 是本地优先的个人研究软件。默认不收集遥测，不创建稳定设备标识；内部 worker 只使用每次启动随机生成的临时会话标识，不包含主机名。不自动上传崩溃报告，也不自动上传日志、诊断包、行情、自选、公式、回测、模型提示词或分析结果。

用户数据保存在本机用户数据目录。诊断包只有在用户明确操作后才会在本地生成；生成前执行允许清单和脱敏检查，程序不会自动上传该文件。

这些默认值同时固化在机器可校验的
[`config/desktop-network-privacy.json`](../config/desktop-network-privacy.json)
中。当前锁定的 `trusted-updater-foundation` 阶段会在 CI 中拒绝缺失或被修改的策略、已枚举的遥测或崩溃 SDK 特征、自动诊断上传、稳定设备标识、出现在精确路径清单之外的已枚举网络导入和直接原语，以及绕过 Rust 宿主或修改源码绑定配置来提前启用 updater 的代码。Python 网络导入使用 AST 检查，别名导入同样受控；所有 Tauri 配置和嵌套 capability JSON 都会递归检查。隐私策略、默认关闭的更新配置和校验器哈希同时绑定到候选安装包证据与 main 验证证明。

## 何时会访问网络

Stock Desk 只为用户请求的功能访问相应服务：

- 获取用户选择或首次向导所需的公开行情、证券列表和数据来源信息；
- 在用户配置模型提供商并明确启动分析后，向该提供商发送完成请求所需的数据；
- 当前源码包含默认关闭的 GitHub Release 可信更新运行链；当前版本不会发起后台检查、下载或安装。若未来通过正式门禁启用，应用可按公开稳定版策略在后台检查，但请求只使用固定匿名请求头，不发送设备标识、使用行为或本地数据摘要；下载和安装仍必须由用户明确操作并在宿主原生确认框中确认。Windows 在验证 Authenticode 吊销状态时可能按系统信任策略访问证书颁发机构的 CRL/OCSP 服务，该系统请求不包含 Stock Desk 行为数据；
- 打开用户明确选择的公开文档、发布页或问题报告页面。

所选行情源、模型提供商和 GitHub 会按各自政策处理直接发送给它们的请求。用户应在配置第三方 Token 或服务前阅读相应提供商的隐私和服务条款。可用连接和配置边界见[配置文档](configuration.md)。

## 不会发生的行为

- 不出售或共享用户行为数据；
- 不在后台发送公式、回测、分析历史或私人提示词；
- 不在未经确认时上传诊断或崩溃信息；
- 不要求为了基本本地使用而创建 Stock Desk 云账户。

如发现隐私或安全问题，请通过 [GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 私下报告。

---

## English summary

Stock Desk is local-first. It has no telemetry, stable device identifier, automatic crash upload, or automatic diagnostic upload by default. Internal workers use a fresh random session identity that contains no hostname. CI confines enumerated network-capable imports and direct primitives to exact reviewed production paths for user-requested market data and explicitly configured model-provider requests. The trusted-update runtime is source-bound and default-off, so the current build performs no background update request. If formally activated later, it may check public stable releases in the background with fixed anonymous headers and no device identifier, behavior, or local-data digest; download and installation still require explicit user action and native confirmation. Windows may contact certificate-authority CRL/OCSP services under the operating system's trust policy while checking Authenticode revocation; Stock Desk sends no behavior data in that system trust request. Local diagnostics are created only on explicit request and are never uploaded automatically.

These defaults are also frozen in the machine-verifiable
[`config/desktop-network-privacy.json`](../config/desktop-network-privacy.json)
policy and enforced by CI. The active `trusted-updater-foundation` phase keeps
the source-bound Rust-host updater configuration disabled while checking its
fixed endpoint, anonymous request allowlist, and absence of environment or
WebView activation. Aliased Python imports, recursive Tauri capability
configuration, and evidence hashes are also checked fail-closed.
