import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { AnalysisPage } from './AnalysisPage';
import type {
  AnalysisApi,
  AnalysisDetail,
  AnalysisReport,
  ModelConfig,
} from './analysisApi';

const digest = (value: string) => `sha256:${value.repeat(64).slice(0, 64)}`;
const runId = '11111111-1111-1111-1111-111111111111';
const childRunId = '22222222-2222-2222-2222-222222222222';
const now = '2026-07-08T08:00:00Z';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const model: ModelConfig = {
  id: digest('a'),
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

const stages: AnalysisDetail['stages'] = [
  ['market', -4, 'data'],
  ['fundamentals', -3, 'data'],
  ['announcements', -2, 'data'],
  ['news', -1, 'data'],
  ['technical', 0, 'role'],
  ['fundamental_news', 1, 'role'],
  ['bull', 2, 'role'],
  ['bear', 3, 'role'],
  ['risk_decision', 4, 'role'],
].map(([stage, ordinal, kind]) => ({
  stage: String(stage),
  ordinal: Number(ordinal),
  kind: kind as 'data' | 'role',
  status: 'succeeded',
  attemptCount: 1,
  sourceRunId: null,
  failureCode: null,
  retryable: null,
  startedAt: now,
  finishedAt: now,
  durationMs: 120,
  retryAllowed: false,
}));

function detail(status = 'succeeded', hasReport = true): AnalysisDetail {
  return {
    runId,
    taskId: 'task-1',
    symbol: '600000.SH',
    parentRunId: null,
    requestedStage: null,
    status,
    taskStatus: status === 'running' ? 'running' : 'succeeded',
    progress: status === 'running' ? 0.5 : 1,
    cancelRequested: false,
    currentStage: status === 'running' ? 'bull' : null,
    snapshotId: digest('b'),
    reportId: hasReport ? digest('c') : null,
    failureCode: null,
    modelConfigId: model.id,
    modelProvider: model.provider,
    modelName: model.model,
    createdAt: now,
    updatedAt: now,
    startedAt: now,
    finishedAt: status === 'running' ? null : now,
    durationMs: status === 'running' ? null : 1000,
    stages,
  };
}

const evidence = {
  evidenceId: digest('d'),
  snapshotId: digest('b'),
  sectionId: digest('e'),
  sectionKind: 'fundamentals' as const,
  canonicalSource: 'tushare',
  sourceRecord: 'income:600000.SH:2025',
  sourceUrl: 'https://example.com/source',
  publishedAt: null,
  dataCutoff: now,
  fetchedAt: now,
  datasetVersion: '2026-07-08',
  excerpt: '净利润同比增长，现金流改善。',
  qualityFlags: [],
};

function report(status: AnalysisReport['status'] = 'complete'): AnalysisReport {
  const claim = {
    text: '盈利质量持续改善',
    evidenceIds: [evidence.evidenceId],
    stance: 'support' as const,
  };
  return {
    schemaVersion: 'analysis-report-v1',
    reportId: digest('c'),
    snapshotId: digest('b'),
    status,
    rating: status === 'complete' ? 'bullish' : null,
    confidence: status === 'complete' ? 0.78 : 0,
    confidenceExplanation: '证据覆盖关键财务与风险维度。',
    coreJudgments: [claim],
    bullClaims: [{ ...claim, text: '收入结构优化' }],
    bearClaims: [{ ...claim, text: '息差仍承压', stance: 'oppose' }],
    risks: [{ ...claim, text: '资产质量波动', stance: 'uncertain' }],
    evidenceItems: [evidence],
    roleOutputs: [],
    modelMetadata: [],
    qualityFlags: [],
    qualityNotes: [],
    missingModules: status === 'partial' ? ['bear'] : [],
    missingSections: status === 'insufficient_evidence' ? ['news'] : [],
    recoveryActions:
      status === 'insufficient_evidence' ? ['配置新闻数据源后重新分析'] : [],
    generatedAt: now,
    disclaimer:
      '本报告仅为研究辅助信息，不构成投资建议、个性化建议或交易指令。',
    retryActions:
      status === 'partial' ? [{ stage: 'bear', action: 'retry_stage' }] : [],
    failedModules: status === 'partial' ? ['bear'] : [],
    blockedModules: [],
    stageFailures:
      status === 'partial'
        ? [{ stage: 'bear', code: 'provider_timeout', attemptCount: 2 }]
        : [],
  };
}

function api(overrides: Partial<AnalysisApi> = {}): AnalysisApi {
  return {
    listModels: vi.fn().mockResolvedValue({ items: [model], nextCursor: null }),
    createModel: vi.fn(),
    createModelSuccessor: vi.fn(),
    testModel: vi.fn().mockResolvedValue({
      configId: model.id,
      connected: true,
      provider: model.provider,
      model: model.model,
      errorCode: null,
      status: 'verified',
      revision: 2,
      testedAt: now,
      lastTestedAt: now,
    }),
    disableModel: vi.fn(),
    preflight: vi.fn().mockResolvedValue({
      symbol: '600000.SH',
      previewSnapshotId: digest('f'),
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
    start: vi.fn().mockResolvedValue({
      runId,
      taskId: 'task-1',
      parentRunId: null,
      requestedStage: null,
      status: 'queued',
      snapshotId: null,
    }),
    listRuns: vi
      .fn()
      .mockResolvedValue({ items: [detail()], nextCursor: null }),
    getRun: vi.fn().mockResolvedValue(detail()),
    cancelRun: vi.fn().mockResolvedValue(detail('cancelled')),
    getReport: vi.fn().mockResolvedValue(report()),
    getEvidence: vi.fn().mockResolvedValue(evidence),
    retryStage: vi.fn().mockResolvedValue({
      runId: childRunId,
      taskId: 'task-2',
      parentRunId: runId,
      requestedStage: 'bear',
      status: 'queued',
      snapshotId: digest('b'),
    }),
    ...overrides,
  };
}

it('shows a complete report and synchronizes a selected claim with persisted evidence', async () => {
  const client = api();
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );

  expect(await screen.findByText('看多')).toBeInTheDocument();
  expect(screen.getByText('78%')).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole('button', { name: /盈利质量持续改善/u }),
  );
  expect(
    screen.getByRole('complementary', { name: '证据详情' }),
  ).toHaveTextContent('净利润同比增长，现金流改善。');
  expect(client.getEvidence).not.toHaveBeenCalled();
});

it('retries a failed partial module and follows the child run', async () => {
  const client = api({
    getReport: vi.fn().mockResolvedValue(report('partial')),
  });
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );
  await userEvent.click(
    await screen.findByRole('button', { name: '重试看空研究模块' }),
  );
  await waitFor(() =>
    expect(client.retryStage).toHaveBeenCalledWith(
      runId,
      'bear',
      expect.anything(),
    ),
  );
  await waitFor(() =>
    expect(client.getRun).toHaveBeenCalledWith(childRunId, expect.anything()),
  );
  expect(
    screen.getByText('已创建阶段重试子任务；当前正在显示该子任务。'),
  ).toBeInTheDocument();
});

it('omits rating for insufficient evidence and provides recovery actions', async () => {
  const client = api({
    getReport: vi.fn().mockResolvedValue(report('insufficient_evidence')),
  });
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );
  expect(await screen.findByText('证据不足，暂不评级')).toBeInTheDocument();
  expect(screen.queryByText('看多')).not.toBeInTheDocument();
  expect(screen.getByText('配置新闻数据源后重新分析')).toBeInTheDocument();
});

it.each(['partial', 'insufficient_evidence'] as const)(
  'treats %s as terminal, stops polling, and loads its report',
  async (status) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getRun = vi.fn().mockResolvedValue(detail(status));
    const getReport = vi.fn().mockResolvedValue(report(status));
    const client = api({ getRun, getReport });
    render(
      <AnalysisPage api={client} initialRunId={runId} pollIntervalMs={10} />,
    );

    expect(
      await screen.findByRole('heading', { name: '600000.SH 智能分析' }),
    ).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(500);
    expect(getRun).toHaveBeenCalledTimes(1);
    expect(getReport).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  },
);

it.each([
  ['failed', '分析失败'],
  ['cancelled', '分析已取消'],
] as const)(
  'shows explicit terminal state for %s without a report',
  async (status, label) => {
    const client = api({
      getRun: vi.fn().mockResolvedValue(detail(status, false)),
    });
    render(<AnalysisPage api={client} initialRunId={runId} />);
    expect(
      await screen.findByRole('heading', { name: label }),
    ).toBeInTheDocument();
    expect(screen.queryByText('等待研究报告')).not.toBeInTheDocument();
  },
);

it('retries a transient polling failure and eventually loads the terminal report', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const getRun = vi
    .fn()
    .mockRejectedValueOnce(new Error('temporary'))
    .mockResolvedValue(detail('partial'));
  const client = api({
    getRun,
    getReport: vi.fn().mockResolvedValue(report('partial')),
  });
  render(
    <AnalysisPage api={client} initialRunId={runId} pollIntervalMs={10} />,
  );
  await vi.advanceTimersByTimeAsync(100);
  expect(await screen.findByText('部分模块未完成')).toBeInTheDocument();
  expect(getRun).toHaveBeenCalledTimes(2);
  vi.useRealTimers();
});

it('runs four-category preflight and starts with a verified model', async () => {
  const client = api();
  render(<AnalysisPage api={client} />);
  await userEvent.type(
    screen.getByRole('textbox', { name: '股票代码' }),
    '600000.SH',
  );
  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: '已验证模型' }),
    model.id,
  );
  await userEvent.click(screen.getByRole('button', { name: '运行预检' }));
  expect(await screen.findByText('行情数据')).toBeInTheDocument();
  expect(screen.getByText('基本面')).toBeInTheDocument();
  expect(screen.getByText('公告')).toBeInTheDocument();
  expect(screen.getByText('新闻')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '启动智能分析' }));
  await waitFor(() => expect(client.start).toHaveBeenCalled());
  await waitFor(() =>
    expect(client.getRun).toHaveBeenCalledWith(runId, expect.anything()),
  );
});

it('polls a running analysis and can cancel it', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const client = api({ getRun: vi.fn().mockResolvedValue(detail('running')) });
  render(
    <AnalysisPage api={client} initialRunId={runId} pollIntervalMs={20} />,
  );
  expect(await screen.findByText('运行中')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '取消分析' }));
  await waitFor(() => expect(client.cancelRun).toHaveBeenCalled());
  vi.useRealTimers();
});

it('shows only masked model state, tests connection explicitly, and never renders a submitted secret', async () => {
  const createModel = vi.fn().mockResolvedValue(model);
  const client = api({ createModel });
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: '模型设置' }),
  );
  expect(screen.getByText('sk-a•••••••tail')).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText('API Key'), 'plaintext-secret');
  await userEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await waitFor(() => expect(createModel).toHaveBeenCalled());
  expect(screen.queryByText('plaintext-secret')).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole('button', { name: '测试 研究模型 连接' }),
  );
  expect(client.testModel).toHaveBeenCalled();
});

it('creates an immutable successor and keeps the original model configuration', async () => {
  const successor = {
    ...model,
    id: digest('9'),
    displayName: '研究模型 v2',
    status: 'unverified' as const,
    revision: 0,
    verifiedAt: null,
    lastTestedAt: null,
  };
  const createModelSuccessor = vi.fn().mockResolvedValue(successor);
  render(<AnalysisPage api={api({ createModelSuccessor })} />);
  await userEvent.click(
    await screen.findByRole('button', { name: '模型设置' }),
  );
  await userEvent.click(screen.getByRole('button', { name: '编辑 研究模型' }));
  expect(screen.getByLabelText('显示名称')).toHaveValue('研究模型');
  await userEvent.clear(screen.getByLabelText('显示名称'));
  await userEvent.type(screen.getByLabelText('显示名称'), '研究模型 v2');
  await userEvent.click(screen.getByRole('button', { name: '创建后继配置' }));
  await waitFor(() =>
    expect(createModelSuccessor).toHaveBeenCalledWith(
      model.id,
      expect.objectContaining({ displayName: '研究模型 v2' }),
    ),
  );
  expect(screen.getByText('研究模型 v2')).toBeInTheDocument();
  expect(screen.getAllByText('研究模型').length).toBeGreaterThan(0);
});

it('uses the tested revision when explicitly disabling a model', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const disableModel = vi.fn().mockResolvedValue({
    ...model,
    status: 'disabled',
    revision: 3,
  });
  const client = api({ disableModel });
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: '模型设置' }),
  );
  await userEvent.click(
    screen.getByRole('button', { name: '测试 研究模型 连接' }),
  );
  await waitFor(() => expect(client.testModel).toHaveBeenCalled());
  await userEvent.click(screen.getByRole('button', { name: '禁用 研究模型' }));
  expect(window.confirm).toHaveBeenCalled();
  expect(disableModel).toHaveBeenCalledWith(model.id, 2);
  await waitFor(() =>
    expect(
      screen.queryByRole('option', { name: /研究模型 · deepseek-chat/u }),
    ).not.toBeInTheDocument(),
  );
});

it('shows complete partial-module and selected evidence provenance details', async () => {
  const partial = {
    ...report('partial'),
    missingModules: ['technical'],
    blockedModules: ['risk_decision'],
  };
  const client = api({ getReport: vi.fn().mockResolvedValue(partial) });
  render(<AnalysisPage api={client} />);
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );
  expect(await screen.findByText(/失败：看空研究/u)).toBeInTheDocument();
  expect(screen.getByText(/缺失：技术研究/u)).toBeInTheDocument();
  expect(screen.getByText(/阻塞：风险决策/u)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: /息差仍承压/u }));
  const panel = screen.getByRole('complementary', { name: '证据详情' });
  expect(panel).toHaveTextContent('立场：反对');
  expect(panel).toHaveTextContent('发布时间：未提供');
  expect(panel).toHaveTextContent('质量标记：无');
  expect(panel).toHaveTextContent('来源路由：未提供');
  expect(panel).toHaveTextContent('income:600000.SH:2025');
});

it('opens model settings with focus and closes with Escape returning focus', async () => {
  render(<AnalysisPage api={api()} />);
  const trigger = await screen.findByRole('button', { name: '模型设置' });
  await userEvent.click(trigger);
  expect(screen.getByRole('button', { name: '关闭模型设置' })).toHaveFocus();
  await userEvent.keyboard('{Escape}');
  expect(
    screen.queryByRole('dialog', { name: '模型设置' }),
  ).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it('selects only one claim when two claims share the same evidence', async () => {
  const shared = {
    ...report(),
    coreJudgments: [
      {
        text: '共享证据判断甲',
        evidenceIds: [evidence.evidenceId],
        stance: 'support' as const,
      },
      {
        text: '共享证据判断乙',
        evidenceIds: [evidence.evidenceId],
        stance: 'uncertain' as const,
      },
    ],
  };
  render(
    <AnalysisPage
      api={api({ getReport: vi.fn().mockResolvedValue(shared) })}
    />,
  );
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );
  const core = screen.getByRole('heading', { name: '核心判断' }).parentElement;
  expect(core).not.toBeNull();
  if (core === null) return;
  expect(within(core).getAllByRole('button', { pressed: true })).toHaveLength(
    1,
  );
  await userEvent.click(
    within(core).getByRole('button', { name: /共享证据判断乙/u }),
  );
  expect(within(core).getAllByRole('button', { pressed: true })).toHaveLength(
    1,
  );
  expect(
    within(core).getByRole('button', { name: /共享证据判断乙/u }),
  ).toHaveAttribute('aria-pressed', 'true');
});

it('keeps successful history when model initialization fails', async () => {
  const client = api({
    listModels: vi.fn().mockRejectedValue(new Error('model unavailable')),
    listRuns: vi
      .fn()
      .mockResolvedValue({ items: [detail()], nextCursor: null }),
  });
  render(<AnalysisPage api={client} />);
  expect(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  ).toBeInTheDocument();
});

it('upserts a newly terminal run into history without duplicates', async () => {
  const terminal = detail('succeeded');
  const client = api({
    listRuns: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
    getRun: vi.fn().mockResolvedValue(terminal),
  });
  render(<AnalysisPage api={client} initialRunId={runId} />);
  expect(
    await screen.findByRole('heading', { name: '600000.SH 智能分析' }),
  ).toBeInTheDocument();
  expect(
    screen.getAllByRole('button', { name: /查看 600000.SH/u }),
  ).toHaveLength(1);
});

it('keeps a terminal history upsert when the stale initial page resolves later', async () => {
  const initialHistory =
    deferred<Awaited<ReturnType<AnalysisApi['listRuns']>>>();
  const client = api({
    listRuns: vi.fn().mockReturnValue(initialHistory.promise),
    getRun: vi.fn().mockResolvedValue(detail('succeeded')),
  });
  render(<AnalysisPage api={client} initialRunId={runId} />);

  expect(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  ).toBeInTheDocument();

  await act(async () => {
    initialHistory.resolve({ items: [], nextCursor: null });
    await initialHistory.promise;
  });

  expect(
    screen.getByRole('button', { name: /查看 600000.SH/u }),
  ).toBeInTheDocument();
});

it('does not send a second cancel while cancellation is already requested', async () => {
  const cancelling = { ...detail('running'), cancelRequested: true };
  const client = api({ getRun: vi.fn().mockResolvedValue(cancelling) });
  render(<AnalysisPage api={client} initialRunId={runId} />);
  const cancel = await screen.findByRole('button', { name: '取消处理中' });
  expect(cancel).toBeDisabled();
  cancel.removeAttribute('disabled');
  fireEvent.click(cancel);
  expect(client.cancelRun).not.toHaveBeenCalled();
});

it('returns evidence drawer focus to its originating claim and toolbar respectively', async () => {
  const scrollIntoView = vi.fn();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: scrollIntoView,
  });
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true }));
  render(<AnalysisPage api={api()} />);
  await userEvent.click(
    await screen.findByRole('button', { name: /查看 600000.SH/u }),
  );
  const claim = await screen.findByRole('button', { name: /息差仍承压/u });
  await userEvent.click(claim);
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
  await userEvent.click(screen.getByRole('button', { name: '关闭证据' }));
  expect(claim).toHaveFocus();

  const toolbar = screen.getByRole('button', { name: '查看证据' });
  await userEvent.click(toolbar);
  await userEvent.click(screen.getByRole('button', { name: '关闭证据' }));
  expect(toolbar).toHaveFocus();
  vi.unstubAllGlobals();
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});
