import { render, screen } from '@testing-library/react';

import { AsyncActionButton } from './AsyncActionButton';

it('shows a spinner only while pending and keeps the visible label stable', () => {
  const { rerender } = render(
    <AsyncActionButton pending={false}>开始</AsyncActionButton>,
  );
  const ready = screen.getByRole('button', { name: '开始' });
  expect(ready).not.toHaveAttribute('aria-busy');
  expect(ready).toBeEnabled();
  expect(screen.queryByTestId('async-action-spinner')).toBeNull();

  rerender(<AsyncActionButton pending>开始</AsyncActionButton>);

  const pending = screen.getByRole('button', { name: '开始' });
  expect(pending).toBeDisabled();
  expect(pending).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByTestId('async-action-spinner')).toBeInTheDocument();
  expect(pending).toHaveTextContent('开始');
});

it('does not present an ordinary disabled button as busy', () => {
  render(
    <AsyncActionButton pending={false} disabled>
      保存
    </AsyncActionButton>,
  );

  const button = screen.getByRole('button', { name: '保存' });
  expect(button).toBeDisabled();
  expect(button).not.toHaveAttribute('aria-busy');
  expect(screen.queryByTestId('async-action-spinner')).toBeNull();
});
