# Windows 安装证据门禁 / Installed-Windows evidence gate

[中文](#中文) · [English](#english)

## 中文

### 权威边界

`windows-installed.yml` 只允许从受保护 `main` 手工调度当前精确提交。仓库 runner 必须为零；GitHub-hosted Linux job 使用固定 audience 的 GitHub OIDC 向仓库外短生命周期 Windows VM broker 申请一次性 lease。broker、VM 和来宾脚本只能发布原始观察，不能写 `passed`、`accepted` 或派生结论。

GitHub-hosted aggregate 是唯一接受判定者。它必须同时满足：

- exact source SHA/tree、不可变 main proof、候选安装器和 WebView2 安装器摘要一致；
- 受保护 environment 提供的 snapshot-policy 与 adapter SHA-256 一致；
- controller request、guest harness、UIA driver、workflow 和仓库内 Ed25519 broker 公钥摘要一致；
- 11 个不同 case 都来自同一次 Actions run 的首轮尝试，且每个 broker lifecycle receipt 验签成功；
- aggregate 要求 clean-snapshot browser baseline 为空，再从 final samples 与 lifecycle events 重新计算 HWND 集合、连续序列、数量、摘要和单调时间边界；这样同一已有浏览器 HWND 内新增标签页也无法绕过，任何矛盾 summary 都拒绝；
- lease 在运行前恢复快照，运行期间 watchdog 已启用，运行后再次恢复并释放，时间单调且不超过一小时；
- 只有 aggregate 派生并 attestation 的 `acceptance-receipt.json` 可作为这个 exact SHA 的真实 Windows 证据。

workflow 存在、任务排队、单个 JSON、单元测试夹具、浏览器视口或 CSS 缩放均不构成真实 Windows 通过。外部 broker 尚未配置、受保护 digest 缺失、环境未批准或任一 case 缺失时，工作流必须 fail closed。

### 固定 11-case 矩阵

- Windows 10 22H2 x64：100%、125%、150%、175%、200%；
- Windows 11 x64：100%、125%、150%、175%、200%；
- Windows 10 22H2 x64、100%、WebView2 缺失且离线安装固定失败。

十个成功 case 都使用中文名、路径含空格的全新标准用户，覆盖 WebView2 已安装/缺失、`1366×768` 与 `640×360` 逻辑窗口、真实 Win32 DPI、四步向导、`000001.SS` 真实日线、六个核心页面和七类弹窗。`win11-dpi-150` 固定阻断 AKShare 并完整回退 BaoStock；禁止跨来源拼接。失败 case 必须证明 Microsoft WebView2 子进程和 NSIS 父进程非零退出、无应用残留、无快捷方式、无伪造窗口或截图。

### UI、权限与隐私观察

审核过的 UIA/Win32 driver 绑定候选 PID、HWND 和可执行文件摘要，使用 `GetDpiForWindow`、`GetDpiForSystem`、`GetDpiForMonitor`、`GetWindowDpiAwarenessContext` 及逻辑/物理坐标往返证明非虚拟化 PMv2。它从当前焦点实际发送 Tab 到每个 onboarding 必要按钮，用 UIA focused element 与公开 contact sheet 中受 manifest 约束的按钮焦点区域前后原始像素变化共同证明可见焦点，再发送 Enter；同时实际发送 Esc，并记录命中测试、控件矩形、兄弟重叠、裁切、窄栏重排、点击/按键与目标窗口截图。aggregate 会从 contact sheet 原始像素独立重算变化，只有摘要、硬编码布尔值、`SetFocus()` 或只声明 `HasKeyboardFocus` 均不构成证据。

来宾还记录标准用户 token、UAC/完整性级别、安装/卸载进程、WebView2 Authenticode、只读安装目录、v1.1 可变目录、旧 v1 canary、外部浏览器 HWND、网络来源/截止时间/行数和脱敏扫描。公开包不得包含 Token、凭据、用户名、用户路径、VM 管理地址、私有日志或未声明文件。ZIP、manifest、record、schema、媒体类型、大小、路径、摘要或签名任一异常都会拒绝整组矩阵。

### 外部 broker 运维前置

仓库不公开 hypervisor、VM 名称、账户秘密或 broker endpoint。维护者必须在 `windows-installed-acceptance` environment 中配置并保护：

- `WINDOWS_VM_BROKER_ENDPOINT`：同源 HTTPS endpoint；客户端拒绝重定向；
- `WINDOWS_VM_SNAPSHOT_POLICY_SHA256`：符合 `schemas/windows-vm-snapshot-policy-v2.schema.json` 的固定 11-case policy 摘要；
- `WINDOWS_VM_ADAPTER_SHA256`：已审核 adapter 摘要；
- `WINDOWS_INSTALLED_POLICY_TOKEN`：仅用于读取 environment/runner policy 的最小权限 Token。

environment 只允许 `main`，禁止管理员绕过，并要求非本人 reviewer。broker 必须验证 GitHub 实际提供的 OIDC `iss/aud/sub/repository/repository_id/repository_owner_id/ref/sha/workflow_ref/workflow_sha/run_id/run_attempt/check_run_id/runner_environment/environment` claims；JWT 中的数字 claim 按原始十进制字符串写入签名 receipt，`runner_environment` 必须为 `github-hosted`。`job_id` 不是 GitHub OIDC claim；客户端把 matrix job 标识作为独立、已签名的 request binding `request_job_id` 发送，aggregate 再与 raw manifest 精确比对，但不得把它描述成 OIDC 身份。只有 matrix broker job 同时拥有 protected environment 和 `id-token: write`；aggregate 可读取 environment digest 但无 OIDC 权限，独立 attestation job 有 OIDC 权限但无 environment/broker secret。broker 按 case 建立独占 lease，并用与 `config/windows-vm-broker-public-key.pem` 对应的私钥签署闭合 lifecycle receipt。第一次 authoritative 运行前，外部 broker/HSM 运维方必须生成实际密钥对、只把公钥提交到仓库并核对指纹；私钥不得进入仓库、Actions secret 或 artifact。没有对应外部私钥的仓库公钥只能使流程 fail closed，不能产生正式证据。公钥轮换、policy/adapter/harness/UIA/workflow 变化都会改变 main proof，必须重新审查。

## English

### Authority boundary

`windows-installed.yml` can be manually dispatched only for the current exact commit of protected `main`. Repository runner inventory must be zero. A GitHub-hosted Linux job uses a fixed-audience GitHub OIDC token to acquire a one-time lease from an external short-lived Windows VM broker. The broker, VM, and guest scripts may publish raw observations only; they cannot declare `passed`, `accepted`, or any derived conclusion.

The GitHub-hosted aggregate is the sole acceptance authority. It requires exact source/proof/candidate identities, protected snapshot-policy and adapter digests, exact controller/guest/UIA/workflow/public-key digests, eleven distinct first-attempt packages from one Actions run, valid Ed25519 lifecycle signatures, and restore-before/run/watchdog/restore-after/release lifecycle ordering within one hour. It requires an empty clean-snapshot browser baseline, then independently recomputes final HWND inventory, contiguous event sequence, count, digest, and monotonic boundaries from raw samples and lifecycle events. This also prevents a new tab in a pre-existing browser HWND from evading detection. Only its subsequently attested `acceptance-receipt.json` is real-Windows evidence for that exact SHA.

Workflow presence, a queued job, one JSON file, a unit fixture, browser viewport emulation, or CSS scaling is not real Windows acceptance. Missing broker configuration, a missing protected digest, a missing approval, or one missing case fails closed.

### Fixed eleven-case matrix

- Windows 10 22H2 x64 at 100%, 125%, 150%, 175%, and 200%;
- Windows 11 x64 at 100%, 125%, 150%, 175%, and 200%;
- Windows 10 22H2 x64 at 100% with WebView2 absent and a fixed offline-install failure.

All ten success cases use a fresh standard account with a non-ASCII name and a profile path containing a space. They cover WebView2 present/absent, `1366×768` and `640×360` logical windows, real Win32 DPI, four-step onboarding, real `000001.SS` daily bars, six core routes, and seven dialog classes. `win11-dpi-150` deterministically blocks AKShare and falls back as a whole segment to BaoStock. The failure case must show nonzero Microsoft WebView2 child and NSIS parent exits with no app, shortcut, shim window, or screenshot residue.

### UI, permission, and privacy observations

The reviewed UIA/Win32 driver binds candidate PID, HWND, and executable digest. It uses target-window DPI APIs and logical/physical coordinate round trips. Starting from the current focus, it sends real Tab input to every required onboarding button, proves the focused UIA element plus before/after raw-pixel changes in manifest-bounded regions of a published contact sheet, and then sends Enter. The aggregate independently recomputes those pixel deltas. It also sends real Esc input and records hit testing, component rectangles, clipping, peer overlap, narrow-rail reflow, actions, and target-window-only captures. Digest-only claims, hard-coded pass booleans, `SetFocus()`, or a bare `HasKeyboardFocus` claim are not accepted.

The guest also records standard-user tokens, UAC/integrity, installer and uninstaller processes, WebView2 Authenticode, read-only install-root behavior, v1.1 mutable-root isolation, the untouched v1 canary, external-browser HWND events, real-data provider/cutoff/row count, and redaction results. Public packages must not contain tokens, credentials, usernames, user paths, VM endpoints, private logs, or undeclared files. Any ZIP, manifest, record, schema, media-type, size, path, digest, or signature mismatch rejects the matrix.

### External broker prerequisites

Hypervisor details, VM names, account secrets, and the broker endpoint stay outside the repository. Maintainers protect `WINDOWS_VM_BROKER_ENDPOINT`, `WINDOWS_VM_SNAPSHOT_POLICY_SHA256`, `WINDOWS_VM_ADAPTER_SHA256`, and the least-privilege `WINDOWS_INSTALLED_POLICY_TOKEN` in the `windows-installed-acceptance` environment. The environment is main-only, non-bypassable, and reviewer-protected. The broker validates the real GitHub OIDC claims `iss/aud/sub/repository/repository_id/repository_owner_id/ref/sha/workflow_ref/workflow_sha/run_id/run_attempt/check_run_id/runner_environment/environment`; numeric JWT claims remain decimal strings in the signed receipt and `runner_environment` must equal `github-hosted`. GitHub does not issue a `job_id` OIDC claim: the client sends the matrix job identity separately as the signed request binding `request_job_id`, and the aggregate compares it with the raw manifest without treating it as identity. Only the broker matrix job has both the protected environment and OIDC permission. The aggregate has environment digests but no OIDC permission; the separate attestation job has OIDC permission but no environment or broker secret. The broker grants an exclusive per-case lease and signs the closed lifecycle receipt with the private key corresponding to `config/windows-vm-broker-public-key.pem`. Before the first authoritative run, the external broker/HSM operator must generate the real key pair, commit only its verified public key, and keep the private key out of the repository, Actions secrets, and artifacts. A repository public key without its externally provisioned private key can only fail closed; it cannot produce authoritative evidence. Any key, policy, adapter, harness, UIA, or workflow change invalidates the main proof and requires review.
