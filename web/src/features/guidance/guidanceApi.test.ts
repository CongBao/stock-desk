import { describe, expect, it, vi } from 'vitest';

import { createGuidanceApi, GuidanceProtocolError } from './guidanceApi';

describe('guidance api', () => {
  it('loads and compare-and-swaps versioned page preferences', async () => {
    const get = vi.fn().mockResolvedValue({
      schema_version: 1,
      revision: 2,
      pages: { market: { content_version: 1, status: 'completed' } },
    });
    const put = vi.fn().mockResolvedValue({
      schema_version: 1,
      revision: 3,
      pages: { market: { content_version: 2, status: 'dismissed' } },
    });
    const api = createGuidanceApi({ get, put });

    await expect(api.get()).resolves.toMatchObject({ revision: 2 });
    await expect(
      api.put({
        expectedRevision: 2,
        page: 'market',
        contentVersion: 2,
        status: 'dismissed',
      }),
    ).resolves.toMatchObject({ revision: 3 });
    expect(put).toHaveBeenCalledWith('/v1/guidance/preferences', {
      body: {
        expected_revision: 2,
        page: 'market',
        content_version: 2,
        status: 'dismissed',
      },
      signal: undefined,
    });
  });

  it('rejects malformed responses', async () => {
    const api = createGuidanceApi({
      get: vi.fn().mockResolvedValue({ revision: 0, pages: {} }),
      put: vi.fn(),
    });
    await expect(api.get()).rejects.toBeInstanceOf(GuidanceProtocolError);
  });
});
