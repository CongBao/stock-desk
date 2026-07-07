import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { AnalysisRunPanel } from './AnalysisRunPanel';
import type { AnalysisApi, ModelConfig } from './analysisApi';

const modelId = `sha256:${'a'.repeat(64)}`;
const now = '2026-07-08T08:00:00Z';

const verifiedModel: ModelConfig = {
  id: modelId,
  displayName: '研究模型',
  provider: 'deepseek',
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  temperature: 0.1,
  timeout: 90,
  maxOutput: 4096,
  apiKeyConfigured: true,
  maskedApiKey: 'sk-a•••••••tail',
  status: 'verified',
  revision: 1,
  verifiedAt: now,
  lastTestedAt: now,
  errorCode: null,
  createdAt: now,
  updatedAt: now,
};

function client() {
  return {
    preflight: vi.fn().mockResolvedValue({
      symbol: '600000.SH',
      previewSnapshotId: `sha256:${'b'.repeat(64)}`,
      reservation: false,
      ratingEligible: true,
      checkedAt: now,
      categories: ['market', 'fundamentals', 'announcements', 'news'].map(
        (kind) => ({
          kind,
          critical: kind !== 'news',
          connectionState: 'available',
          routeSource: 'tushare',
          actualSource: 'tushare',
          orderedCandidates: [],
          attemptedSources: ['tushare'],
          missingReason: null,
          recoveryCode: null,
          permissionGap: false,
          dataCutoff: now,
          fetchedAt: now,
          datasetVersion: 'v1',
          qualityFlags: [],
        }),
      ),
    }),
    start: vi.fn(),
  } as unknown as AnalysisApi;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const preflightResult = {
  symbol: '600000.SH',
  previewSnapshotId: `sha256:${'b'.repeat(64)}`,
  reservation: false as const,
  ratingEligible: true,
  checkedAt: now,
  categories: [],
};

function panel(api: AnalysisApi, models: readonly ModelConfig[]) {
  return (
    <AnalysisRunPanel
      api={api}
      models={models}
      onModelsChange={() => undefined}
      history={[]}
      nextCursor={null}
      onLoadMore={() => undefined}
      onOpenRun={() => undefined}
      onStarted={() => undefined}
    />
  );
}

it.each(['disabled', 'failed'] as const)(
  'clears selection and preflight when the selected model becomes %s',
  async (status) => {
    const api = client();
    const view = render(panel(api, [verifiedModel]));
    await userEvent.type(screen.getByLabelText('股票代码'), '600000.SH');
    await userEvent.selectOptions(screen.getByLabelText('已验证模型'), modelId);
    await userEvent.click(screen.getByRole('button', { name: '运行预检' }));
    expect(await screen.findByText('数据覆盖满足评级门槛')).toBeInTheDocument();

    view.rerender(panel(api, [{ ...verifiedModel, status }]));

    const selector = screen.getByLabelText('已验证模型');
    await waitFor(() => expect(selector).toHaveValue(''));
    expect(screen.queryByText('数据覆盖满足评级门槛')).not.toBeInTheDocument();
    const start = screen.getByRole('button', { name: '启动智能分析' });
    expect(start).toBeDisabled();

    start.removeAttribute('disabled');
    fireEvent.click(start);
    expect(api.start).not.toHaveBeenCalled();
  },
);

it('ignores a late preflight response after the symbol changes', async () => {
  const pending = deferred<typeof preflightResult>();
  const api = client();
  vi.mocked(api.preflight).mockReturnValueOnce(pending.promise);
  render(panel(api, [verifiedModel]));
  await userEvent.type(screen.getByLabelText('股票代码'), '600000.SH');
  await userEvent.selectOptions(screen.getByLabelText('已验证模型'), modelId);
  await userEvent.click(screen.getByRole('button', { name: '运行预检' }));
  await userEvent.clear(screen.getByLabelText('股票代码'));
  await userEvent.type(screen.getByLabelText('股票代码'), '600001.SH');
  pending.resolve(preflightResult);

  await waitFor(() =>
    expect(screen.getByRole('button', { name: '运行预检' })).toBeEnabled(),
  );
  expect(screen.queryByText('数据覆盖满足评级门槛')).not.toBeInTheDocument();
  const start = screen.getByRole('button', { name: '启动智能分析' });
  expect(start).toBeDisabled();
  start.removeAttribute('disabled');
  fireEvent.click(start);
  expect(api.start).not.toHaveBeenCalled();
});

it.each(['6', '1.5'])(
  'rejects invalid max retries %s in both controls and handler',
  async (retries) => {
    const api = client();
    render(panel(api, [verifiedModel]));
    await userEvent.type(screen.getByLabelText('股票代码'), '600000.SH');
    await userEvent.selectOptions(screen.getByLabelText('已验证模型'), modelId);
    await userEvent.click(screen.getByRole('button', { name: '运行预检' }));
    await screen.findByText('数据覆盖满足评级门槛');
    const retryInput = screen.getByLabelText('最大重试次数');
    await userEvent.clear(retryInput);
    await userEvent.type(retryInput, retries);
    const start = screen.getByRole('button', { name: '启动智能分析' });
    expect(start).toBeDisabled();
    start.removeAttribute('disabled');
    fireEvent.click(start);
    expect(api.start).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('0 到 5 的整数');
  },
);
