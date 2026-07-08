import { useEffect, useRef } from 'react';

import type {
  EndpointState,
  SystemStatus,
  TaskStatus,
} from '../shared/api/useSystemStatus';

type ContextPanelProps = {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly systemStatus: SystemStatus;
};

const endpointLabels: Record<EndpointState, string> = {
  checking: '检查中',
  available: '可用',
  protocol: '协议异常',
  unavailable: '暂不可用',
};

const overallLabels: Record<SystemStatus['overall'], string> = {
  checking: '检查中',
  healthy: '正常',
  degraded: '降级',
  unavailable: '不可用',
};

const taskStatusLabels: Record<TaskStatus, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已成功',
  failed: '已失败',
  cancelled: '已取消',
};
const taskStageLabels = {
  queued: '排队',
  executing: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
} as const;

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'short',
  timeStyle: 'medium',
});

function formatTimestamp(timestamp: string): string {
  return dateFormatter.format(new Date(timestamp));
}

export function ContextPanel({
  isOpen,
  onClose,
  systemStatus,
}: ContextPanelProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!isOpen) {
      return undefined;
    }

    const focusTimer = window.setTimeout(() => {
      closeButtonRef.current?.focus();
    }, 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };

    document.addEventListener('keydown', handleKeyDown);

    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, onClose]);

  return (
    <aside
      id="context-panel"
      className="context-panel"
      aria-label="上下文状态"
      data-open={String(isOpen)}
    >
      <header className="context-panel-header">
        <div>
          <span className="panel-kicker">CONTEXT</span>
          <h2>工作区状态</h2>
        </div>
        <button
          ref={closeButtonRef}
          className="panel-close"
          type="button"
          aria-label="关闭上下文面板"
          onClick={onClose}
        >
          <span aria-hidden="true">×</span>
        </button>
      </header>

      <section
        className="status-card"
        aria-labelledby="connection-status-title"
      >
        <div className="card-heading-row">
          <h3 id="connection-status-title">连接状态</h3>
          <span className="neutral-badge" data-state={systemStatus.overall}>
            {overallLabels[systemStatus.overall]}
          </span>
        </div>
        <dl className="status-list">
          <div>
            <dt className="visually-hidden">行情数据</dt>
            <dd>行情数据：按需读取本地缓存</dd>
          </div>
          <div>
            <dt className="visually-hidden">公式引擎</dt>
            <dd>公式引擎 tdx-v1 已就绪</dd>
          </div>
          <div>
            <dt className="visually-hidden">API 服务</dt>
            <dd>API 服务{endpointLabels[systemStatus.health]}</dd>
          </div>
          <div>
            <dt className="visually-hidden">任务存储</dt>
            <dd>任务存储{endpointLabels[systemStatus.tasks]}</dd>
          </div>
          <div>
            <dt className="visually-hidden">任务 Worker</dt>
            <dd>任务 Worker：未检测</dd>
          </div>
        </dl>
        <p className="last-checked">
          {systemStatus.checkedAt === null
            ? '最近检查：尚未完成'
            : `最近检查：${dateFormatter.format(systemStatus.checkedAt)}`}
        </p>
        <button
          className="status-retry"
          type="button"
          disabled={systemStatus.isRetryDisabled}
          onClick={() => void systemStatus.retry()}
        >
          重新检测
        </button>
      </section>

      <section className="recent-tasks" aria-labelledby="recent-tasks-title">
        <div className="card-heading-row">
          <h3 id="recent-tasks-title">近期任务</h3>
          <span className="task-count">最多 5 项</span>
        </div>
        {systemStatus.tasks === 'checking' ? (
          <p className="task-empty">正在读取近期任务</p>
        ) : systemStatus.tasks !== 'available' ? (
          <p className="task-empty">任务列表暂不可用</p>
        ) : systemStatus.recentTasks.length === 0 ? (
          <p className="task-empty">暂无近期任务</p>
        ) : (
          <ol className="task-list">
            {systemStatus.recentTasks.map((task) => {
              const statusLabel = taskStatusLabels[task.status];
              return (
                <li
                  key={task.id}
                  aria-label={`${task.kind} ${statusLabel}`}
                  data-status={task.status}
                >
                  <div className="task-heading">
                    <strong>{task.presentation.label}</strong>
                    <span>{statusLabel}</span>
                  </div>
                  <p>进度 {Math.round(task.progress * 100)}%</p>
                  <p className="task-id">任务 {task.id}</p>
                  <p>创建 {formatTimestamp(task.createdAt)}</p>
                  <p>更新 {formatTimestamp(task.updatedAt)}</p>
                  <p>
                    {task.finishedAt === null
                      ? '完成 未结束'
                      : `完成 ${formatTimestamp(task.finishedAt)}`}
                  </p>
                  {task.presentation.stage === null ? null : (
                    <p>
                      {taskStageLabels[task.presentation.stage]} ·{' '}
                      {task.presentation.processed}/{task.presentation.total} ·
                      失败 {task.presentation.failed}
                    </p>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <section className="context-note" aria-labelledby="current-stage-title">
        <span className="note-index" aria-hidden="true">
          01
        </span>
        <div>
          <h3 id="current-stage-title">当前阶段</h3>
          <p>行情数据工作区已接入搜索、股票池、K 线和来源追溯。</p>
        </div>
      </section>

      <section className="context-note" aria-labelledby="data-note-title">
        <span className="note-index" aria-hidden="true">
          02
        </span>
        <div>
          <h3 id="data-note-title">数据说明</h3>
          <p>行情页只读取已写入本地的缓存，不静默请求外部实时行情。</p>
        </div>
      </section>

      <div className="context-footer">
        <span className="context-footer-mark" aria-hidden="true" />
        <span>Market data boundary active</span>
      </div>
    </aside>
  );
}
