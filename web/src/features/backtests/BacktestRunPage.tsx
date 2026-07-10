import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';

import { ApiError } from '../../shared/api/client';
import {
  BacktestProtocolError,
  backtestApi,
  type BacktestApi,
  type BacktestLog,
  type BacktestOverview,
  type BacktestReportApi,
} from './backtestApi';
import { coalesceBacktestOverview } from './backtestOverviewState';
import { BacktestReportPage } from './BacktestReportPage';
import { backtestPollDelay, backtestPollDelays } from './backtestPolling';
import { RunProgress } from './RunProgress';

const terminalStatuses = new Set([
  'succeeded',
  'partial_failed',
  'failed',
  'cancelled',
]);

function supportsReports(
  api: BacktestApi,
): api is BacktestApi & BacktestReportApi {
  const candidate = api as Partial<BacktestReportApi>;
  return (
    typeof candidate.getReport === 'function' &&
    typeof candidate.getTrades === 'function' &&
    typeof candidate.getGroups === 'function' &&
    typeof candidate.getFailures === 'function' &&
    typeof candidate.getReportLogs === 'function' &&
    typeof candidate.getReplay === 'function'
  );
}

const RunLog = memo(function RunLog({
  logs,
  logError,
  onRetry,
}: {
  readonly logs: readonly BacktestLog[];
  readonly logError: string | null;
  readonly onRetry: () => void;
}) {
  const items = useMemo(
    () =>
      logs.map((log) => (
        <li key={log.ordinal}>
          <span>{log.level}</span> {log.message}
        </li>
      )),
    [logs],
  );

  return (
    <section
      className="run-log"
      aria-labelledby="run-log-heading"
      aria-live="polite"
    >
      <h3 id="run-log-heading">运行日志</h3>
      {logError !== null ? (
        <div role="alert">
          <p>{logError}</p>
          <button type="button" className="secondary-action" onClick={onRetry}>
            重试读取日志
          </button>
        </div>
      ) : null}
      {logs.length === 0 ? <p>暂无日志</p> : <ol>{items}</ol>}
    </section>
  );
});

export function BacktestRunPage({
  api = backtestApi,
  pollDelays = backtestPollDelays,
}: {
  readonly api?: BacktestApi;
  readonly pollDelays?: readonly number[];
}) {
  const { runId = '' } = useParams();
  const location = useLocation();
  const submissionNotice = (() => {
    const value: unknown = location.state;
    if (
      typeof value !== 'object' ||
      value === null ||
      !('submissionNotice' in value) ||
      !Array.isArray(value.submissionNotice)
    )
      return [];
    return value.submissionNotice
      .filter((item): item is string => typeof item === 'string')
      .slice(0, 20);
  })();
  const [run, setRun] = useState<BacktestOverview | null>(null);
  const [logs, setLogs] = useState<readonly BacktestLog[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [permanentError, setPermanentError] = useState(false);
  const [logRetryGeneration, setLogRetryGeneration] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const cancelLock = useRef(false);
  const cancelController = useRef<AbortController | null>(null);
  const retryLogs = useCallback(
    () => setLogRetryGeneration((value) => value + 1),
    [],
  );

  useEffect(() => {
    let active = true;
    let terminal = false;
    let finalLogRequested = false;
    let logInFlight = false;
    let runAttempt = 0;
    let logAttempt = 0;
    let finalLogFailures = 0;
    let runTimer: number | undefined;
    let logTimer: number | undefined;
    let afterCursor: string | null = null;
    const controllers = new Set<AbortController>();
    setLogError(null);

    function stop() {
      terminal = true;
      if (runTimer !== undefined) window.clearTimeout(runTimer);
      if (logTimer !== undefined) window.clearTimeout(logTimer);
      for (const controller of controllers) controller.abort();
      controllers.clear();
    }

    function requestFinalLog() {
      terminal = true;
      finalLogRequested = true;
      if (runTimer !== undefined) window.clearTimeout(runTimer);
      if (logTimer !== undefined) window.clearTimeout(logTimer);
      if (!logInFlight) void pollLogs(true);
    }

    async function pollRun() {
      if (!active || terminal) return;
      const controller = new AbortController();
      controllers.add(controller);
      try {
        const result = await api.getRun(runId, { signal: controller.signal });
        if (!active || controller.signal.aborted) return;
        setRun((current) => coalesceBacktestOverview(current, result));
        setError(null);
        setPermanentError(false);
        if (terminalStatuses.has(result.status)) {
          requestFinalLog();
          return;
        }
        runTimer = window.setTimeout(
          () => void pollRun(),
          backtestPollDelay(runAttempt++, pollDelays),
        );
      } catch (failure) {
        if (active && !controller.signal.aborted) {
          if (
            failure instanceof BacktestProtocolError ||
            (failure instanceof ApiError &&
              (failure.status === 404 || failure.status === 422))
          ) {
            setError('该回测不存在，或本地服务返回了不兼容的运行记录。');
            setPermanentError(true);
            stop();
            return;
          }
          setError('暂时无法读取回测进度，请稍后重试。');
          runTimer = window.setTimeout(
            () => void pollRun(),
            backtestPollDelay(runAttempt++, pollDelays),
          );
        }
      } finally {
        controllers.delete(controller);
      }
    }

    async function pollLogs(final = false) {
      if (!active || (terminal && !final && !finalLogRequested)) return;
      logInFlight = true;
      const controller = new AbortController();
      controllers.add(controller);
      let moreLogs = false;
      let retryFinal = false;
      const priorCursor = afterCursor;
      try {
        const page = await api.getLogs(runId, {
          afterCursor,
          signal: controller.signal,
        });
        if (!active || controller.signal.aborted) return;
        finalLogFailures = 0;
        setLogError(null);
        afterCursor = page.afterCursor ?? afterCursor;
        const cursorAdvanced = afterCursor !== priorCursor;
        moreLogs =
          final && page.nextCursor !== null && afterCursor !== priorCursor;
        setLogs((current) => {
          if (page.items.length === 0 && !cursorAdvanced) return current;
          const byOrdinal = new Map(
            current.map((item) => [item.ordinal, item]),
          );
          for (const item of page.items) byOrdinal.set(item.ordinal, item);
          return [...byOrdinal.values()]
            .sort((left, right) => left.ordinal - right.ordinal)
            .slice(-300);
        });
        if (!final && !finalLogRequested)
          logTimer = window.setTimeout(
            () => void pollLogs(),
            backtestPollDelay(logAttempt++, pollDelays),
          );
      } catch (failure) {
        const permanent =
          failure instanceof BacktestProtocolError ||
          (failure instanceof ApiError &&
            (failure.status === 404 || failure.status === 422));
        if (
          active &&
          !controller.signal.aborted &&
          (final || finalLogRequested)
        ) {
          if (!permanent && finalLogFailures < 3) {
            finalLogFailures += 1;
            retryFinal = true;
          } else {
            finalLogRequested = false;
            setLogError(
              '未能读取完整运行日志；可手动重试，已加载的运行结果不受影响。',
            );
          }
        }
        if (
          active &&
          !controller.signal.aborted &&
          !final &&
          !finalLogRequested
        )
          logTimer = window.setTimeout(
            () => void pollLogs(),
            backtestPollDelay(logAttempt++, pollDelays),
          );
      } finally {
        controllers.delete(controller);
        logInFlight = false;
        if (active && retryFinal)
          logTimer = window.setTimeout(
            () => void pollLogs(true),
            backtestPollDelay(finalLogFailures - 1, pollDelays),
          );
        else if (active && finalLogRequested && !final) void pollLogs(true);
        else if (final && moreLogs)
          logTimer = window.setTimeout(() => void pollLogs(true), 0);
        else if (final) stop();
      }
    }

    void pollRun();
    void pollLogs();
    return () => {
      active = false;
      cancelController.current?.abort();
      stop();
    };
  }, [api, logRetryGeneration, pollDelays, runId]);

  async function cancel() {
    if (cancelLock.current || run === null || terminalStatuses.has(run.status))
      return;
    cancelLock.current = true;
    setCancelling(true);
    setError(null);
    try {
      cancelController.current?.abort();
      const controller = new AbortController();
      cancelController.current = controller;
      await api.cancel(runId, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setCancelRequested(true);
    } catch {
      if (cancelController.current?.signal.aborted) return;
      cancelLock.current = false;
      setCancelling(false);
      setError('取消请求未被接受，回测可能仍在运行，请稍后重试。');
    }
  }

  const terminal = run !== null && terminalStatuses.has(run.status);
  return (
    <article className="backtest-run-page">
      <header className="page-heading">
        <div>
          <span className="eyebrow">BACKTEST / v0.4.0</span>
          <h2 data-page-heading tabIndex={-1}>
            回测运行
          </h2>
          <p>可离开本页继续使用工作台；任务和部分结果已持久化。</p>
        </div>
        <Link className="secondary-action" to="/backtests">
          返回回测工作台
        </Link>
      </header>
      {submissionNotice.map((notice) => (
        <p key={notice} className="workspace-notice" role="status">
          {notice}
        </p>
      ))}
      {error !== null ? (
        <p role="alert" className="backtest-error-summary">
          {error}
        </p>
      ) : null}
      {run === null && !permanentError ? (
        <p role="status">正在读取运行状态…</p>
      ) : run !== null ? (
        <>
          <RunProgress run={run} />
          {!terminal ? (
            <button
              type="button"
              className="danger-action"
              disabled={cancelling}
              onClick={() => void cancel()}
            >
              {cancelling ? '正在取消…' : '取消回测'}
            </button>
          ) : null}
          {cancelRequested || run.status === 'cancelled' ? (
            <p className="partial-result-note">
              取消不会删除已持久化的数据；已保留的部分结果仍可在报告中查看。
            </p>
          ) : null}
          {terminal ? (
            <section
              className="report-shell"
              aria-labelledby="report-shell-heading"
            >
              <h3 id="report-shell-heading">回测结果</h3>
              {supportsReports(api) ? (
                <BacktestReportPage api={api} runId={runId} />
              ) : (
                <p>
                  {run.status === 'succeeded'
                    ? '运行已完成，结论与交易明细可继续查看。'
                    : '运行已结束；报告将保留所有已持久化的部分结果、失败和日志。'}
                </p>
              )}
            </section>
          ) : null}
        </>
      ) : null}
      <RunLog logs={logs} logError={logError} onRetry={retryLogs} />
    </article>
  );
}
