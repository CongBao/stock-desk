import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
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
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  await user.click(screen.getByRole('button', { name: '关于 stock-desk' }));

  const about = screen.getByRole('dialog', { name: '关于 stock-desk' });
  expect(about).toHaveTextContent('stock-desk');
  expect(about).toHaveTextContent('v1.0.0');
  expect(
    screen.getByRole('link', { name: 'github.com/CongBao/stock-desk' }),
  ).toHaveAttribute('href', 'https://github.com/CongBao/stock-desk');
});
