import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { RouteEffects } from './RouteEffects';

beforeEach(() =>
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined),
);

it('recognizes a direct backtest run URL as strategy backtesting', async () => {
  render(
    <MemoryRouter
      initialEntries={['/backtests/11111111-1111-1111-1111-111111111111']}
    >
      <h2 data-page-heading tabIndex={-1}>
        运行详情
      </h2>
      <RouteEffects />
    </MemoryRouter>,
  );

  await waitFor(() => expect(document.title).toBe('策略回测 · stock-desk'));
  expect(screen.getByRole('status')).toHaveTextContent('已进入：策略回测');
  expect(screen.getByRole('heading', { name: '运行详情' })).toHaveFocus();
});
