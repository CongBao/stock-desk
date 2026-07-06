import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { BacktestTrade } from './backtestApi';
import { TradeTable } from './TradeTable';

const trade = {
  buyCommission: '5',
  entryFillAt: '2025-01-02T01:30:00Z',
  entrySignalAt: '2025-01-01T00:00:00Z',
  exitFillAt: '2025-01-04T01:30:00Z',
  exitSignalAt: '2025-01-03T00:00:00Z',
  fillGrossPnl: '23.5',
  floatingPnl: null,
  floatingReturn: null,
  holdingBars: 2,
  holdingDays: 2,
  investedCost: '10010',
  markAt: null,
  netPnl: '12.5',
  netReturn: '0.00125',
  orderEvents: [],
  ordinal: 0,
  quantity: 1000,
  realized: true,
  referenceGrossPnl: '25.5',
  sellCommission: '5',
  sellTax: '1',
  slippageCost: '2',
  symbol: '600000.SH',
} as unknown as BacktestTrade;

it('shows each persisted gross-to-net and cost value without client aggregation', async () => {
  const user = userEvent.setup();
  render(<TradeTable items={[trade]} />);

  await user.click(screen.getByText('成本与盈亏桥接'));
  expect(
    screen.getByText('参考口径毛盈亏').nextElementSibling,
  ).toHaveTextContent('25.5');
  expect(
    screen.getByText('成交口径毛盈亏').nextElementSibling,
  ).toHaveTextContent('23.5');
  expect(screen.getByText('买入佣金').nextElementSibling).toHaveTextContent(
    '5',
  );
  expect(screen.getByText('卖出佣金').nextElementSibling).toHaveTextContent(
    '5',
  );
  expect(screen.getByText('卖出印花税').nextElementSibling).toHaveTextContent(
    '1',
  );
  expect(screen.getByText('滑点成本').nextElementSibling).toHaveTextContent(
    '2',
  );
  expect(screen.getByText('投入成本').nextElementSibling).toHaveTextContent(
    '10010',
  );
  expect(screen.getByText('净盈亏').nextElementSibling).toHaveTextContent(
    '12.5',
  );
  expect(screen.getByText('净收益率').nextElementSibling).toHaveTextContent(
    '0.00125',
  );
});

it('sorts only the current page by every supported key and opens the selected replay', async () => {
  const user = userEvent.setup();
  const second: BacktestTrade = {
    ...trade,
    entryFillAt: '2025-01-03T01:30:00Z',
    netReturn: '0.02',
    ordinal: 1,
    symbol: '000001.SZ',
  };
  const onReplay = vi.fn();
  render(<TradeTable items={[trade, second]} onReplay={onReplay} />);

  const body = screen.getAllByRole('rowgroup')[1];
  expect(within(body).getAllByRole('row')[0]).toHaveTextContent('000001.SZ');
  await user.selectOptions(screen.getByRole('combobox'), 'entryFillAt');
  expect(within(body).getAllByRole('row')[0]).toHaveTextContent('600000.SH');
  await user.selectOptions(screen.getByRole('combobox'), 'result');
  expect(within(body).getAllByRole('row')[0]).toHaveTextContent('000001.SZ');
  await user.click(screen.getAllByRole('button', { name: '固定回放' })[0]);
  expect(onReplay).toHaveBeenCalledWith(second);
});

it('renders the empty page and preserves open-trade null semantics', async () => {
  const user = userEvent.setup();
  const mounted = render(<TradeTable items={[]} />);
  expect(screen.getByText('当前页没有记录。')).toBeVisible();

  mounted.rerender(
    <TradeTable
      items={[
        {
          ...trade,
          exitFillAt: null,
          exitSignalAt: null,
          floatingPnl: '8',
          floatingReturn: '0.008',
          markAt: '2025-01-05T00:00:00Z',
          netPnl: null,
          netReturn: null,
          realized: false,
        },
      ]}
    />,
  );
  expect(screen.getAllByText('0.008')).toHaveLength(2);
  await user.click(screen.getByText('成本与盈亏桥接'));
  expect(screen.getAllByText('开放仓位（未实现）')).toHaveLength(2);
});
