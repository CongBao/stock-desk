import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import type {
  AnalysisClaim,
  AnalysisDetail,
  AnalysisReport,
} from './analysisApi';

const ratingLabels: Readonly<Record<string, string>> = {
  strong_bullish: '强烈看多',
  bullish: '看多',
  neutral: '中性',
  bearish: '看空',
  strong_bearish: '强烈看空',
};

const stageLabels: Readonly<Record<string, string>> = {
  technical: '技术研究',
  fundamental_news: '基本面与新闻',
  bull: '看多研究',
  bear: '看空研究',
  risk_decision: '风险决策',
};

function Claims({
  title,
  claims,
  selectedClaim,
  onSelect,
}: {
  readonly title: string;
  readonly claims: readonly AnalysisClaim[];
  readonly selectedClaim: AnalysisClaim | null;
  readonly onSelect: (claim: AnalysisClaim, trigger: HTMLButtonElement) => void;
}) {
  if (claims.length === 0) return null;
  return (
    <section className="analysis-claim-section">
      <h4>{title}</h4>
      <ul>
        {claims.map((claim) => (
          <li key={`${title}:${claim.text}`}>
            <button
              type="button"
              aria-pressed={selectedClaim === claim}
              onClick={(event) => onSelect(claim, event.currentTarget)}
            >
              <span>{claim.text}</span>
              <small>
                {claim.stance === 'support'
                  ? '支持证据'
                  : claim.stance === 'oppose'
                    ? '反对证据'
                    : '不确定证据'}{' '}
                · {claim.evidenceIds.length} 条
              </small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ConclusionPanel({
  run,
  report,
  selectedClaim,
  onSelectClaim,
  onRetry,
  retryingStage,
}: {
  readonly run: AnalysisDetail | null;
  readonly report: AnalysisReport | null;
  readonly selectedClaim: AnalysisClaim | null;
  readonly onSelectClaim: (
    claim: AnalysisClaim,
    trigger: HTMLButtonElement,
  ) => void;
  readonly onRetry: (stage: string) => void;
  readonly retryingStage: string | null;
}) {
  if (run === null) {
    return (
      <section className="analysis-conclusion" aria-label="研究结论">
        <div className="analysis-welcome">
          <span className="panel-kicker">INTELLIGENT RESEARCH</span>
          <h3>从可复核证据形成研究判断</h3>
          <p>先运行四类数据预检，再由九阶段流程生成研究报告。</p>
        </div>
      </section>
    );
  }
  if (report === null) {
    const terminalWithoutReport =
      run.status === 'failed'
        ? {
            title: '分析失败',
            detail: `失败代码：${run.failureCode ?? '未提供'}。可检查模型与数据源后重新发起。`,
          }
        : run.status === 'cancelled'
          ? {
              title: '分析已取消',
              detail: '取消已持久化；本次运行没有生成研究报告。',
            }
          : null;
    return (
      <section className="analysis-conclusion" aria-label="研究结论">
        <div className="analysis-running" role="status" aria-live="polite">
          <span className="analysis-progress-orbit" aria-hidden="true" />
          <h3>
            {terminalWithoutReport?.title ??
              (run.status === 'running' ? '智能分析运行中' : '等待研究报告')}
          </h3>
          {terminalWithoutReport === null ? (
            <>
              <p>
                当前阶段：{run.currentStage ?? '等待调度'} · 分析进度{' '}
                {Math.round(run.progress * 100)}%
              </p>
              <progress value={run.progress} max={1}>
                {Math.round(run.progress * 100)}%
              </progress>
            </>
          ) : (
            <p>{terminalWithoutReport.detail}</p>
          )}
        </div>
      </section>
    );
  }
  return (
    <article className="analysis-conclusion" aria-label="研究结论">
      <header className="analysis-report-heading">
        <div>
          <span className="panel-kicker">RESEARCH REPORT</span>
          <h3>{run.symbol} 智能分析</h3>
          <p>生成于 {new Date(report.generatedAt).toLocaleString('zh-CN')}</p>
        </div>
        {report.rating === null ? (
          <strong className="rating-unavailable">证据不足，暂不评级</strong>
        ) : (
          <div className="analysis-rating" data-rating={report.rating}>
            <span>{ratingLabels[report.rating]}</span>
            <strong>{Math.round(report.confidence * 100)}%</strong>
            <small>结论置信度</small>
          </div>
        )}
      </header>

      <section className="confidence-explanation">
        <h4>置信度说明</h4>
        <p>{report.confidenceExplanation}</p>
      </section>

      {report.status === 'partial' ? (
        <section
          className="analysis-report-alert"
          aria-label="部分报告缺失模块"
        >
          <h4>部分模块未完成</h4>
          <p>
            失败：
            {report.failedModules
              .map((stage) => stageLabels[stage] ?? stage)
              .join('、')}
          </p>
          <p>
            缺失：
            {report.missingModules.length === 0
              ? '无'
              : report.missingModules
                  .map((stage) => stageLabels[stage] ?? stage)
                  .join('、')}
          </p>
          <p>
            阻塞：
            {report.blockedModules.length === 0
              ? '无'
              : report.blockedModules
                  .map((stage) => stageLabels[stage] ?? stage)
                  .join('、')}
          </p>
          {report.stageFailures.map((failure) => (
            <p key={failure.stage}>
              {stageLabels[failure.stage] ?? failure.stage}：{failure.code}
              （尝试 {failure.attemptCount} 次）
            </p>
          ))}
          <div className="analysis-retry-actions">
            {report.retryActions.map((retry) => (
              <AsyncActionButton
                key={retry.stage}
                type="button"
                pending={retryingStage === retry.stage}
                disabled={retryingStage !== null}
                onClick={() => onRetry(retry.stage)}
              >
                {`重试${stageLabels[retry.stage] ?? retry.stage}模块`}
              </AsyncActionButton>
            ))}
          </div>
        </section>
      ) : null}

      {report.status === 'insufficient_evidence' ? (
        <section
          className="analysis-report-alert"
          aria-label="证据不足恢复建议"
        >
          <h4>缺失数据与恢复建议</h4>
          <p>
            缺失板块：{report.missingSections.join('、') || '关键证据覆盖不足'}
          </p>
          <ul>
            {report.recoveryActions.map((action) => (
              <li key={action}>{action}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <Claims
        title="核心判断"
        claims={report.coreJudgments}
        selectedClaim={selectedClaim}
        onSelect={onSelectClaim}
      />
      <div className="analysis-thesis-grid">
        <Claims
          title="看多论证"
          claims={report.bullClaims}
          selectedClaim={selectedClaim}
          onSelect={onSelectClaim}
        />
        <Claims
          title="看空论证"
          claims={report.bearClaims}
          selectedClaim={selectedClaim}
          onSelect={onSelectClaim}
        />
      </div>
      <Claims
        title="主要风险"
        claims={report.risks}
        selectedClaim={selectedClaim}
        onSelect={onSelectClaim}
      />

      <details className="analysis-metadata">
        <summary>模型与快照元数据</summary>
        <dl>
          <div>
            <dt>报告 ID</dt>
            <dd>{report.reportId}</dd>
          </div>
          <div>
            <dt>快照 ID</dt>
            <dd>{report.snapshotId}</dd>
          </div>
          <div>
            <dt>模型配置</dt>
            <dd>
              {run.modelProvider} / {run.modelName}
            </dd>
          </div>
          <div>
            <dt>角色执行记录</dt>
            <dd>{report.modelMetadata.length} 条</dd>
          </div>
        </dl>
      </details>
      <footer className="analysis-disclaimer">{report.disclaimer}</footer>
    </article>
  );
}
