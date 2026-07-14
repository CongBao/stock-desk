# 代码签名政策 / Code signing policy

## 当前状态

Stock Desk 已于 2026-07-11 提交 SignPath Foundation 免费开源代码签名申请，当前状态为 `application-submitted / pending-review`。申请获批前，发布资产仍是未签名资产；用户必须以发布页清单中的 `signed` 字段和实际 Authenticode 验证结果为准。

**Free code signing provided by [SignPath.io](https://signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).** 此声明描述获批后的签名服务；在申请、接入和验证完成前，不表示任何现有资产已经签名。

## 角色与责任

- Committer、reviewer：[CongBao](https://github.com/CongBao)
- Release signing approver：[CongBao](https://github.com/CongBao)

Stock Desk 目前由个人维护者维护。所有正式签名请求都必须经过人工批准；自动构建成功不能替代签名批准。

状态按 `application-submitted`、`pending-review`、`approved`、`integrated`、`SmartScreen-verified` 依次记录；任一状态都不能替代后续状态。只有 `integrated` 后 CI 才能请求签名，只有签名链和全新机器验证完成后才能记录 `SmartScreen-verified`。

## 构建来源与签名边界

- 只签署由公开仓库 [CongBao/stock-desk](https://github.com/CongBao/stock-desk) 的受保护 `main` 提交和 GitHub Actions 工作流生成的 Stock Desk 自有二进制。
- 签名请求必须绑定精确 commit、tree、工作流运行和不可变构建产物摘要；拒绝来自分支别名、`latest`、本地上传或身份不匹配的产物。
- 第三方开源依赖可以按其许可证随安装包分发，但不会使用 Stock Desk 的签名策略冒充其上游发布者。
- 正式发布至少验证宿主程序、Python sidecar 和 Windows 安装器的 Authenticode 信任链、时间戳和 SHA-256。
- 正式更新门禁只接收实际安装包、签名文件、SignPath 回执和 Windows 10 22H2/Windows 11 x64 回执的文件路径。验证器从安装包字节重新计算 SHA-256，使用仓库中固定的 Tauri 公钥执行 Minisign/Ed25519 验证，并要求 WinVerifyTrust、GitHub exact-SHA attestation 与各回执绑定同一 source revision 和 payload digest；元数据中的布尔值或自报摘要不能替代这些验证。Tauri 私钥只允许存在于受保护的发布环境，绝不进入仓库、日志或发布元数据。
- `latest.json` 是 Stock Desk 的严格元数据封装，其中携带与 Tauri 更新签名兼容的 Minisign/Ed25519 签名。Rust 宿主只执行一条有界传输链：按 32 KiB 上限和固定 GitHub 两跳策略取得元数据，将不可变重定向版本逐字段绑定到目标、URL、签名、源码提交与摘要，再按 512 MiB 上限逐块下载。SHA-256、Tauri 兼容签名和 WinVerifyTrust 必须作用于同一字节；宿主验证后以只读句柄绑定文件身份、锁定暂存目录，在停止 sidecar 前刷新 Authenticode 证书链与吊销证据，并仅在 `CreateProcessW` 返回真实进程与主线程句柄后提交退出。启动失败保留当前版本并恢复服务，成功启动的暂存包由后续启动安全清理。Web IPC 只能请求宿主显示原生确认框，不能用参数或事件伪造确认、路径、摘要或验证结果。
- 当前仓库尚未配置生产 Tauri 公钥，SignPath 正式工作流和真实 Windows 10/11 更新回执也未完成，因此源码中的运行链保持关闭并 fail closed；这不影响继续发布明确标记的未签名 prerelease。
- 申请未获批或信任验证未通过时，资产只能明确标记为 unsigned prerelease，不得作为受信正式版本发布。

## 用户隐私与安全

Stock Desk 的数据处理和网络行为见[隐私政策](privacy.md)。程序默认不使用遥测、不自动上传崩溃报告，也不自动上传诊断包。

安全问题请通过 [GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 私下报告。

---

## English summary

Stock Desk submitted its SignPath Foundation open-source code-signing application on 2026-07-11. Its current state is `application-submitted / pending-review`. Existing artifacts remain unsigned unless both the release manifest and Authenticode verification explicitly prove otherwise.

The project is maintained, reviewed, and release-approved by [CongBao](https://github.com/CongBao). Every formal signing request requires manual approval and must originate from the public repository's exact, attested `main` build. See the [privacy policy](privacy.md) for data and network behavior.

The trusted-update gate consumes actual installer, signature, SignPath-receipt,
and Windows-receipt files. It hashes the installer bytes, verifies the
Minisign/Ed25519 signature with the repository-pinned production public key,
and requires WinVerifyTrust plus exact-SHA GitHub attestations for every receipt.
Claimed booleans or digests are never accepted as proof. The production key,
formal SignPath workflow, and real Windows 10/11 update receipts are not yet
present, so trusted updates remain disabled and fail closed. The private key
exists only in the protected release environment.
`latest.json` is a strict Stock Desk envelope carrying a Tauri-compatible
Minisign/Ed25519 update signature. The Rust host uses one bounded,
repository-confined transport path,
binds the immutable redirect version to every extended field, downloads with a
hard limit, and verifies Minisign/Ed25519, SHA-256, and WinVerifyTrust over the
same bytes. It then binds the read-only file identity, locks its staging
directory, refreshes Authenticode chain and revocation evidence before stopping
the sidecar, and commits exit only after `CreateProcessW` returns real process and
primary-thread handles. A later startup safely removes retained staging files. Web IPC can
request a native host prompt but cannot forge consent, a path, a digest, or a
trust result.
