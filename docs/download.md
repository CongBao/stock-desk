# 下载与真实性验证 / Downloads and authenticity

## 下载

请只从 [Stock Desk GitHub Releases](https://github.com/CongBao/stock-desk/releases) 下载公开版本。最新版本入口：[Latest Release](https://github.com/CongBao/stock-desk/releases/latest)。

`v1.1.0` 的 Windows x64 文件名为 `stock-desk-1.1.0-unsigned-x64-setup.exe`。下载后使用同一发布页的 `UNSIGNED-WINDOWS-SHA256SUMS` 核对内容摘要，并检查候选清单中的平台、架构、版本、source revision 和 `signed: false` 状态。`UNSIGNED-WINDOWS-sbom-v1.1.0.spdx.json` 是从同一 exact-SHA Windows 候选目录生成并纳入校验和的 SPDX SBOM；builder provenance 文件记录候选来源。

## 代码签名状态

SignPath Foundation 因项目曝光率不足拒绝了 Stock Desk 的免费签名申请。`v1.1.0` 是明确标记的 unsigned release，没有 Authenticode 证书，也不是自签名版本；Windows 可能显示未知发布者或 SmartScreen 提示。production updater 和签名 job 继续硬禁用。计划中的 `v1.2` 将通过 Microsoft Store / MSIX 获取商店签名与分发信任。

完整规则、维护者角色、人工批准和构建来源限制见[代码签名政策](code-signing-policy.md)。隐私与网络行为见[隐私政策](privacy.md)。

## 验证原则

- 文件名、版本和架构必须与发布说明一致；
- SHA-256 必须与发布页校验文件一致；
- 对声称已签名的 Windows 资产，Authenticode 状态、签名主体和时间戳必须有效；
- 对提供应用内更新元数据的正式版本，发布门禁必须读取实际安装包与证据文件，重新计算安装包 SHA-256，并同时通过固定生产公钥的 Tauri Minisign/Ed25519 签名、WinVerifyTrust、SignPath exact-SHA attestation，以及 Windows 10 22H2/Windows 11 x64 exact-SHA 实机回执；自报布尔值或摘要不构成证据，缺少任一项都不得发布更新元数据；
- 如果任一验证失败，请停止安装并通过 [GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 报告。

---

## English summary

Download Stock Desk only from its [GitHub Releases](https://github.com/CongBao/stock-desk/releases). The `v1.1.0` Windows x64 installer is `stock-desk-1.1.0-unsigned-x64-setup.exe`; verify it with `UNSIGNED-WINDOWS-SHA256SUMS` and its exact-main candidate manifest. `UNSIGNED-WINDOWS-sbom-v1.1.0.spdx.json` is the checksummed SPDX SBOM generated from that exact candidate directory, while the builder provenance file records its origin. SignPath rejected the free-signing application because the project did not yet have enough exposure. v1.1.0 is unsigned, not self-signed, and may trigger Unknown Publisher or SmartScreen warnings. Its production updater and signing jobs remain hard-disabled. The planned v1.2 will target Microsoft Store / MSIX distribution for Store signing and trust.
