import type { BacktestHistogramBin } from './backtestApi';

const labels: Readonly<Record<string, string>> = {
  lt_neg_20pct: '< -20%',
  neg_20_to_10pct: '-20% ～ -10%',
  neg_10_to_5pct: '-10% ～ -5%',
  neg_5_to_0pct: '-5% ～ 0',
  zero: '0',
  pos_0_to_5pct: '0 ～ 5%',
  pos_5_to_10pct: '5% ～ 10%',
  pos_10_to_20pct: '10% ～ 20%',
  gt_20pct: '> 20%',
};

export function DistributionChart({
  bins,
}: {
  readonly bins: readonly BacktestHistogramBin[];
}) {
  const maximum = Math.max(1, ...bins.map((bin) => bin.count));
  return (
    <section
      className="report-distribution"
      aria-labelledby="distribution-title"
    >
      <h4 id="distribution-title">单笔净收益分布</h4>
      <ol aria-label="单笔净收益固定九档分布">
        {bins.map((bin) => (
          <li key={bin.code}>
            <span>{labels[bin.code] ?? bin.code}</span>
            <span className="distribution-track" aria-hidden="true">
              <span
                style={{ width: `${String((bin.count / maximum) * 100)}%` }}
              />
            </span>
            <strong>{bin.count}</strong>
          </li>
        ))}
      </ol>
    </section>
  );
}
