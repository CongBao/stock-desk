import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { App } from './App';

vi.mock('../features/formulas/FormulaStudioPage', () => ({
  FormulaStudioPage: () => <h2 data-page-heading>公式工作台</h2>,
}));

vi.mock('../features/analysis/AnalysisPage', () => ({
  AnalysisPage: () => <h2 data-page-heading>智能分析</h2>,
}));

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('shows stock-desk name version and repository in about information', async () => {
  const user = userEvent.setup();
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <MemoryRouter initialEntries={['/market']}>
        <App onboardingApi={null} />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  expect(screen.getByRole('img', { name: 'Stock Desk' })).toHaveAttribute(
    'src',
    '/brand-icon.svg',
  );
  await user.click(screen.getByRole('button', { name: '关于 stock-desk' }));

  const about = screen.getByRole('dialog', { name: '关于 stock-desk' });
  expect(about.tagName).toBe('DIALOG');
  expect(about).toHaveTextContent('stock-desk');
  expect(about).toHaveTextContent('v1.1.0');
  expect(
    screen.getByRole('link', { name: 'github.com/CongBao/stock-desk' }),
  ).toHaveAttribute('href', 'https://github.com/CongBao/stock-desk');

  const close = screen.getByRole('button', { name: '关闭关于信息' });
  expect(close).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: '关于 stock-desk' }),
    ).toHaveFocus(),
  );
});
