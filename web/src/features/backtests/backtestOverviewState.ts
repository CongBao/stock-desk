import type { BacktestOverview } from './backtestApi';

export function coalesceBacktestOverview(
  current: BacktestOverview | null,
  next: BacktestOverview,
): BacktestOverview {
  if (current === null) return next;
  const currentKeys = Object.keys(current) as (keyof BacktestOverview)[];
  const nextKeys = Object.keys(next) as (keyof BacktestOverview)[];
  if (currentKeys.length !== nextKeys.length) return next;
  return currentKeys.every((key) => current[key] === next[key])
    ? current
    : next;
}
