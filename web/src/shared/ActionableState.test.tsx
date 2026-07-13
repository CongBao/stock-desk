import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ActionableState } from './ActionableState';

it.each([
  'loading',
  'empty',
  'offline',
  'permission',
  'error',
  'sidecar-unavailable',
] as const)('renders an actionable %s state', async (kind) => {
  const user = userEvent.setup();
  const onAction = vi.fn();

  render(
    <ActionableState
      kind={kind}
      title="暂时无法继续"
      reason="本地服务尚未准备好。"
      actionLabel="重新尝试"
      onAction={onAction}
      failureId="desk_ab12cd34"
    />,
  );

  expect(screen.getByRole('status')).toHaveAttribute('data-state-kind', kind);
  expect(screen.getByText('本地服务尚未准备好。')).toBeVisible();
  expect(screen.getByText('故障标识：desk_ab12cd34')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '重新尝试' }));
  expect(onAction).toHaveBeenCalledOnce();
});

it('fails closed instead of rendering unsafe technical details', () => {
  render(
    <ActionableState
      kind="error"
      title="HTTP 503 Traceback"
      reason={'C:\\' + 'Users\\alice\\secret.txt Authorization: Bearer token'}
      actionLabel="重试"
      onAction={() => undefined}
      failureId="../../private"
    />,
  );

  expect(screen.getByRole('status')).toHaveTextContent('暂时无法显示详细原因');
  expect(document.body).not.toHaveTextContent(
    /HTTP|Traceback|Users|Authorization|Bearer|secret|\.\./u,
  );
  expect(screen.queryByText(/故障标识/u)).not.toBeInTheDocument();
});

it('explains why an action is temporarily unavailable', () => {
  render(
    <ActionableState
      kind="loading"
      title="正在载入"
      reason="正在读取本地任务。"
      actionLabel="重新读取"
      onAction={() => undefined}
      actionDisabledReason="当前读取完成后即可重试。"
    />,
  );

  expect(screen.getByRole('button', { name: '重新读取' })).toBeDisabled();
  expect(screen.getByText('当前读取完成后即可重试。')).toBeVisible();
});
