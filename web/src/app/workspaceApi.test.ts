import { createWorkspaceApi, WorkspaceProtocolError } from './workspaceApi';

const response = {
  schema_version: 1,
  revision: 7,
  updated_at: '2026-07-12T06:00:00Z',
  expires_at: '2027-01-08T06:00:00Z',
  restored: true,
  notice: null,
  workspace: {
    current_page: '/formulas',
    instrument: {
      symbol: '600000.SH',
      name: '浦发银行',
      exchange: 'SH',
      kind: 'stock',
    },
    period: '1w',
    adjustment: 'hfq',
    zoom: { start: 20, end: 80 },
    main_chart: 'candlestick',
    subchart: {
      kind: 'formula',
      formula_version_id: '00000000-0000-4000-8000-000000000001',
    },
  },
} as const;

it('strictly decodes the allowlisted workspace and emits the exact PUT contract', async () => {
  const get = vi.fn(() => Promise.resolve(response));
  const put = vi.fn(() => Promise.resolve({ ...response, revision: 8 }));
  const api = createWorkspaceApi({ get, put });

  const restored = await api.get();
  expect(restored.workspace).toEqual({
    currentPage: '/formulas',
    instrument: {
      symbol: '600000.SH',
      name: '浦发银行',
      exchange: 'SH',
      instrumentKind: 'stock',
    },
    period: '1w',
    adjustment: 'hfq',
    zoom: { start: 20, end: 80 },
    mainChart: 'candlestick',
    subchart: {
      kind: 'formula',
      formulaVersionId: '00000000-0000-4000-8000-000000000001',
    },
  });

  await api.put({
    expectedRevision: restored.revision,
    workspace: restored.workspace,
  });
  expect(put).toHaveBeenCalledWith('/v1/workspace', {
    body: {
      expected_revision: 7,
      current_page: '/formulas',
      instrument: {
        symbol: '600000.SH',
        name: '浦发银行',
        exchange: 'SH',
        kind: 'stock',
      },
      period: '1w',
      adjustment: 'hfq',
      zoom: { start: 20, end: 80 },
      main_chart: 'candlestick',
      subchart: {
        kind: 'formula',
        formula_version_id: '00000000-0000-4000-8000-000000000001',
      },
    },
    signal: undefined,
  });
  expect(JSON.stringify(vi.mocked(put).mock.calls[0])).not.toMatch(
    /token|session|https?:|[?#]/u,
  );
});

it.each([
  { ...response, extra: 'unknown' },
  {
    ...response,
    workspace: { ...response.workspace, current_page: 'https://evil.invalid' },
  },
  {
    ...response,
    workspace: { ...response.workspace, current_page: '/market?token=secret' },
  },
  {
    ...response,
    workspace: {
      ...response.workspace,
      subchart: { kind: 'formula', formula_version_id: '../../session' },
    },
  },
])(
  'rejects malformed, URL-bearing, or unknown workspace data',
  async (value) => {
    const api = createWorkspaceApi({
      get: vi.fn(() => Promise.resolve(value)),
      put: vi.fn(),
    });

    await expect(api.get()).rejects.toBeInstanceOf(WorkspaceProtocolError);
  },
);
