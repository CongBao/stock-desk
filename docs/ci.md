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

当前未签名的 `v1.1.0-alpha.N` 和 `v1.1.0-beta.N` 标签只消费同一提交已经成功生成的 exact-SHA `main` proof 与 Windows candidate。Release 会重新验证 tag、GitHub attestation、proof、candidate manifest、版本化安装器文件名和内容摘要，然后只发布显著标记的 Windows x64 unsigned prerelease；它不重跑 unit/E2E，也不重建桌面安装包。`v1.1.0` stable 和 `rc` 标签在独立的 SignPath、可信更新及 Windows 10/11 普通用户安装链完成前保持 fail closed。SignPath 申请已提交但仍为 pending，因此当前证据不代表 Authenticode、SmartScreen 或正式发布门禁已经通过。

### 优化前基线

已记录基线包括：Python 全量关键路径最高约 `41m32s`、Chromium E2E 约 `16m54s`、本地连续候选约 `85m`；2026-07-11 最终 main run 的完整 CI 为 `32m48s`。v1.1 目标是普通 PR 10–20 分钟、高风险 PR 20–30 分钟、main 25–35 分钟。P50/P95 只能根据至少五次连续同类运行公布，不能通过跳过门禁美化。

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

### Release reuse

Current unsigned `v1.1.0-alpha.N` and `v1.1.0-beta.N` tags consume only the exact-SHA `main` proof and Windows candidate already produced successfully for the same commit. Release revalidates the tag, GitHub attestation, proof, candidate manifest, versioned installer name, and content digests, then publishes only a clearly labelled Windows x64 unsigned prerelease; it neither reruns unit/E2E nor rebuilds the desktop installer. `v1.1.0` stable and `rc` tags remain fail-closed until the separate SignPath, trusted-update, and Windows 10/11 standard-user installation chain exists. The SignPath application is submitted but still pending, so this evidence does not claim that Authenticode, SmartScreen, or formal-release gates have passed.

### Pre-optimization baseline

Recorded baselines include a Python critical path of about `41m32s`, Chromium E2E of about `16m54s`, and consecutive local candidates of about `85m`; the final 2026-07-11 main CI completed in `32m48s`. v1.1 targets 10–20 minutes for a typical PR, 20–30 minutes for a high-risk PR, and 25–35 minutes for `main`. P50/P95 figures require at least five consecutive comparable runs and may never be improved by skipping gates.
