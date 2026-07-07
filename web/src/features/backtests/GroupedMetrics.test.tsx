import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { BacktestReportApi } from './backtestApi';
import { GroupedMetrics } from './GroupedMetrics';

it('keeps one bounded group page, switches dimensions, and repeats the disclaimer', async () => {
  const user = userEvent.setup();
  const getGroups = vi.fn().mockResolvedValue({
    items: [
      {
        averageHoldingDays: '4',
        dimension: 'symbol',
        key: '600000.SH',
        meanNetReturn: '0.01',
        medianNetReturn: '0.01',
        negativeCount: 1,
        netPnlTotal: '20',
        payoffRatio: '2',
        positiveCount: 1,
        realizedCount: 2,
        realizedDenominator: 2,
        shareOfAll: '1',
        winRate: '0.5',
        zeroCount: 0,
      },
    ],
    nextCursor: 'next-page',
  });
  const api = { getGroups } as unknown as BacktestReportApi;
  render(
    <GroupedMetrics
      api={api}
      disclaimer="independent trade samples, not portfolio return"
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    />,
  );

  expect(await screen.findByText('600000.SH')).toBeVisible();
  expect(
    screen.getByText('independent trade samples, not portfolio return'),
  ).toBeVisible();
  await user.click(screen.getByRole('button', { name: '下一页' }));
  expect(getGroups).toHaveBeenLastCalledWith(
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'symbol',
    expect.objectContaining({ cursor: 'next-page' }),
  );
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '上一页' })).toBeEnabled(),
  );
  await user.click(screen.getByRole('button', { name: '上一页' }));
  expect(getGroups).toHaveBeenLastCalledWith(
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'symbol',
    expect.objectContaining({ cursor: null }),
  );
  await user.click(screen.getByRole('radio', { name: '按月' }));
  expect(getGroups).toHaveBeenLastCalledWith(
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'entry_month',
    expect.objectContaining({ cursor: null }),
  );
});

it('renders independent empty and error states', async () => {
  const emptyApi = {
    getGroups: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
  } as unknown as BacktestReportApi;
  const mounted = render(
    <GroupedMetrics
      api={emptyApi}
      disclaimer="independent trade samples, not portfolio return"
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    />,
  );
  expect(await screen.findByText('该维度没有已实现样本。')).toBeVisible();

  const errorApi = {
    getGroups: vi.fn().mockRejectedValue(new Error('failed')),
  } as unknown as BacktestReportApi;
  mounted.rerender(
    <GroupedMetrics
      api={errorApi}
      disclaimer="independent trade samples, not portfolio return"
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    />,
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '分组数据读取失败',
  );
});
