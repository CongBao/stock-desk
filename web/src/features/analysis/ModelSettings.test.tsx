import { render, screen, waitFor } from '@testing-library/react';
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
