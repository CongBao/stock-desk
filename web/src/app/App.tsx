import {
  lazy,
  memo,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';

import { MarketPage } from '../features/market/MarketPage';
import { BacktestRunPage } from '../features/backtests/BacktestRunPage';
import { BacktestWorkspacePage } from '../features/backtests/BacktestWorkspacePage';
import { DataSourcesPage } from '../features/settings/DataSourcesPage';
import { TaskCenterPage } from '../features/tasks/TaskCenterPage';
import { DesktopStartup } from '../features/desktop/DesktopStartup';
import { DesktopExitGuard } from '../features/desktop/DesktopExitGuard';
import { DesktopTaskRecovery } from '../features/desktop/DesktopTaskRecovery';
import { DesktopUpdateNotice } from '../features/desktop/DesktopUpdateNotice';
import { DESKTOP_BUILD_VERSION, displayDesktopVersion } from './buildIdentity';
import { OnboardingGate } from '../features/onboarding/OnboardingGate';
import { useOnboardingDemoMode } from '../features/onboarding/demoMode';
import { useMarketStore } from '../features/market/marketStore';
import { ContextualGuidance } from '../features/guidance/ContextualGuidance';
import type { OnboardingApi } from '../features/onboarding/onboardingApi';
import { useSystemStatus } from '../shared/api/useSystemStatus';
import type { WorkerState } from '../shared/api/useSystemStatus';
import { ContextPanel } from './ContextPanel';
import { AppIcon } from './AppIcon';
import { NotFoundPage } from './NotFoundPage';
import { RouteEffects } from './RouteEffects';
import { appRoutes } from './routes';
import { useWorkspaceStore } from './store';
import { WorkspaceStoreProvider } from './WorkspaceStoreProvider';
import { WorkspacePersistenceGate } from './WorkspacePersistenceGate';
import type { WorkspaceApi } from './workspaceApi';
import { createDesktopBridge, type DesktopBridge } from './desktopBridge';
import { createTauriAdapter } from './tauriAdapter';
import { ThemeSelector } from './ThemeProvider';

const tauriAdapter = createTauriAdapter();
const defaultDesktopBridge: DesktopBridge =
  tauriAdapter === undefined
    ? createDesktopBridge()
    : createDesktopBridge(tauriAdapter);

const FormulaStudioPage = lazy(async () => {
  const module = await import('../features/formulas/FormulaStudioPage');
  return { default: module.FormulaStudioPage };
});

const AnalysisPage = lazy(async () => {
  const module = await import('../features/analysis/AnalysisPage');
  return { default: module.AnalysisPage };
});

const systemStateLabels = {
  checking: '系统检查中',
  healthy: '系统正常',
  degraded: '服务降级',
  unavailable: '服务不可用',
} as const;

const workerStateLabels: Record<WorkerState, string> = {
  checking: 'Worker 检查中',
  running: 'Worker 运行中',
  not_detected: 'Worker 未检测',
  unavailable: 'Worker 状态不可用',
  api_offline: 'Worker：API 离线',
};

const productIdentity = {
  name: 'stock-desk',
  repository: 'https://github.com/CongBao/stock-desk',
} as const;

type NavigationRailProps = {
  readonly collapsed: boolean;
  readonly onToggle: () => void;
  readonly readonlyDemo: boolean;
  readonly productVersion: string;
};

function NavigationRail({
  collapsed,
  onToggle,
  readonlyDemo,
  productVersion,
}: NavigationRailProps) {
  return (
    <div className="navigation-rail">
      <div className="brand-lockup">
        <span className="brand-mark" aria-hidden="true">
          <AppIcon name="market" />
        </span>
        <div>
          {collapsed ? null : <h1>stock-desk</h1>}
          <p>个人 A 股工作台</p>
        </div>
      </div>

      <button
        className="navigation-toggle"
        type="button"
        aria-controls="primary-navigation"
        aria-expanded={!collapsed}
        aria-label={collapsed ? '展开主导航' : '收起主导航'}
        title={collapsed ? '展开主导航' : '收起主导航'}
        onClick={onToggle}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.8"
        >
          <path d={collapsed ? 'm9 5 7 7-7 7' : 'm15 5-7 7 7 7'} />
        </svg>
        <span className="navigation-toggle-label">
          {collapsed ? '展开导航' : '收起导航'}
        </span>
      </button>

      <nav
        id="primary-navigation"
        className="primary-navigation"
        aria-label="主导航"
      >
        <p className="nav-section-label">工作区</p>
        <ul>
          {appRoutes
            .filter((route) => !readonlyDemo || route.path === '/market')
            .map((route) => (
              <li key={route.path}>
                <NavLink
                  className="nav-link"
                  to={route.path}
                  title={route.label}
                >
                  <span className="nav-icon" aria-hidden="true">
                    <AppIcon name={route.icon} />
                  </span>
                  <span className="nav-label">{route.label}</span>
                </NavLink>
              </li>
            ))}
        </ul>
      </nav>

      <div className="rail-footer">
        <span className="version-label">{productVersion} · Task Center</span>
        <span>本地优先 · 个人使用</span>
      </div>
    </div>
  );
}

function AboutDialog({
  onClose,
  onExportDiagnostics,
  productVersion,
}: {
  readonly onClose: () => void;
  readonly onExportDiagnostics: () => Promise<
    'cancelled' | 'saved' | undefined
  >;
  readonly productVersion: string;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [diagnosticState, setDiagnosticState] = useState<
    'cancelled' | 'failed' | 'saving' | 'saved' | null
  >(null);

  useEffect(() => {
    closeRef.current?.focus();
    const containFocus = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      const first = focusable[0];
      const last = focusable.at(-1);
      if (first === undefined || last === undefined) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', containFocus);
    return () => window.removeEventListener('keydown', containFocus);
  }, [onClose]);

  async function exportDiagnostics() {
    if (diagnosticState === 'saving') return;
    setDiagnosticState('saving');
    try {
      const result = await onExportDiagnostics();
      setDiagnosticState(
        result === 'saved'
          ? 'saved'
          : result === 'cancelled'
            ? 'cancelled'
            : 'failed',
      );
    } catch {
      setDiagnosticState('failed');
    }
  }

  return (
    <div className="about-backdrop" role="presentation">
      <section
        ref={dialogRef}
        className="about-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="about-title"
      >
        <header>
          <div>
            <span className="panel-kicker">PRODUCT IDENTITY</span>
            <h2 id="about-title">关于 {productIdentity.name}</h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            aria-label="关闭关于信息"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <dl>
          <div>
            <dt>产品</dt>
            <dd>{productIdentity.name}</dd>
          </div>
          <div>
            <dt>版本</dt>
            <dd>{productVersion}</dd>
          </div>
          <div>
            <dt>公开仓库</dt>
            <dd>
              <a
                href={productIdentity.repository}
                target="_blank"
                rel="noreferrer"
              >
                github.com/CongBao/stock-desk
              </a>
            </dd>
          </div>
        </dl>
        <p>本地优先的个人 A 股分析工作台。</p>
        <div className="diagnostic-export-control">
          <button
            type="button"
            disabled={diagnosticState === 'saving'}
            onClick={() => void exportDiagnostics()}
          >
            {diagnosticState === 'saving' ? '正在准备诊断包…' : '导出诊断包'}
          </button>
          <p>
            诊断包仅保存到你选择的本机位置，不会自动上传，也不包含用户名、文件路径、会话凭证或原始日志。
          </p>
          {diagnosticState === null || diagnosticState === 'saving' ? null : (
            <p role="status">
              {diagnosticState === 'saved'
                ? '诊断包已保存到本机，未上传。'
                : diagnosticState === 'cancelled'
                  ? '已取消导出，没有写入文件。'
                  : '暂时无法导出。请确认使用最新 WebView2 后重试。'}
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

const WorkspaceRoutes = memo(function WorkspaceRoutes() {
  return (
    <>
      <RouteEffects />
      <Routes>
        <Route path="/" element={<Navigate to="/market" replace />} />
        {appRoutes.map((route) => (
          <Route
            key={route.path}
            path={route.path}
            element={
              route.path === '/market' ? (
                <MarketPage />
              ) : route.path === '/formulas' ? (
                <Suspense
                  fallback={
                    <p className="workspace-route-loading" role="status">
                      正在加载公式工作台…
                    </p>
                  }
                >
                  <FormulaStudioPage />
                </Suspense>
              ) : route.path === '/settings' ? (
                <DataSourcesPage />
              ) : route.path === '/backtests' ? (
                <BacktestWorkspacePage />
              ) : route.path === '/analysis' ? (
                <Suspense
                  fallback={
                    <p className="workspace-route-loading" role="status">
                      正在加载智能分析工作台…
                    </p>
                  }
                >
                  <AnalysisPage />
                </Suspense>
              ) : route.path === '/tasks' ? (
                <TaskCenterPage />
              ) : (
                <NotFoundPage />
              )
            }
          />
        ))}
        <Route path="/backtests/:runId" element={<BacktestRunPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </>
  );
});

function WorkspaceShell({
  desktopBridge,
}: {
  readonly desktopBridge: DesktopBridge;
}) {
  const location = useLocation();
  const readonlyDemo = useOnboardingDemoMode();
  const selectedInstrument = useMarketStore(
    (state) => state.selectedInstrument,
  );
  const period = useMarketStore((state) => state.period);
  const adjustment = useMarketStore((state) => state.adjustment);
  const zoom = useMarketStore((state) => state.zoom);
  const mainChart = useMarketStore((state) => state.mainChart);
  const subchart = useMarketStore((state) => state.subchart);
  const isContextOpen = useWorkspaceStore((state) => state.isContextOpen);
  const openContext = useWorkspaceStore((state) => state.openContext);
  const closeContext = useWorkspaceStore((state) => state.closeContext);
  const contextToggleRef = useRef<HTMLButtonElement>(null);
  const aboutToggleRef = useRef<HTMLButtonElement>(null);
  const systemStatus = useSystemStatus();
  const [isAboutOpen, setIsAboutOpen] = useState(false);
  const [productVersion, setProductVersion] = useState(() =>
    displayDesktopVersion(DESKTOP_BUILD_VERSION),
  );
  const [isNavigationCollapsed, setIsNavigationCollapsed] = useState(() =>
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 1200px)').matches
      : false,
  );
  const closeAbout = useCallback(() => {
    setIsAboutOpen(false);
    window.setTimeout(() => aboutToggleRef.current?.focus(), 0);
  }, []);
  const workspaceKicker =
    location.pathname === '/tasks'
      ? 'STOCK DESK / TASK CENTER'
      : 'STOCK DESK / MARKET DATA';

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const tabletQuery = window.matchMedia('(max-width: 1200px)');
    const followViewport = (event: MediaQueryListEvent) => {
      setIsNavigationCollapsed(event.matches);
    };
    tabletQuery.addEventListener('change', followViewport);
    return () => tabletQuery.removeEventListener('change', followViewport);
  }, []);

  useEffect(() => {
    let active = true;
    const state = desktopBridge.getUpdateState();
    if (state instanceof Promise) {
      void state
        .then((update) => {
          if (active)
            setProductVersion(displayDesktopVersion(update.currentVersion));
        })
        .catch(() => {
          if (active) setProductVersion(displayDesktopVersion(null));
        });
    } else {
      setProductVersion(displayDesktopVersion(state.currentVersion));
    }
    return () => {
      active = false;
    };
  }, [desktopBridge]);

  function closeContextPanel() {
    closeContext();
    contextToggleRef.current?.focus();
  }

  return (
    <>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <div
        className="app-shell"
        data-navigation-collapsed={isNavigationCollapsed}
        data-workspace-symbol={selectedInstrument?.symbol ?? ''}
        data-workspace-period={period}
        data-workspace-adjustment={adjustment}
        data-workspace-zoom-start={zoom.start}
        data-workspace-zoom-end={zoom.end}
        data-workspace-main-chart={mainChart}
        data-workspace-subchart={subchart.kind}
        data-workspace={
          location.pathname === '/formulas'
            ? 'formulas'
            : location.pathname.startsWith('/backtests')
              ? 'backtests'
              : location.pathname === '/analysis'
                ? 'analysis'
                : location.pathname === '/tasks'
                  ? 'tasks'
                  : 'default'
        }
      >
        <div className="desktop-update-slot">
          <DesktopUpdateNotice bridge={desktopBridge} />
        </div>
        <NavigationRail
          collapsed={isNavigationCollapsed}
          onToggle={() => setIsNavigationCollapsed((collapsed) => !collapsed)}
          readonlyDemo={readonlyDemo}
          productVersion={productVersion}
        />

        <main id="main-content" className="workspace" tabIndex={-1}>
          <header className="workspace-topbar">
            <div>
              {isNavigationCollapsed ? (
                <h1 className="topbar-product-name">stock-desk</h1>
              ) : null}
              <span className="topbar-kicker">{workspaceKicker}</span>
              <span
                className="topbar-state"
                data-state={systemStatus.overall}
                aria-live="polite"
              >
                <span className="status-symbol" aria-hidden="true" />
                <span>{systemStateLabels[systemStatus.overall]}</span>
                <span className="status-scope">已检测：API / 任务存储</span>
                <span className="worker-scope" data-state={systemStatus.worker}>
                  {workerStateLabels[systemStatus.worker]}
                </span>
              </span>
            </div>
            <div className="topbar-actions">
              <ContextualGuidance />
              <button
                ref={aboutToggleRef}
                className="about-toggle"
                type="button"
                aria-haspopup="dialog"
                aria-expanded={isAboutOpen}
                aria-label="关于 stock-desk"
                onClick={() => setIsAboutOpen(true)}
              >
                <span aria-hidden="true">i</span>
                关于
              </button>
              <button
                ref={contextToggleRef}
                className="context-toggle"
                type="button"
                aria-controls="context-panel"
                aria-expanded={isContextOpen}
                aria-label={isContextOpen ? '隐藏上下文面板' : '打开上下文面板'}
                onClick={isContextOpen ? closeContextPanel : openContext}
              >
                <span aria-hidden="true">◫</span>
                状态面板
              </button>
            </div>
          </header>

          <WorkspaceRoutes />
        </main>

        <ContextPanel
          isOpen={isContextOpen}
          onClose={closeContextPanel}
          systemStatus={systemStatus}
        />
      </div>
      {isAboutOpen ? (
        <AboutDialog
          onClose={closeAbout}
          productVersion={productVersion}
          onExportDiagnostics={async () => {
            const result = await desktopBridge.exportDiagnostics();
            return result === 'saved' || result === 'cancelled'
              ? result
              : undefined;
          }}
        />
      ) : null}
    </>
  );
}

export function App({
  desktopBridge = defaultDesktopBridge,
  onboardingApi,
  workspaceApi,
}: {
  readonly desktopBridge?: DesktopBridge;
  readonly onboardingApi?: OnboardingApi | null;
  readonly workspaceApi?: WorkspaceApi;
}) {
  const shell = (
    <WorkspaceStoreProvider>
      <WorkspaceShell desktopBridge={desktopBridge} />
    </WorkspaceStoreProvider>
  );
  const workspace =
    onboardingApi === null ? (
      shell
    ) : (
      <WorkspacePersistenceGate api={workspaceApi}>
        {shell}
      </WorkspacePersistenceGate>
    );
  return (
    <>
      <div className="global-theme-control">
        <ThemeSelector />
      </div>
      <DesktopExitGuard bridge={desktopBridge}>
        <DesktopStartup bridge={desktopBridge}>
          <DesktopTaskRecovery bridge={desktopBridge}>
            {onboardingApi === null ? (
              workspace
            ) : (
              <OnboardingGate
                api={onboardingApi}
                onDiagnostics={() => void desktopBridge.exportDiagnostics()}
              >
                {workspace}
              </OnboardingGate>
            )}
          </DesktopTaskRecovery>
        </DesktopStartup>
      </DesktopExitGuard>
    </>
  );
}
