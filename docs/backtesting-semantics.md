# 回测语义 / Backtesting semantics

## 打包桌面兼容性证据

Windows candidate A 会安装实际 NSIS 候选包，并在 Tauri WebView 中通过宿主认证 IPC
提交 12 个完整回测：MACD 与参数化自定义公式 × 单股与股票池 × 日线、周线与
60 分钟。每个任务都由打包 sidecar Worker 执行；证据记录冻结公式版本/参数、股票池
快照、数据快照、报告和结果摘要，并逐项核对绑定到 `v1.0.0` 固定提交与 tree 的离线
语义基准。只读演示、普通浏览器、`TestClient` 或前端路由伪造都不能生成通过证据。

另外一条较大的自定义公式股票池任务会走桌面关闭检查点协议，随后真正重启 sidecar，
由不同 Worker 身份恢复并完成。Windows 宿主捕获器会以随机 nonce 与 WebView 握手，
交叉记录原生窗口、隔离 WebView2 进程、重启前后不同的 sidecar OS PID 及相同可执行文件
哈希；页面脚本自报的布尔值本身不足以通过校验。证据、公开确定性夹具、安装包 SHA-256、源码 SHA/tree、
v1 基准及生成器、schema 和校验器均进入 candidate artifact manifest，并由同一提交的
不可变 main proof 继续约束。这个自动化证明面向 GitHub-hosted Windows runner；它不
替代正式发布前独立的 Windows 10/11 普通用户实机验收。

## English: packaged desktop compatibility evidence

Windows candidate A installs the actual NSIS candidate and submits a complete
12-cell matrix from the Tauri WebView through authenticated host IPC: MACD and a
parameterized custom formula, single stock and pool, across daily, weekly, and
60-minute periods. The packaged sidecar Worker executes every task. Evidence
binds frozen formula parameters, pool and data snapshots, reports, result hashes,
the installer digest, source SHA/tree, the immutable v1 oracle and generator,
schema, verifier, artifact manifest, and the same-SHA main proof. Read-only demo,
a normal browser, `TestClient`, and router-only navigation fail closed.

A larger custom-pool run also enters the desktop shutdown checkpoint protocol,
restarts the sidecar, and resumes under a different Worker identity. A
nonce-bound Windows host observation cross-checks the native window, isolated
WebView2 processes, different before/after sidecar OS PIDs, and the unchanged
sidecar executable digest; self-reported booleans alone cannot pass. This hosted
runner evidence does not replace the separate clean Windows 10/11 standard-user
release acceptance.

## 基础语义 / Core semantics

Stock Desk v0.4.0 turns the BUY and SELL outputs of one immutable trading-formula version into reproducible historical trade samples. The engine is research software: it does not connect to a broker, place orders, share capital across stocks, or predict future performance.

## Frozen inputs

Submitting a run stores an immutable snapshot containing:

- formula/version/checksum, compatibility and engine versions, and normalized parameters;
- single-stock identity or exact preset/custom-pool revision;
- listed-instrument dataset version;
- signal, execution, and execution-status manifest identities for every runnable symbol;
- period (`1d`, `1w`, or `60m`), adjustment, Shanghai-time half-open scoring range, and warm-up policy;
- fixed share quantity, commission basis points and minimum, sell tax, slippage, and all execution/cost/sizing rule versions.

A later market update or formula revision cannot change an existing run. Replay reopens these exact pins and verifies the recomputed SignalSeries identity. Missing, mixed, corrupt, or incompatible identities fail closed; replay never falls back to the latest cache.

## Signals and execution

Signals are calculated on the selected period. BUY/SELL becomes known only after that bar closes. The corresponding order first attempts to execute at the next eligible opening price:

- daily signal → next eligible trading-day open;
- weekly signal → next eligible daily open after the completed week;
- 60-minute signal → next eligible 60-minute bar open, never the signaling bar.

The engine models one state machine per stock: flat, pending buy, held, or pending sell. There is at most one position and one pending order. Repeated same-side signals are ignored. Conflicting simultaneous signals are recorded and ignored. An opposite signal cancels a pending order. A blocked order remains pending until an eligible open, an opposite signal, or the range end.

Execution uses the pinned historical status companion rather than inferring tradability from missing prices. It fails closed on unknown status. Every frozen status snapshot declares one evidence grade:

- `authoritative` (Tushare): explicit exchange calendar, suspension, and historical side-specific upper/lower price-limit evidence;
- `basic_no_price_limits` (BaoStock): explicit exchange calendar and `tradestatus`, without historical price-limit evidence;
- `mixed` (pool report only): at least one runnable symbol uses the basic grade.

Both strict and basic execution apply:

- A-share T+1 sell eligibility;
- exchange closure and suspension;
- exact price/open availability for the execution period.

The frozen rule version makes this distinction replayable: fully authoritative
runs keep `a-share-v1`, while any basic or mixed run uses `a-share-v2`. A
snapshot whose evidence grade and rule version disagree is rejected rather than
silently changing its fill semantics.

Only `authoritative` execution additionally blocks buys at the historical upper limit and sells at the historical lower limit. Basic execution never approximates those limits from board, ST, IPO, volume, price movement, or missing bars. Preflight, report, replay provenance, and exports keep a visible `basic_execution_status` warning because this limitation can overestimate fill opportunities; it does not require an extra confirmation click.

Weekly charts keep weekly signals on weekly coordinates. Their exact daily fill bar is disclosed separately in replay rather than being drawn at a false weekly timestamp.

## Costs and sizing

V1 uses a fixed positive share quantity in multiples of 100. It does not model shared cash, position competition, capital allocation, rebalancing, or partial fills.

Each fill applies adverse slippage. Commission is calculated independently on buy and sell and respects the configured minimum. Sell tax applies only to sells. Every trade retains reference-open gross PnL, fill-price gross PnL, buy/sell commission, sell tax, slippage, invested cost, and net PnL so the gross-to-net bridge remains auditable and costs are not double counted.

## Samples and metrics

Every stock is simulated independently. Pool aggregation combines independent realized trade samples; it is not a portfolio return and has no equity curve.

A realized trade wins only when its net PnL—and therefore net return—is greater than zero. Win rate is:

`positive realized trades / all realized trades`

Zero-return realized trades remain in the denominator. When no trade is realized, win rate and realized-return statistics are “not calculable,” not zero. Reports also disclose positive/negative/zero counts, mean and median net return, payoff ratio, largest win/loss, total realized net PnL, average holding bars/days, a fixed nine-bin return distribution, symbol/month/year groups, and sample reliability/concentration.

A position still held at the range end is marked with the final available price. Its floating PnL and return are shown under open positions and never enter realized win rate or realized-return totals.

Run outcomes distinguish:

- successfully processed runnable symbols;
- runnable symbols that failed during execution;
- processed frozen data gaps;
- symbols left unprocessed after cancellation.

One symbol failure does not stop the rest of a pool. Cancellation stops new claims and preserves checkpoints, trades, failures, logs, and partial report data.

## Reports, replay, and exports

The report starts with conclusions and sample reliability, then provides bounded cursor pages for groups, realized trades, open positions, failures, and logs. Reproducibility metadata and the independent-sample disclaimer remain visible throughout.

Selecting a trade opens a pinned K-line main chart and formula subchart, plus the full textual order lifecycle and exact execution-bar evidence. JSON and CSV exports are deterministic for a completed snapshot, include safe metadata, and neutralize spreadsheet formulas and private path/secret-like diagnostics.

## Known limitations

V1 intentionally does not model order-book depth, latency, partial fills, cash availability, dividends, financing, short selling, shared portfolio capital, broker-specific fees, or live trading. Adjustment choice is fixed per run; price comparisons and returns use that frozen convention. BaoStock basic execution does not check historical price limits and may therefore overestimate fill opportunities; use Tushare authoritative status when strict price-limit evidence is required. Historical results and estimated win rate are descriptive samples, not investment advice or a guarantee of future performance.
