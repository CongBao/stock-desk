import { useCallback, useEffect, useRef, useState } from 'react';

import { AnalysisRunPanel } from './AnalysisRunPanel';
import { ConclusionPanel } from './ConclusionPanel';
import { EvidencePanel } from './EvidencePanel';
import { ProcessRail } from './ProcessRail';
import {
  analysisApi,
  type AnalysisApi,
  type AnalysisClaim,
  type AnalysisDetail,
  type AnalysisOverview,
  type AnalysisReport,
  type EvidenceItem,
  type ModelConfig,
} from './analysisApi';

const terminalStatuses = new Set([
  'succeeded',
  'partial',
  'insufficient_evidence',
  'failed',
  'cancelled',
]);

const retryCreatedStatus = '已创建阶段重试子任务；当前正在显示该子任务。';

export function AnalysisPage({
  api = analysisApi,
  initialRunId = null,
  pollIntervalMs = 800,
}: {
  readonly api?: AnalysisApi;
  readonly initialRunId?: string | null;
  readonly pollIntervalMs?: number;
}) {
  const [models, setModels] = useState<readonly ModelConfig[]>([]);
  const [history, setHistory] = useState<readonly AnalysisOverview[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(initialRunId);
  const [run, setRun] = useState<AnalysisDetail | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<
    readonly EvidenceItem[]
  >([]);
  const [selectedClaim, setSelectedClaim] = useState<AnalysisClaim | null>(
    null,
  );
  const [retryingStage, setRetryingStage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState('');
  const [drawer, setDrawer] = useState<'process' | 'evidence' | null>(null);
  const processButtonRef = useRef<HTMLButtonElement>(null);
  const evidenceButtonRef = useRef<HTMLButtonElement>(null);
  const processCloseRef = useRef<HTMLButtonElement>(null);
  const evidenceCloseRef = useRef<HTMLButtonElement>(null);
  const claimTriggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (drawer === null) return;
    requestAnimationFrame(() =>
      (drawer === 'process'
        ? processCloseRef
        : evidenceCloseRef
      ).current?.focus(),
    );
  }, [drawer]);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .listModels({ signal: controller.signal })
      .then((modelPage) => setModels(modelPage.items))
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setStatusMessage(
            error instanceof Error ? error.message : '加载模型配置失败',
          );
      });
    void api
      .listRuns({ signal: controller.signal })
      .then((historyPage) => {
        setHistory((current) => {
          const merged = new Map(
            historyPage.items.map((item) => [item.runId, item]),
          );
          for (const item of current) merged.set(item.runId, item);
          return [...merged.values()].sort(
            (left, right) =>
              Date.parse(right.createdAt) - Date.parse(left.createdAt),
          );
        });
        setHistoryCursor(historyPage.nextCursor);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setStatusMessage(
            error instanceof Error ? error.message : '加载历史报告失败',
          );
      });
    return () => controller.abort();
  }, [api]);

  useEffect(() => {
    if (runId === null) return undefined;
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let transientFailures = 0;

    async function poll() {
      try {
        const detail = await api.getRun(runId as string, {
          signal: controller.signal,
        });
        if (!active) return;
        transientFailures = 0;
        setRun(detail);
        setStatusMessage((current) =>
          current === retryCreatedStatus ? current : '',
        );
        if (terminalStatuses.has(detail.status)) {
          setHistory((current) =>
            [
              detail,
              ...current.filter((item) => item.runId !== detail.runId),
            ].sort(
              (left, right) =>
                Date.parse(right.createdAt) - Date.parse(left.createdAt),
            ),
          );
          if (detail.reportId !== null) {
            const persistedReport = await api.getReport(detail.runId, {
              signal: controller.signal,
            });
            if (active) {
              setReport(persistedReport);
              const first = persistedReport.coreJudgments[0];
              if (first !== undefined) {
                setSelectedClaim(first);
                setSelectedEvidence(
                  persistedReport.evidenceItems.filter((item) =>
                    first.evidenceIds.includes(item.evidenceId),
                  ),
                );
              }
            }
          }
          return;
        }
        attempt += 1;
        const delay = Math.min(
          pollIntervalMs * 2 ** Math.min(attempt, 3),
          5000,
        );
        timer = setTimeout(() => void poll(), delay);
      } catch (error) {
        if (active && !controller.signal.aborted) {
          transientFailures += 1;
          setStatusMessage(
            transientFailures <= 3
              ? '分析状态暂时不可用，正在自动重试…'
              : error instanceof Error
                ? error.message
                : '获取分析状态失败',
          );
          if (transientFailures <= 3) {
            timer = setTimeout(
              () => void poll(),
              Math.min(pollIntervalMs * transientFailures, 5000),
            );
          }
        }
      }
    }
    setRun(null);
    setReport(null);
    setSelectedEvidence([]);
    setSelectedClaim(null);
    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [api, pollIntervalMs, runId]);

  const openRun = useCallback((id: string) => {
    setRunId(id);
    setStatusMessage('正在打开不可变历史报告…');
    document
      .querySelector<HTMLElement>('.analysis-report-workspace')
      ?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, []);

  async function loadMore() {
    if (historyCursor === null) return;
    const controller = new AbortController();
    try {
      const page = await api.listRuns({
        cursor: historyCursor,
        signal: controller.signal,
      });
      setHistory((items) => [...items, ...page.items]);
      setHistoryCursor(page.nextCursor);
    } catch (error) {
      setStatusMessage(
        error instanceof Error ? error.message : '加载历史报告失败',
      );
    }
  }

  function selectClaim(claim: AnalysisClaim, trigger: HTMLButtonElement) {
    if (report === null) return;
    claimTriggerRef.current = trigger;
    setSelectedClaim(claim);
    setSelectedEvidence(
      report.evidenceItems.filter((item) =>
        claim.evidenceIds.includes(item.evidenceId),
      ),
    );
    if (window.matchMedia?.('(max-width: 1280px)').matches) {
      setDrawer('evidence');
      requestAnimationFrame(() =>
        document
          .getElementById('analysis-evidence-drawer')
          ?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' }),
      );
    }
  }

  async function cancel() {
    if (
      run === null ||
      run.cancelRequested ||
      !['queued', 'running'].includes(run.status)
    )
      return;
    const controller = new AbortController();
    try {
      const cancelled = await api.cancelRun(run.runId, {
        signal: controller.signal,
      });
      setRun(cancelled);
      setStatusMessage('取消请求已记录；已持久化数据不会删除。');
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : '取消分析失败');
    }
  }

  async function retry(stage: string) {
    if (run === null) return;
    const controller = new AbortController();
    setRetryingStage(stage);
    try {
      const child = await api.retryStage(run.runId, stage, {
        signal: controller.signal,
      });
      setStatusMessage(retryCreatedStatus);
      setRunId(child.runId);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : '阶段重试失败');
    } finally {
      setRetryingStage(null);
    }
  }

  function closeDrawer() {
    const active = drawer;
    if (active === 'process') processButtonRef.current?.focus();
    else if (claimTriggerRef.current !== null) {
      claimTriggerRef.current.focus();
      claimTriggerRef.current = null;
    } else evidenceButtonRef.current?.focus();
    setDrawer(null);
  }

  const cancellable =
    run !== null && ['queued', 'running'].includes(run.status);

  return (
    <div className="analysis-page">
      <header className="page-heading analysis-page-heading">
        <div>
          <span className="page-kicker">
            A-SHARE INTELLIGENCE / RESEARCH ONLY
          </span>
          <h2 data-page-heading tabIndex={-1}>
            智能分析
          </h2>
          <p>以快照、九阶段研究流程和可追溯证据，形成不可变的辅助研究报告。</p>
        </div>
        <span className="release-badge">Stage 4 · Analysis</span>
      </header>

      <AnalysisRunPanel
        api={api}
        models={models}
        onModelsChange={setModels}
        history={history}
        nextCursor={historyCursor}
        onLoadMore={() => void loadMore()}
        onOpenRun={openRun}
        onStarted={openRun}
      />

      <div
        className="analysis-report-toolbar"
        role="toolbar"
        aria-label="报告面板工具栏"
      >
        <button
          ref={processButtonRef}
          type="button"
          aria-expanded={drawer === 'process'}
          aria-controls="analysis-process-drawer"
          onClick={() => setDrawer('process')}
        >
          查看分析流程
        </button>
        <strong>{run?.symbol ?? '尚未选择报告'}</strong>
        {cancellable ? (
          <button
            type="button"
            disabled={run.cancelRequested}
            onClick={() => void cancel()}
          >
            {run.cancelRequested ? '取消处理中' : '取消分析'}
          </button>
        ) : null}
        <button
          ref={evidenceButtonRef}
          type="button"
          aria-expanded={drawer === 'evidence'}
          aria-controls="analysis-evidence-drawer"
          onClick={() => {
            claimTriggerRef.current = null;
            setDrawer('evidence');
          }}
        >
          查看证据
        </button>
      </div>

      <p className="analysis-global-status" role="status" aria-live="polite">
        {statusMessage}
      </p>

      <section
        className="analysis-report-workspace"
        aria-label="智能分析报告工作区"
      >
        <div
          id="analysis-process-drawer"
          className="analysis-drawer analysis-process-drawer"
          data-open={drawer === 'process'}
        >
          <button
            ref={processCloseRef}
            className="analysis-drawer-close"
            type="button"
            onClick={closeDrawer}
          >
            关闭分析流程
          </button>
          <ProcessRail run={run} />
        </div>
        <ConclusionPanel
          run={run}
          report={report}
          selectedClaim={selectedClaim}
          onSelectClaim={selectClaim}
          onRetry={(stage) => void retry(stage)}
          retryingStage={retryingStage}
        />
        <div
          id="analysis-evidence-drawer"
          className="analysis-drawer analysis-evidence-drawer"
          data-open={drawer === 'evidence'}
        >
          <button
            ref={evidenceCloseRef}
            className="analysis-drawer-close"
            type="button"
            onClick={closeDrawer}
          >
            关闭证据
          </button>
          <EvidencePanel claim={selectedClaim} items={selectedEvidence} />
        </div>
      </section>
    </div>
  );
}
