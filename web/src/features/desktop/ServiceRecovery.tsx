import { useState } from 'react';

import type {
  DesktopRecoveryReason,
  TauriDesktopBridge,
} from '../../app/desktopBridge';

const reasonMessages: Record<DesktopRecoveryReason, string> = {
  permission_denied: 'Stock Desk 无法初始化用户数据，请检查当前账户权限。',
  restart_limit_reached:
    '已达到安全重启上限。请打开诊断查看故障标识，或安全退出后再试。',
  sidecar_unavailable: '桌面服务暂时不可用，您的数据仍保留在本机。',
  startup_timeout: '桌面服务未能及时启动，您可以安全重试。',
  version_mismatch: '应用组件版本不一致，请重新安装当前版本。',
};

type ServiceRecoveryProps = {
  readonly bridge: TauriDesktopBridge;
  readonly reason: DesktopRecoveryReason;
  readonly canRestart: boolean;
  readonly onRestarting: () => Promise<void>;
};

export function ServiceRecovery({
  bridge,
  reason,
  canRestart,
  onRestarting,
}: ServiceRecoveryProps) {
  const [pendingAction, setPendingAction] = useState<
    'restart' | 'diagnostics' | 'exit' | null
  >(null);
  const [message, setMessage] = useState<string | null>(null);

  async function runAction(
    action: 'restart' | 'diagnostics' | 'exit',
    operation: () => Promise<void>,
  ) {
    if (pendingAction !== null) return;
    setPendingAction(action);
    setMessage(null);
    try {
      await operation();
    } catch {
      setMessage(
        action === 'restart'
          ? '服务暂时无法重启，请稍后重试或选择其他操作。'
          : '操作暂时无法完成，请稍后重试。',
      );
    } finally {
      setPendingAction(null);
    }
  }

  return (
    <main className="desktop-recovery" aria-labelledby="recovery-title">
      <section className="desktop-recovery-card">
        <span className="panel-kicker">STOCK DESK / RECOVERY</span>
        <h1 id="recovery-title">桌面服务需要恢复</h1>
        <p>{reasonMessages[reason]}</p>
        <p className="desktop-diagnostic-privacy">
          诊断包仅保存到本机，不会自动上传；不包含用户名、文件路径、会话凭证或原始日志。
        </p>
        <div className="desktop-recovery-actions">
          {canRestart ? (
            <button
              type="button"
              disabled={pendingAction !== null}
              onClick={() => void runAction('restart', onRestarting)}
            >
              重启服务
            </button>
          ) : null}
          <button
            type="button"
            disabled={pendingAction !== null}
            onClick={() =>
              void runAction('diagnostics', bridge.openDiagnostics)
            }
          >
            打开诊断
          </button>
          <button
            type="button"
            disabled={pendingAction !== null}
            onClick={() => void runAction('exit', bridge.requestExit)}
          >
            安全退出
          </button>
        </div>
        {message === null ? null : <p role="status">{message}</p>}
      </section>
    </main>
  );
}
