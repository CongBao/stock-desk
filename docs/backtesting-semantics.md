# Backtesting semantics

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

Execution uses the pinned historical status companion rather than inferring tradability from missing prices. It fails closed on unknown status and applies:

- A-share T+1 sell eligibility;
- exchange closure and suspension;
- the historical side-specific upper limit for buys and lower limit for sells;
- exact price/open availability for the execution period.

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

V1 intentionally does not model order-book depth, latency, partial fills, cash availability, dividends, financing, short selling, shared portfolio capital, broker-specific fees, or live trading. Adjustment choice is fixed per run; price comparisons and returns use that frozen convention. Historical results and estimated win rate are descriptive samples, not investment advice or a guarantee of future performance.
