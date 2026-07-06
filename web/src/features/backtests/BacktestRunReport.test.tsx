import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { BacktestApi, BacktestReportApi } from './backtestApi';
import { BacktestRunPage } from './BacktestRunPage';

const reportPage = vi.hoisted(() => vi.fn());
vi.mock('./BacktestReportPage', () => ({
  BacktestReportPage: (props: unknown) => {
    reportPage(props);
    return <section>完整结论报告</section>;
  },
}));

it('embeds the conclusion report on the existing terminal run route', async () => {
  const runId = '11111111-1111-1111-1111-111111111111';
  const overview = {
    createdAt: '2026-07-07T00:00:00Z',
    failed: 0,
    finishedAt: '2026-07-07T00:00:03Z',
    processed: 1,
    progress: 1,
    resultHash: `sha256:${'c'.repeat(64)}`,
    runId,
    snapshotId: `sha256:${'a'.repeat(64)}`,
    stage: 'completed',
    startedAt: '2026-07-07T00:00:01Z',
    status: 'succeeded',
    taskId: '22222222-2222-2222-2222-222222222222',
    total: 1,
    updatedAt: '2026-07-07T00:00:03Z',
  } as const;
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getRun: vi.fn().mockResolvedValue(overview),
    getLogs: vi
      .fn()
      .mockResolvedValue({ afterCursor: null, items: [], nextCursor: null }),
    listRuns: vi.fn(),
    preflight: vi.fn(),
    getFailures: vi.fn(),
    getGroups: vi.fn(),
    getReport: vi.fn(),
    getReplay: vi.fn(),
    getReportLogs: vi.fn(),
    getTrades: vi.fn(),
  } satisfies BacktestApi & BacktestReportApi;
  render(
    <MemoryRouter initialEntries={[`/backtests/${runId}`]}>
      <Routes>
        <Route
          path="/backtests/:runId"
          element={<BacktestRunPage api={api} />}
        />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText('完整结论报告')).toBeVisible();
  expect(reportPage).toHaveBeenCalledWith(
    expect.objectContaining({ api, runId }),
  );
});
