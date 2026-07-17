# v1 acceptance coverage

<!-- requirements-yaml-sha256: 8a4ee5871c092c9db9c5277f3d196907ade3089d617f3d58175bd253a44af494 -->

The machine-readable acceptance authority is `tests/acceptance/requirements.yml`. The digest above is checked by `scripts/check_requirement_coverage.py` so this public summary cannot silently drift from the matrix.

The matrix maps the 82 authoritative requirements in their original stable-ID order and the 10 explicit non-goals in their original exclusion order. A public-safe frozen registry binds every ID to its behavior key, acceptance-text digest, metadata, and exact semantic reference set. Each entry also has an owning stage, mapping status, and assertion-level evidence.

Evidence state and requirement status are intentionally separate:

- `existing` evidence names a tracked assertion that the mapping checker can collect or a fixed registered gate.
- `planned` evidence names an exact future assertion. It completes the mapping but cannot verify a requirement.
- `manual` evidence is limited to operational or publication work and defines a procedure, final artifact, release gate, and completion state.
- `mapped` means the semantic requirement and its proof contract are complete. `verified` is allowed only when no planned or incomplete manual evidence remains.

Run the mapping gate during development:

```console
uv run python scripts/check_requirement_coverage.py --mode mapping
```

The tag-candidate gate uses `--mode pre-publish`. With all planned assertions
now resolved, it runs existing pytest evidence with xfail semantics disabled and
rejects incomplete manual artifacts required by `release-acceptance`. Manual
records explicitly assigned to `final-release-audit` are deferred because some
of them bind the signed tag, public release page, and final lineage that do not
exist before publication.

After publication, `--mode release` is the post-release audit. It rejects every
planned assertion and every incomplete manual artifact, including
`final-release-audit`. A successful mapping or pre-publish check therefore does
not claim the post-publication audit is complete.

Non-goals are enforced by an inventory over public OpenAPI names, API and worker identifiers, Web UI claims, and public documentation claims. The inventory covers the absence of broker/live ordering, shared-capital portfolios, realtime/tick/Level-2 feeds, target prices or specific allocations, a second native product UI, accounts/RBAC/subscriptions/billing, dynamic screening, condition-selection/color-K formulas, drawing/multi-stock/multi-period linkage, and AI formula generation/explanation/repair. A minimal installed launcher that opens the browser workstation is permitted.

## 桌面交互验收 / Desktop interaction acceptance

| 环境 / Environment | 必须证明 / Required proof | 不得替代 / Must not replace |
| --- | --- | --- |
| macOS 本地真实 Tauri `.app` | PR 合并前运行 `pnpm desktop:test:macos:full`，以真实物理点击完成七步真实产品旅程 / Run `pnpm desktop:test:macos:full` before merge and complete the seven-action real product journey with real physical clicks | 不替代 Windows 安装、权限和卸载验收 / Does not replace Windows installation, permission, or uninstall acceptance |
| GitHub Hosted Windows | UIA `InvokePattern` + CDP/Playwright 四步自动化；`input_method = windows-uia-and-cdp-automation`；`physical_mouse_click = false` | 物理点击、真实 Win10 普通用户或 UAC 安全桌面 / Physical input, a real Win10 standard user, or UAC secure desktop |
| 真实 Win10 普通用户 / Real Win10 standard user | 安装、首次向导、默认指数、普通 A 股、退出、卸载 / Install, onboarding, default index, ordinary A-share, exit, and uninstall | 不可由 Hosted 或 macOS 自动化替代 / Cannot be replaced by Hosted or macOS automation |

macOS 门禁仅供开发（development-only）：它使用隔离临时数据，脱敏证据保存在 Git 忽略的
`test-results/macos-full-product`，成功或失败后都必须清理宿主、sidecar、后代进程和临时产品
数据。它不发布 macOS 安装包，也不替代 Windows（does not replace Windows）NSIS、WebView2、
权限、卸载或真实 Win10 普通用户验收。

所有需要域名命名空间的桌面身份使用用户持有域名对应的 `com.baozijuan.stockdesk`。All domain-namespaced desktop identities use the user-owned domain as `com.baozijuan.stockdesk`.
