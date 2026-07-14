import { StrictMode, useRef, useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ModalDialog } from './ModalDialog';

function DialogFixture({
  onEscape,
  onClose,
}: {
  readonly onEscape?: () => void;
  readonly onClose?: () => void;
}) {
  const initialFocusRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);

  return (
    <>
      <button ref={returnFocusRef} type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      {open ? (
        <ModalDialog
          backdropClassName="test-backdrop"
          aria-label="测试弹窗"
          initialFocusRef={initialFocusRef}
          returnFocusRef={returnFocusRef}
          onEscape={() => {
            onEscape?.();
            setOpen(false);
            onClose?.();
          }}
        >
          <button ref={initialFocusRef} type="button">
            第一个
          </button>
          <a href="#test">中间</a>
          <button type="button" onClick={() => setOpen(false)}>
            最后一个
          </button>
        </ModalDialog>
      ) : null}
    </>
  );
}

it('uses the native modal API and returns owned focus after close', async () => {
  const user = userEvent.setup();
  const showModalDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'showModal',
  );
  const closeDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'close',
  );
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  });
  const close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: showModal,
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value: close,
  });
  try {
    render(<DialogFixture />);

    const trigger = screen.getByRole('button', { name: '打开' });
    await user.click(trigger);
    expect(showModal).toHaveBeenCalledOnce();
    expect(screen.getByRole('dialog', { name: '测试弹窗' })).toHaveAttribute(
      'open',
    );
    expect(screen.getByRole('button', { name: '第一个' })).toHaveFocus();

    await user.click(screen.getByRole('button', { name: '最后一个' }));
    expect(close).toHaveBeenCalledOnce();
    await waitFor(() => expect(trigger).toHaveFocus());
  } finally {
    if (showModalDescriptor === undefined)
      delete (HTMLDialogElement.prototype as { showModal?: unknown }).showModal;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'showModal',
        showModalDescriptor,
      );
    if (closeDescriptor === undefined)
      delete (HTMLDialogElement.prototype as { close?: unknown }).close;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'close',
        closeDescriptor,
      );
  }
});

it('falls back to the open attribute without showModal support', async () => {
  const user = userEvent.setup();
  const descriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'showModal',
  );
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: undefined,
  });
  try {
    render(<DialogFixture />);
    await user.click(screen.getByRole('button', { name: '打开' }));
    expect(screen.getByRole('dialog', { name: '测试弹窗' })).toHaveAttribute(
      'open',
    );
  } finally {
    if (descriptor === undefined)
      delete (HTMLDialogElement.prototype as { showModal?: unknown }).showModal;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'showModal',
        descriptor,
      );
  }
});

it('traps Tab in both directions and lets the caller own Escape', async () => {
  const user = userEvent.setup();
  const onEscape = vi.fn();
  render(<DialogFixture onEscape={onEscape} />);

  await user.click(screen.getByRole('button', { name: '打开' }));
  const first = screen.getByRole('button', { name: '第一个' });
  const last = screen.getByRole('button', { name: '最后一个' });
  await user.tab({ shift: true });
  expect(last).toHaveFocus();
  await user.tab();
  expect(first).toHaveFocus();

  await user.keyboard('{Escape}');
  expect(onEscape).toHaveBeenCalledOnce();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('keeps Escape safe when the caller omits a close callback', async () => {
  const user = userEvent.setup();
  const initialFocusRef = { current: null as HTMLButtonElement | null };
  render(
    <ModalDialog
      backdropClassName="test-backdrop"
      aria-label="不可关闭"
      initialFocusRef={initialFocusRef}
    >
      <button ref={initialFocusRef} type="button">
        等待
      </button>
    </ModalDialog>,
  );

  await user.keyboard('{Escape}');
  const dialog = screen.getByRole('dialog', { name: '不可关闭' });
  expect(dialog).toBeInTheDocument();
  const cancelEvent = new Event('cancel', { cancelable: true });
  fireEvent(dialog, cancelEvent);
  expect(cancelEvent.defaultPrevented).toBe(true);
  expect(dialog).toBeInTheDocument();
});

it('moves focus to the modal when an async state disables the active control', async () => {
  const user = userEvent.setup();

  function BusyFixture() {
    const actionRef = useRef<HTMLButtonElement>(null);
    const [pending, setPending] = useState(false);
    return (
      <ModalDialog
        backdropClassName="test-backdrop"
        aria-label="异步弹窗"
        initialFocusRef={actionRef}
      >
        <p role="status">{pending ? '正在处理' : '可以操作'}</p>
        <button
          ref={actionRef}
          type="button"
          disabled={pending}
          onClick={() => setPending(true)}
        >
          开始处理
        </button>
      </ModalDialog>
    );
  }

  render(<BusyFixture />);
  const action = screen.getByRole('button', { name: '开始处理' });
  expect(action).toHaveFocus();
  await user.click(action);
  expect(action).toBeDisabled();
  expect(screen.getByRole('dialog', { name: '异步弹窗' })).toHaveFocus();
});

it('preserves visible programmatic focus with a negative tabindex across rerenders', async () => {
  const user = userEvent.setup();

  function StatusFixture() {
    const actionRef = useRef<HTMLButtonElement>(null);
    const statusRef = useRef<HTMLParagraphElement>(null);
    const [pending, setPending] = useState(false);
    return (
      <ModalDialog
        backdropClassName="test-backdrop"
        aria-label="状态焦点弹窗"
        initialFocusRef={actionRef}
      >
        <p ref={statusRef} role="status" tabIndex={-1}>
          {pending ? '正在处理' : '可以操作'}
        </p>
        <button
          ref={actionRef}
          type="button"
          disabled={pending}
          onClick={() => {
            statusRef.current?.focus();
            setPending(true);
          }}
        >
          开始异步处理
        </button>
      </ModalDialog>
    );
  }

  render(<StatusFixture />);
  await user.click(screen.getByRole('button', { name: '开始异步处理' }));
  expect(screen.getByRole('status')).toHaveFocus();
});

it('does not let a lower shared modal handle topmost keyboard input', async () => {
  const user = userEvent.setup();
  const lowerEscape = vi.fn();
  const upperEscape = vi.fn();
  render(
    <>
      <ModalDialog
        backdropClassName="test-backdrop"
        aria-label="下层"
        onEscape={lowerEscape}
      >
        <button type="button">下层操作</button>
      </ModalDialog>
      <ModalDialog
        backdropClassName="test-backdrop"
        aria-label="上层"
        onEscape={upperEscape}
      >
        <button type="button">上层操作</button>
      </ModalDialog>
    </>,
  );

  screen.getByRole('button', { name: '下层操作' }).focus();
  await user.keyboard('{Escape}');
  expect(lowerEscape).not.toHaveBeenCalled();
  screen.getByRole('button', { name: '上层操作' }).focus();
  await user.keyboard('{Escape}');
  expect(upperEscape).toHaveBeenCalledOnce();
});

it('does not refocus on StrictMode replay or callback-only rerenders', async () => {
  const user = userEvent.setup();
  const onEscape = vi.fn();

  function RerenderFixture() {
    const initialFocusRef = useRef<HTMLButtonElement>(null);
    const [revision, setRevision] = useState(0);
    return (
      <ModalDialog
        backdropClassName="test-backdrop"
        aria-label="稳定焦点"
        initialFocusRef={initialFocusRef}
        onEscape={() => {
          onEscape(revision);
        }}
      >
        <button ref={initialFocusRef} type="button">
          初始
        </button>
        <button type="button" onClick={() => setRevision((value) => value + 1)}>
          更新回调
        </button>
      </ModalDialog>
    );
  }

  render(
    <StrictMode>
      <RerenderFixture />
    </StrictMode>,
  );
  const rerender = screen.getByRole('button', { name: '更新回调' });
  await user.click(rerender);
  expect(rerender).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(onEscape).toHaveBeenCalledWith(1);
});

it('does not steal focus when focus was no longer owned at unmount', async () => {
  const user = userEvent.setup();
  const view = render(<DialogFixture />);
  const trigger = screen.getByRole('button', { name: '打开' });
  await user.click(trigger);
  const outside = document.createElement('button');
  document.body.append(outside);
  outside.focus();

  view.unmount();
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  expect(outside).toHaveFocus();
});

it('excludes hidden, inert, aria-hidden, negative-tabindex, and disabled-fieldset controls from both Tab boundaries', async () => {
  const user = userEvent.setup();
  const firstRef = { current: null as HTMLButtonElement | null };
  render(
    <ModalDialog
      backdropClassName="test-backdrop"
      aria-label="过滤不可用控件"
      initialFocusRef={firstRef}
    >
      <button type="button" hidden>
        hidden
      </button>
      <div style={{ display: 'none' }}>
        <button type="button">display none</button>
      </div>
      <div style={{ visibility: 'hidden' }}>
        <button type="button">visibility hidden</button>
      </div>
      <div aria-hidden="true">
        <button type="button">aria hidden</button>
      </div>
      <div inert>
        <button type="button">inert</button>
      </div>
      <fieldset disabled>
        <button type="button">fieldset disabled</button>
      </fieldset>
      <button type="button" tabIndex={-1}>
        negative tabindex
      </button>
      <button ref={firstRef} type="button">
        可用首项
      </button>
      <button type="button">可用末项</button>
      <div hidden>
        <button type="button">hidden last</button>
      </div>
    </ModalDialog>,
  );

  const first = screen.getByRole('button', { name: '可用首项' });
  const last = screen.getByRole('button', { name: '可用末项' });
  expect(first).toHaveFocus();
  await user.tab({ shift: true });
  expect(last).toHaveFocus();
  await user.tab();
  expect(first).toHaveFocus();
});

it('lets an inner popover consume Escape before the modal handles it', async () => {
  const user = userEvent.setup();
  const modalEscape = vi.fn();
  const popoverEscape = vi.fn();
  render(
    <ModalDialog
      backdropClassName="test-backdrop"
      aria-label="外层弹窗"
      onEscape={modalEscape}
    >
      <div role="dialog" aria-label="内层浮层">
        <button
          type="button"
          onKeyDown={(event) => {
            if (event.key !== 'Escape') return;
            event.preventDefault();
            popoverEscape();
          }}
        >
          关闭浮层
        </button>
      </div>
    </ModalDialog>,
  );

  screen.getByRole('button', { name: '关闭浮层' }).focus();
  await user.keyboard('{Escape}');
  expect(popoverEscape).toHaveBeenCalledOnce();
  expect(modalEscape).not.toHaveBeenCalled();
});

it('tries each valid focus-return candidate until focus is actually restored', async () => {
  const user = userEvent.setup();

  function FocusFallbackFixture() {
    const preferredRef = useRef<HTMLButtonElement>(null);
    const fallbackRef = useRef<HTMLButtonElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          打开焦点测试
        </button>
        <button ref={preferredRef} type="button">
          首选返回点
        </button>
        <button ref={fallbackRef} type="button">
          备用返回点
        </button>
        {open ? (
          <ModalDialog
            backdropClassName="test-backdrop"
            aria-label="焦点恢复"
            initialFocusRef={closeRef}
            returnFocusRef={preferredRef}
            fallbackFocusRef={fallbackRef}
          >
            <button ref={closeRef} type="button" onClick={() => setOpen(false)}>
              关闭焦点测试
            </button>
          </ModalDialog>
        ) : null}
      </>
    );
  }

  render(<FocusFallbackFixture />);
  await user.click(screen.getByRole('button', { name: '打开焦点测试' }));
  const preferred = screen.getByRole('button', { name: '首选返回点' });
  const preferredFocus = vi
    .spyOn(preferred, 'focus')
    .mockImplementation(() => undefined);
  await user.click(screen.getByRole('button', { name: '关闭焦点测试' }));

  const fallback = screen.getByRole('button', { name: '备用返回点' });
  await waitFor(() => expect(fallback).toHaveFocus());
  expect(preferredFocus).toHaveBeenCalledOnce();
});

it('skips hidden, disabled, and inert focus-return candidates', async () => {
  const user = userEvent.setup();

  function UnavailableFocusFixture() {
    const hiddenRef = useRef<HTMLButtonElement>(null);
    const disabledRef = useRef<HTMLButtonElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    const [triggerInert, setTriggerInert] = useState(false);
    return (
      <>
        <div inert={triggerInert || undefined}>
          <button
            type="button"
            onClick={() => {
              setOpen(true);
              setTriggerInert(true);
            }}
          >
            打开不可用焦点测试
          </button>
        </div>
        <button ref={hiddenRef} type="button" hidden>
          隐藏返回点
        </button>
        <button ref={disabledRef} type="button" disabled>
          禁用返回点
        </button>
        {open ? (
          <ModalDialog
            backdropClassName="test-backdrop"
            aria-label="不可用焦点恢复"
            initialFocusRef={closeRef}
            returnFocusRef={hiddenRef}
            fallbackFocusRef={disabledRef}
          >
            <button ref={closeRef} type="button" onClick={() => setOpen(false)}>
              关闭不可用焦点测试
            </button>
          </ModalDialog>
        ) : null}
      </>
    );
  }

  render(<UnavailableFocusFixture />);
  const trigger = screen.getByRole('button', { name: '打开不可用焦点测试' });
  const triggerFocus = vi.spyOn(trigger, 'focus');
  await user.click(trigger);
  triggerFocus.mockClear();
  const hidden = document.querySelector<HTMLButtonElement>('button[hidden]');
  const disabled = screen.getByRole('button', { name: '禁用返回点' });
  const hiddenFocus = vi.spyOn(hidden!, 'focus');
  const disabledFocus = vi.spyOn(disabled, 'focus');
  await user.click(screen.getByRole('button', { name: '关闭不可用焦点测试' }));
  await new Promise((resolve) => window.setTimeout(resolve, 0));

  expect(hiddenFocus).not.toHaveBeenCalled();
  expect(disabledFocus).not.toHaveBeenCalled();
  expect(triggerFocus).not.toHaveBeenCalled();
  expect(document.body).toHaveFocus();
});

it('fails closed when an available native showModal implementation throws', () => {
  const descriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'showModal',
  );
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    throw new DOMException('not allowed', 'InvalidStateError');
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: showModal,
  });
  const consoleError = vi
    .spyOn(console, 'error')
    .mockImplementation(() => undefined);
  try {
    expect(() =>
      render(
        <ModalDialog backdropClassName="test-backdrop" aria-label="拒绝降级">
          <button type="button">操作</button>
        </ModalDialog>,
      ),
    ).toThrow();
    const attemptedDialog = showModal.mock.contexts[0];
    expect(attemptedDialog).not.toHaveAttribute('open');
  } finally {
    consoleError.mockRestore();
    if (descriptor === undefined)
      delete (HTMLDialogElement.prototype as { showModal?: unknown }).showModal;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'showModal',
        descriptor,
      );
  }
});

it('keeps native showModal, close, and stack order balanced under StrictMode replay', async () => {
  const user = userEvent.setup();
  const showModalDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'showModal',
  );
  const closeDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    'close',
  );
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    if (this.open) throw new DOMException('already open', 'InvalidStateError');
    this.setAttribute('open', '');
  });
  const close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: showModal,
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value: close,
  });
  const lowerEscape = vi.fn();
  const upperEscape = vi.fn();

  function StrictStackFixture() {
    const [upperOpen, setUpperOpen] = useState(true);
    return (
      <>
        <ModalDialog
          backdropClassName="test-backdrop"
          aria-label="严格下层"
          onEscape={lowerEscape}
        >
          <button type="button">严格下层操作</button>
        </ModalDialog>
        {upperOpen ? (
          <StrictMode>
            <ModalDialog
              backdropClassName="test-backdrop"
              aria-label="严格上层"
              onEscape={() => {
                upperEscape();
                setUpperOpen(false);
              }}
            >
              <button type="button">严格上层操作</button>
            </ModalDialog>
          </StrictMode>
        ) : null}
      </>
    );
  }

  let view: ReturnType<typeof render> | undefined;
  try {
    view = render(
      <StrictMode>
        <StrictStackFixture />
      </StrictMode>,
    );
    expect(showModal.mock.calls.length).toBe(close.mock.calls.length + 2);

    screen.getByRole('button', { name: '严格上层操作' }).focus();
    await user.keyboard('{Escape}');
    expect(upperEscape).toHaveBeenCalledOnce();
    expect(showModal.mock.calls.length).toBe(close.mock.calls.length + 1);

    screen.getByRole('button', { name: '严格下层操作' }).focus();
    await user.keyboard('{Escape}');
    expect(lowerEscape).toHaveBeenCalledOnce();
    view.unmount();
    expect(close).toHaveBeenCalledTimes(showModal.mock.calls.length);
  } finally {
    view?.unmount();
    if (showModalDescriptor === undefined)
      delete (HTMLDialogElement.prototype as { showModal?: unknown }).showModal;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'showModal',
        showModalDescriptor,
      );
    if (closeDescriptor === undefined)
      delete (HTMLDialogElement.prototype as { close?: unknown }).close;
    else
      Object.defineProperty(
        HTMLDialogElement.prototype,
        'close',
        closeDescriptor,
      );
  }
});
