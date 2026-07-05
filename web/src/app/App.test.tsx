import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  useNavigate,
  type MemoryRouterProps,
} from 'react-router-dom';

import { App } from './App';

function HistoryBackControl() {
  const navigate = useNavigate();

  return (
    <button type="button" onClick={() => void navigate(-1)}>
      测试返回
    </button>
  );
}

function renderApp(
  initialEntries: MemoryRouterProps['initialEntries'] = ['/market'],
  withBackControl = false,
) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      {withBackControl ? <HistoryBackControl /> : null}
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

it('shows the product identity and all primary navigation items', () => {
  renderApp();

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
  renderApp(['/']);

  expect(
    await screen.findByRole('region', { name: 'K 线主图布局预览' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('region', { name: '公式副图布局预览' }),
  ).toBeInTheDocument();
  expect(screen.getByText('布局预览 / 非实时数据')).toBeInTheDocument();
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
});

it.each(['/market/', '/MARKET'])(
  'keeps route effects aligned with market content for %s',
  async (pathname) => {
    renderApp([pathname]);

    const heading = screen.getByRole('heading', {
      level: 2,
      name: '行情工作区',
    });
    await waitFor(() => expect(heading).toHaveFocus());

    expect(
      screen.getByRole('region', { name: 'K 线主图布局预览' }),
    ).toBeInTheDocument();
    expect(document.title).toBe('行情工作区 · stock-desk');
    expect(screen.getByRole('status')).toHaveTextContent('已进入：行情工作区');
  },
);

it('updates route title, focus, announcement, scroll, and browser history', async () => {
  const user = userEvent.setup();
  renderApp(['/market'], true);

  const marketHeading = await screen.findByRole('heading', {
    level: 2,
    name: '行情工作区',
  });
  await waitFor(() => expect(marketHeading).toHaveFocus());
  expect(document.title).toBe('行情工作区 · stock-desk');

  const formulaLink = screen.getByRole('link', { name: '自定义公式' });
  await user.click(formulaLink);

  const formulaHeading = screen.getByRole('heading', {
    level: 2,
    name: '自定义公式',
  });
  await waitFor(() => expect(formulaHeading).toHaveFocus());
  expect(formulaLink).toHaveAttribute('aria-current', 'page');
  expect(document.title).toBe('自定义公式 · stock-desk');
  expect(screen.getByRole('status')).toHaveTextContent('已进入：自定义公式');
  expect(screen.getByText('计划版本 v0.3.0')).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '测试返回' }));

  await waitFor(() =>
    expect(
      screen.getByRole('heading', { level: 2, name: '行情工作区' }),
    ).toHaveFocus(),
  );
  expect(screen.getByRole('link', { name: '行情' })).toHaveAttribute(
    'aria-current',
    'page',
  );
  expect(document.title).toBe('行情工作区 · stock-desk');
  expect(window.scrollTo).toHaveBeenCalledWith({
    behavior: 'auto',
    left: 0,
    top: 0,
  });
});

it('renders an explicit focus-managed not-found page', async () => {
  renderApp(['/does-not-exist']);

  const heading = screen.getByRole('heading', {
    level: 2,
    name: '页面未找到',
  });
  await waitFor(() => expect(heading).toHaveFocus());
  expect(document.title).toBe('页面未找到 · stock-desk');
  expect(screen.getByRole('status')).toHaveTextContent('已进入：页面未找到');
  expect(screen.getByRole('link', { name: '返回行情工作区' })).toHaveAttribute(
    'href',
    '/market',
  );
});

it('opens the drawer after the click settles and restores focus on Escape', async () => {
  const user = userEvent.setup();
  renderApp();

  const toggle = screen.getByRole('button', { name: '打开上下文面板' });
  const panel = screen.getByRole('complementary', { name: '上下文状态' });

  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAttribute('aria-controls', 'context-panel');
  expect(panel).toHaveAttribute('data-open', 'false');

  await user.click(toggle);

  const closeButton = screen.getByRole('button', { name: '关闭上下文面板' });
  await waitFor(() => expect(closeButton).toHaveFocus());
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  expect(toggle).toHaveAccessibleName('隐藏上下文面板');
  expect(panel).toHaveAttribute('data-open', 'true');

  await user.keyboard('{Escape}');

  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAccessibleName('打开上下文面板');
  expect(panel).toHaveAttribute('data-open', 'false');
  expect(toggle).toHaveFocus();
});

it('supports both the drawer trigger and close button without trapping focus', async () => {
  const user = userEvent.setup();
  renderApp();

  const toggle = screen.getByRole('button', { name: '打开上下文面板' });
  await user.click(toggle);
  const closeButton = screen.getByRole('button', { name: '关闭上下文面板' });
  await waitFor(() => expect(closeButton).toHaveFocus());

  await user.tab();
  expect(closeButton).not.toHaveFocus();

  await user.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveFocus();

  await user.click(toggle);
  await waitFor(() => expect(closeButton).toHaveFocus());
  await user.click(closeButton);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveFocus();
});

it('creates isolated drawer state for every App mount', async () => {
  const user = userEvent.setup();
  const firstMount = renderApp();

  await user.click(screen.getByRole('button', { name: '打开上下文面板' }));
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toHaveAttribute('data-open', 'true');
  firstMount.unmount();

  renderApp();

  expect(
    screen.getByRole('button', { name: '打开上下文面板' }),
  ).toHaveAttribute('aria-expanded', 'false');
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toHaveAttribute('data-open', 'false');
});

it('uses one navigation landmark and one complementary context landmark', () => {
  renderApp();

  expect(screen.getAllByRole('navigation')).toHaveLength(1);
  expect(screen.getAllByRole('main')).toHaveLength(1);
  expect(screen.getAllByRole('complementary')).toHaveLength(1);
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toBeInTheDocument();
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  expect(screen.getByRole('link', { name: '跳到主要内容' })).toHaveAttribute(
    'href',
    '#main-content',
  );
});
