import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MarketInstrument } from '../../market/marketApi';
import { ScopeStep } from './ScopeStep';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function instrument(symbol: string): MarketInstrument {
  return {
    symbol,
    name: '测试股票',
    instrumentKind: 'stock',
    listingStatus: 'listed',
  } as unknown as MarketInstrument;
}

it('aborts and clears search busy state when the query changes, then ignores stale results', async () => {
  const user = userEvent.setup();
  const first = deferred<readonly MarketInstrument[]>();
  const second = deferred<readonly MarketInstrument[]>();
  const searchInstruments = vi
    .fn()
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  render(
    <ScopeStep
      scope={{ kind: 'single', symbol: '' }}
      pools={[]}
      marketApiClient={{ searchInstruments }}
      onChange={vi.fn()}
    />,
  );

  await user.type(screen.getByLabelText('证券'), '浦发');
  await user.click(screen.getByRole('button', { name: '搜索证券' }));
  expect(screen.getByRole('button', { name: '搜索中…' })).toBeDisabled();
  await user.clear(screen.getByLabelText('证券'));
  await user.type(screen.getByLabelText('证券'), '平安');
  expect(screen.getByRole('button', { name: '搜索证券' })).toBeEnabled();

  first.resolve([instrument('600000.SH')]);
  await Promise.resolve();
  expect(
    screen.queryByRole('button', { name: /600000.SH/u }),
  ).not.toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '搜索证券' }));
  second.resolve([instrument('000001.SZ')]);
  expect(
    await screen.findByRole('button', { name: /000001.SZ/u }),
  ).toBeVisible();
});

it('filters out non-stock and delisted instruments from selectable results', async () => {
  const user = userEvent.setup();
  const searchInstruments = vi
    .fn()
    .mockResolvedValue([
      instrument('600000.SH'),
      { ...instrument('000300.SH'), instrumentKind: 'index' },
      { ...instrument('000001.SZ'), listingStatus: 'delisted' },
    ]);
  render(
    <ScopeStep
      scope={{ kind: 'single', symbol: '' }}
      pools={[]}
      marketApiClient={{ searchInstruments }}
      onChange={vi.fn()}
    />,
  );
  await user.type(screen.getByLabelText('证券'), '股票');
  await user.click(screen.getByRole('button', { name: '搜索证券' }));
  expect(
    await screen.findByRole('button', { name: /600000.SH/u }),
  ).toBeVisible();
  expect(
    screen.queryByRole('button', { name: /000300.SH/u }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: /000001.SZ/u }),
  ).not.toBeInTheDocument();
});
