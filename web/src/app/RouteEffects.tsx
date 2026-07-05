import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';

import { appRoutes } from './routes';

function getPageTitle(pathname: string): string {
  if (pathname === '/') {
    return appRoutes[0].title;
  }

  return (
    appRoutes.find((route) => route.path === pathname)?.title ?? '页面未找到'
  );
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
