# 代码签名政策 / Code signing policy

## 当前状态

Stock Desk 已开始申请 SignPath Foundation 的免费开源代码签名服务。申请获批前，发布资产仍是未签名资产；用户必须以发布页清单中的 `signed` 字段和实际 Authenticode 验证结果为准。

**Free code signing provided by [SignPath.io](https://signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).** 此声明描述获批后的签名服务；在申请、接入和验证完成前，不表示任何现有资产已经签名。

## 角色与责任

- Committer、reviewer：[CongBao](https://github.com/CongBao)
- Release signing approver：[CongBao](https://github.com/CongBao)

Stock Desk 目前由个人维护者维护。所有正式签名请求都必须经过人工批准；自动构建成功不能替代签名批准。

## 构建来源与签名边界

- 只签署由公开仓库 [CongBao/stock-desk](https://github.com/CongBao/stock-desk) 的受保护 `main` 提交和 GitHub Actions 工作流生成的 Stock Desk 自有二进制。
- 签名请求必须绑定精确 commit、tree、工作流运行和不可变构建产物摘要；拒绝来自分支别名、`latest`、本地上传或身份不匹配的产物。
- 第三方开源依赖可以按其许可证随安装包分发，但不会使用 Stock Desk 的签名策略冒充其上游发布者。
- 正式发布至少验证宿主程序、Python sidecar 和 Windows 安装器的 Authenticode 信任链、时间戳和 SHA-256。
- 申请未获批或信任验证未通过时，资产只能明确标记为 unsigned prerelease，不得作为受信正式版本发布。

## 用户隐私与安全

Stock Desk 的数据处理和网络行为见[隐私政策](privacy.md)。程序默认不使用遥测、不自动上传崩溃报告，也不自动上传诊断包。

安全问题请通过 [GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) 私下报告。

---

## English summary

Stock Desk has started applying for the free SignPath Foundation open-source code-signing service. Existing artifacts remain unsigned unless both the release manifest and Authenticode verification explicitly prove otherwise.

The project is maintained, reviewed, and release-approved by [CongBao](https://github.com/CongBao). Every formal signing request requires manual approval and must originate from the public repository's exact, attested `main` build. See the [privacy policy](privacy.md) for data and network behavior.
