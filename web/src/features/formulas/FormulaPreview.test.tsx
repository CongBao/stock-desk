import { render, screen } from '@testing-library/react';

import { FormulaPreview } from './FormulaPreview';

vi.mock('../market/MarketChart', () => ({
  MarketChart: ({
    formulaEmptyMessage,
  }: {
    readonly formulaEmptyMessage?: string;
  }) => <p>{formulaEmptyMessage}</p>,
}));

const baseProps = {
  adjustment: 'qfq' as const,
  isLoading: false,
  onAdjustmentChange: vi.fn(),
  onPeriodChange: vi.fn(),
  onPreview: vi.fn(),
  onSymbolChange: vi.fn(),
  period: '1d' as const,
  previewDisabled: false,
  symbol: '600000.SH',
};

it('describes an idle main placement as a main-chart overlay rather than a subchart', () => {
  render(<FormulaPreview {...baseProps} placement="main" />);

  expect(
    screen.getByText('保存并运行预览后在 K 线主图叠加公式输出与买卖点'),
  ).toBeVisible();
  expect(screen.queryByText(/副图/u)).not.toBeInTheDocument();
});

it('keeps the explicit subchart description for subchart placement', () => {
  render(<FormulaPreview {...baseProps} placement="subchart" />);

  expect(
    screen.getByText('保存并运行预览后显示公式副图与买卖点'),
  ).toBeVisible();
});
