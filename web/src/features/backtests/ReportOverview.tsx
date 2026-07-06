import type { BacktestReport } from './backtestApi';
import { DistributionChart } from './DistributionChart';

const percent = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 2,
  style: 'percent',
});

function ratio(value: string | null) {
  return value === null ? '不可计算' : percent.format(Number(value));
}

const reliabilityLabels = { high: '高', medium: '中', low: '低' } as const;

export function ReportOverview({
  report,
}: {
  readonly report: BacktestReport;
}) {
  const metrics = report.metrics;
  return (
    <section
      className="report-overview"
      aria-labelledby="report-overview-heading"
    >
      <header>
        <div>
          <span className="panel-kicker">CONCLUSION FIRST</span>
          <h3 id="report-overview-heading">回测结论</h3>
        </div>
        <span className="status-badge">{report.overview.status}</span>
      </header>
      <p className="backtest-disclaimer">{report.disclaimer}</p>
      {metrics === null ? (
        <div className="report-empty-conclusion" role="note">
          <strong>聚合结论不可计算</strong>
          <p>运行未完整聚合；仍可查看已持久化的明细、失败与日志。</p>
        </div>
      ) : (
        <>
          <div className="report-metric-grid">
            <article>
              <span>胜率</span>
              <strong>{ratio(metrics.winRate)}</strong>
              <small>{metrics.winRateDenominator} 个已实现样本</small>
            </article>
            <article>
              <span>平均单笔净收益</span>
              <strong>{ratio(metrics.meanNetReturn)}</strong>
            </article>
            <article>
              <span>中位单笔净收益</span>
              <strong>{ratio(metrics.medianNetReturn)}</strong>
            </article>
            <article>
              <span>样本可靠性</span>
              <strong>{reliabilityLabels[metrics.reliability.level]}</strong>
              <small>
                {metrics.reliability.reason ?? '样本数量与集中度满足要求'}
              </small>
            </article>
            <article>
              <span>已实现样本</span>
              <strong>{metrics.realizedCount}</strong>
            </article>
            <article>
              <span>已实现净盈亏</span>
              <strong>{metrics.realizedNetPnlTotal}</strong>
            </article>
            <article>
              <span>盈亏比</span>
              <strong>{metrics.payoffRatio ?? '不可计算'}</strong>
              {metrics.payoffRatioReason === null ? null : (
                <small>{metrics.payoffRatioReason}</small>
              )}
            </article>
            <article>
              <span>最大单笔盈利</span>
              <strong>{ratio(metrics.maxWinReturn)}</strong>
              {metrics.maxWinReturnReason === null ? null : (
                <small>{metrics.maxWinReturnReason}</small>
              )}
            </article>
            <article>
              <span>最大单笔亏损</span>
              <strong>{ratio(metrics.maxLossReturn)}</strong>
              {metrics.maxLossReturnReason === null ? null : (
                <small>{metrics.maxLossReturnReason}</small>
              )}
            </article>
            <article>
              <span>平均持有 K 线</span>
              <strong>{metrics.averageHoldingBars ?? '不可计算'}</strong>
              {metrics.averageHoldingBarsReason === null ? null : (
                <small>{metrics.averageHoldingBarsReason}</small>
              )}
            </article>
            <article>
              <span>平均持有天数</span>
              <strong>{metrics.averageHoldingDays ?? '不可计算'}</strong>
              {metrics.averageHoldingDaysReason === null ? null : (
                <small>{metrics.averageHoldingDaysReason}</small>
              )}
            </article>
            <article>
              <span>开放仓位样本</span>
              <strong>{metrics.openTrades.count}</strong>
            </article>
            <article>
              <span>开放仓位浮动盈亏</span>
              <strong>{metrics.openTrades.floatingPnlTotal}</strong>
            </article>
            <article>
              <span>开放仓位平均浮动收益</span>
              <strong>{ratio(metrics.openTrades.meanFloatingReturn)}</strong>
              {metrics.openTrades.meanFloatingReturnReason === null ? null : (
                <small>{metrics.openTrades.meanFloatingReturnReason}</small>
              )}
            </article>
          </div>
          {metrics.realizedCount === 0 ? (
            <p className="report-empty-conclusion">
              <strong>无已实现样本</strong>，胜率与单笔收益不可计算。
            </p>
          ) : null}
          <DistributionChart bins={metrics.histogram} />
        </>
      )}
      <dl className="report-counts">
        <div>
          <dt>成功处理</dt>
          <dd>{report.outcomes.succeeded}</dd>
        </div>
        <div>
          <dt>失败</dt>
          <dd>{report.outcomes.failed}</dd>
        </div>
        <div>
          <dt>数据不足</dt>
          <dd>{report.outcomes.dataInsufficient}</dd>
        </div>
        <div>
          <dt>未处理</dt>
          <dd>{report.outcomes.unprocessed}</dd>
        </div>
        <div>
          <dt>开放仓位</dt>
          <dd>{metrics?.openTrades.count ?? '不可计算'}</dd>
        </div>
      </dl>
    </section>
  );
}
