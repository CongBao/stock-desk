import { useEffect, useRef } from 'react';

import type { BacktestPreflight } from '../backtestApi';
import type { BacktestDraft } from '../backtestDraft';
import { RemediationLinks } from '../RemediationLinks';

const periodLabels = { '1d': '日线', '1w': '周线', '60m': '60 分钟' } as const;
const adjustmentLabels = {
  none: '不复权',
  qfq: '前复权',
  hfq: '后复权',
} as const;
const gapLabels: Readonly<Record<string, string>> = {
  missing_data: '缺少所选区间数据',
  missing_signal_data: '缺少信号覆盖',
  missing_execution_data: '缺少下一周期开盘数据',
  missing_execution_status: '缺少停牌/涨跌停状态',
  corrupt_data: '本地数据校验失败',
};
const warningLabels: Readonly<Record<string, string>> = {
  partial_pool_gaps:
    '部分证券数据不足；仅可运行证券会进入回测，缺口将保留为冻结结果。',
  partial_data: '部分证券数据覆盖不足，请核对可运行数量与缺口样例。',
};

export function ReviewStep({
  draft,
  formulaName,
  scopeName,
  preflight,
  busy,
  partialConfirmed,
  onPartialConfirmed,
  onPreflight,
}: {
  readonly draft: BacktestDraft;
  readonly formulaName: string;
  readonly scopeName: string;
  readonly preflight: BacktestPreflight | null;
  readonly busy: boolean;
  readonly partialConfirmed: boolean;
  readonly onPartialConfirmed: (confirmed: boolean) => void;
  readonly onPreflight: () => void;
}) {
  const resultRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (preflight !== null) resultRef.current?.focus();
  }, [preflight]);
  return (
    <section
      className="backtest-step"
      aria-labelledby="backtest-review-heading"
    >
      <h3 id="backtest-review-heading" tabIndex={-1}>
        5. 复核
      </h3>
      <p>
        收盘确认，下一对应周期开盘尝试成交；卖出遵守 A 股
        T+1，停牌、涨跌停和数据状态按固化规则处理。
      </p>
      <p className="backtest-disclaimer">每只股票独立模拟，不代表组合收益。</p>
      <dl className="review-intent">
        <div>
          <dt>公式</dt>
          <dd>{formulaName}</dd>
        </div>
        <div>
          <dt>范围</dt>
          <dd>{scopeName}</dd>
        </div>
        <div>
          <dt>周期与复权</dt>
          <dd>
            {periodLabels[draft.period]} · {adjustmentLabels[draft.adjustment]}
          </dd>
        </div>
        <div>
          <dt>上海时间半开区间</dt>
          <dd>
            {draft.startDate} 00:00（含）至 {draft.endDate} 00:00（不含）
          </dd>
        </div>
        <div>
          <dt>固定数量</dt>
          <dd>{draft.quantityShares} 股（100 股整数倍）</dd>
        </div>
        <div>
          <dt>佣金 / 最低佣金 / 卖出税 / 滑点</dt>
          <dd>
            {draft.commissionBps} 基点 / {draft.minimumCommission} 元 /{' '}
            {draft.sellTaxBps} 基点 / {draft.slippageBps} 基点
          </dd>
        </div>
      </dl>
      <button
        type="button"
        className="primary-action"
        disabled={busy}
        onClick={onPreflight}
      >
        {busy ? '预检中…' : '运行预检'}
      </button>
      {preflight === null ? (
        <p className="review-empty">
          预检会重新核对公式、数据覆盖和执行规则，不会创建任务。
        </p>
      ) : (
        <div
          ref={resultRef}
          className="preflight-result"
          aria-label="服务端预检结果"
          aria-live="polite"
          role="status"
          tabIndex={-1}
        >
          <strong>
            可运行 {preflight.scope.runnable} / {preflight.scope.total}
          </strong>
          <span>缺口 {preflight.scope.gapCount}</span>
          <dl>
            <div>
              <dt>规范参数</dt>
              <dd>
                {preflight.formula.normalizedParameters
                  .map((value) => `${value.name}=${value.value}`)
                  .join('，') || '无'}
              </dd>
            </div>
            <div>
              <dt>信号 / 成交 / 状态覆盖</dt>
              <dd>
                {preflight.coverage.signal} / {preflight.coverage.execution} /{' '}
                {preflight.coverage.status}
              </dd>
            </div>
            <div>
              <dt>预热</dt>
              <dd>
                {preflight.warmup.unboundedDependency
                  ? '完整历史前缀'
                  : `${String(preflight.warmup.lookbackBars ?? 0)} 根`}
              </dd>
            </div>
            <div>
              <dt>数量</dt>
              <dd>{preflight.quantityShares} 股</dd>
            </div>
            <div>
              <dt>成本</dt>
              <dd>
                {preflight.costs.commissionBps} /{' '}
                {preflight.costs.minimumCommission} /{' '}
                {preflight.costs.sellTaxBps} / {preflight.costs.slippageBps}
              </dd>
            </div>
            <div>
              <dt>执行规则</dt>
              <dd>收盘信号后下一对应周期开盘尝试成交</dd>
            </div>
            <div>
              <dt>成本规则</dt>
              <dd>A 股佣金、最低佣金、卖出印花税与滑点</dd>
            </div>
            <div>
              <dt>仓位规则</dt>
              <dd>固定股数、100 股整数倍</dd>
            </div>
            <div>
              <dt>预计工作量</dt>
              <dd>
                {preflight.estimatedWorkload.formulaRows} 行 ·{' '}
                {preflight.estimatedWorkload.runnableSymbols} 只
              </dd>
            </div>
          </dl>
          {preflight.scope.gapSample.length > 0 ? (
            <>
              <h4>数据缺口样例</h4>
              <ul>
                {preflight.scope.gapSample.map((gap) => (
                  <li key={`${gap.symbol}-${gap.reason}`}>
                    {gap.symbol} · {gapLabels[gap.reason] ?? '数据覆盖不足'}
                  </li>
                ))}
              </ul>
              {preflight.scope.gapsTruncated ? <p>仅显示前 100 条</p> : null}
            </>
          ) : null}
          {[
            ...new Set(
              preflight.scope.warnings.map(
                (warning) =>
                  warningLabels[warning] ??
                  '服务端提示存在数据覆盖差异，请核对冻结范围与缺口样例。',
              ),
            ),
          ].map((warning) => (
            <p className="warning-text" key={warning}>
              {warning}
            </p>
          ))}
          <p className="backtest-disclaimer">{preflight.disclaimer}</p>
          {preflight.scope.runnable > 0 && preflight.scope.gapCount > 0 ? (
            <label>
              <input
                type="checkbox"
                checked={partialConfirmed}
                onChange={(event) => onPartialConfirmed(event.target.checked)}
              />
              我确认本次仅回测 {preflight.scope.runnable}{' '}
              只可运行证券，缺口证券保留为冻结结果
            </label>
          ) : null}
          {preflight.scope.runnable === 0 ? <RemediationLinks /> : null}
          <details>
            <summary>不可变证据版本</summary>
            <dl>
              <div>
                <dt>公式版本</dt>
                <dd>{preflight.formula.formulaVersionId}</dd>
              </div>
              <div>
                <dt>预览快照</dt>
                <dd>{preflight.previewSnapshotId}</dd>
              </div>
              <div>
                <dt>范围版本</dt>
                <dd>
                  {preflight.scope.revisionOrSnapshotId ??
                    preflight.scope.symbol}
                </dd>
              </div>
              <div>
                <dt>执行 / 成本 / 仓位规则版本</dt>
                <dd>
                  {preflight.rules.executionRulesVersion} ·{' '}
                  {preflight.rules.costModelVersion} ·{' '}
                  {preflight.rules.sizingVersion}
                </dd>
              </div>
            </dl>
          </details>
        </div>
      )}
    </section>
  );
}
