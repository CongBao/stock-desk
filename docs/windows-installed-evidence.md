# Windows 安装原始证据门禁 / Raw Windows installation evidence gate

[中文](#中文) · [English](#english)

## 中文

### 这一步证明什么

本页定义尚未激活的真实 Windows 证据接口：raw schema、来宾/控制器参考 harness 和独立 verifier 已可审计；`windows-installed.yml` 仍固定使用 GitHub-hosted runner，仓库仍禁止持久化 self-hosted runner，而且尚未接入外部短生命周期 VM adapter/JIT 服务。因此当前 workflow 只会 fail closed，不能生成真实通过回执。

外部隔离服务接入后，Windows 10 22H2 与 Windows 11 全新快照必须以普通用户运行场景，并按仓库外受保护策略固定 profile、OS build、镜像摘要、WebView 初始状态和失败注入。恢复、运行、清理和再次恢复必须形成同一 lifecycle receipt；GitHub-hosted 汇总任务才可重算 controller request、来宾 harness 与 workflow 摘要，打开原始文件并独立派生结果。

控制器和来宾不能写 `passed`。只有汇总任务生成的 `windows-installed-verification-<SHA>` 回执才表示这一次精确提交的三个首轮场景通过；工作流存在、排队、controller diagnostic、单个 JSON 或 synthetic unit fixture 都不构成真实 Windows 通过。

### 公开接口与私有边界

- `scripts/windows_installed_vm_harness.ps1` 是供未来仓库外、短生命周期隔离服务采用的公开参考边界，不会在仓库 runner 上直接调用。服务内的受保护 controller 通过 `STOCK_DESK_WINDOWS_VM_ADAPTER` 指向机器所有的虚拟化适配器，并独立固定适配器和快照策略 SHA-256。摘要缺失或文件不匹配时只写 non-passing diagnostic 并以 `86` 退出。
- `scripts/windows_installed_guest_harness.ps1` 是复制到全新 VM 的公开来宾观察器。适配器必须把已验证 installer、controller request、clean snapshot 身份和该脚本送入普通用户会话。
- 适配器可使用 Hyper-V、VMware 或其他受管虚拟化，但必须实现三个动作：`RestoreCleanSnapshot`、`RunInstalledAcceptance`、`CleanupAndRestoreSnapshot`，并写 `stock-desk-windows-vm-adapter-result-v1` 结果。受批准适配器必须在来宾内测量实际复制并执行的 harness 摘要；来宾同时校验自身脚本摘要，controller lifecycle receipt 再把两者绑定。凭据、VM 地址、hypervisor 配置和账户秘密不得进入仓库或上传证据。
- 适配器只能写私有临时目录；公开 wrapper 会依据 manifest 重建封闭上传包。redaction、截图绑定、清理或再次恢复任一失败，都会先删除 raw package，只留下固定 non-passing diagnostic。
- controller 在读取、哈希或复制来宾输出前，强制 raw manifest 不超过 1 MiB、单条 record 不超过 8 MiB、全部 manifest 绑定文件合计不超过 16 MiB、公开文本合计不超过 2 MiB，并逐级拒绝来源路径中的 reparse point。外部隔离服务还必须在证据上传后删除 controller 输入、私有 adapter 输出、状态及安装包副本。
- Restore 和 acceptance 必须通过受保护 adapter 获取或续租独占 controller lease。adapter 只接收加域生成的 lease/controller-binding 摘要和一小时 TTL，并必须由机器侧 watchdog 在 Actions 被取消、超时或 runner 失联后自动恢复 clean snapshot。Cleanup 必须先恢复再原子释放 lease；公开 lifecycle 只包含 opaque 摘要、时间和状态，不包含 VM 名称、端点或凭据。
- 只有成功场景生成应用目标窗口 PNG；摘要必须与窗口事件一致，且 ready marker 必须来自根窗口之外的 UI Automation descendant WebView 内容，单独的 `Stock Desk` 根标题不算就绪。静默 `/S` 失败场景不伪造错误窗口或图片，而是同时绑定真实 `MicrosoftEdgeWebView2RuntimeInstaller.exe` 子进程的路径摘要/文件摘要/Token/非零退出、NSIS 父进程非零中止、失败注入身份及未留下应用文件/快捷方式。
- 普通用户证据同时检查 Administrators 组成员、linked token、integrity level。外部浏览器不依赖每个进程唯一的 `MainWindowHandle`：来宾用 Win32 `EnumWindows` 枚举同一 PID 的全部可见顶层 HWND，并在 installer 前先启动 `SetWinEventHook`，持续捕获 `CREATE`、`SHOW`、`HIDE`、`DESTROY`，直到 final inventory 完成为止；installer、应用就绪和稳定期轮询只作交叉检查。manifest schema 绑定 hook 起止、baseline/final、订阅事件、事件数量及事件流摘要；任何非 baseline browser HWND 的生命周期事件（包括两次轮询之间短暂出现再消失）都会失败。runtime inventory 覆盖机器级和当前用户 WebView2。
- PR 与 main 的 GitHub-hosted Windows 门禁会直接提取并编译来宾 harness 中同一段 production C#，运行一个名为 `chrome.exe` 的隔离 probe：同 PID 两个持久 HWND 验证 `EnumWindows`，第三个 HWND 在两次枚举之间完成 SHOW/HIDE/DESTROY 验证 hook，注入 unhook 失败验证不会生成 stopped summary。结果由 artifact manifest 绑定 source SHA/tree、producer、harness/driver/workflow 摘要，main proof 再签名消费；非 Windows 本地环境可以跳过，但 CI 不能跳过。
- 公开证据只有布尔/摘要/版本/固定角色和脱敏日志，不含用户名、用户路径、Token、凭据或 VM 管理信息。原始包 schema 位于 `schemas/windows-installed-raw-evidence-v1.schema.json`；控制器的机器外快照清单必须符合 `schemas/windows-vm-snapshot-policy-v1.schema.json`，但真实 VM 名称、凭据和管理端点不会进入仓库。

### 无真实控制器时

没有外部短生命周期 VM adapter/JIT 服务与真实来宾观察时，流程只会失败；它不会使用测试 fixture，也不会生成 verified receipt。当前阶段交付的是可审计的 schema、验证器、参考合约和 Windows observer 快速门禁，不是真实 Windows 安装或完整旅程验收结果。

## English

### What this gate proves

This page defines an inactive real-Windows evidence interface. The raw schemas, guest/controller reference harnesses, and independent verifier are auditable, but `windows-installed.yml` remains fixed to GitHub-hosted runners, persistent repository self-hosted runners remain forbidden, and no external short-lived VM adapter/JIT service is connected. The workflow therefore fails closed and cannot currently issue a real passing receipt.

After an isolated external service is connected, protected clean Windows 10 22H2 and Windows 11 snapshots must execute each scenario as an ordinary user. Restore-before-run and cleanup/restore-after-run must form one lifecycle receipt before a GitHub-hosted aggregate may recompute the controller request, guest harness, workflow digests, and raw observations.

Neither controller nor guest may write `passed`. Only the aggregate's `windows-installed-verification-<SHA>` receipt means all three first-attempt scenarios passed for that exact commit. Workflow presence, a queued run, a controller diagnostic, one JSON document, or a synthetic unit fixture is not real Windows evidence.

### Public contract and private boundary

- `scripts/windows_installed_vm_harness.ps1` is a public reference boundary for a future external short-lived isolated service; it is not invoked on a repository runner. Inside that service, a protected controller points `STOCK_DESK_WINDOWS_VM_ADAPTER` at its machine-owned adapter and independently pins approved adapter and snapshot-policy SHA-256 values.
- `scripts/windows_installed_guest_harness.ps1` is the public guest observer copied into a clean VM. The adapter supplies the verified installer, controller request, clean-snapshot identity, and reviewed script to an ordinary interactive user session.
- An adapter may use Hyper-V, VMware, or another managed hypervisor, but it must implement `RestoreCleanSnapshot`, `RunInstalledAcceptance`, and `CleanupAndRestoreSnapshot`, then write a `stock-desk-windows-vm-adapter-result-v1` result. The approved adapter measures the actual guest-side copied/executed harness; the guest also verifies its own script hash, and the lifecycle receipt binds both measurements. Credentials, VM addresses, hypervisor configuration, and account secrets stay out of the repository and uploaded evidence.
- The adapter writes only a private temporary directory. The public wrapper rebuilds a closed upload package from manifest-bound files. A redaction, screenshot-binding, cleanup, or final-restore failure deletes raw observations before leaving a fixed non-passing diagnostic.
- Every raw manifest is limited to 1 MiB, every record to 8 MiB, all manifest-bound records together to 16 MiB, and the combined public text to 2 MiB before the controller hashes or copies guest output. The controller rejects reparse points in every source-path component. The external service must scrub controller input, private adapter output, state, and copied installers after upload.
- Restore and acceptance calls must acquire or renew an exclusive controller lease through the protected adapter. The adapter receives only a domain-separated lease digest and controller-binding digest plus a one-hour TTL; it must maintain a machine-owned watchdog that restores the clean snapshot when the lease expires, even if Actions is cancelled, times out, or loses the runner. Cleanup restores first, then atomically releases the lease. Public lifecycle evidence contains only the opaque digests and lease timestamps/state—never VM names, endpoints, or credentials.
- Only success scenarios create a target-window PNG. Its digest must equal the window event, and readiness must come from meaningful descendant WebView UI Automation text—not the root `Stock Desk` title. Silent `/S` failure does not invent an error window or screenshot: it binds the real `MicrosoftEdgeWebView2RuntimeInstaller.exe` child path hash, file hash, token and nonzero exit to the NSIS parent's nonzero abort, the fixed injection, and absence of application/shortcut residue.
- Standard-user checks cover Administrators membership, linked token, and integrity level. Browser evidence does not rely on one `MainWindowHandle` per process: Win32 `EnumWindows` inventories every visible top-level HWND, including multiple windows owned by one PID, and a `SetWinEventHook` starts before the installer and continuously captures `CREATE`, `SHOW`, `HIDE`, and `DESTROY` through the final inventory. Polling during installer execution, readiness, and the stable period is only a cross-check. The manifest schema binds hook start/stop, baseline/final, subscriptions, event count, and event-stream digest; any lifecycle event for a non-baseline browser HWND—including a window that appears and disappears between polls—fails. Runtime inventory supports both machine and per-user WebView2 installations.
- A mandatory GitHub-hosted Windows job for every PR and `main` compiles the exact production C# extracted from the guest harness and runs an isolated `chrome.exe` probe. Two persistent HWNDs under one PID exercise `EnumWindows`; a third HWND completes SHOW/HIDE/DESTROY between enumerations; injected unhook failure must not produce a stopped summary. An artifact manifest binds the result to source SHA/tree, producer, and harness/driver/workflow digests, and the signed main proof consumes it. Non-Windows local environments may skip this integration, but CI may not.
- Public evidence contains only booleans, digests, versions, fixed roles, and redacted logs—never usernames, user paths, tokens, credentials, or VM-management details. The raw package schema is `schemas/windows-installed-raw-evidence-v1.schema.json`; the protected machine-side policy must conform to `schemas/windows-vm-snapshot-policy-v1.schema.json` without publishing VM names, credentials, or endpoints.

### Without a real controller

Without an external short-lived VM adapter/JIT service and real guest observation, the workflow only fails. It does not substitute a fixture and cannot create a verified receipt. This stage delivers auditable schemas, a verifier, reference contracts, and a fast Windows observer gate—not a fabricated installation or full-journey acceptance result.
