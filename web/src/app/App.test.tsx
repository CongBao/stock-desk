import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from './App';

beforeEach(() => {
  window.history.pushState({}, '', '/market');
});

it('shows the product identity and all primary navigation items', () => {
  render(<App />);

  expect(screen.getByText('stock-desk')).toBeInTheDocument();
  for (const label of [
    '行情',
    '自定义公式',
    '策略回测',
    '智能分析',
    '任务中心',
    '设置',
  ]) {
    expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
  }
});

it('opens on a clearly labelled non-live market layout preview', async () => {
  window.history.pushState({}, '', '/');

  render(<App />);

  expect(
    await screen.findByRole('region', { name: 'K 线主图布局预览' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('region', { name: '公式副图布局预览' }),
  ).toBeInTheDocument();
  expect(screen.getByText('布局预览 / 非实时数据')).toBeInTheDocument();
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
});

it('updates the current navigation item and explains planned releases', async () => {
  const user = userEvent.setup();
  render(<App />);

  const formulaLink = screen.getByRole('link', { name: '自定义公式' });
  await user.click(formulaLink);

  expect(formulaLink).toHaveAttribute('aria-current', 'page');
  expect(
    screen.getByRole('heading', { level: 2, name: '自定义公式' }),
  ).toBeInTheDocument();
  expect(screen.getByText('计划版本 v0.3.0')).toBeInTheDocument();

  const backtestLink = screen.getByRole('link', { name: '策略回测' });
  await user.click(backtestLink);

  expect(backtestLink).toHaveAttribute('aria-current', 'page');
  expect(formulaLink).not.toHaveAttribute('aria-current');
  expect(screen.getByText('计划版本 v0.4.0')).toBeInTheDocument();
});

it('opens and closes the accessible context drawer', async () => {
  const user = userEvent.setup();
  render(<App />);

  const toggle = screen.getByRole('button', { name: '打开上下文面板' });
  const panel = screen.getByRole('complementary', { name: '上下文状态' });

  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAttribute('aria-controls', 'context-panel');
  expect(panel).toHaveAttribute('data-open', 'false');

  await user.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  expect(toggle).toHaveAccessibleName('隐藏上下文面板');
  expect(panel).toHaveAttribute('data-open', 'true');

  await user.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAccessibleName('打开上下文面板');
  expect(panel).toHaveAttribute('data-open', 'false');

  await user.click(toggle);
  const closeButton = screen.getByRole('button', { name: '关闭上下文面板' });
  expect(closeButton).toHaveFocus();
  await user.click(closeButton);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAccessibleName('打开上下文面板');
  expect(panel).toHaveAttribute('data-open', 'false');
  expect(toggle).toHaveFocus();
});

it('uses unique named landmarks and provides a keyboard skip link', () => {
  render(<App />);

  expect(screen.getAllByRole('navigation')).toHaveLength(1);
  expect(screen.getAllByRole('main')).toHaveLength(1);
  expect(
    screen.getAllByRole('complementary', { name: '上下文状态' }),
  ).toHaveLength(1);
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  expect(screen.getByRole('link', { name: '跳到主要内容' })).toHaveAttribute(
    'href',
    '#main-content',
  );
});
