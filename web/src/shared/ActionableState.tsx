import { useId } from 'react';

import { safeDisplayCopy } from './safeUserMessage';

export type ActionableStateKind =
  | 'loading'
  | 'empty'
  | 'offline'
  | 'permission'
  | 'error'
  | 'sidecar-unavailable';

type ActionableStateProps = {
  readonly kind: ActionableStateKind;
  readonly title: string;
  readonly reason: string;
  readonly actionLabel: string;
  readonly onAction: () => void;
  readonly actionDisabledReason?: string;
  readonly failureId?: string;
};

const failureIdPattern = /^[a-z][a-z0-9_]{3,63}$/u;

export function ActionableState({
  kind,
  title,
  reason,
  actionLabel,
  onAction,
  actionDisabledReason,
  failureId,
}: ActionableStateProps) {
  const disabledReasonId = useId();
  const safeTitle = safeDisplayCopy(title, '暂时无法继续');
  const safeReason = safeDisplayCopy(
    reason,
    '暂时无法显示详细原因，请重新尝试。',
  );
  const safeActionLabel = safeDisplayCopy(actionLabel, '重新尝试');
  const safeDisabledReason =
    actionDisabledReason === undefined
      ? undefined
      : safeDisplayCopy(actionDisabledReason, '请稍后再试。');
  const safeFailureId =
    failureId !== undefined && failureIdPattern.test(failureId)
      ? failureId
      : undefined;

  return (
    <section
      className="actionable-state"
      data-state-kind={kind}
      role="status"
      aria-live={kind === 'loading' ? 'polite' : 'assertive'}
    >
      <span className="actionable-state-icon" aria-hidden="true" />
      <div className="actionable-state-copy">
        <h3>{safeTitle}</h3>
        <p>{safeReason}</p>
        {safeFailureId === undefined ? null : (
          <small>故障标识：{safeFailureId}</small>
        )}
      </div>
      <div className="actionable-state-action">
        <button
          type="button"
          disabled={safeDisabledReason !== undefined}
          aria-describedby={
            safeDisabledReason === undefined ? undefined : disabledReasonId
          }
          onClick={onAction}
        >
          {safeActionLabel}
        </button>
        {safeDisabledReason === undefined ? null : (
          <small id={disabledReasonId}>{safeDisabledReason}</small>
        )}
      </div>
    </section>
  );
}
