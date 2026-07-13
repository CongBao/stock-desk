import type { Page, Route } from '@playwright/test';

import { expect, test } from './fixtures';

const digest = (character: string) => `sha256:${character.repeat(64)}`;
const now = '2026-07-08T08:00:00Z';
const taskFinishedAt = '2026-07-08T08:00:01.080Z';
const modelId = digest('a');
const snapshotId = digest('b');
const completeRunId = '11111111-1111-1111-1111-111111111111';
const partialRunId = '22222222-2222-2222-2222-222222222222';
const insufficientRunId = '33333333-3333-3333-3333-333333333333';
const retryRunId = '44444444-4444-4444-4444-444444444444';
const analysisTaskId = '55555555-5555-4555-8555-555555555555';
const analysisTaskEventId = '66666666-6666-4666-8666-666666666666';
const secretTaskPayload = 'TASK-PAYLOAD-MUST-STAY-PRIVATE';

const model = {
  id: modelId,
  public_config_hash: modelId,
  display_name: 'E2E DeepSeek',
  provider: 'deepseek',
  base_url: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  temperature: 0.1,
  timeout: 90.0,
  max_output: 4096,
  api_key_configured: true,
  masked_api_key: 'sk-e•••••••-key',
  status: 'unverified',
  revision: 0,
  verified_at: null,
  last_tested_at: null,
  error_code: null,
  supersedes_id: null,
  created_at: now,
  updated_at: now,
};

const stageNames = [
  'market',
  'fundamentals',
  'announcements',
  'news',
  'technical',
  'fundamental_news',
  'bull',
  'bear',
  'risk_decision',
] as const;

function stages(
  status:
    | 'queued'
    | 'analysts'
    | 'reviewers'
    | 'risk'
    | 'succeeded'
    | 'partial'
    | 'insufficient',
) {
  return stageNames.map((stage, index) => {
    const failed = status === 'partial' && stage === 'bull';
    const blocked = status === 'partial' && stage === 'risk_decision';
    const evidenceBlocked = status === 'insufficient' && index >= 4;
    const running =
      (status === 'analysts' &&
        (stage === 'technical' || stage === 'fundamental_news')) ||
      (status === 'reviewers' && (stage === 'bull' || stage === 'bear')) ||
      (status === 'risk' && stage === 'risk_decision');
    const succeeded =
      status === 'succeeded' ||
      (status === 'partial' && index < 8 && !failed) ||
      (status === 'insufficient' && index < 4) ||
      (status === 'analysts' && index < 4) ||
      (status === 'reviewers' && index < 6) ||
      (status === 'risk' && index < 8);
    return {
      stage,
      ordinal: index - 4,
      kind: index < 4 ? 'data' : 'role',
      status: failed
        ? 'failed'
        : blocked || evidenceBlocked
          ? 'blocked'
          : running
            ? 'running'
            : succeeded
              ? 'succeeded'
              : 'pending',
      attempt_count: succeeded || failed || running ? 1 : 0,
      source_run_id: null,
      failure_code: failed
        ? 'model_timeout'
        : blocked || evidenceBlocked
          ? 'dependency_failed'
          : null,
      retryable: failed ? true : null,
      started_at: succeeded || failed || running ? now : null,
      finished_at: succeeded || failed ? now : null,
      duration_ms: succeeded || failed ? 120 : null,
      retry_allowed: failed,
    };
  });
}

function overview(
  runId: string,
  status: string,
  options: {
    readonly reportId?: string | null;
    readonly progress?: number;
    readonly currentStage?: string | null;
    readonly parentRunId?: string | null;
    readonly requestedStage?: string | null;
  } = {},
) {
  const terminal = ['succeeded', 'partial', 'insufficient_evidence'].includes(
    status,
  );
  return {
    run_id: runId,
    task_id: `task-${runId.slice(0, 8)}`,
    symbol: '600000.SH',
    parent_run_id: options.parentRunId ?? null,
    requested_stage: options.requestedStage ?? null,
    status,
    task_status: terminal ? 'succeeded' : status,
    progress: options.progress ?? (terminal ? 1 : 0),
    cancel_requested: false,
    current_stage: options.currentStage ?? null,
    snapshot_id: status === 'queued' ? null : snapshotId,
    report_id: options.reportId ?? null,
    failure_code: null,
    model_config_id: modelId,
    model_provider: 'deepseek',
    model_name: 'deepseek-chat',
    created_at: now,
    updated_at: now,
    started_at: status === 'queued' ? null : now,
    finished_at: terminal ? now : null,
    duration_ms: terminal ? 1080 : null,
  };
}

const primaryEvidence = {
  evidence_id: digest('d'),
  snapshot_id: snapshotId,
  section_id: digest('e'),
  section_kind: 'fundamentals',
  canonical_source: 'akshare',
  source_record: 'income:600000.SH:2025',
  source_url: 'https://example.com/fundamentals',
  published_at: null,
  data_cutoff: now,
  fetched_at: now,
  dataset_version: 'fixture-v1',
  excerpt: '净利润同比增长，现金流改善。',
  quality_flags: ['degraded_source'],
  route: { selected_source: 'akshare', degraded_from: 'tushare' },
};

const riskEvidence = {
  ...primaryEvidence,
  evidence_id: digest('f'),
  section_id: digest('1'),
  section_kind: 'news',
  source_record: 'news:600000.SH:1',
  source_url: 'https://example.com/news',
  published_at: now,
  excerpt: '行业息差仍有下行压力。',
  quality_flags: [],
  route: null,
};

const primaryClaim = {
  text: '盈利质量持续改善',
  evidence_ids: [primaryEvidence.evidence_id],
  stance: 'support',
};
const riskClaim = {
  text: '行业息差仍承压',
  evidence_ids: [riskEvidence.evidence_id],
  stance: 'oppose',
};

function report(
  status: 'complete' | 'partial' | 'insufficient_evidence',
  runId: string,
) {
  const complete = status === 'complete';
  const partial = status === 'partial';
  return {
    schema_version: 'analysis-report-v1',
    report_id: digest(runId[0] ?? '9'),
    snapshot_id: snapshotId,
    status,
    rating: complete ? 'bullish' : null,
    confidence: complete ? 0.78 : 0.0,
    confidence_explanation: complete
      ? '证据覆盖关键财务与风险维度。'
      : partial
        ? '看多研究失败，报告仅保留已完成内容。'
        : '关键基本面证据缺失，禁止输出评级。',
    core_judgments: complete || partial ? [primaryClaim] : [],
    bull_claims: complete ? [primaryClaim] : [],
    bear_claims: complete || partial ? [riskClaim] : [],
    risks: complete ? [riskClaim] : [],
    evidence_items: complete || partial ? [primaryEvidence, riskEvidence] : [],
    role_outputs: [],
    model_metadata: complete ? [{ model: 'deepseek-chat' }] : [],
    quality_flags: partial ? ['partial'] : [],
    quality_notes: [],
    missing_modules: partial ? ['bull', 'risk_decision'] : [],
    missing_sections:
      status === 'insufficient_evidence' ? ['fundamentals'] : [],
    recovery_actions:
      status === 'insufficient_evidence'
        ? ['配置基本面数据权限后重新运行预检。']
        : [],
    generated_at: now,
    disclaimer:
      '本报告仅为研究辅助信息，不构成投资建议、个性化建议或交易指令。',
    retry_actions: partial ? [{ stage: 'bull', action: 'retry_stage' }] : [],
    failed_modules: partial ? ['bull'] : [],
    blocked_modules: partial ? ['risk_decision'] : [],
    stage_failures: partial
      ? [{ stage: 'bull', code: 'model_timeout', attempt_count: 2 }]
      : [],
  };
}

async function fulfill(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

function createGate() {
  let permits = 0;
  const waiters: (() => void)[] = [];
  return {
    release() {
      const waiter = waiters.shift();
      if (waiter === undefined) permits += 1;
      else waiter();
    },
    async wait() {
      if (permits > 0) {
        permits -= 1;
        return;
      }
      await new Promise<void>((resolve) => waiters.push(resolve));
    },
  };
}

async function installFlowStubs(page: Page) {
  let configured = false;
  let verified = false;
  let completePolls = 0;
  const completePollGate = createGate();
  const detailRunIds: string[] = [];
  const retryRequests: { parentRunId: string; requestedStage: string }[] = [];
  const taskRequests: string[] = [];
  const isolatedModuleRequests: string[] = [];
  let unsafeTaskListArmed = false;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const rawPathname = new URL(request.url()).pathname;
    if (!rawPathname.startsWith('/api/')) {
      await route.fallback();
      return;
    }
    const pathname = decodeURIComponent(rawPathname);
    const method = request.method();
    if (/\/(?:formulas|backtests)(?:\/|$)/u.test(pathname)) {
      isolatedModuleRequests.push(request.url());
    }
    if (
      pathname === '/api/v1/onboarding/state' ||
      pathname === '/api/v1/workspace'
    ) {
      await route.fallback();
      return;
    }
    if (pathname.endsWith('/health')) {
      await fulfill(route, {
        name: 'stock-desk',
        status: 'ok',
        api_version: 'v1',
      });
      return;
    }
    if (pathname.endsWith('/settings/models') && method === 'GET') {
      await fulfill(route, {
        items: configured
          ? [{ ...model, status: verified ? 'verified' : 'unverified' }]
          : [],
        next_cursor: null,
      });
      return;
    }
    if (pathname.endsWith('/settings/models') && method === 'POST') {
      const serialized = request.postData() ?? '';
      expect(serialized).toContain('"timeout":90.0');
      expect(serialized).toContain('"temperature":0.1');
      expect(serialized).toContain('"api_key":"e2e-secret-key"');
      configured = true;
      await fulfill(route, model, 201);
      return;
    }
    if (pathname.endsWith(`/${modelId}/test`) && method === 'POST') {
      verified = true;
      await fulfill(route, {
        config_id: modelId,
        connected: true,
        provider: 'deepseek',
        model: 'deepseek-chat',
        error_code: null,
        status: 'verified',
        revision: 1,
        tested_at: now,
        last_tested_at: now,
      });
      return;
    }
    if (pathname.endsWith('/analysis/preflight') && method === 'POST') {
      await fulfill(route, {
        symbol: '600000.SH',
        preview_snapshot_id: digest('9'),
        reservation: false,
        rating_eligible: true,
        checked_at: now,
        categories: ['market', 'fundamentals', 'announcements', 'news'].map(
          (kind, index) => ({
            kind,
            critical: index < 2,
            connection_state: index === 0 ? 'available' : 'degraded',
            route_source: index === 0 ? 'market_cache' : 'tushare',
            actual_source: index === 0 ? 'tushare' : 'akshare',
            ordered_candidates: [],
            attempted_sources:
              index === 0 ? ['market_cache'] : ['tushare', 'akshare'],
            missing_reason: null,
            recovery_code: null,
            permission_gap: index > 0,
            data_cutoff: now,
            fetched_at: now,
            dataset_version: 'fixture-v1',
            quality_flags: index === 0 ? [] : ['degraded_source'],
          }),
        ),
      });
      return;
    }
    if (pathname.endsWith('/analysis') && method === 'POST') {
      await fulfill(
        route,
        {
          run_id: completeRunId,
          task_id: 'task-complete',
          parent_run_id: null,
          requested_stage: null,
          status: 'queued',
          snapshot_id: null,
        },
        202,
      );
      return;
    }
    if (pathname.endsWith('/analysis') && method === 'GET') {
      await fulfill(route, {
        items: [
          overview(partialRunId, 'partial', {
            reportId: report('partial', partialRunId).report_id,
          }),
          overview(insufficientRunId, 'insufficient_evidence', {
            reportId: report('insufficient_evidence', insufficientRunId)
              .report_id,
          }),
        ],
        next_cursor: null,
      });
      return;
    }
    if (pathname.endsWith('/tasks') && method === 'GET') {
      taskRequests.push(request.url());
      const taskUrl = new URL(request.url());
      expect(taskUrl.searchParams.get('view')).toBe('safe');
      const task = {
        id: analysisTaskId,
        kind: 'analysis.run',
        status: 'succeeded',
        progress: 1,
        cancel_requested: false,
        created_at: now,
        updated_at: taskFinishedAt,
        started_at: now,
        finished_at: taskFinishedAt,
        duration_ms: 1080,
        presentation: {
          label: '智能分析',
          stage: null,
          processed: null,
          total: null,
          failed: null,
          target: null,
        },
      };
      if (
        unsafeTaskListArmed &&
        taskUrl.searchParams.get('limit') === '100'
      ) {
        await fulfill(route, [
          {
            ...task,
            payload: { analysis_run_id: secretTaskPayload },
            result: { private_summary: secretTaskPayload },
            error: { private_detail: secretTaskPayload },
          },
        ]);
        return;
      }
      await fulfill(route, [task]);
      return;
    }
    if (pathname.endsWith('/tasks/metrics') && method === 'GET') {
      await fulfill(route, {
        total: 1,
        by_status: {
          queued: 0,
          running: 0,
          succeeded: 1,
          failed: 0,
          cancelled: 0,
        },
        failure_count: 0,
        completed_count: 1,
        average_duration_ms: 1080,
        min_duration_ms: 1080,
        max_duration_ms: 1080,
      });
      return;
    }
    if (
      pathname.endsWith(`/tasks/${analysisTaskId}/events`) &&
      method === 'GET'
    ) {
      taskRequests.push(request.url());
      expect(new URL(request.url()).searchParams.get('view')).toBe('safe');
      await fulfill(route, [
        {
          id: analysisTaskEventId,
          task_id: analysisTaskId,
          level: 'info',
          progress: 1,
          occurred_at: now,
          presentation: {
            label: '任务已完成',
            stage: null,
            processed: null,
            total: null,
            failed: null,
          },
        },
      ]);
      return;
    }
    if (pathname.endsWith(`/tasks/${analysisTaskId}`) && method === 'GET') {
      taskRequests.push(request.url());
      expect(new URL(request.url()).searchParams.get('view')).toBe('safe');
      await fulfill(route, {
        id: analysisTaskId,
        kind: 'analysis.run',
        status: 'succeeded',
        progress: 1,
        cancel_requested: false,
        created_at: now,
        updated_at: taskFinishedAt,
        started_at: now,
        finished_at: taskFinishedAt,
        duration_ms: 1080,
        presentation: {
          label: '智能分析',
          stage: null,
          processed: null,
          total: null,
          failed: null,
          target: null,
        },
      });
      return;
    }
    if (pathname.endsWith(`/${partialRunId}/stages/bull/retry`)) {
      retryRequests.push({
        parentRunId: partialRunId,
        requestedStage: 'bull',
      });
      await fulfill(
        route,
        {
          run_id: retryRunId,
          task_id: 'task-retry',
          parent_run_id: partialRunId,
          requested_stage: 'bull',
          status: 'queued',
          snapshot_id: snapshotId,
        },
        202,
      );
      return;
    }
    const reportMatch = pathname.match(/\/analysis\/([^/]+)\/report$/u);
    if (reportMatch) {
      const runId = reportMatch[1] ?? '';
      const status =
        runId === partialRunId
          ? 'partial'
          : runId === insufficientRunId
            ? 'insufficient_evidence'
            : 'complete';
      await fulfill(route, report(status, runId));
      return;
    }
    const detailMatch = pathname.match(/\/analysis\/([^/]+)$/u);
    if (detailMatch && method === 'GET') {
      const runId = detailMatch[1] ?? '';
      detailRunIds.push(runId);
      if (runId === completeRunId) {
        completePolls += 1;
        await completePollGate.wait();
        if (completePolls === 1) {
          await fulfill(route, {
            ...overview(runId, 'running', {
              progress: 0.44,
              currentStage: 'technical',
            }),
            stages: stages('analysts'),
          });
        } else if (completePolls === 2) {
          await fulfill(route, {
            ...overview(runId, 'running', {
              progress: 0.66,
              currentStage: 'bull',
            }),
            stages: stages('reviewers'),
          });
        } else if (completePolls === 3) {
          await fulfill(route, {
            ...overview(runId, 'running', {
              progress: 0.88,
              currentStage: 'risk_decision',
            }),
            stages: stages('risk'),
          });
        } else {
          await fulfill(route, {
            ...overview(runId, 'succeeded', {
              reportId: report('complete', runId).report_id,
            }),
            stages: stages('succeeded'),
          });
        }
        return;
      }
      if (runId === partialRunId) {
        await fulfill(route, {
          ...overview(runId, 'partial', {
            reportId: report('partial', runId).report_id,
          }),
          stages: stages('partial'),
        });
        return;
      }
      if (runId === insufficientRunId) {
        await fulfill(route, {
          ...overview(runId, 'insufficient_evidence', {
            reportId: report('insufficient_evidence', runId).report_id,
          }),
          stages: stages('insufficient'),
        });
        return;
      }
      await fulfill(route, {
        ...overview(runId, 'succeeded', {
          reportId: report('complete', runId).report_id,
          parentRunId: partialRunId,
          requestedStage: 'bull',
        }),
        stages: stages('succeeded').map((item) => ({
          ...item,
          status:
            item.stage === 'bull' || item.stage === 'risk_decision'
              ? 'succeeded'
              : 'reused',
          source_run_id:
            item.stage === 'bull' || item.stage === 'risk_decision'
              ? null
              : partialRunId,
        })),
      });
      return;
    }
    await fulfill(route, { code: 'not_found' }, 404);
  });
  return {
    releaseNextPoll: () => completePollGate.release(),
    detailRunIds,
    retryRequests,
    taskRequests,
    isolatedModuleRequests,
    armUnsafeTaskList: () => {
      unsafeTaskListArmed = true;
    },
    useSafeTaskList: () => {
      unsafeTaskListArmed = false;
    },
  };
}

test('configures a model and completes traceable analysis, retry, and insufficient flows', async ({
  page,
}) => {
  const flow = await installFlowStubs(page);
  await page.setViewportSize({ width: 1100, height: 960 });
  await page.goto('/analysis');

  await page.getByRole('button', { name: '模型设置' }).click();
  const dialog = page.getByRole('dialog', { name: '模型设置' });
  await dialog.getByLabel('显示名称').fill('E2E DeepSeek');
  await dialog.getByLabel('API Key').fill('e2e-secret-key');
  await dialog.getByRole('button', { name: '保存模型配置' }).click();
  await expect(dialog.getByRole('status')).toContainText('模型配置已安全保存');
  await expect(dialog.getByText('sk-e•••••••-key')).toBeVisible();
  await expect(dialog.getByText('e2e-secret-key')).toHaveCount(0);
  await dialog.getByRole('button', { name: '测试 E2E DeepSeek 连接' }).click();
  await expect(dialog.getByRole('status')).toContainText('连接测试通过');
  await expect(dialog.getByText('已验证', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭模型设置' }).click();

  await page.getByLabel('股票代码').fill('600000.SH');
  await page
    .getByLabel('已验证模型')
    .selectOption({ label: 'E2E DeepSeek · deepseek-chat' });
  await page.getByRole('button', { name: '运行预检' }).click();
  const preflight = page.getByLabel('四类数据预检结果');
  await expect(preflight).toContainText('行情数据');
  await expect(preflight).toContainText('基本面');
  await expect(preflight).toContainText('公告');
  await expect(preflight).toContainText('新闻');
  await expect(preflight).toContainText('数据覆盖满足评级门槛');

  await page.getByRole('button', { name: '启动智能分析' }).click();
  await page.getByRole('button', { name: '查看分析流程' }).click();
  const process = page.locator('#analysis-process-drawer[data-open="true"]');
  const stage = (label: string) => process.locator('li', { hasText: label });

  flow.releaseNextPoll();
  await expect(stage('技术研究')).toContainText('运行中');
  await expect(stage('基本面与新闻')).toContainText('运行中');

  flow.releaseNextPoll();
  await expect(stage('看多论证')).toContainText('运行中', { timeout: 6_000 });
  await expect(stage('看空论证')).toContainText('运行中');

  flow.releaseNextPoll();
  await expect(stage('风险与结论')).toContainText('运行中', { timeout: 8_000 });

  flow.releaseNextPoll();
  await expect(
    page.getByRole('heading', { name: '600000.SH 智能分析' }),
  ).toBeVisible({ timeout: 8_000 });
  await expect(page.getByText('看多', { exact: true })).toBeVisible();
  await expect(page.getByText('不构成投资建议')).toBeVisible();

  await page
    .getByRole('button', { name: /行业息差仍承压/u })
    .first()
    .click();
  const evidence = page.getByRole('complementary', { name: '证据详情' });
  await expect(evidence).toContainText('行业息差仍有下行压力');
  await expect(evidence).toContainText('立场：反对');
  await expect(evidence).toContainText('akshare');
  await expect(evidence).toContainText('数据截止');
  await expect(evidence).toContainText('采集时间');
  await expect(
    evidence.getByRole('link', { name: /打开来源页面/u }),
  ).toHaveAttribute('href', 'https://example.com/news');

  const history = page.getByLabel('历史报告滚动区');
  await history
    .locator('li', { hasText: '部分完成' })
    .getByRole('button')
    .click();
  await expect(page.getByLabel('部分报告缺失模块')).toContainText('看多研究');
  await page.getByRole('button', { name: '重试看多研究模块' }).click();
  await expect(page.getByText('已创建阶段重试子任务')).toBeVisible();
  expect(flow.retryRequests).toEqual([
    { parentRunId: partialRunId, requestedStage: 'bull' },
  ]);
  await expect.poll(() => flow.detailRunIds.at(-1)).toBe(retryRunId);
  await page.getByRole('button', { name: '查看分析流程' }).click();
  await expect(process).toContainText('阶段重试子运行');
  await expect(process).toContainText('父运行保持不可变');
  await expect(process).toContainText('重试阶段看多论证');
  await expect(stage('技术研究')).toContainText('已复用');
  await expect(stage('看多论证')).toContainText('已完成');
  await expect(stage('风险与结论')).toContainText('已完成');

  await history
    .locator('li', { hasText: '证据不足' })
    .getByRole('button')
    .click();
  await expect(page.getByText('证据不足，暂不评级')).toBeVisible();
  await expect(page.getByLabel('证据不足恢复建议')).toContainText(
    '配置基本面数据权限后重新运行预检',
  );

  for (const target of [
    'analysis-run',
    'analysis-process',
    'analysis-evidence',
    'analysis-conclusion',
  ]) {
    const anchor = page.locator(`[data-guidance-target="${target}"]`);
    await expect(anchor).toHaveCount(1);
    const box = await anchor.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(0);
    expect(box?.height ?? 0).toBeGreaterThan(0);
  }

  flow.armUnsafeTaskList();
  await page.getByRole('link', { name: '任务中心' }).click();
  await expect(page.getByRole('heading', { name: '任务中心' })).toBeVisible();
  await expect(
    page.getByRole('heading', { name: '任务列表暂不可用' }),
  ).toBeVisible();
  await expect(page.locator('body')).not.toContainText(secretTaskPayload);
  flow.useSafeTaskList();
  await page.getByRole('button', { name: '重新读取' }).click();
  await expect(
    page.getByRole('button', {
      name: new RegExp(`智能分析.*${analysisTaskId}`, 'u'),
    }),
  ).toBeVisible();
  await expect(page.getByText('安全任务摘要')).toBeVisible();
  await expect(page.locator('body')).not.toContainText(secretTaskPayload);
  for (const target of [
    'tasks-metrics',
    'tasks-filters',
    'tasks-list',
    'tasks-refresh',
  ]) {
    const anchor = page.locator(`[data-guidance-target="${target}"]`);
    await expect(anchor).toHaveCount(1);
    const box = await anchor.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(0);
    expect(box?.height ?? 0).toBeGreaterThan(0);
  }
  expect(flow.taskRequests.length).toBeGreaterThanOrEqual(3);
  expect(flow.isolatedModuleRequests).toEqual([]);
});
