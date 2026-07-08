import type { BacktestOverview } from './backtestApi';

const labels: Readonly<Record<string, string>> = {
  queued: '等待执行',
  running: '运行中',
  succeeded: '已完成',
  partial_failed: '部分完成',
  failed: '失败',
  cancelled: '已取消',
};
const stageLabels: Readonly<Record<string, string>> = {
  queued: '排队中',
  executing: '计算中',
  completed: '已汇总',
  failed: '失败',
  cancelled: '已取消',
};

export function RunProgress({ run }: { readonly run: BacktestOverview }) {
  return (
    <section
      className="run-progress"
      aria-labelledby="run-progress-heading"
      data-rendered-progress={[
        run.status,
        run.stage,
        run.processed,
        run.total,
        run.failed,
      ].join('|')}
    >
      <div role="status" aria-live="polite">
        <span className="status-badge" data-status={run.status}>
          {labels[run.status] ?? '未知状态'}
        </span>
        <span>
          {run.processed} / {run.total}
        </span>
      </div>
      <h3 id="run-progress-heading">运行进度</h3>
      <progress
        aria-labelledby="run-progress-heading"
        max={run.total}
        value={run.processed}
      >
        {Math.round(run.progress * 100)}%
      </progress>
      <p>{Math.round(run.progress * 100)}%</p>
      <dl>
        <div>
          <dt>已处理</dt>
          <dd>{run.processed}</dd>
        </div>
        <div>
          <dt>数据不足/失败</dt>
          <dd>{run.failed}</dd>
        </div>
        <div>
          <dt>阶段</dt>
          <dd>{stageLabels[run.stage] ?? '未知阶段'}</dd>
        </div>
      </dl>
    </section>
  );
}
