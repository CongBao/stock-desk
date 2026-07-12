import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MarketInstrumentRail } from './MarketInstrumentRail';

const shanghai = {
  symbol: '000001.SS',
  name: '上证指数',
  instrumentKind: 'index',
} as const;
const pufa = {
  symbol: '600000.SH',
  name: '浦发银行',
  instrumentKind: 'stock',
} as const;

it('turns an empty watchlist into an actionable Shanghai Composite starting point', async () => {
  const user = userEvent.setup();
  const onAdd = vi.fn();
  const onSelect = vi.fn();
  render(
    <MarketInstrumentRail
      collapsed={false}
      onAdd={onAdd}
      onRemove={vi.fn()}
      onSelect={onSelect}
      onToggle={vi.fn()}
      recent={[]}
      selectedSymbol={null}
      watchlist={[]}
    />,
  );

  expect(screen.getByText('还没有自选股')).toBeInTheDocument();
  await user.click(
    screen.getByRole('button', { name: '查看上证指数 000001.SS' }),
  );
  expect(onSelect).toHaveBeenCalledWith(shanghai);
  await user.click(screen.getByRole('button', { name: '添加第一只自选' }));
  expect(onAdd).toHaveBeenCalledWith(shanghai);
});

it('keeps watchlist and deduplicated recent items separate with add/remove actions', async () => {
  const user = userEvent.setup();
  const onRemove = vi.fn();
  const onSelect = vi.fn();
  render(
    <MarketInstrumentRail
      collapsed={false}
      onAdd={vi.fn()}
      onRemove={onRemove}
      onSelect={onSelect}
      onToggle={vi.fn()}
      recent={[pufa, shanghai]}
      selectedSymbol="600000.SH"
      watchlist={[pufa]}
    />,
  );

  const watchlist = screen.getByRole('list', { name: '自选股' });
  const recent = screen.getByRole('list', { name: '最近访问' });
  expect(watchlist).toHaveTextContent('浦发银行');
  expect(recent).toHaveTextContent('浦发银行');
  expect(recent).toHaveTextContent('上证指数');
  await user.click(screen.getByRole('button', { name: '从自选移除浦发银行' }));
  expect(onRemove).toHaveBeenCalledWith(pufa);
  await user.click(
    screen.getByRole('button', { name: '查看上证指数 000001.SS' }),
  );
  expect(onSelect).toHaveBeenCalledWith(shanghai);
});

it('uses a semantic SVG toggle without a textual abbreviation', async () => {
  const user = userEvent.setup();
  const onToggle = vi.fn();
  render(
    <MarketInstrumentRail
      collapsed
      onAdd={vi.fn()}
      onRemove={vi.fn()}
      onSelect={vi.fn()}
      onToggle={onToggle}
      recent={[]}
      selectedSymbol={null}
      watchlist={[]}
    />,
  );

  const toggle = screen.getByRole('button', { name: '展开自选与最近访问' });
  expect(toggle.querySelector('svg')).not.toBeNull();
  expect(toggle).not.toHaveTextContent(/ZX|ZJ|WATCH/u);
  await user.click(toggle);
  expect(onToggle).toHaveBeenCalledOnce();
});
