import { useEffect, useRef, useState, type ReactNode } from 'react';

import type { DesktopBridge } from '../../app/desktopBridge';
import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import { ModalDialog } from '../../shared/ModalDialog';

const desktopRecoveryApi = createApiClient();

type RecoveryStatus = {
  readonly required: boolean;
  readonly queued: number;
  readonly running: number;
  readonly analysis: number;
  readonly backtest: number;
  readonly market: number;
  readonly other: number;
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function decodeStatus(value: JsonValue | undefined): RecoveryStatus {
  if (
    !record(value) ||
    Object.keys(value).sort().join(',') !==
      'analysis,backtest,market,other,queued,required,running'
  )
    throw new TypeError('Invalid desktop recovery status');
  if (
    typeof value.required !== 'boolean' ||
    !Number.isSafeInteger(value.queued) ||
    (value.queued as number) < 0 ||
    !Number.isSafeInteger(value.running) ||
    (value.running as number) < 0 ||
    !Number.isSafeInteger(value.analysis) ||
    (value.analysis as number) < 0 ||
    !Number.isSafeInteger(value.backtest) ||
    (value.backtest as number) < 0 ||
    !Number.isSafeInteger(value.market) ||
    (value.market as number) < 0 ||
    !Number.isSafeInteger(value.other) ||
    (value.other as number) < 0 ||
    (value.analysis as number) +
      (value.backtest as number) +
      (value.market as number) +
      (value.other as number) !==
      (value.queued as number) + (value.running as number) ||
    value.required !== (value.queued as number) + (value.running as number) > 0
  )
    throw new TypeError('Invalid desktop recovery status');
  return {
    required: value.required,
    queued: value.queued as number,
    running: value.running as number,
    analysis: value.analysis as number,
    backtest: value.backtest as number,
    market: value.market as number,
    other: value.other as number,
  };
}

export function DesktopTaskRecovery({
  bridge,
  api = desktopRecoveryApi,
  children,
}: {
  readonly bridge: DesktopBridge;
  readonly api?: Pick<ApiClient, 'get' | 'post'>;
  readonly children: ReactNode;
}) {
  const [status, setStatus] = useState<RecoveryStatus | 'loading' | 'error'>(
    bridge.isDesktop
      ? 'loading'
      : {
          required: false,
          queued: 0,
          running: 0,
          analysis: 0,
          backtest: 0,
          market: 0,
          other: 0,
        },
  );
  const [pending, setPending] = useState(false);
  const [confirmAnalysisResume, setConfirmAnalysisResume] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const returnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!bridge.isDesktop) return;
    let active = true;
    void api
      .get('/desktop/recovery')
      .then((value) => {
        if (active) setStatus(decodeStatus(value));
      })
      .catch(() => {
        if (active) setStatus('error');
      });
    return () => {
      active = false;
    };
  }, [api, bridge.isDesktop, loadAttempt]);

  useEffect(() => {
    if (typeof status === 'object' && status.required) {
      (confirmAnalysisResume ? returnRef : cancelRef).current?.focus();
    }
  }, [status, confirmAnalysisResume]);

  if (!bridge.isDesktop || (typeof status === 'object' && !status.required))
    return children;
  if (status === 'loading') return <p role="status">正在检查未完成任务…</p>;
  if (status === 'error')
    return (
      <main className="desktop-task-recovery">
        <h1>无法检查未完成任务</h1>
        <p>
          任务数据仍保留在本机。可以重启本地服务后重试、打开诊断或安全退出。
        </p>
        <div className="desktop-recovery-actions">
          <button
            type="button"
            disabled={pending}
            onClick={() => {
              setPending(true);
              void bridge
                .restartService()
                .then(() => {
                  setStatus('loading');
                  setLoadAttempt((attempt) => attempt + 1);
                })
                .catch(() => undefined)
                .finally(() => setPending(false));
            }}
          >
            重启服务并重试
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={() => void bridge.openDiagnostics().catch(() => undefined)}
          >
            打开诊断
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={() => void bridge.requestExit().catch(() => undefined)}
          >
            安全退出
          </button>
        </div>
      </main>
    );

  async function resolve(choice: 'resume' | 'cancel', confirmedCost = false) {
    if (pending || typeof status !== 'object') return;
    if (choice === 'resume' && status.analysis > 0 && !confirmedCost) {
      setConfirmAnalysisResume(true);
      return;
    }
    setPending(true);
    try {
      const result =
        choice === 'resume'
          ? await api.post('/desktop/recovery/resume', {
              body: { confirm_analysis_cost: confirmedCost },
            })
          : await api.post('/desktop/recovery/cancel');
      if (
        !record(result) ||
        result.status !== (choice === 'resume' ? 'resumed' : 'cancelled')
      )
        throw new TypeError('Invalid desktop recovery response');
      setStatus({
        required: false,
        queued: 0,
        running: 0,
        analysis: 0,
        backtest: 0,
        market: 0,
        other: 0,
      });
    } catch {
      setPending(false);
      setStatus('error');
    }
  }

  return (
    <main
      className="desktop-task-recovery"
      aria-labelledby="task-recovery-title"
    >
      <ModalDialog
        backdropClassName="desktop-task-recovery-backdrop"
        className="desktop-task-recovery-dialog"
        aria-labelledby="task-recovery-title"
        initialFocusRef={cancelRef}
      >
        <span className="panel-kicker">STOCK DESK / RECOVERY</span>
        <h1 id="task-recovery-title">发现上次未完成的任务</h1>
        <p>
          {confirmAnalysisResume
            ? `其中 ${status.analysis} 个分析任务可能再次调用模型 API 并产生费用。确认后才会继续。`
            : '你可以从已保存的安全检查点继续，也可以取消这些任务。选择前不会自动执行任务。'}
        </p>
        <dl className="desktop-exit-counts">
          <div>
            <dt>排队任务</dt>
            <dd>{status.queued}</dd>
          </div>
          <div>
            <dt>运行任务</dt>
            <dd>{status.running}</dd>
          </div>
        </dl>
        <div className="desktop-recovery-actions">
          <button
            ref={cancelRef}
            type="button"
            disabled={pending}
            onClick={() => void resolve('cancel')}
          >
            取消未完成任务
          </button>
          {confirmAnalysisResume ? (
            <>
              <button
                ref={returnRef}
                type="button"
                disabled={pending}
                onClick={() => setConfirmAnalysisResume(false)}
              >
                返回
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => void resolve('resume', true)}
              >
                确认继续并产生费用
              </button>
            </>
          ) : (
            <button
              type="button"
              disabled={pending}
              onClick={() => void resolve('resume')}
            >
              继续未完成任务
            </button>
          )}
        </div>
      </ModalDialog>
    </main>
  );
}
