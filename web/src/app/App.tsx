import { lazy, Suspense, useEffect, useRef, useState } from 'react';
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
import { useSystemStatus } from '../shared/api/useSystemStatus';
import { ContextPanel } from './ContextPanel';
import { AppIcon } from './AppIcon';
import { NotFoundPage } from './NotFoundPage';
import { RouteEffects } from './RouteEffects';
import { appRoutes } from './routes';
import { useWorkspaceStore } from './store';
import { WorkspaceStoreProvider } from './WorkspaceStoreProvider';

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

type NavigationRailProps = {
  readonly collapsed: boolean;
  readonly onToggle: () => void;
};

function NavigationRail({ collapsed, onToggle }: NavigationRailProps) {
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
          {appRoutes.map((route) => (
            <li key={route.path}>
              <NavLink className="nav-link" to={route.path} title={route.label}>
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
        <span className="version-label">v1.0.0 · Task Center</span>
        <span>本地优先 · 个人使用</span>
      </div>
    </div>
  );
}

function WorkspaceShell() {
  const location = useLocation();
  const isContextOpen = useWorkspaceStore((state) => state.isContextOpen);
  const openContext = useWorkspaceStore((state) => state.openContext);
  const closeContext = useWorkspaceStore((state) => state.closeContext);
  const contextToggleRef = useRef<HTMLButtonElement>(null);
  const systemStatus = useSystemStatus();
  const [isNavigationCollapsed, setIsNavigationCollapsed] = useState(() =>
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 1200px)').matches
      : false,
  );
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
        <NavigationRail
          collapsed={isNavigationCollapsed}
          onToggle={() => setIsNavigationCollapsed((collapsed) => !collapsed)}
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
                <span className="worker-scope">Worker 未检测</span>
              </span>
            </div>
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
          </header>

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
        </main>

        <ContextPanel
          isOpen={isContextOpen}
          onClose={closeContextPanel}
          systemStatus={systemStatus}
        />
      </div>
    </>
  );
}

export function App() {
  return (
    <WorkspaceStoreProvider>
      <WorkspaceShell />
    </WorkspaceStoreProvider>
  );
}
