import { useEffect, useRef } from 'react';

type ContextPanelProps = {
  readonly isOpen: boolean;
  readonly onClose: () => void;
};

export function ContextPanel({ isOpen, onClose }: ContextPanelProps) {
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
          <span className="neutral-badge">未接入</span>
        </div>
        <dl className="status-list">
          <div>
            <dt>行情数据</dt>
            <dd>等待 v0.2.0</dd>
          </div>
          <div>
            <dt>公式引擎</dt>
            <dd>等待 v0.3.0</dd>
          </div>
          <div>
            <dt>任务 API</dt>
            <dd>本阶段接入</dd>
          </div>
        </dl>
      </section>

      <section className="context-note" aria-labelledby="current-stage-title">
        <span className="note-index" aria-hidden="true">
          01
        </span>
        <div>
          <h3 id="current-stage-title">当前阶段</h3>
          <p>正在建立可访问、可扩展的应用壳层与本地服务基础。</p>
        </div>
      </section>

      <section className="context-note" aria-labelledby="data-note-title">
        <span className="note-index" aria-hidden="true">
          02
        </span>
        <div>
          <h3 id="data-note-title">数据说明</h3>
          <p>当前页面不连接数据源，也不展示证券价格或交易信号。</p>
        </div>
      </section>

      <div className="context-footer">
        <span className="context-footer-mark" aria-hidden="true" />
        <span>System boundary ready</span>
      </div>
    </aside>
  );
}
