import { useRef } from 'react';
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
} from 'react-router-dom';

import { ContextPanel } from './ContextPanel';
import { MarketPage } from './MarketPage';
import { PlannedPage } from './PlannedPage';
import { appRoutes } from './routes';
import { useWorkspaceStore } from './store';

function NavigationRail() {
  return (
    <aside className="navigation-rail">
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
        <span className="version-label">v0.1.0 · Foundation</span>
        <span>本地优先 · 个人使用</span>
      </div>
    </aside>
  );
}

function WorkspaceShell() {
  const isContextOpen = useWorkspaceStore((state) => state.isContextOpen);
  const openContext = useWorkspaceStore((state) => state.openContext);
  const closeContext = useWorkspaceStore((state) => state.closeContext);
  const contextToggleRef = useRef<HTMLButtonElement>(null);

  function closeContextPanel() {
    closeContext();
    contextToggleRef.current?.focus();
  }

  return (
    <>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <div className="app-shell">
        <NavigationRail />

        <main id="main-content" className="workspace" tabIndex={-1}>
          <header className="workspace-topbar">
            <div>
              <span className="topbar-kicker">STOCK DESK / FOUNDATION</span>
              <span className="topbar-state">
                <span className="status-symbol" aria-hidden="true" />
                本地服务基础
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

          <Routes>
            <Route path="/" element={<Navigate to="/market" replace />} />
            {appRoutes.map((route) => (
              <Route
                key={route.path}
                path={route.path}
                element={
                  route.path === '/market' ? (
                    <MarketPage />
                  ) : (
                    <PlannedPage route={route} />
                  )
                }
              />
            ))}
            <Route path="*" element={<Navigate to="/market" replace />} />
          </Routes>
        </main>

        <ContextPanel isOpen={isContextOpen} onClose={closeContextPanel} />
      </div>
    </>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <WorkspaceShell />
    </BrowserRouter>
  );
}
