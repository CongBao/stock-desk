import {
  type Dispatch,
  type SetStateAction,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import { safeUserMessage } from '../../shared/safeUserMessage';
import type {
  AnalysisApi,
  AnalysisOverview,
  ModelConfig,
  PreflightResult,
} from './analysisApi';
import { ModelSettings } from './ModelSettings';

const categoryLabels: Readonly<Record<string, string>> = {
  market: '行情数据',
  fundamentals: '基本面',
  announcements: '公告',
  news: '新闻',
};

const statusLabels: Readonly<Record<string, string>> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  partial: '部分完成',
  insufficient_evidence: '证据不足',
  failed: '失败',
  cancelled: '已取消',
};

export function AnalysisRunPanel({
  api,
  models,
  onModelsChange,
  history,
  nextCursor,
  onLoadMore,
  onOpenRun,
  onStarted,
}: {
  readonly api: AnalysisApi;
  readonly models: readonly ModelConfig[];
  readonly onModelsChange: Dispatch<SetStateAction<readonly ModelConfig[]>>;
  readonly history: readonly AnalysisOverview[];
  readonly nextCursor: string | null;
  readonly onLoadMore: () => void;
  readonly onOpenRun: (runId: string) => void;
  readonly onStarted: (runId: string) => void;
}) {
  const [symbol, setSymbol] = useState('');
  const [modelId, setModelId] = useState('');
  const [maxRetries, setMaxRetries] = useState('2');
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [busy, setBusy] = useState<'preflight' | 'start' | null>(null);
  const [message, setMessage] = useState('');
  const preflightGenerationRef = useRef(0);
  const preflightControllerRef = useRef<AbortController | null>(null);
  const verifiedModels = useMemo(
    () => models.filter((model) => model.status === 'verified'),
    [models],
  );
  const selectedModelIsVerified = verifiedModels.some(
    (model) => model.id === modelId,
  );
  const maxRetriesIsValid = /^[0-5]$/u.test(maxRetries);

  function invalidatePreflight() {
    preflightGenerationRef.current += 1;
    preflightControllerRef.current?.abort();
    preflightControllerRef.current = null;
    setPreflight(null);
    setBusy((current) => (current === 'preflight' ? null : current));
  }

  useEffect(
    () => () => {
      preflightGenerationRef.current += 1;
      preflightControllerRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (modelId === '' || selectedModelIsVerified) return;
    setModelId('');
    invalidatePreflight();
    setMessage('所选模型已不可用，请重新选择已验证模型并运行预检。');
  }, [modelId, selectedModelIsVerified]);

  async function runPreflight() {
    const generation = preflightGenerationRef.current + 1;
    preflightGenerationRef.current = generation;
    preflightControllerRef.current?.abort();
    const controller = new AbortController();
    preflightControllerRef.current = controller;
    setBusy('preflight');
    setMessage('');
    setPreflight(null);
    try {
      const result = await api.preflight(symbol, {
        signal: controller.signal,
      });
      if (
        preflightGenerationRef.current === generation &&
        !controller.signal.aborted
      )
        setPreflight(result);
    } catch (error) {
      if (
        preflightGenerationRef.current === generation &&
        !controller.signal.aborted
      )
        setMessage(safeUserMessage(error, '预检失败'));
    } finally {
      if (preflightGenerationRef.current === generation) {
        preflightControllerRef.current = null;
        setBusy(null);
      }
    }
  }

  async function start() {
    if (!maxRetriesIsValid) {
      setMessage('最大重试次数必须是 0 到 5 的整数。');
      return;
    }
    if (
      preflight === null ||
      preflight.symbol !== symbol ||
      !verifiedModels.some((model) => model.id === modelId)
    ) {
      setModelId('');
      setPreflight(null);
      setMessage('所选模型已不可用，请重新选择已验证模型并运行预检。');
      return;
    }
    setBusy('start');
    setMessage('');
    try {
      const submission = await api.start({
        symbol,
        modelConfigId: modelId,
        maxRetries: Number(maxRetries),
      });
      onStarted(submission.runId);
      setMessage('分析任务已提交，初始快照将在数据阶段完成后生成。');
    } catch (error) {
      setMessage(safeUserMessage(error, '分析启动失败'));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="analysis-run-panel"
      aria-labelledby="analysis-run-title"
    >
      <header>
        <div>
          <span className="page-kicker">RESEARCH CONTROL</span>
          <h3 id="analysis-run-title">新建分析</h3>
        </div>
        <ModelSettings
          api={api}
          models={models}
          onModelsChange={onModelsChange}
        />
      </header>
      <div className="analysis-run-controls">
        <label>
          股票代码
          <input
            aria-label="股票代码"
            value={symbol}
            placeholder="600000.SH"
            pattern="[0-9]{6}\.(SH|SZ|BJ)"
            onChange={(event) => {
              setSymbol(event.target.value.toUpperCase());
              invalidatePreflight();
            }}
          />
        </label>
        <label>
          已验证模型
          <select
            aria-label="已验证模型"
            value={modelId}
            onChange={(event) => {
              setModelId(event.target.value);
              invalidatePreflight();
            }}
          >
            <option value="">请选择</option>
            {verifiedModels.map((model) => (
              <option key={model.id} value={model.id}>
                {model.displayName} · {model.model}
              </option>
            ))}
          </select>
        </label>
        <label>
          最大重试次数
          <input
            aria-label="最大重试次数"
            type="number"
            min="0"
            max="5"
            value={maxRetries}
            onChange={(event) => {
              const next = event.target.value;
              setMaxRetries(next);
              setMessage(
                /^[0-5]$/u.test(next)
                  ? ''
                  : '最大重试次数必须是 0 到 5 的整数。',
              );
            }}
          />
        </label>
        <AsyncActionButton
          type="button"
          className="analysis-secondary-button"
          pending={busy === 'preflight'}
          disabled={busy !== null || !/^[0-9]{6}\.(SH|SZ|BJ)$/u.test(symbol)}
          onClick={() => void runPreflight()}
        >
          运行预检
        </AsyncActionButton>
        <AsyncActionButton
          type="button"
          className="analysis-primary-button"
          pending={busy === 'start'}
          disabled={
            busy !== null ||
            preflight === null ||
            preflight.symbol !== symbol ||
            !selectedModelIsVerified ||
            !maxRetriesIsValid
          }
          onClick={() => void start()}
        >
          启动智能分析
        </AsyncActionButton>
      </div>
      {preflight === null ? null : (
        <div className="preflight-diagnostics" aria-label="四类数据预检结果">
          {preflight.categories.map((category) => (
            <article key={category.kind} data-state={category.connectionState}>
              <div>
                <strong>
                  {categoryLabels[category.kind] ?? category.kind}
                </strong>
                <span>
                  {category.connectionState === 'available'
                    ? '可用'
                    : category.connectionState === 'degraded'
                      ? '降级'
                      : '缺失'}
                </span>
              </div>
              <p>
                路由：{category.routeSource} →{' '}
                {category.actualSource ?? '无可用来源'}
              </p>
              {category.permissionGap ? <p>存在权限缺口</p> : null}
              {category.missingReason === null ? null : (
                <p>{category.missingReason}</p>
              )}
              {category.recoveryCode === null ? null : (
                <small>恢复代码：{category.recoveryCode}</small>
              )}
            </article>
          ))}
          <p className="preflight-eligibility">
            {preflight.ratingEligible
              ? '数据覆盖满足评级门槛'
              : '数据覆盖不足，本次报告不会给出评级'}
          </p>
        </div>
      )}
      <p className="analysis-action-message" role="status" aria-live="polite">
        {message}
      </p>
      <section
        className="analysis-history"
        aria-labelledby="analysis-history-title"
      >
        <header>
          <h3 id="analysis-history-title">历史报告</h3>
          <span>按时间倒序</span>
        </header>
        <div className="analysis-history-scroll" aria-label="历史报告滚动区">
          {history.length === 0 ? (
            <p className="analysis-empty">暂无历史分析。</p>
          ) : (
            <ul>
              {history.map((run) => (
                <li key={run.runId}>
                  <button
                    type="button"
                    aria-label={`查看 ${run.symbol} ${new Date(run.createdAt).toLocaleDateString('zh-CN')} 报告`}
                    onClick={() => onOpenRun(run.runId)}
                  >
                    <span>
                      <strong>{run.symbol}</strong>
                      <small>{run.modelName}</small>
                    </span>
                    <span>
                      {statusLabels[run.status] ?? run.status}
                      <time dateTime={run.createdAt}>
                        {new Date(run.createdAt).toLocaleString('zh-CN')}
                      </time>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {nextCursor === null ? null : (
            <button
              type="button"
              className="analysis-more"
              onClick={onLoadMore}
            >
              加载更多历史报告
            </button>
          )}
        </div>
      </section>
    </section>
  );
}
