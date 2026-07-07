import type { ReactNode } from 'react';

export type AppIconName =
  'analysis' | 'backtests' | 'formulas' | 'market' | 'settings' | 'tasks';

type AppIconProps = {
  readonly name: AppIconName;
};

export function AppIcon({ name }: AppIconProps) {
  const paths = {
    market: (
      <>
        <path d="M4 18V6" />
        <path d="M4 18h16" />
        <path d="m6.5 14 3.2-3.5 3.1 2.4L18 7" />
      </>
    ),
    formulas: (
      <>
        <path d="M15.5 5.5c-3.5 0-4 2.5-4.8 6.5L9.5 18" />
        <path d="M7 10h7" />
        <path d="m15.5 13 4 5" />
        <path d="m19.5 13-4 5" />
      </>
    ),
    backtests: (
      <>
        <path d="M5 19V5" />
        <path d="M5 19h14" />
        <path d="m7.5 15 3-4 3 2 4-6" />
        <path d="M15 7h2.5v2.5" />
      </>
    ),
    analysis: (
      <>
        <path d="M12 3v3" />
        <path d="M12 18v3" />
        <path d="M3 12h3" />
        <path d="M18 12h3" />
        <path d="m6 6 2.1 2.1" />
        <path d="m15.9 15.9 2.1 2.1" />
        <circle cx="12" cy="12" r="3.5" />
      </>
    ),
    tasks: (
      <>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 7v5l3 2" />
        <path d="M8 3.8 6.5 2.5" />
        <path d="m16 3.8 1.5-1.3" />
      </>
    ),
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M12 3v2" />
        <path d="M12 19v2" />
        <path d="m5.6 5.6 1.5 1.5" />
        <path d="m16.9 16.9 1.5 1.5" />
        <path d="M3 12h2" />
        <path d="M19 12h2" />
        <path d="m5.6 18.4 1.5-1.5" />
        <path d="m16.9 7.1 1.5-1.5" />
      </>
    ),
  } satisfies Record<AppIconName, ReactNode>;

  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
    >
      {paths[name]}
    </svg>
  );
}
