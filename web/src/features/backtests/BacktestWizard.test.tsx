import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MarketPoolSummary } from '../market/marketApi';
import { BacktestWizard } from './BacktestWizard';
import type { BacktestDraft } from './backtestDraft';
import type { BacktestApi, BacktestPreflight } from './backtestApi';
import type { FormulaChoice } from './steps/FormulaStep';

const completeState: BacktestDraft = {
  adjustment: 'qfq',
  commissionBps: '2.5',
  endDate: '2026-01-02',
  formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  formulaParameters: { FAST: 12 },
  formulaVersionId: '11111111-1111-1111-1111-111111111111',
  minimumCommission: '5',
  period: '1d',
  quantityShares: 1000,
  scope: { kind: 'single', symbol: '600000.SH' },
  sellTaxBps: '5',
  slippageBps: '1',
  startDate: '2025-01-02',
};

const formulaChoices: readonly FormulaChoice[] = [
  {
    createdAt: '2026-07-07T00:00:00Z',
    formulaType: 'trading',
    id: completeState.formulaId,
    latestVersion: 1,
    name: 'MACD 金叉',
    placement: 'subchart',
    updatedAt: '2026-07-07T00:00:00Z',
    versions: [
      {
        checksum: `sha256:${'b'.repeat(64)}`,
        compatibilityVersion: 'tdx-v1',
        createdAt: '2026-07-07T00:00:00Z',
        engineVersion: 'formula-engine-v1',
        formulaId: completeState.formulaId,
        formulaType: 'trading',
        id: completeState.formulaVersionId,
        name: 'MACD 金叉',
        parameterSchema: {
          FAST: { default: 12, kind: 'integer', label: '快线周期' },
        },
        placement: 'subchart',
        source: 'BUY:CROSS(DIF,DEA);',
        version: 1,
      },
    ],
  },
];

beforeEach(() => localStorage.clear());

const preflight: BacktestPreflight = {
  adjustment: 'qfq',
  costs: {
    commissionBps: '2.5',
    minimumCommission: '5',
    sellTaxBps: '5',
    slippageBps: '1',
  },
  coverage: { execution: 2, signal: 2, status: 2 },
  disclaimer: '每只股票独立模拟，不代表组合收益',
  estimatedWorkload: { formulaRows: 500, runnableSymbols: 2, symbols: 3 },
  formula: {
    compatibilityVersion: 'tdx-v1',
    engineVersion: 'formula-engine-v1',
    formulaChecksum: `sha256:${'b'.repeat(64)}`,
    formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    formulaVersionId: completeState.formulaVersionId,
    normalizedParameters: [{ kind: 'integer', name: 'FAST', value: '12' }],
  },
  period: '1d',
  previewSnapshotId: `sha256:${'a'.repeat(64)}`,
  quantityShares: 1000,
  reservation: false,
  rules: {
    costModelVersion: 'a-share-cost-v1',
    executionRulesVersion: 'a-share-v1',
    sizingVersion: 'fixed-lot-v1',
  },
  scope: {
    gapCount: 1,
    gapSample: [{ reason: 'missing_data', symbol: '000001.SZ' }],
    gapsTruncated: false,
    kind: 'custom',
    poolId: '22222222-2222-2222-2222-222222222222',
    revisionOrSnapshotId: '7',
    runnable: 2,
    symbol: null,
    total: 3,
    warnings: ['partial_pool_gaps'],
  },
  scoringEnd: '2026-01-01T16:00:00Z',
  scoringStart: '2025-01-01T16:00:00Z',
  warmup: {
    lookbackBars: 35,
    policyVersion: 'formula-warmup-v1',
    unboundedDependency: false,
  },
};

function api(): BacktestApi {
  return {
    cancel: vi.fn(),
    create: vi.fn().mockResolvedValue({
      runId: '33333333-3333-3333-3333-333333333333',
      snapshotId: `sha256:${'c'.repeat(64)}`,
      taskId: '44444444-4444-4444-4444-444444444444',
      warnings: [],
    }),
    getLogs: vi.fn(),
    getRun: vi.fn(),
    listRuns: vi.fn(),
    preflight: vi.fn().mockResolvedValue(preflight),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it('requires formula, scope, period, costs, then review', () => {
  render(<BacktestWizard />);
  expect(screen.getByRole('heading', { name: '1. 公式' })).toBeVisible();
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();
  expect(screen.getAllByRole('button', { name: /[1-5]\. /u })).toHaveLength(5);
  expect(screen.getByText('尚无可执行交易公式。')).toBeVisible();
});

it('shows next-open and independent-pool semantics before submit', async () => {
  const user = userEvent.setup();
  render(
    <BacktestWizard
      api={api()}
      formulaChoices={formulaChoices}
      initialState={completeState}
    />,
  );

  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  expect(screen.getByText(/收盘确认，下一对应周期开盘尝试成交/u)).toBeVisible();
  expect(screen.getByText(/每只股票独立模拟，不代表组合收益/u)).toBeVisible();
});

it('rejects hidden parameters not declared by the selected immutable version', async () => {
  const user = userEvent.setup();
  render(
    <BacktestWizard
      api={api()}
      formulaChoices={formulaChoices}
      initialState={{
        ...completeState,
        formulaParameters: { ...completeState.formulaParameters, HIDDEN: 1 },
      }}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  expect(screen.getByRole('alert')).toHaveTextContent(
    '公式参数与所选版本不一致',
  );
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();
});

it('invalidates review immediately on refresh and again when a custom pool revision changes', async () => {
  const user = userEvent.setup();
  const customDraft: BacktestDraft = {
    ...completeState,
    scope: {
      kind: 'custom',
      poolId: '22222222-2222-2222-2222-222222222222',
      revision: 1,
    },
  };
  const pool = (revision: number) =>
    ({
      category: null,
      kind: 'custom',
      memberCount: 3,
      name: '我的股票池',
      poolId: '22222222-2222-2222-2222-222222222222',
      revision,
      snapshotId: null,
    }) as unknown as MarketPoolSummary;
  const client = api();
  const mounted = render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={customDraft}
      pools={[pool(1)]}
      catalogRevision={0}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  expect(await screen.findByText('可运行 2 / 3')).toBeVisible();

  mounted.rerender(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={customDraft}
      pools={[pool(1)]}
      catalogRevision={1}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByText('可运行 2 / 3')).not.toBeInTheDocument(),
  );
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();

  await user.click(screen.getByRole('button', { name: '运行预检' }));
  expect(await screen.findByText('可运行 2 / 3')).toBeVisible();
  mounted.rerender(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={customDraft}
      pools={[pool(2)]}
      catalogRevision={1}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByText('可运行 2 / 3')).not.toBeInTheDocument(),
  );
});

it('invalidates server review after every edit and prevents duplicate submit', async () => {
  const user = userEvent.setup();
  const client = api();
  const onSubmitted = vi.fn();
  render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
      onSubmitted={onSubmitted}
    />,
  );

  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  expect(await screen.findByText('可运行 2 / 3')).toBeVisible();
  expect(screen.getByRole('status', { name: '服务端预检结果' })).toHaveFocus();
  expect(screen.getByText('缺口 1')).toBeVisible();
  expect(screen.getByText('000001.SZ · 缺少所选区间数据')).toBeVisible();
  await user.click(screen.getByRole('checkbox', { name: /我确认本次仅回测/u }));
  expect(screen.getByRole('button', { name: '提交回测' })).toBeEnabled();

  await user.click(screen.getByRole('button', { name: '4. 成本' }));
  await user.clear(screen.getByLabelText('佣金（基点）'));
  await user.type(screen.getByLabelText('佣金（基点）'), '3');
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();

  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  await user.click(
    await screen.findByRole('checkbox', { name: /我确认本次仅回测/u }),
  );
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '提交回测' })).toBeEnabled(),
  );
  await user.dblClick(screen.getByRole('button', { name: '提交回测' }));
  await waitFor(() => expect(client.create).toHaveBeenCalledTimes(1));
  expect(onSubmitted).toHaveBeenCalledTimes(1);
});

it('retains the complete draft and focuses a safe error after submit failure', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.create).mockRejectedValue(new Error('secret token'));
  render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  await user.click(
    await screen.findByRole('checkbox', { name: /我确认本次仅回测/u }),
  );
  await user.click(await screen.findByRole('button', { name: '提交回测' }));

  const alert = await screen.findByRole('alert');
  expect(alert).toHaveFocus();
  expect(alert).toHaveTextContent('完整草稿已保留');
  expect(alert).not.toHaveTextContent('secret token');
  expect(localStorage.getItem('stock-desk.backtest-draft.v1')).toContain(
    completeState.formulaVersionId,
  );
  expect(screen.getByRole('link', { name: '更新行情数据' })).toHaveAttribute(
    'href',
    '/market',
  );
});

it('blocks zero-runnable scope while showing remediation links', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.preflight).mockResolvedValue({
    ...preflight,
    coverage: { execution: 0, signal: 0, status: 0 },
    estimatedWorkload: { ...preflight.estimatedWorkload, runnableSymbols: 0 },
    scope: { ...preflight.scope, total: 1, runnable: 0, gapCount: 1 },
  });
  render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  expect(await screen.findByText('可运行 0 / 1')).toBeVisible();
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();
  expect(
    screen.getByRole('navigation', { name: '修复回测配置' }),
  ).toBeVisible();
});

it('fences an in-flight preflight when any input changes', async () => {
  const user = userEvent.setup();
  const pending = deferred<BacktestPreflight>();
  const client = api();
  vi.mocked(client.preflight).mockReturnValue(pending.promise);
  render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  expect(screen.getByRole('button', { name: '预检中…' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '4. 成本' }));
  await user.clear(screen.getByLabelText('滑点（基点）'));
  await user.type(screen.getByLabelText('滑点（基点）'), '2');
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  expect(screen.getByRole('button', { name: '运行预检' })).toBeEnabled();
  pending.resolve(preflight);
  await Promise.resolve();
  expect(screen.queryByText('可运行 2 / 3')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled();
});

it('does not navigate when submit resolves after unmount', async () => {
  const user = userEvent.setup();
  const pending = deferred<Awaited<ReturnType<BacktestApi['create']>>>();
  const client = api();
  vi.mocked(client.create).mockReturnValue(pending.promise);
  const onSubmitted = vi.fn();
  const mounted = render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
      onSubmitted={onSubmitted}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  await user.click(
    await screen.findByRole('checkbox', { name: /我确认本次仅回测/u }),
  );
  await user.click(screen.getByRole('button', { name: '提交回测' }));
  mounted.unmount();
  pending.resolve({
    runId: '33333333-3333-3333-3333-333333333333',
    snapshotId: `sha256:${'c'.repeat(64)}`,
    taskId: '44444444-4444-4444-4444-444444444444',
    warnings: [],
  });
  await Promise.resolve();
  expect(onSubmitted).not.toHaveBeenCalled();
});

it('rejects an exact decimal longer than the backend 64-character limit', async () => {
  const user = userEvent.setup();
  render(
    <BacktestWizard
      api={api()}
      formulaChoices={formulaChoices}
      initialState={{ ...completeState, minimumCommission: '1'.repeat(65) }}
    />,
  );
  await user.click(screen.getByRole('button', { name: '4. 成本' }));
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent('不超过 64 个字符');
  expect(alert).toHaveFocus();
});

it('freezes every wizard control while an already-sent create request settles', async () => {
  const user = userEvent.setup();
  const pending = deferred<Awaited<ReturnType<BacktestApi['create']>>>();
  const client = api();
  vi.mocked(client.create).mockReturnValue(pending.promise);
  const onSubmitted = vi.fn();
  render(
    <BacktestWizard
      api={client}
      formulaChoices={formulaChoices}
      initialState={completeState}
      onSubmitted={onSubmitted}
    />,
  );
  await user.click(screen.getByRole('button', { name: '5. 复核' }));
  await user.click(screen.getByRole('button', { name: '运行预检' }));
  await user.click(
    await screen.findByRole('checkbox', { name: /我确认本次仅回测/u }),
  );
  await user.click(screen.getByRole('button', { name: '提交回测' }));

  expect(screen.getByRole('button', { name: '4. 成本' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '上一步' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '提交中…' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '4. 成本' }));
  expect(screen.getByRole('heading', { name: '5. 复核' })).toBeVisible();

  pending.resolve({
    runId: '33333333-3333-3333-3333-333333333333',
    snapshotId: `sha256:${'c'.repeat(64)}`,
    taskId: '44444444-4444-4444-4444-444444444444',
    warnings: [],
  });
  await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
});
