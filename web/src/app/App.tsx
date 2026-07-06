import { lazy, Suspense, useRef } from 'react';
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
import { useSystemStatus } from '../shared/api/useSystemStatus';
import { ContextPanel } from './ContextPanel';
import { NotFoundPage } from './NotFoundPage';
import { PlannedPage } from './PlannedPage';
import { RouteEffects } from './RouteEffects';
import { appRoutes } from './routes';
import { useWorkspaceStore } from './store';
import { WorkspaceStoreProvider } from './WorkspaceStoreProvider';

const FormulaStudioPage = lazy(async () => {
  const module = await import('../features/formulas/FormulaStudioPage');
  return { default: module.FormulaStudioPage };
});

const systemStateLabels = {
  checking: '系统检查中',
  healthy: '系统正常',
  degraded: '服务降级',
  unavailable: '服务不可用',
} as const;

function NavigationRail() {
  return (
    <div className="navigation-rail">
      <div className="brand-lockup">
        <span className="brand-mark" aria-hidden="true">
          SD
        </span>
        <div>
          <h1>stock-desk</h1>
          <p>个人 A 股工作台</p>
        </div>
      </div>

      <nav className="primary-navigation" aria-label="主导航">
        <p className="nav-section-label">工作区</p>
        <ul>
          {appRoutes.map((route) => (
            <li key={route.path}>
              <NavLink className="nav-link" to={route.path}>
                <span className="nav-icon" aria-hidden="true">
                  {route.icon}
                </span>
                <span>{route.label}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <div className="rail-footer">
        <span className="version-label">v0.4.0 · Strategy Backtest</span>
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
        data-workspace={
          location.pathname === '/formulas'
            ? 'formulas'
            : location.pathname.startsWith('/backtests')
              ? 'backtests'
              : 'default'
        }
      >
        <NavigationRail />

        <main id="main-content" className="workspace" tabIndex={-1}>
          <header className="workspace-topbar">
            <div>
              <span className="topbar-kicker">STOCK DESK / MARKET DATA</span>
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
                  ) : (
                    <PlannedPage route={route} />
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
