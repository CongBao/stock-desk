import { useEffect } from 'react';
import { matchPath, useLocation } from 'react-router-dom';

import { appRoutes } from './routes';

function getPageTitle(pathname: string): string {
  if (matchPath({ end: true, path: '/' }, pathname)) {
    return appRoutes[0].title;
  }

  if (
    matchPath(
      { caseSensitive: false, end: true, path: '/backtests/:runId' },
      pathname,
    )
  ) {
    return (
      appRoutes.find((route) => route.path === '/backtests')?.title ??
      '策略回测'
    );
  }

  const matchedRoute = appRoutes.find((route) =>
    matchPath({ caseSensitive: false, end: true, path: route.path }, pathname),
  );

  return matchedRoute?.title ?? '页面未找到';
}

export function RouteEffects() {
  const location = useLocation();
  const pageTitle = getPageTitle(location.pathname);

  useEffect(() => {
    document.title = `${pageTitle} · stock-desk`;
    window.scrollTo({ behavior: 'auto', left: 0, top: 0 });

    const focusTimer = window.setTimeout(() => {
      document.querySelector<HTMLElement>('[data-page-heading]')?.focus();
    }, 0);

    return () => window.clearTimeout(focusTimer);
  }, [location.key, pageTitle]);

  return (
    <p className="visually-hidden" role="status" aria-live="polite">
      已进入：{pageTitle}
    </p>
  );
}
