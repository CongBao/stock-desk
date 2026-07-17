import type { ButtonHTMLAttributes, ReactNode } from 'react';

type AsyncActionButtonProps = Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'aria-busy'
> & {
  readonly pending: boolean;
  readonly children: ReactNode;
};

export function AsyncActionButton({
  pending,
  disabled,
  children,
  className,
  ...props
}: AsyncActionButtonProps) {
  return (
    <button
      {...props}
      className={['async-action-button', className].filter(Boolean).join(' ')}
      disabled={disabled === true || pending}
      aria-busy={pending || undefined}
    >
      {pending ? (
        <span
          className="async-action-spinner"
          data-testid="async-action-spinner"
          aria-hidden="true"
        />
      ) : null}
      <span>{children}</span>
    </button>
  );
}
