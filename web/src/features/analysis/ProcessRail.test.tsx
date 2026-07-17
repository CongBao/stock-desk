import { render, screen, within } from '@testing-library/react';

import { ProcessRail } from './ProcessRail';
import type { AnalysisDetail } from './analysisApi';

const now = '2026-07-08T08:00:00Z';

function run(overrides: Partial<AnalysisDetail> = {}): AnalysisDetail {
  return {
    runId: '11111111-1111-1111-1111-111111111111',
    taskId: 'task-1',
    symbol: '600000.SH',
    parentRunId: null,
    requestedStage: null,
    status: 'succeeded',
    taskStatus: 'succeeded',
    progress: 1,
    cancelRequested: false,
    currentStage: null,
    snapshotId:
      'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    reportId: null,
    failureCode: null,
    modelConfigId:
      'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    modelProvider: 'deepseek',
    modelName: 'deepseek-chat',
    createdAt: now,
    updatedAt: now,
    startedAt: now,
    finishedAt: now,
    durationMs: 1_000,
    stages: [],
    ...overrides,
  };
}

describe('ProcessRail', () => {
  it('renders the empty guidance before a run is selected', () => {
    render(<ProcessRail run={null} />);

    expect(
      screen.getByRole('complementary', { name: '分析流程' }),
    ).toHaveTextContent('启动或打开历史分析后查看阶段状态。');
  });

  it('explains retry lineage, reused stages, failures, and safe fallbacks', () => {
    const { rerender } = render(
      <ProcessRail
        run={run({
          parentRunId: '00000000-0000-0000-0000-000000000000',
          requestedStage: 'bear',
          status: 'partial',
          taskStatus: 'running',
          snapshotId: null,
          stages: [
            {
              stage: 'custom_stage',
              ordinal: 2,
              kind: 'role',
              status: 'custom_status',
              attemptCount: 2,
              sourceRunId: null,
              failureCode: 'provider_timeout',
              retryable: true,
              startedAt: now,
              finishedAt: null,
              durationMs: null,
              retryAllowed: true,
            },
            {
              stage: 'market',
              ordinal: 1,
              kind: 'data',
              status: 'reused',
              attemptCount: 1,
              sourceRunId: '00000000-0000-0000-0000-000000000000',
              failureCode: null,
              retryable: null,
              startedAt: now,
              finishedAt: now,
              durationMs: 120.4,
              retryAllowed: false,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText('分析状态：').parentElement).toHaveTextContent(
      '分析状态：部分完成',
    );
    expect(screen.getByText('任务状态：运行中')).toBeInTheDocument();
    const items = screen.getAllByRole('listitem');
    expect(within(items[0]).getByText('行情快照')).toBeInTheDocument();
    expect(within(items[0]).getByText('已复用')).toBeInTheDocument();
    expect(
      within(items[0]).getByText('尝试 1 次 · 120 ms'),
    ).toBeInTheDocument();
    expect(within(items[1]).getByText('custom_stage')).toBeInTheDocument();
    expect(within(items[1]).getByText('custom_status')).toBeInTheDocument();
    expect(
      within(items[1]).getByText('失败代码：provider_timeout'),
    ).toBeInTheDocument();
    expect(screen.getByText('阶段重试子运行')).toBeInTheDocument();
    expect(screen.getByText('父任务保持不变')).toBeInTheDocument();
    expect(screen.getByText('看空论证')).toBeInTheDocument();
    expect(screen.getByText('运行前暂未生成')).toBeInTheDocument();

    rerender(
      <ProcessRail
        run={run({
          status: 'custom_run_status',
          taskStatus: 'custom_task_status',
          requestedStage: 'custom_retry_stage',
        })}
      />,
    );

    expect(screen.getByText('分析状态：').parentElement).toHaveTextContent(
      'custom_run_status',
    );
    expect(
      screen.getByText('任务状态：custom_task_status'),
    ).toBeInTheDocument();
    expect(screen.getByText('custom_retry_stage')).toBeInTheDocument();
  });
});
