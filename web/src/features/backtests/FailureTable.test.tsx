import { render, screen } from '@testing-library/react';

import { FailureTable } from './FailureTable';

it('renders both the empty state and a persisted failure row', () => {
  const mounted = render(<FailureTable items={[]} />);
  expect(screen.getByText('当前页没有失败记录。')).toBeVisible();

  mounted.rerender(
    <FailureTable
      items={[
        {
          detail: { dataset: 'signal' },
          ordinal: 0,
          reason: 'missing_signal_data',
          symbol: '600000.SH',
        },
      ]}
    />,
  );
  expect(screen.getByRole('rowheader', { name: '600000.SH' })).toBeVisible();
  expect(screen.getByText('missing_signal_data')).toBeVisible();
});
