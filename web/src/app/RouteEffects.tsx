import { useEffect, useRef } from 'react';
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
  const isMarket =
    location.pathname.replace(/\/+$/u, '').toLowerCase() === '/market';
  const hasFocusedMarketSearch = useRef(false);

  useEffect(() => {
    document.title = `${pageTitle} · stock-desk`;
    window.scrollTo({ behavior: 'auto', left: 0, top: 0 });

    const focusTimer = window.setTimeout(() => {
      const focusMarketSearch = isMarket && !hasFocusedMarketSearch.current;
      if (focusMarketSearch) hasFocusedMarketSearch.current = true;
      document
        .querySelector<HTMLElement>(
          focusMarketSearch
            ? '[data-route-primary-focus]'
            : '[data-page-heading]',
        )
        ?.focus();
    }, 0);

    return () => window.clearTimeout(focusTimer);
  }, [isMarket, location.key, pageTitle]);

  return (
    <p className="visually-hidden" role="status" aria-live="polite">
      已进入：{pageTitle}
    </p>
  );
}
