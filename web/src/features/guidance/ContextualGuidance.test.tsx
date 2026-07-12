import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { ContextualGuidance, GUIDANCE_TOURS } from './ContextualGuidance';
import type { GuidanceApi, GuidancePreferences } from './guidanceApi';

function preferences(
  pages: GuidancePreferences['pages'] = {},
): GuidancePreferences {
  return { schemaVersion: 1, revision: 0, pages };
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

  it('opens on first visit, traps focus, and persists completion', async () => {
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
});
