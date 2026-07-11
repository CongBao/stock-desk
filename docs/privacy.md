# 隐私政策 / Privacy policy

## 默认原则

Stock Desk 是本地优先的个人研究软件。默认不收集遥测，不创建稳定设备标识，不自动上传崩溃报告，也不自动上传日志、诊断包、行情、自选、公式、回测、模型提示词或分析结果。

用户数据保存在本机用户数据目录。诊断包只有在用户明确操作后才会在本地生成；生成前执行允许清单和脱敏检查，程序不会自动上传该文件。

## 何时会访问网络

Stock Desk 只为用户请求的功能访问相应服务：

- 获取用户选择或首次向导所需的公开行情、证券列表和数据来源信息；
- 在用户配置模型提供商并明确启动分析后，向该提供商发送完成请求所需的数据；
- 检查公开 GitHub Release 更新；更新检查不发送稳定设备标识或使用行为，下载和安装更新需要用户确认；
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

Stock Desk is local-first. It has no telemetry, stable device identifier, automatic crash upload, or automatic diagnostic upload by default. Network access is limited to user-requested market data, explicitly configured model-provider requests, public GitHub release checks without behavioral identifiers, and links the user chooses to open. Local diagnostics are created only on explicit request and are never uploaded automatically.
