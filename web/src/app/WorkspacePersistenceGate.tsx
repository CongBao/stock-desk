import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useOnboardingDemoMode } from '../features/onboarding/demoMode';
import { useMarketStore } from '../features/market/marketStore';
import { ApiError } from '../shared/api/client';
import {
  workspaceApi,
  type WorkspaceApi,
  type WorkspaceInstrument,
  type WorkspaceNotice,
  type WorkspaceRoute,
  type WorkspaceValue,
} from './workspaceApi';

const SAVE_DELAY_MS = 300;
const DEFAULT_WORKSPACE: WorkspaceValue = {
  currentPage: '/market',
  instrument: {
    symbol: '000001.SS',
    name: '上证指数',
    exchange: 'SH',
    instrumentKind: 'index',
  },
  period: '1d',
  adjustment: 'none',
  zoom: { start: 0, end: 100 },
  mainChart: 'candlestick',
  subchart: { kind: 'volume' },
};

const NOTICE_MESSAGES: Record<WorkspaceNotice, string> = {
  workspace_missing: '未找到上次工作区，已安全打开默认行情。',
  workspace_corrupt: '上次工作区已损坏，已安全打开默认行情。',
  workspace_schema_unsupported: '上次工作区版本不兼容，已安全打开默认行情。',
  workspace_expired: '上次工作区已过期，已安全打开默认行情。',
  workspace_route_invalid: '上次页面无效，已安全打开默认行情。',
  workspace_instrument_unavailable: '上次证券已不可用，已安全打开上证指数。',
  workspace_chart_unavailable: '上次副图已不可用，已安全恢复默认图表。',
};

function stable(value: unknown): string {
  return JSON.stringify(value);
}

function same(left: unknown, right: unknown): boolean {
  return stable(left) === stable(right);
}

function mergeWorkspaceChanges(
  base: WorkspaceValue,
  local: WorkspaceValue,
  remote: WorkspaceValue,
): WorkspaceValue {
  return {
    currentPage: same(local.currentPage, base.currentPage)
      ? remote.currentPage
      : local.currentPage,
    instrument: same(local.instrument, base.instrument)
      ? remote.instrument
      : local.instrument,
    period: same(local.period, base.period) ? remote.period : local.period,
    adjustment: same(local.adjustment, base.adjustment)
      ? remote.adjustment
      : local.adjustment,
    zoom: same(local.zoom, base.zoom) ? remote.zoom : local.zoom,
    mainChart: same(local.mainChart, base.mainChart)
      ? remote.mainChart
      : local.mainChart,
    subchart: same(local.subchart, base.subchart)
      ? remote.subchart
      : local.subchart,
  };
}

function routeFromPath(pathname: string): WorkspaceRoute | null {
  const normalized = pathname.replace(/\/+$/u, '') || '/';
  if (normalized.startsWith('/backtests/')) return '/backtests';
  return [
    '/market',
    '/formulas',
    '/backtests',
    '/analysis',
    '/tasks',
    '/settings',
  ].includes(normalized)
    ? (normalized as WorkspaceRoute)
    : null;
}

function normalizedInstrument(value: {
  readonly symbol: string;
  readonly name: string;
  readonly exchange?: 'SH' | 'SZ' | 'BJ';
  readonly instrumentKind?: 'stock' | 'index' | 'etf' | 'fund' | 'bond';
}): WorkspaceInstrument | null {
  const suffix = value.symbol.slice(-2);
  const exchange =
    value.exchange ??
    (suffix === 'SH' || suffix === 'SS'
      ? 'SH'
      : suffix === 'SZ'
        ? 'SZ'
        : suffix === 'BJ'
          ? 'BJ'
          : null);
  if (exchange === null) return null;
  return {
    symbol: value.symbol,
    name: value.name,
    exchange,
    instrumentKind:
      value.instrumentKind ??
      (value.symbol === '000001.SS' ? 'index' : 'stock'),
  };
}

export function WorkspacePersistenceGate({
  api = workspaceApi,
  children,
}: {
  readonly api?: WorkspaceApi;
  readonly children: ReactNode;
}) {
  const demoMode = useOnboardingDemoMode();
  const location = useLocation();
  const navigate = useNavigate();
  const selectedInstrument = useMarketStore(
    (state) => state.selectedInstrument,
  );
  const period = useMarketStore((state) => state.period);
  const adjustment = useMarketStore((state) => state.adjustment);
  const zoom = useMarketStore((state) => state.zoom);
  const mainChart = useMarketStore((state) => state.mainChart);
  const subchart = useMarketStore((state) => state.subchart);
  const restoreMarket = useMarketStore((state) => state.restoreWorkspace);
  const [ready, setReady] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const baselineRef = useRef<WorkspaceValue | null>(null);
  const revisionRef = useRef(0);
  const requestRef = useRef(0);

  const applyWorkspace = useCallback(
    (workspace: WorkspaceValue) => {
      restoreMarket({
        adjustment: workspace.adjustment,
        instrument: workspace.instrument,
        mainChart: workspace.mainChart,
        period: workspace.period,
        subchart: workspace.subchart,
        zoom: workspace.zoom,
      });
      const preservesBacktestEntry =
        location.pathname.startsWith('/backtests/') ||
        (location.pathname === '/backtests' && location.search.length > 0);
      if (!preservesBacktestEntry) {
        void navigate(workspace.currentPage, { replace: true });
      }
    },
    [location.pathname, location.search, navigate, restoreMarket],
  );
  const applyWorkspaceRef = useRef(applyWorkspace);
  applyWorkspaceRef.current = applyWorkspace;

  useEffect(() => {
    if (demoMode) {
      setReady(true);
      return undefined;
    }
    const controller = new AbortController();
    const request = requestRef.current + 1;
    requestRef.current = request;
    setReady(false);
    void api
      .get({ signal: controller.signal })
      .then((restored) => {
        if (requestRef.current !== request || controller.signal.aborted) return;
        revisionRef.current = restored.revision;
        baselineRef.current = restored.workspace;
        applyWorkspaceRef.current(restored.workspace);
        setMessage(
          restored.notice === null ? null : NOTICE_MESSAGES[restored.notice],
        );
      })
      .catch(() => {
        if (requestRef.current !== request || controller.signal.aborted) return;
        revisionRef.current = 0;
        baselineRef.current = DEFAULT_WORKSPACE;
        applyWorkspaceRef.current(DEFAULT_WORKSPACE);
        setMessage('工作区恢复暂不可用，已安全打开默认行情。');
      })
      .finally(() => {
        if (requestRef.current === request && !controller.signal.aborted) {
          setReady(true);
        }
      });
    return () => controller.abort();
  }, [api, demoMode]);

  const localWorkspace = useMemo<WorkspaceValue | null>(() => {
    const currentPage = routeFromPath(location.pathname);
    const instrument =
      selectedInstrument === null
        ? null
        : normalizedInstrument(selectedInstrument);
    if (currentPage === null || instrument === null) return null;
    return {
      currentPage,
      instrument,
      period,
      adjustment,
      zoom,
      mainChart,
      subchart,
    };
  }, [
    adjustment,
    location.pathname,
    mainChart,
    period,
    selectedInstrument,
    subchart,
    zoom,
  ]);

  useEffect(() => {
    const baseline = baselineRef.current;
    if (
      demoMode ||
      !ready ||
      baseline === null ||
      localWorkspace === null ||
      same(localWorkspace, baseline)
    ) {
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const candidate = localWorkspace;
      const expectedRevision = revisionRef.current;
      void api
        .put(
          { expectedRevision, workspace: candidate },
          { signal: controller.signal },
        )
        .then((saved) => {
          if (controller.signal.aborted) return;
          revisionRef.current = saved.revision;
          baselineRef.current = saved.workspace;
          setMessage(
            saved.notice === null ? null : NOTICE_MESSAGES[saved.notice],
          );
        })
        .catch(async (error: unknown) => {
          if (controller.signal.aborted) return;
          if (!(error instanceof ApiError) || error.status !== 409) {
            setMessage('工作区暂未保存；当前操作仍可继续，请稍后重试。');
            return;
          }
          try {
            const latest = await api.get({ signal: controller.signal });
            if (controller.signal.aborted) return;
            const merged = mergeWorkspaceChanges(
              baseline,
              candidate,
              latest.workspace,
            );
            const saved = await api.put(
              { expectedRevision: latest.revision, workspace: merged },
              { signal: controller.signal },
            );
            if (controller.signal.aborted) return;
            revisionRef.current = saved.revision;
            baselineRef.current = saved.workspace;
            applyWorkspaceRef.current(saved.workspace);
            setMessage('工作区在其他窗口发生变化，已安全合并并保存。');
          } catch {
            if (!controller.signal.aborted) {
              setMessage('工作区发生并发变化，尚未覆盖其他窗口；请重试。');
            }
          }
        });
    }, SAVE_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, demoMode, localWorkspace, ready]);

  if (demoMode) return children;
  if (!ready) {
    return (
      <main className="workspace-restore-shell">
        <p role="status">正在恢复工作区…</p>
      </main>
    );
  }
  return (
    <>
      {message === null ? null : (
        <div
          className="workspace-restore-notice"
          role="status"
          aria-live="polite"
        >
          {message}
        </div>
      )}
      {children}
    </>
  );
}
