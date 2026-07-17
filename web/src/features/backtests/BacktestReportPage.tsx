import { useEffect, useId, useRef, useState } from 'react';

import {
  backtestExportUrl,
  backtestApi,
  type BacktestFailure,
  type BacktestLog,
  type BacktestReport,
  type BacktestReportApi,
  type BacktestTrade,
} from './backtestApi';
import { FailureTable } from './FailureTable';
import { GroupedMetrics } from './GroupedMetrics';
import { ReportOverview } from './ReportOverview';
import { TradeTable } from './TradeTable';
import { TradeReplay } from './TradeReplay';
import {
  basicExecutionStatusWarning,
  executionStatusEvidenceLabel,
} from './executionStatusEvidence';

const tabs = [
  ['overview', '结论概览'],
  ['trades', '交易明细'],
  ['open', '开放仓位'],
  ['failures', '失败记录'],
  ['logs', '运行日志'],
] as const;
type TabId = (typeof tabs)[number][0];

type PageState<T> = {
  readonly items: readonly T[];
  readonly nextCursor: string | null;
  readonly loading: boolean;
  readonly error: boolean;
};
const emptyPage = { items: [], nextCursor: null, loading: false, error: false };

export function BacktestReportPage({
  api = backtestApi,
  report: suppliedReport,
  runId: suppliedRunId,
}: {
  readonly api?: BacktestReportApi;
  readonly report?: BacktestReport;
  readonly runId?: string;
}) {
  const runId = suppliedReport?.overview.runId ?? suppliedRunId ?? '';
  const [loadedReport, setLoadedReport] = useState<{
    readonly runId: string;
    readonly value: BacktestReport;
  } | null>(null);
  const [reportErrorRunId, setReportErrorRunId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>('overview');
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<
    readonly (string | null)[]
  >([]);
  const [replayTrade, setReplayTrade] = useState<BacktestTrade | null>(null);
  const [trades, setTrades] = useState<PageState<BacktestTrade>>(emptyPage);
  const [open, setOpen] = useState<PageState<BacktestTrade>>(emptyPage);
  const [failures, setFailures] =
    useState<PageState<BacktestFailure>>(emptyPage);
  const [logs, setLogs] = useState<PageState<BacktestLog>>(emptyPage);
  const tabRefs = useRef(new Map<TabId, HTMLButtonElement>());
  const id = useId();

  useEffect(() => {
    if (suppliedReport !== undefined || runId === '') return undefined;
    const controller = new AbortController();
    setReportErrorRunId(null);
    void api
      .getReport(runId, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setLoadedReport({ runId, value });
      })
      .catch(() => {
        if (!controller.signal.aborted) setReportErrorRunId(runId);
      });
    return () => controller.abort();
  }, [api, runId, suppliedReport]);

  useEffect(() => {
    setActiveTab('overview');
    setCursor(null);
    setCursorHistory([]);
    setReplayTrade(null);
    setTrades(emptyPage);
    setOpen(emptyPage);
    setFailures(emptyPage);
    setLogs(emptyPage);
  }, [runId]);

  useEffect(() => {
    if (runId === '' || activeTab === 'overview') return undefined;
    const controller = new AbortController();
    if (activeTab === 'trades' || activeTab === 'open') {
      const setter = activeTab === 'trades' ? setTrades : setOpen;
      setter({ ...emptyPage, loading: true });
      void api
        .getTrades(runId, activeTab === 'trades' ? 'realized' : 'open', {
          cursor,
          signal: controller.signal,
        })
        .then((page) => {
          if (!controller.signal.aborted)
            setter({ ...page, loading: false, error: false });
        })
        .catch(() => {
          if (!controller.signal.aborted) setter({ ...emptyPage, error: true });
        });
    } else if (activeTab === 'failures') {
      setFailures({ ...emptyPage, loading: true });
      void api
        .getFailures(runId, { cursor, signal: controller.signal })
        .then((page) => {
          if (!controller.signal.aborted)
            setFailures({ ...page, loading: false, error: false });
        })
        .catch(() => {
          if (!controller.signal.aborted)
            setFailures({ ...emptyPage, error: true });
        });
    } else {
      setLogs({ ...emptyPage, loading: true });
      void api
        .getReportLogs(runId, { cursor, signal: controller.signal })
        .then((page) => {
          if (!controller.signal.aborted)
            setLogs({ ...page, loading: false, error: false });
        })
        .catch(() => {
          if (!controller.signal.aborted)
            setLogs({ ...emptyPage, error: true });
        });
    }
    return () => controller.abort();
  }, [activeTab, api, cursor, runId]);

  function activateTab(tabId: TabId) {
    setActiveTab(tabId);
    setCursor(null);
    setCursorHistory([]);
    setReplayTrade(null);
  }

  function selectTab(index: number) {
    const tab = tabs[(index + tabs.length) % tabs.length];
    if (tab === undefined) return;
    activateTab(tab[0]);
    tabRefs.current.get(tab[0])?.focus();
  }

  const report =
    suppliedReport ??
    (loadedReport?.runId === runId ? loadedReport.value : null);
  if (reportErrorRunId === runId)
    return (
      <p className="backtest-error-summary" role="alert">
        回测报告暂时无法读取，运行明细仍保留在本地。
      </p>
    );
  if (report === null) return <p role="status">正在读取固定回测报告…</p>;

  const page =
    activeTab === 'trades' ? trades : activeTab === 'open' ? open : null;
  return (
    <section className="backtest-report" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="visually-hidden">
        回测结果
      </h3>
      <ReportOverview report={report} />
      <ReportMetadata report={report} />
      <div className="report-tabs" role="tablist" aria-label="回测报告分区">
        {tabs.map(([tabId, label], index) => (
          <button
            key={tabId}
            ref={(node) => {
              if (node === null) tabRefs.current.delete(tabId);
              else tabRefs.current.set(tabId, node);
            }}
            type="button"
            role="tab"
            id={`${id}-tab-${tabId}`}
            aria-controls={`${id}-panel-${tabId}`}
            aria-selected={activeTab === tabId}
            tabIndex={activeTab === tabId ? 0 : -1}
            onClick={() => activateTab(tabId)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight') selectTab(index + 1);
              else if (event.key === 'ArrowLeft') selectTab(index - 1);
              else if (event.key === 'Home') selectTab(0);
              else if (event.key === 'End') selectTab(tabs.length - 1);
              else return;
              event.preventDefault();
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <section
        className="report-tab-panel"
        role="tabpanel"
        id={`${id}-panel-${activeTab}`}
        aria-labelledby={`${id}-tab-${activeTab}`}
      >
        {activeTab === 'overview' ? (
          <GroupedMetrics
            api={api}
            disclaimer={report.disclaimer}
            runId={runId}
          />
        ) : page !== null ? (
          page.loading ? (
            <p role="status">正在读取当前页…</p>
          ) : page.error ? (
            <p role="alert">当前页读取失败，请切换分区后重试。</p>
          ) : (
            <>
              <TradeTable items={page.items} onReplay={setReplayTrade} />
              {replayTrade === null ? null : (
                <TradeReplay
                  key={`${replayTrade.symbol}-${String(replayTrade.ordinal)}`}
                  api={api}
                  runId={runId}
                  trade={replayTrade}
                />
              )}
            </>
          )
        ) : activeTab === 'failures' ? (
          failures.loading ? (
            <p role="status">正在读取失败记录…</p>
          ) : failures.error ? (
            <p role="alert">失败记录读取失败。</p>
          ) : (
            <FailureTable items={failures.items} />
          )
        ) : logs.loading ? (
          <p role="status">正在读取日志…</p>
        ) : logs.error ? (
          <p role="alert">日志读取失败。</p>
        ) : logs.items.length === 0 ? (
          <p>当前页没有日志。</p>
        ) : (
          <ol className="report-log-list">
            {logs.items.map((log) => (
              <li key={log.ordinal}>
                <span>{log.level}</span> {log.message}
              </li>
            ))}
          </ol>
        )}
        {activeTab === 'overview' ? null : (
          <CursorControls
            cursor={cursor}
            history={cursorHistory}
            loading={
              page?.loading ??
              (activeTab === 'failures' ? failures.loading : logs.loading)
            }
            nextCursor={
              page !== null
                ? page.nextCursor
                : activeTab === 'failures'
                  ? failures.nextCursor
                  : logs.nextCursor
            }
            onChange={(next, history) => {
              setReplayTrade(null);
              setCursor(next);
              setCursorHistory(history);
            }}
          />
        )}
      </section>
    </section>
  );
}

function ReportMetadata({ report }: { readonly report: BacktestReport }) {
  return (
    <section
      className="report-metadata"
      aria-labelledby="report-metadata-title"
    >
      <h4 id="report-metadata-title">回测数据与计算规则</h4>
      <p className="backtest-disclaimer">{report.disclaimer}</p>
      {report.warnings.includes('basic_execution_status') ? (
        <p className="warning-text">{basicExecutionStatusWarning}</p>
      ) : null}
      <dl>
        <div>
          <dt>快照</dt>
          <dd>{report.overview.snapshotId}</dd>
        </div>
        <div>
          <dt>结果哈希</dt>
          <dd>{report.overview.resultHash ?? '未生成'}</dd>
        </div>
        <div>
          <dt>公式版本</dt>
          <dd>{report.formulaVersionId}</dd>
        </div>
        <div>
          <dt>公式校验和</dt>
          <dd>{report.formulaChecksum}</dd>
        </div>
        <div>
          <dt>公式参数</dt>
          <dd>
            {report.formulaParameters.length === 0
              ? '无'
              : report.formulaParameters
                  .map((parameter) => `${parameter.name}=${parameter.value}`)
                  .join('；')}
          </dd>
        </div>
        <div>
          <dt>公式引擎</dt>
          <dd>{report.formulaEngineVersion}</dd>
        </div>
        <div>
          <dt>兼容版本</dt>
          <dd>{report.compatibilityVersion}</dd>
        </div>
        <div>
          <dt>回测引擎</dt>
          <dd>{report.backtestEngineVersion}</dd>
        </div>
        <div>
          <dt>预热策略</dt>
          <dd>{report.warmupPolicyVersion}</dd>
        </div>
        <div>
          <dt>周期 / 复权</dt>
          <dd>
            {report.period} / {report.adjustment}
          </dd>
        </div>
        <div>
          <dt>执行规则</dt>
          <dd>{report.executionRulesVersion}</dd>
        </div>
        <div>
          <dt>成交状态证据</dt>
          <dd>
            {executionStatusEvidenceLabel(report.executionStatusEvidenceLevel)}
          </dd>
        </div>
        <div>
          <dt>成本 / 仓位</dt>
          <dd>
            {report.costModelVersion} / {report.sizingVersion}
          </dd>
        </div>
        <div>
          <dt>佣金（bps）</dt>
          <dd>{report.costs.commissionBps}</dd>
        </div>
        <div>
          <dt>最低佣金</dt>
          <dd>{report.costs.minimumCommission}</dd>
        </div>
        <div>
          <dt>卖出印花税（bps）</dt>
          <dd>{report.costs.sellTaxBps}</dd>
        </div>
        <div>
          <dt>滑点（bps）</dt>
          <dd>{report.costs.slippageBps}</dd>
        </div>
        <div>
          <dt>信号数据源</dt>
          <dd>{report.provenance.sourceIds.signal.join('、') || '未记录'}</dd>
        </div>
        <div>
          <dt>执行数据源</dt>
          <dd>
            {report.provenance.sourceIds.execution.join('、') || '未记录'}
          </dd>
        </div>
        <div>
          <dt>状态数据源</dt>
          <dd>{report.provenance.sourceIds.status.join('、') || '未记录'}</dd>
        </div>
        <div>
          <dt>证券数据集</dt>
          <dd>{report.provenance.instrumentDatasetVersion}</dd>
        </div>
        <div>
          <dt>来源证据</dt>
          <dd>{report.provenance.digest}</dd>
        </div>
      </dl>
      <nav className="report-exports" aria-label="导出固定回测结果">
        <a href={backtestExportUrl(report.overview.runId, 'trades', 'csv')}>
          导出交易 CSV
        </a>
        <a href={backtestExportUrl(report.overview.runId, 'open', 'csv')}>
          导出开放仓位 CSV
        </a>
        <a href={backtestExportUrl(report.overview.runId, 'failures', 'csv')}>
          导出失败 CSV
        </a>
        <a href={backtestExportUrl(report.overview.runId, 'logs', 'json')}>
          导出日志 JSON
        </a>
      </nav>
    </section>
  );
}

function CursorControls({
  cursor,
  history,
  loading,
  nextCursor,
  onChange,
}: {
  readonly cursor: string | null;
  readonly history: readonly (string | null)[];
  readonly loading: boolean;
  readonly nextCursor: string | null;
  readonly onChange: (
    cursor: string | null,
    history: readonly (string | null)[],
  ) => void;
}) {
  return (
    <div className="cursor-controls" role="group" aria-label="当前报告页翻页">
      <button
        type="button"
        disabled={history.length === 0 || loading}
        onClick={() => onChange(history.at(-1) ?? null, history.slice(0, -1))}
      >
        上一页
      </button>
      <button
        type="button"
        disabled={nextCursor === null || loading}
        onClick={() => {
          if (nextCursor !== null) onChange(nextCursor, [...history, cursor]);
        }}
      >
        下一页
      </button>
    </div>
  );
}
