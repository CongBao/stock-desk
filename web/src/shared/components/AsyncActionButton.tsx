import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';

type AsyncActionButtonProps = Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'aria-busy'
> & {
  readonly pending: boolean;
  readonly children: ReactNode;
};

export const AsyncActionButton = forwardRef<
  HTMLButtonElement,
  AsyncActionButtonProps
>(function AsyncActionButton(
  { pending, disabled, children, className, ...props },
  ref,
) {
  return (
    <button
      {...props}
      ref={ref}
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
});
