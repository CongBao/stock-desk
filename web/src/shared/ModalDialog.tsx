import {
  useLayoutEffect,
  useRef,
  type ComponentPropsWithoutRef,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from 'react';

const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

const modalStack: HTMLDialogElement[] = [];

type DialogProps = Omit<
  ComponentPropsWithoutRef<'dialog'>,
  'children' | 'onCancel' | 'onKeyDown' | 'open'
>;

export type ModalDialogProps = DialogProps & {
  readonly backdropClassName: string;
  readonly children: ReactNode;
  readonly fallbackFocusRef?: RefObject<HTMLElement | null>;
  readonly initialFocusRef?: RefObject<HTMLElement | null>;
  /** Omitting this callback makes Escape a safe no-op. */
  readonly onEscape?: () => void;
  readonly returnFocusRef?: RefObject<HTMLElement | null>;
};

function openModal(dialog: HTMLDialogElement) {
  if (dialog.open) return;
  if (typeof dialog.showModal === 'function') {
    dialog.showModal();
    return;
  }
  dialog.setAttribute('open', '');
}

function closeModal(dialog: HTMLDialogElement) {
  if (!dialog.open) return;
  if (typeof dialog.close === 'function') {
    try {
      dialog.close();
      return;
    } catch {
      // Fall through to the attribute fallback for partial dialog support.
    }
  }
  dialog.removeAttribute('open');
}

function removeFromStack(dialog: HTMLDialogElement) {
  const index = modalStack.lastIndexOf(dialog);
  if (index >= 0) modalStack.splice(index, 1);
}

function isInsideDisabledFieldset(element: HTMLElement): boolean {
  let searchFrom = element.parentElement;
  while (searchFrom !== null) {
    const fieldset =
      searchFrom.closest<HTMLFieldSetElement>('fieldset[disabled]');
    if (fieldset === null) return false;
    const firstLegend = Array.from(fieldset.children).find(
      (child) => child instanceof HTMLLegendElement,
    );
    if (firstLegend === undefined || !firstLegend.contains(element))
      return true;
    searchFrom = fieldset.parentElement;
  }
  return false;
}

function hasUnavailableAncestor(element: HTMLElement): boolean {
  let current: HTMLElement | null = element;
  while (current !== null) {
    if (
      current.hidden ||
      current.getAttribute('aria-hidden') === 'true' ||
      current.hasAttribute('inert') ||
      (current as HTMLElement & { inert?: boolean }).inert === true
    )
      return true;
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden') return true;
    current = current.parentElement;
  }
  return false;
}

function isDisabled(element: HTMLElement): boolean {
  return element.matches(':disabled') || isInsideDisabledFieldset(element);
}

function isTabbable(element: HTMLElement): boolean {
  return (
    element.tabIndex >= 0 &&
    !isDisabled(element) &&
    !hasUnavailableAncestor(element)
  );
}

function getTabbableControls(dialog: HTMLDialogElement): HTMLElement[] {
  return Array.from(
    dialog.querySelectorAll<HTMLElement>(focusableSelector),
  ).filter(isTabbable);
}

function canRestoreFocus(
  element: HTMLElement | null | undefined,
): element is HTMLElement {
  return (
    element !== null &&
    element !== undefined &&
    element.isConnected &&
    !isDisabled(element) &&
    !hasUnavailableAncestor(element)
  );
}

export function ModalDialog({
  backdropClassName,
  children,
  fallbackFocusRef,
  initialFocusRef,
  onEscape,
  returnFocusRef,
  ...dialogProps
}: ModalDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const capturedFocusRef = useRef<HTMLElement | null>(null);
  const capturedFocusOnceRef = useRef(false);
  const latestRef = useRef({
    fallbackFocusRef,
    initialFocusRef,
    onEscape,
    returnFocusRef,
  });

  useLayoutEffect(() => {
    latestRef.current = {
      fallbackFocusRef,
      initialFocusRef,
      onEscape,
      returnFocusRef,
    };
  });

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null || !dialog.open || modalStack.at(-1) !== dialog) return;

    const active = document.activeElement;
    const focusWasLost = active === null || active === document.body;
    const activeControlBecameUnavailable =
      active instanceof HTMLElement &&
      dialog.contains(active) &&
      (isDisabled(active) || hasUnavailableAncestor(active));
    if (focusWasLost || activeControlBecameUnavailable)
      dialog.focus({ preventScroll: true });
  });

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return undefined;

    if (!capturedFocusOnceRef.current) {
      capturedFocusOnceRef.current = true;
      capturedFocusRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
    }

    openModal(dialog);
    removeFromStack(dialog);
    modalStack.push(dialog);
    latestRef.current.initialFocusRef?.current?.focus();

    return () => {
      removeFromStack(dialog);
      const ownedFocus = dialog.contains(document.activeElement);
      closeModal(dialog);
      if (!ownedFocus) return;

      window.setTimeout(() => {
        if (
          document.activeElement !== document.body &&
          document.activeElement !== null
        )
          return;
        const preferredTargets = [
          latestRef.current.returnFocusRef?.current,
          latestRef.current.fallbackFocusRef?.current,
          capturedFocusRef.current,
        ];
        for (const target of preferredTargets) {
          if (!canRestoreFocus(target)) continue;
          target.focus();
          if (document.activeElement === target) return;
        }
      }, 0);
    };
  }, []);

  function isTopmost() {
    return modalStack.at(-1) === dialogRef.current;
  }

  function handleEscape(event: {
    preventDefault(): void;
    stopPropagation(): void;
  }) {
    event.preventDefault();
    if (!isTopmost()) return;
    event.stopPropagation();
    latestRef.current.onEscape?.();
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDialogElement>) {
    if (event.defaultPrevented) return;
    if (!isTopmost()) return;
    if (event.key === 'Escape') {
      handleEscape(event);
      return;
    }
    if (event.key !== 'Tab') return;

    const controls = getTabbableControls(event.currentTarget);
    const first = controls[0];
    const last = controls.at(-1);
    if (first === undefined || last === undefined) {
      event.preventDefault();
      return;
    }
    const active = document.activeElement;
    if (
      event.shiftKey &&
      (active === first || !event.currentTarget.contains(active))
    ) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      (active === last || !event.currentTarget.contains(active))
    ) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className={backdropClassName} role="presentation">
      <dialog
        {...dialogProps}
        ref={dialogRef}
        aria-modal="true"
        tabIndex={dialogProps.tabIndex ?? -1}
        onCancel={(event) => handleEscape(event)}
        onKeyDown={handleKeyDown}
      >
        {children}
      </dialog>
    </div>
  );
}
