const BACKTEST_POLL_DELAYS = [500, 1000, 2000, 5000] as const;

export function backtestPollDelay(
  attempt: number,
  schedule: readonly number[] = BACKTEST_POLL_DELAYS,
) {
  return schedule[Math.min(attempt, schedule.length - 1)] ?? 5000;
}

export const backtestPollDelays = BACKTEST_POLL_DELAYS;
