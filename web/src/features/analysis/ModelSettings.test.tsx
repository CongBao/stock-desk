import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';

import { ModelSettings } from './ModelSettings';
import type {
  AnalysisApi,
  ModelConfig,
  ModelConnectionResult,
} from './analysisApi';

const now = '2026-07-08T08:00:00Z';
const id = (character: string) => `sha256:${character.repeat(64)}`;
const model = (character: string, name: string): ModelConfig => ({
  id: id(character),
  displayName: name,
  provider: 'deepseek',
  baseUrl: 'https://custom.example/v1',
  model: `${name}-model`,
  temperature: 0.1,
  timeout: 90,
  maxOutput: 4096,
  apiKeyConfigured: true,
  maskedApiKey: 'abcd•••••••wxyz',
  status: 'verified',
  revision: 1,
  verifiedAt: now,
  lastTestedAt: now,
  errorCode: null,
  createdAt: now,
  updatedAt: now,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function Harness({
  api,
  initial,
}: {
  api: AnalysisApi;
  initial: ModelConfig[];
}) {
  const [models, setModels] = useState<readonly ModelConfig[]>(initial);
  return (
    <>
      <ModelSettings api={api} models={models} onModelsChange={setModels} />
      <output data-testid="models-state">{JSON.stringify(models)}</output>
    </>
  );
}

function SessionHarness({
  api,
  initial,
}: {
  api: AnalysisApi;
  initial: ModelConfig[];
}) {
  const [instance, setInstance] = useState(0);
  const [models, setModels] = useState<readonly ModelConfig[]>(initial);
  return (
    <>
      <button type="button" onClick={() => setInstance((value) => value + 1)}>
        替换模型设置会话
      </button>
      <ModelSettings
        key={instance}
        api={api}
        models={models}
        onModelsChange={setModels}
      />
      <output data-testid="models-state">{JSON.stringify(models)}</output>
    </>
  );
}

function api(): AnalysisApi {
  return {
    testModel: vi.fn(),
    disableModel: vi.fn(),
    createModel: vi.fn(),
    createModelSuccessor: vi.fn(),
  } as unknown as AnalysisApi;
}

function connection(
  item: ModelConfig,
  revision: number,
): ModelConnectionResult {
  return {
    configId: item.id,
    connected: true,
    provider: item.provider,
    model: item.model,
    errorCode: null,
    status: 'verified',
    revision,
    testedAt: now,
    lastTestedAt: now,
  };
}

it('preserves two out-of-order model test results and locks each active config', async () => {
  const first = model('a', '模型甲');
  const second = model('b', '模型乙');
  const firstPending = deferred<ModelConnectionResult>();
  const secondPending = deferred<ModelConnectionResult>();
  const client = api();
  vi.mocked(client.testModel).mockImplementation((configId) =>
    configId === first.id ? firstPending.promise : secondPending.promise,
  );
  render(<Harness api={client} initial={[first, second]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  await userEvent.click(
    screen.getByRole('button', { name: '测试 模型甲 连接' }),
  );
  expect(
    screen.getByRole('button', { name: '测试 模型甲 连接' }),
  ).toBeDisabled();
  expect(screen.getByRole('button', { name: '禁用 模型甲' })).toBeDisabled();
  await userEvent.click(
    screen.getByRole('button', { name: '测试 模型乙 连接' }),
  );
  secondPending.resolve(connection(second, 3));
  await waitFor(() =>
    expect(screen.getByTestId('models-state')).toHaveTextContent(
      '"revision":3',
    ),
  );
  firstPending.resolve(connection(first, 2));
  await waitFor(() => {
    const state = JSON.parse(
      screen.getByTestId('models-state').textContent ?? '[]',
    ) as ModelConfig[];
    expect(state.find((item) => item.id === first.id)?.revision).toBe(2);
    expect(state.find((item) => item.id === second.id)?.revision).toBe(3);
  });
});

it('traps focus in both directions inside the modal', async () => {
  render(<Harness api={api()} initial={[model('a', '模型甲')]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  const close = screen.getByRole('button', { name: '关闭模型设置' });
  const last = screen.getByRole('button', { name: '禁用 模型甲' });
  expect(close).toHaveFocus();
  await userEvent.keyboard('{Shift>}{Tab}{/Shift}');
  expect(last).toHaveFocus();
  await userEvent.tab();
  expect(close).toHaveFocus();
});

it('protects dirty fields behind a default-safe discard confirmation', async () => {
  const client = api();
  render(<Harness api={client} initial={[]} />);
  const trigger = screen.getByRole('button', { name: '模型设置' });
  await userEvent.click(trigger);
  const displayName = screen.getByLabelText('显示名称');
  await userEvent.clear(displayName);
  await userEvent.type(displayName, '尚未保存的模型');

  await userEvent.keyboard('{Escape}');
  expect(
    screen.getByRole('alertdialog', {
      name: '放弃未保存的模型设置？',
    }),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '继续编辑' })).toHaveFocus();

  await userEvent.keyboard('{Escape}');
  expect(
    screen.queryByRole('alertdialog', {
      name: '放弃未保存的模型设置？',
    }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText('显示名称')).toHaveValue('尚未保存的模型');
  await waitFor(() => expect(screen.getByLabelText('显示名称')).toHaveFocus());

  await userEvent.click(screen.getByRole('button', { name: '关闭模型设置' }));
  await userEvent.click(screen.getByRole('button', { name: '放弃更改' }));
  expect(
    screen.queryByRole('dialog', { name: '模型设置' }),
  ).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();

  await userEvent.click(trigger);
  expect(screen.getByLabelText('显示名称')).toHaveValue('研究模型');
  expect(client.createModel).not.toHaveBeenCalled();
});

it('updates the dirty baseline after a successful save', async () => {
  const client = api();
  vi.mocked(client.createModel).mockResolvedValue(model('b', '已保存模型'));
  render(<Harness api={client} initial={[]} />);
  const trigger = screen.getByRole('button', { name: '模型设置' });
  await userEvent.click(trigger);
  await userEvent.type(screen.getByLabelText('API Key'), 'local-secret');
  await userEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await waitFor(() => expect(client.createModel).toHaveBeenCalledOnce());
  expect(
    screen.getByText('模型配置已安全保存，请测试连接后使用。'),
  ).toBeInTheDocument();

  await userEvent.keyboard('{Escape}');
  expect(
    screen.queryByRole('alertdialog', {
      name: '放弃未保存的模型设置？',
    }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole('dialog', { name: '模型设置' }),
  ).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it('locks a pending save, moves focus to status, and prevents duplicate submission', async () => {
  const pending = deferred<ModelConfig>();
  const client = api();
  vi.mocked(client.createModel).mockReturnValue(pending.promise);
  render(<Harness api={client} initial={[model('a', '模型甲')]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  await userEvent.type(screen.getByLabelText('API Key'), 'local-secret');
  await userEvent.click(screen.getByRole('button', { name: '保存模型配置' }));

  const dialog = screen.getByRole('dialog', { name: '模型设置' });
  const status = within(dialog).getByRole('status');
  expect(dialog).toHaveAttribute('aria-busy', 'true');
  expect(status).toHaveTextContent('正在保存模型配置…');
  await waitFor(() => expect(status).toHaveFocus());
  expect(screen.getByRole('button', { name: '关闭模型设置' })).toBeDisabled();
  expect(screen.getByLabelText('提供商')).toBeDisabled();
  expect(screen.getByRole('button', { name: '编辑 模型甲' })).toBeDisabled();

  await userEvent.keyboard('{Escape}{Escape}');
  expect(dialog).toBeInTheDocument();
  expect(client.createModel).toHaveBeenCalledOnce();

  pending.resolve(model('b', '已保存模型'));
  await waitFor(() => expect(dialog).toHaveAttribute('aria-busy', 'false'));
  expect(status).toHaveTextContent('模型配置已安全保存');
});

it('ignores a deferred save result after its editor session is replaced', async () => {
  const pending = deferred<ModelConfig>();
  const client = api();
  vi.mocked(client.createModel).mockReturnValue(pending.promise);
  render(<SessionHarness api={client} initial={[]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  await userEvent.type(screen.getByLabelText('API Key'), 'old-secret');
  await userEvent.click(screen.getByRole('button', { name: '保存模型配置' }));

  await userEvent.click(
    screen.getByRole('button', { name: '替换模型设置会话' }),
  );
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  const displayName = screen.getByLabelText('显示名称');
  await userEvent.clear(displayName);
  await userEvent.type(displayName, '新会话草稿');

  pending.resolve(model('c', '过期结果'));
  await waitFor(() => expect(displayName).toHaveValue('新会话草稿'));
  expect(screen.getByTestId('models-state')).toHaveTextContent('[]');
  expect(screen.queryByText('过期结果')).not.toBeInTheDocument();
});

it('keeps disable confirmation cancel-safe and calls the API only explicitly', async () => {
  const item = model('a', '模型甲');
  const client = api();
  vi.mocked(client.disableModel).mockResolvedValue({
    ...item,
    status: 'disabled',
    revision: 2,
  });
  render(<Harness api={client} initial={[item]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));

  await userEvent.click(screen.getByRole('button', { name: '禁用 模型甲' }));
  expect(screen.getByRole('button', { name: '取消禁用' })).toHaveFocus();
  expect(client.disableModel).not.toHaveBeenCalled();
  await userEvent.keyboard('{Escape}');
  expect(client.disableModel).not.toHaveBeenCalled();
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '禁用 模型甲' })).toHaveFocus(),
  );
  expect(
    screen.queryByRole('alertdialog', { name: '确认禁用模型配置？' }),
  ).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: '禁用 模型甲' }));
  await userEvent.click(screen.getByRole('button', { name: '取消禁用' }));
  expect(client.disableModel).not.toHaveBeenCalled();

  await userEvent.click(screen.getByRole('button', { name: '禁用 模型甲' }));
  await userEvent.click(screen.getByRole('button', { name: '确认禁用' }));
  expect(client.disableModel).toHaveBeenCalledWith(item.id, item.revision);
});

it('applies provider defaults while preserving same-provider edit values', async () => {
  render(<Harness api={api()} initial={[model('a', '模型甲')]} />);
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  await userEvent.selectOptions(screen.getByLabelText('提供商'), 'ollama');
  expect(screen.getByLabelText('Base URL')).toHaveValue(
    'http://127.0.0.1:11434',
  );
  expect(screen.getByLabelText('模型')).toHaveValue('qwen2.5:7b');
  expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText('提供商'), 'deepseek');
  expect(screen.getByLabelText('Base URL')).toHaveValue(
    'https://api.deepseek.com',
  );
  expect(screen.getByLabelText('模型')).toHaveValue('deepseek-chat');
  await userEvent.click(screen.getByRole('button', { name: '编辑 模型甲' }));
  expect(screen.getByLabelText('Base URL')).toHaveValue(
    'https://custom.example/v1',
  );
  expect(screen.getByLabelText('模型')).toHaveValue('模型甲-model');
});

it('labels failed verification distinctly and shows only the safe error code', async () => {
  render(
    <Harness
      api={api()}
      initial={[
        {
          ...model('a', '模型甲'),
          status: 'failed',
          errorCode: 'provider_timeout',
        },
      ]}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: '模型设置' }));
  expect(screen.getByText('验证失败')).toBeInTheDocument();
  expect(screen.getByText('错误代码：provider_timeout')).toBeInTheDocument();
  expect(screen.queryByText('待验证')).not.toBeInTheDocument();
});
