import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { ContextualGuidance, GUIDANCE_TOURS } from './ContextualGuidance';
import type { GuidanceApi, GuidancePreferences } from './guidanceApi';
import theme from '../../app/theme.css?raw';

function preferences(
  pages: GuidancePreferences['pages'] = {},
): GuidancePreferences {
  return { schemaVersion: 1, revision: 0, pages };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function renderGuidance(
  api: GuidanceApi,
  path = '/market',
  target = 'market-search',
) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <button data-guidance-target={target}>目标控件</button>
      <ContextualGuidance api={api} />
    </MemoryRouter>,
  );
}

describe('contextual guidance', () => {
  it('defines independent versioned tours with 3-5 anchored steps', () => {
    expect(Object.keys(GUIDANCE_TOURS)).toEqual([
      'market',
      'formula',
      'backtest',
      'analysis',
      'tasks',
    ]);
    for (const tour of Object.values(GUIDANCE_TOURS)) {
      expect(tour.contentVersion).toBeGreaterThan(0);
      expect(tour.steps.length).toBeGreaterThanOrEqual(3);
      expect(tour.steps.length).toBeLessThanOrEqual(5);
      for (const step of tour.steps) {
        expect(step.target).toMatch(/^\[data-guidance-target=/);
        expect(step.expectedResult.length).toBeGreaterThan(0);
      }
    }
  });

  it('opens on first visit, traps focus in both directions, restores focus, and persists completion', async () => {
    const user = userEvent.setup();
    const put = vi.fn().mockResolvedValue({
      ...preferences({ market: { contentVersion: 1, status: 'completed' } }),
      revision: 1,
    });
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(preferences()),
      put,
    };
    renderGuidance(api);

    const dialog = await screen.findByRole('dialog', { name: '行情快速引导' });
    expect(dialog).toHaveTextContent('预期结果');
    const skip = screen.getByRole('button', { name: '跳过引导' });
    const next = screen.getByRole('button', { name: '下一步' });
    await waitFor(() => expect(next).toHaveFocus());
    await user.tab({ shift: true });
    expect(skip).toHaveFocus();
    await user.tab();
    expect(next).toHaveFocus();
    await user.tab();
    expect(skip).toHaveFocus();
    await user.tab();
    expect(next).toHaveFocus();

    for (
      let index = 1;
      index < GUIDANCE_TOURS.market.steps.length;
      index += 1
    ) {
      await user.click(screen.getByRole('button', { name: '下一步' }));
    }
    await user.click(screen.getByRole('button', { name: '完成引导' }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(
        expect.objectContaining({
          page: 'market',
          contentVersion: GUIDANCE_TOURS.market.contentVersion,
          status: 'completed',
        }),
        expect.anything(),
      ),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '帮助' })).toHaveFocus(),
    );
  });

  it('does not repeat a matching version but Help can reopen it without writes', async () => {
    const user = userEvent.setup();
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(
        preferences({
          market: {
            contentVersion: GUIDANCE_TOURS.market.contentVersion,
            status: 'dismissed',
          },
        }),
      ),
      put: vi.fn(),
    };
    renderGuidance(api);
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '帮助' }));
    await user.click(
      screen.getByRole('menuitem', { name: '重新打开行情引导' }),
    );
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.put).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '帮助' })).toHaveFocus(),
    );
  });

  it('closes only the Help menu on Escape when no tour modal is open', async () => {
    const user = userEvent.setup();
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(
        preferences({
          market: {
            contentVersion: GUIDANCE_TOURS.market.contentVersion,
            status: 'completed',
          },
        }),
      ),
      put: vi.fn(),
    };
    renderGuidance(api);
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    const help = screen.getByRole('button', { name: '帮助' });
    await user.click(help);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await user.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(help).toHaveFocus();
    expect(api.put).not.toHaveBeenCalled();
  });

  it('persists an automatic Escape as dismissed before safely closing', async () => {
    const user = userEvent.setup();
    const put = vi.fn().mockResolvedValue(
      preferences({
        market: {
          contentVersion: GUIDANCE_TOURS.market.contentVersion,
          status: 'dismissed',
        },
      }),
    );
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(preferences()),
      put,
    };
    renderGuidance(api);

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(
        expect.objectContaining({
          page: 'market',
          status: 'dismissed',
        }),
        expect.anything(),
      ),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '帮助' })).toHaveFocus(),
    );
  });

  it('keeps automatic persistence single-flight across repeated Escape and clicks', async () => {
    const user = userEvent.setup();
    const pending = deferred<GuidancePreferences>();
    const put = vi.fn().mockReturnValue(pending.promise);
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(preferences()),
      put,
    };
    renderGuidance(api);

    const dialog = await screen.findByRole('dialog', {
      name: '行情快速引导',
    });
    await user.keyboard('{Escape}{Escape}');

    expect(put).toHaveBeenCalledOnce();
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('正在保存引导进度…');
    await waitFor(() => expect(status).toHaveFocus());
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '跳过引导' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '跳过引导' }));
    expect(put).toHaveBeenCalledOnce();

    pending.resolve(
      preferences({
        market: {
          contentVersion: GUIDANCE_TOURS.market.contentVersion,
          status: 'dismissed',
        },
      }),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '帮助' })).toHaveFocus(),
    );
  });

  it('aborts an in-flight automatic persistence request on unmount', async () => {
    const user = userEvent.setup();
    const pending = deferred<GuidancePreferences>();
    let signal: AbortSignal | undefined;
    const put = vi.fn<GuidanceApi['put']>((_update, options) => {
      signal = options?.signal;
      return pending.promise;
    });
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(preferences()),
      put,
    };
    const view = renderGuidance(api);

    await screen.findByRole('dialog', { name: '行情快速引导' });
    await user.click(screen.getByRole('button', { name: '跳过引导' }));
    expect(signal?.aborted).toBe(false);

    view.unmount();
    expect(signal?.aborted).toBe(true);
    pending.resolve(preferences());
    await Promise.resolve();
    expect(put).toHaveBeenCalledOnce();
  });

  it('honors an inner handled Escape instead of closing the tour', async () => {
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(preferences()),
      put: vi.fn(),
    };
    renderGuidance(api);

    const dialog = await screen.findByRole('dialog');
    const next = screen.getByRole('button', { name: '下一步' });
    next.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') event.preventDefault();
    });
    fireEvent.keyDown(next, { key: 'Escape' });

    expect(dialog).toBeInTheDocument();
    expect(api.put).not.toHaveBeenCalled();
  });

  it('only re-prompts the page whose content version changed', async () => {
    const api: GuidanceApi = {
      get: vi.fn().mockResolvedValue(
        preferences({
          market: { contentVersion: 1, status: 'completed' },
          formula: {
            contentVersion: GUIDANCE_TOURS.formula.contentVersion,
            status: 'completed',
          },
        }),
      ),
      put: vi.fn(),
    };
    renderGuidance(api);
    expect(await screen.findByRole('dialog')).toHaveAccessibleName(
      '行情快速引导',
    );
  });

  it('keeps a 640x360 tour scrollable, viewport bounded, and action-reachable', () => {
    const guidanceStyles = theme.slice(
      theme.indexOf('.guidance-layer'),
      theme.indexOf("[data-guidance-active='true']"),
    );

    expect(guidanceStyles).toContain('display: contents');
    expect(guidanceStyles).toContain('position: fixed');
    expect(guidanceStyles).toContain('100dvh');
    expect(guidanceStyles).toContain('overflow: auto');
    expect(guidanceStyles).toContain('background: var(--surface-1)');
    expect(guidanceStyles).toContain('color: var(--text-primary)');
    expect(guidanceStyles).toContain('color: var(--text-secondary)');
    expect(guidanceStyles).not.toContain('var(--surface,');
    expect(guidanceStyles).toContain("[data-placement='top']");
    expect(guidanceStyles).toContain("[data-placement='bottom']");
    expect(guidanceStyles).toContain('.guidance-actions');
  });
});
