# 下载与真实性验证 / Downloads and authenticity

## 下载

请只从 [Stock Desk GitHub Releases](https://github.com/CongBao/stock-desk/releases) 下载公开版本。最新版本入口：[Latest Release](https://github.com/CongBao/stock-desk/releases/latest)。

下载后使用同一发布页的 `SHA256SUMS` 和对应 `.sha256` 文件核对内容摘要。安装前还应检查发布清单中的平台、架构、版本、source revision 和 `signed` 状态。

## 代码签名状态

**Free code signing provided by [SignPath.io](https://signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).** Stock Desk 的 SignPath Foundation 申请和 CI 接入仍在进行中；在公开清单与 Windows Authenticode 验证都明确成功前，任何现有资产都应视为未签名。

完整规则、维护者角色、人工批准和构建来源限制见[代码签名政策](code-signing-policy.md)。隐私与网络行为见[隐私政策](privacy.md)。

## 验证原则

- 文件名、版本和架构必须与发布说明一致；
- SHA-256 必须与发布页校验文件一致；
- 对声称已签名的 Windows 资产，Authenticode 状态、签名主体和时间戳必须有效；
- 如果任一验证失败，请停止安装并通过 [GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 报告。

---

## English summary

Download Stock Desk only from its [GitHub Releases](https://github.com/CongBao/stock-desk/releases). Verify the release SHA-256 files and manifest. The SignPath Foundation application and CI integration are still pending, so existing artifacts remain unsigned unless both the manifest and Windows Authenticode verification explicitly prove a trusted signature.
