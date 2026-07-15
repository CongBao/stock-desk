# CI 与不可变交付证据 / CI and immutable delivery evidence

[中文](#中文) · [English](#english)

## 中文

Stock Desk 的 PR、`main` 和 Release 使用不同职责的流水线，减少重复执行，但不降低安全门禁。

### 风险图

PR 变更按 backend、web、Tauri、installer、dependency、documentation 和 cross-domain 分类。分类结果包含 profile、必跑 job、可跳过 job 和原因。未知路径、空或无效 diff、工作流、锁文件、权限、签名、证明及发布契约一律 fail closed 到相应全量门禁。PR 的增量结果只用于反馈，不能替代 `main` 证明。

### `main` 全量证明

每个 `main` SHA 将完整 Python 测试唯一分配给 unit、integration、acceptance/performance 和 security 四个隔离 shard。每个 shard 输出同一 source SHA/tree 绑定的 JUnit、nodeid inventory 和 parallel coverage；聚合器拒绝遗漏、重复、xfail 冒充成功、陈旧报告或身份不匹配，并以至少两位小数强制 combined branch coverage `>=85.00%`。

需求映射先验证 YAML schema、authority digest 和 selector collect，再与相同 SHA 的成功 JUnit nodeid 精确交叉验证，不会再次运行同一批测试。浏览器验收使用一次内容哈希绑定的确定性 snapshot、一次服务启动和一次 Playwright 调度；首轮失败不能被 retry 覆盖。

Web、Python、OCI、SBOM/provenance 等产物都有内容 manifest，记录 source commit/tree、锁文件、工具链和 SHA-256。OCI 构建一次，Compose、SBOM 和 Trivy 必须验证并消费同一 image digest。最终 proof 只有在 CI、Security 和 CodeQL 的全部必需 job 对同一 SHA 成功时才会生成并接受 GitHub attestation。

### 缓存边界

允许缓存的只有按 OS、架构、工具链和 lockfile 键控的依赖下载、编译和浏览器中间物。JUnit、coverage、数据库、需求证据、签名、release proof 和最终 artifact identity 永不从缓存接受。warm cache 与 clean miss 必须执行相同门禁。

### Release 复用

当前未签名的 `v1.1.0-alpha.N` 和 `v1.1.0-beta.N` 标签只消费同一提交已经成功生成的 exact-SHA `main` proof 与 Windows candidate。Release 会重新验证 tag、GitHub attestation、proof、candidate manifest、版本化安装器文件名和内容摘要，然后只发布显著标记的 Windows x64 unsigned prerelease；它不重跑 unit/E2E，也不重建桌面安装包。仓库中的 formal DAG 目前只是硬禁用骨架：SignPath job 使用字面 `false` 门禁，任何 input、变量、密钥或环境配置都不能启动它，因此 `v1.1.0-rc.N` 和 stable 都无法签名或发布。后续受审变更必须先加入 NSIS 安装控制语义等价证明与真实 SmartScreen/MOTW 新机证据，才能移除门禁。SignPath 申请仍为 pending，当前证据不代表 Authenticode、SmartScreen 或正式发布门禁已经通过。

PR 与 main 都使用锁定版本 `cargo-audit 0.22.2` 对 `src-tauri/Cargo.lock`
执行 RustSec 检查。已知漏洞使用工具默认的非零失败语义，yanked crate 由
`--deny yanked` 明确阻断；命令不配置任何 advisory ignore。上游维护状态等 warning
仍完整显示，但不会把 Tauri 的跨平台传递依赖误报成漏洞。任何工具安装、数据库更新或
审计失败都会阻断。缓存只包含该固定工具和 RustSec advisory database；审计结论、报告、
证明和发布资产从不进入缓存，每次运行都会重新计算结论。

### Windows 安装后验收

`windows-installed.yml` 只允许从受保护 `main` 手工调度当前精确提交。仓库持久化 self-hosted runner 注册被明确禁止；入口门禁固定使用 GitHub-hosted `ubuntu-24.04`，通过 GitHub API 要求仓库 runner inventory 的 `total_count=0` 且列表为空，同时要求输入 SHA、`GITHUB_SHA`、`GITHUB_WORKFLOW_SHA`、实时 `origin/main` 完全一致，`GITHUB_WORKFLOW_REF` 精确指向本仓库 main 上的 workflow。非 main 调度不能越过 environment 的精确 main 分支策略。

runner inventory、environment 和分支策略查询使用 protected environment secret `WINDOWS_INSTALLED_POLICY_TOKEN`；main 分支身份查询继续使用 job 自带的只读 `GITHUB_TOKEN`。该 secret 只能由入口门禁引用，必须是独立的最小权限 fine-grained token：仓库 Administration 与 Actions 均为只读，不得授予 Contents 或任何写权限。token 缺失、权限不足、API 拒绝、存在任一仓库 runner、管理员可绕过，或 custom deployment branch policies 不是唯一的 `type=branch, name=main`，门禁都会失败。

管理员可先保存 environment API 响应，运行 `python scripts/windows_installed_environment_policy.py bootstrap-payload --existing environment.json > policy.json`，再以 `gh api --method PUT repos/CongBao/stock-desk/environments/windows-installed-acceptance --input policy.json` 应用。删除其他 deployment branch policies 后，用 `python scripts/windows_installed_environment_policy.py bootstrap-branch-policy-payload > main-policy.json` 和 `gh api --method POST repos/CongBao/stock-desk/environments/windows-installed-acceptance/deployment-branch-policies --input main-policy.json` 建立唯一 main 规则。启用前还必须在仓库 Settings → Actions → Runners 删除所有现有 runner 注册，并以 `gh api repos/CongBao/stock-desk/actions/runners > runners.json` 确认结果为零；最后把 environment、branch-policy 和 runner 三份 API 响应一起交给 verifier。不得仅凭 environment 名称或 runner 离线状态推断边界安全。

通过入口门禁后，GitHub-hosted 预检重新验证成功的 main proof、attestation、候选清单和摘要。11 个并行 Linux job 通过 fixed-audience OIDC 调用仓库外短生命周期 Windows VM broker；受保护 environment 独立固定 broker endpoint、11-case snapshot policy 和 adapter 摘要。broker/guest 只返回签名 lifecycle 与 raw bytes，不能声明通过。GitHub-hosted aggregate（无 OIDC 权限）对 controller、guest/UIA、workflow、公钥、policy、adapter、exact-SHA 产物和十一个首轮 case 独立验签/验摘要后才派生 `acceptance-receipt.json`；随后无 environment/broker secret 的隔离 job 才对 receipt 做 attestation。外部 broker、secret、人工 environment approval 或任一 case 缺失时按设计 fail closed；不得为本仓库注册持久化 runner。完整信任边界和矩阵见 `docs/windows-installed-evidence.md`。

PR 与 main 还运行 GitHub-hosted Windows browser/UIA observer integrations：它验证同 PID 多 HWND、轮询间瞬态窗口和 unhook fail-closed；同时编译并启动受控 WinForms/UIA 窗口，真实执行 reviewed driver 的目标 HWND DPI、Tab→contact-sheet 原始焦点区域像素变化→Enter→实际 Esc 关闭对话框路径，并保留全部 raw runtime probe 文件。exact-SHA artifact 在 main 上进入 validation proof。快速集成只验证观察器/driver 本身，不替代真实安装旅程；完整 VM 仍负责真实应用的全尺寸/全 DPI Tab/Esc 矩阵。

### 优化前基线

已记录基线包括：Python 全量关键路径最高约 `41m32s`、Chromium E2E 约 `16m54s`、本地连续候选约 `85m`；2026-07-11 最终 main run 的完整 CI 为 `32m48s`。v1.1 目标是普通 PR 10–20 分钟、高风险 PR 20–30 分钟、main 25–35 分钟。部署耗时台账保存成功、失败、取消、超时、跳过和已废弃的原始样本，并以不可变哈希链与外部连续性 seal 防止删尾；ledger 与 seal 通过可恢复事务日志提交，半提交只能继续既定追加，不能改写既有历史。报告始终输出六个固定分类，零样本分类也明确标记为 `incomplete`。P50/P95 只能根据至少连续五次可比运行公布；可比身份由分类、workflow、完整 ref 和完整环境基线共同确定，任一字段漂移都会开始新的连续段并在报告中保留全部漂移段。重试 attempt 仍保留为原始证据，但同一 run id 只能计作一次连续运行；每个 run 的 queue/wall 代表值分别取该 run 所有 attempt 的最大值，再计算 nearest-rank，因此快速重试不能压低百分位。不足五次必须标为 `incomplete`，不能通过重试、跳过门禁或删除失败/已废弃样本美化。

## English

Stock Desk assigns different responsibilities to pull-request, `main`, and release workflows so repeated work can be removed without weakening a security gate.

### Risk graph

PR changes are classified as backend, web, Tauri, installer, dependency, documentation, or cross-domain. The decision records the profile, required jobs, skipped jobs, and reasons. Unknown paths, empty or invalid diffs, workflows, lockfiles, permissions, signing, proof, and release contracts fail closed to the applicable full gates. Incremental PR results improve feedback only and never replace a `main` proof.

### Full `main` proof

Every `main` SHA uniquely assigns the complete Python inventory to isolated unit, integration, acceptance/performance, and security shards. Each shard emits exact-SHA/tree JUnit, nodeid inventory, and parallel coverage. The aggregator rejects omissions, duplicates, xfail-as-success, stale reports, or identity mismatches, and enforces combined branch coverage `>=85.00%` at two-decimal precision or better.

Requirement mapping validates the YAML schema, authority digest, and selector collection before exact matching against successful JUnit nodeids from the same SHA; it does not execute the selectors again. Browser acceptance uses one content-bound deterministic snapshot, one service startup, and one Playwright scheduling pass. A retry cannot erase an authoritative first-run failure.

Web, Python, OCI, and SBOM/provenance outputs carry content manifests with source commit/tree, lockfiles, toolchains, and SHA-256. The OCI image is built once; Compose, SBOM, and Trivy must verify and consume the same image digest. The final proof is generated and attested only when every required CI, Security, and CodeQL job succeeds for the same SHA.

### Cache boundary

Only dependency downloads and compiler/browser intermediates keyed by OS, architecture, toolchain, and lockfile may be cached. JUnit, coverage, databases, requirement evidence, signatures, release proofs, and final artifact identities are never accepted from a cache. Warm-cache and clean-miss runs execute identical gates.

Both PR and main run pinned `cargo-audit 0.22.2` against the exact desktop
lockfile. Known vulnerabilities fail through cargo-audit's default exit status,
yanked crates fail through `--deny yanked`, and no advisory is ignored. Upstream
maintenance warnings remain visible without being misclassified as
vulnerabilities. Tool installation, advisory-database update, or audit failure
blocks the gate. Caches may contain only the pinned tool and advisory database,
never audit conclusions, reports, proofs, or release artifacts.

### Release reuse

Current unsigned `v1.1.0-alpha.N` and `v1.1.0-beta.N` tags consume only the exact-SHA `main` proof and Windows candidate already produced successfully for the same commit. Release revalidates the tag, GitHub attestation, proof, candidate manifest, versioned installer name, and content digests, then publishes only a clearly labelled Windows x64 unsigned prerelease; it neither reruns unit/E2E nor rebuilds the desktop installer. The checked-in formal DAG is currently a hard-disabled scaffold: the SignPath job has a literal `false` gate that no input, variable, secret, or environment can enable, so neither `v1.1.0-rc.N` nor stable can sign or publish. A later reviewed change must add NSIS installation-control equivalence and real fresh-machine SmartScreen/MOTW evidence before removing that gate. The SignPath application is still pending, and current evidence does not claim that Authenticode, SmartScreen, or formal-release gates have passed.

### Installed Windows acceptance

`windows-installed.yml` accepts a manual dispatch only for the current exact commit of protected `main`. Persistent repository self-hosted runner registrations are forbidden. The entry guard is fixed to GitHub-hosted `ubuntu-24.04` and uses the GitHub API to require a repository runner inventory with `total_count=0` and an empty list. It also requires the input SHA, `GITHUB_SHA`, `GITHUB_WORKFLOW_SHA`, and live `origin/main` to be identical, while `GITHUB_WORKFLOW_REF` must identify this repository's workflow on main. A non-main dispatch cannot pass the environment's exact-main branch policy.

The runner-inventory, environment, and branch-policy queries use the protected-environment secret `WINDOWS_INSTALLED_POLICY_TOKEN`; the main-branch identity query continues to use the job's read-only `GITHUB_TOKEN`. Only the entry guard may reference the secret. It must be a separate least-privilege fine-grained token with repository Administration and Actions set to read-only, no Contents access, and no write permission. A missing or underprivileged token, a rejected API request, any registered repository runner, administrator bypass, or any custom deployment branch-policy set other than exactly `type=branch, name=main` fails closed.

For bootstrap, an administrator can save the current environment API response, run `python scripts/windows_installed_environment_policy.py bootstrap-payload --existing environment.json > policy.json`, and apply it with `gh api --method PUT repos/CongBao/stock-desk/environments/windows-installed-acceptance --input policy.json`. After removing every other deployment branch policy, generate the sole main rule with `python scripts/windows_installed_environment_policy.py bootstrap-branch-policy-payload > main-policy.json` and apply it with `gh api --method POST repos/CongBao/stock-desk/environments/windows-installed-acceptance/deployment-branch-policies --input main-policy.json`. Before enabling the workflow, remove every existing registration under Settings → Actions → Runners and confirm zero inventory with `gh api repos/CongBao/stock-desk/actions/runners > runners.json`. Then run the verifier against the environment, branch-policy, and runner API responses together. Neither the environment name nor an offline runner is accepted as proof of this boundary.

After the entry guard, a GitHub-hosted preflight revalidates the successful main proof, attestations, candidate manifest, and digests. Eleven parallel Linux jobs use fixed-audience OIDC to call an external short-lived Windows VM broker. The protected environment independently pins the endpoint, eleven-case snapshot policy, and adapter digests. Broker and guest outputs are signed lifecycle receipts plus raw bytes only; they cannot declare acceptance. A GitHub-hosted aggregate with no OIDC permission verifies controller, guest/UIA, workflow, key, policy, adapter, exact-SHA artifact, signature, and all eleven first-attempt identities before deriving `acceptance-receipt.json`. A separate attestation job has OIDC permission but no protected environment or broker secret. A missing broker, secret, environment approval, or case fails closed. See `docs/windows-installed-evidence.md` for the complete trust boundary and matrix.

PR and `main` also run GitHub-hosted Windows browser/UIA observer integrations. They verify multiple HWNDs under one PID, between-poll transient windows, and unhook fail-closed behavior. The job also compiles and launches a controlled WinForms/UIA window and executes the reviewed driver's target-HWND DPI plus real Tab → raw contact-sheet focus-region pixel delta → Enter → real Esc dialog-close runtime path, preserving every raw probe file. Its exact-SHA artifact enters the main validation proof. This fast integration verifies the observer/driver only; it does not replace the real VM's full-size, full-DPI installed-application Tab/Esc matrix.

### Pre-optimization baseline

Recorded baselines include a Python critical path of about `41m32s`, Chromium E2E of about `16m54s`, and consecutive local candidates of about `85m`; the final 2026-07-11 main CI completed in `32m48s`. v1.1 targets 10–20 minutes for a typical PR, 20–30 minutes for a high-risk PR, and 25–35 minutes for `main`. The deployment-latency ledger retains raw successful, failed, cancelled, timed-out, skipped, and invalidated samples in an immutable hash chain with an external continuity seal. A recoverable transaction journal commits the ledger and seal, so an interrupted commit can only finish its predetermined append and cannot rewrite existing history. Reports always emit all six fixed categories, with zero-sample categories explicitly marked `incomplete`. P50/P95 figures require five consecutive comparable runs whose category, workflow, full ref, and full environment baseline are identical; any drift starts a new streak and every drift segment remains visible. Retry attempts remain raw evidence, but one run id counts only once toward completeness. Each run's queue and wall representatives are the respective maxima across all its attempts before nearest-rank calculation, so fast retries cannot lower a percentile. Fewer than five are reported as `incomplete`, and figures may never be improved with retries, skipped gates, or deletion of failed or invalidated samples.
