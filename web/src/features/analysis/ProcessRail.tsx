import type { AnalysisDetail } from './analysisApi';

const stageLabels: Readonly<Record<string, string>> = {
  market: '行情快照',
  fundamentals: '基本面快照',
  announcements: '公告快照',
  news: '新闻快照',
  technical: '技术研究',
  fundamental_news: '基本面与新闻',
  bull: '看多论证',
  bear: '看空论证',
  risk_decision: '风险与结论',
};

const statusLabels: Readonly<Record<string, string>> = {
  pending: '待执行',
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  partial: '部分完成',
  insufficient_evidence: '证据不足',
  failed: '失败',
  skipped: '已跳过',
  cancelled: '已取消',
};

export function ProcessRail({ run }: { readonly run: AnalysisDetail | null }) {
  return (
    <aside className="analysis-process" aria-label="分析流程">
      <header>
        <span className="panel-kicker">PROCESS</span>
        <h3>九阶段流程</h3>
      </header>
      {run === null ? (
        <p className="analysis-empty">启动或打开历史分析后查看阶段状态。</p>
      ) : (
        <>
          <div className="run-status-summary" aria-live="polite">
            <span>
              分析状态：
              <strong>{statusLabels[run.status] ?? run.status}</strong>
            </span>
            <span>
              任务状态：{statusLabels[run.taskStatus] ?? run.taskStatus}
            </span>
          </div>
          <ol className="analysis-stage-list">
            {[...run.stages]
              .sort((left, right) => left.ordinal - right.ordinal)
              .map((stage) => (
                <li key={stage.stage} data-status={stage.status}>
                  <span className="stage-marker" aria-hidden="true" />
                  <div>
                    <strong>{stageLabels[stage.stage] ?? stage.stage}</strong>
                    <span>{statusLabels[stage.status] ?? stage.status}</span>
                    <small>
                      尝试 {stage.attemptCount} 次
                      {stage.durationMs === null
                        ? ''
                        : ` · ${String(Math.round(stage.durationMs))} ms`}
                    </small>
                    {stage.failureCode === null ? null : (
                      <small>失败代码：{stage.failureCode}</small>
                    )}
                  </div>
                </li>
              ))}
          </ol>
          <dl className="analysis-snapshot-list">
            <div>
              <dt>快照</dt>
              <dd>{run.snapshotId ?? '运行前暂未生成'}</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{run.modelName}</dd>
            </div>
          </dl>
        </>
      )}
    </aside>
  );
}
