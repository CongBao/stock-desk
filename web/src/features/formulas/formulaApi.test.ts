import type { ApiClient, JsonValue } from '../../shared/api/client';
import {
  createFormulaApi,
  FormulaProtocolError,
  type FormulaCreateInput,
} from './formulaApi';

const DIGEST = `sha256:${'a'.repeat(64)}`;

function clientReturning(value: JsonValue): {
  readonly client: ApiClient;
  readonly get: ReturnType<typeof vi.fn>;
  readonly post: ReturnType<typeof vi.fn>;
} {
  const get = vi.fn(() => Promise.resolve(value));
  const post = vi.fn(() => Promise.resolve(value));
  return {
    client: { get, post, put: vi.fn(() => Promise.resolve(value)) },
    get,
    post,
  };
}

it('decodes bounded compatibility metadata for editor assistance', async () => {
  const payload = {
    compatibility_version: 'tdx-v1',
    official_reference: 'https://example.invalid/tdx-v1',
    fields: [
      {
        canonical_name: 'CLOSE',
        name: 'C',
        source_name: 'close',
        summary_zh: '收盘价',
        unit: 'price',
        value_type: 'number_series',
        scale_denominator: 1,
        scale_numerator: 1,
      },
    ],
    functions: [
      {
        category: 'statistics',
        dispatch_key: 'ema',
        future_behavior: 'past_only',
        max_args: 2,
        min_args: 2,
        name: 'EMA',
        parameters: [
          {
            name: 'X',
            accepted_kinds: ['number_series'],
            required: true,
            constant: false,
            minimum: null,
            maximum: null,
            constraints_zh: '',
          },
        ],
        result_kind: 'number_series',
        relations: [],
        semantics_zh: '指数加权',
        signature: 'EMA(X, N)',
        summary_zh: '指数移动平均',
      },
    ],
    parser_limits: {
      absolute_exponent: 10,
      ast_nodes: 100,
      identifier_chars: 64,
      nesting_depth: 16,
      numeric_literal_chars: 32,
      source_bytes: 64000,
      statements: 100,
    },
    runtime_semantics: {
      division_by_zero: 'null',
      json_numbers: 'finite',
      null_propagation: 'documented',
      numeric_storage: 'float64',
      provenance: 'fixed',
    },
    value_kind_hierarchy: { integer_scalar: ['scalar'] },
  } as const satisfies JsonValue;
  const { client } = clientReturning(payload);

  const result = await createFormulaApi(client).listFunctions();

  expect(result.functions[0]).toMatchObject({
    name: 'EMA',
    signature: 'EMA(X, N)',
    summaryZh: '指数移动平均',
  });
  expect(result.functions[0]?.parameters[0]?.constraintsZh).toBe('');
  expect(result.fields[0]).toMatchObject({ name: 'C', canonicalName: 'CLOSE' });
});

it('uses stable API paths and snake-case mutation payloads', async () => {
  const response = {
    id: 'formula-1',
    name: '自定义 MACD',
    formula_type: 'trading',
    placement: 'subchart',
    latest_version: 1,
    created_at: '2026-07-06T00:00:00Z',
    updated_at: '2026-07-06T00:00:00Z',
    draft: {
      formula_id: 'formula-1',
      revision: 1,
      source: 'X:CLOSE;',
      source_checksum: DIGEST,
      parameter_schema: {},
      diagnostics: [],
      executable_version_id: 'version-1',
      updated_at: '2026-07-06T00:00:00Z',
    },
  } as const satisfies JsonValue;
  const { client, post } = clientReturning(response);
  const input: FormulaCreateInput = {
    name: '自定义 MACD',
    formulaType: 'trading',
    placement: 'subchart',
    source: 'X:CLOSE;',
    parameterSchema: {},
  };

  await createFormulaApi(client).createFormula(input);

  expect(post).toHaveBeenCalledWith('/formulas', {
    body: {
      name: '自定义 MACD',
      formula_type: 'trading',
      placement: 'subchart',
      source: 'X:CLOSE;',
      parameter_schema: {},
    },
    signal: undefined,
  });
});

it('rejects malformed diagnostics instead of rendering untrusted positions', async () => {
  const { client } = clientReturning({
    valid: false,
    diagnostics: [
      {
        code: 'unsupported_function',
        function: 'UNKNOWN',
        explanation: 'unsupported',
        span: { line: 0, column: 1, end_line: 1, end_column: 2 },
        blocks_preview: true,
        blocks_save: true,
        blocks_backtest: true,
      },
    ],
  });

  await expect(
    createFormulaApi(client).validateFormula({
      formulaType: 'indicator',
      source: 'X:UNKNOWN(CLOSE);',
      parameterSchema: {},
    }),
  ).rejects.toBeInstanceOf(FormulaProtocolError);
});

it('accepts only JavaScript-safe integer defaults from formula responses', async () => {
  const templatePayload = (defaultValue: number): JsonValue => ({
    items: [
      {
        template_id: 'safe-integer',
        name: '安全整数',
        formula_type: 'indicator',
        placement: 'subchart',
        source: 'X:CLOSE+N;',
        parameter_schema: {
          N: { kind: 'integer', default: defaultValue },
        },
      },
    ],
  });

  const safe = await createFormulaApi(
    clientReturning(templatePayload(Number.MAX_SAFE_INTEGER)).client,
  ).listTemplates();
  expect(safe[0]?.parameterSchema['N']?.default).toBe(Number.MAX_SAFE_INTEGER);

  await expect(
    createFormulaApi(
      clientReturning(templatePayload(2 ** 53)).client,
    ).listTemplates(),
  ).rejects.toBeInstanceOf(FormulaProtocolError);
});

function summaryPayload(id: string): JsonValue {
  return {
    id,
    name: `公式 ${id}`,
    formula_type: 'indicator',
    placement: 'subchart',
    latest_version: 1,
    created_at: '2026-07-06T00:00:00Z',
    updated_at: '2026-07-06T00:00:00Z',
  };
}

function versionPayload(id: string, version: number): JsonValue {
  return {
    id,
    formula_id: 'formula-1',
    version,
    name: '历史公式',
    formula_type: 'indicator',
    placement: 'subchart',
    source: `X:CLOSE+${String(version)};`,
    parameter_schema: {},
    compatibility_version: 'tdx-v1',
    engine_version: 'formula-engine-v1',
    checksum: DIGEST,
    validation_result: [],
    copied_from_version_id: null,
    created_at: '2026-07-06T00:00:00Z',
  };
}

it('loads every bounded formula catalog page for selectors with more than 100 items', async () => {
  const firstItems = Array.from({ length: 100 }, (_, index) =>
    summaryPayload(`formula-${String(index + 1)}`),
  );
  const get = vi
    .fn()
    .mockResolvedValueOnce({ items: firstItems, next_cursor: 'formula-100' })
    .mockResolvedValueOnce({
      items: [summaryPayload('formula-101')],
      next_cursor: null,
    });
  const api = createFormulaApi({
    get,
    post: vi.fn(),
    put: vi.fn(),
  });

  const result = await api.listFormulas();

  expect(result.items).toHaveLength(101);
  expect(result.nextCursor).toBeNull();
  expect(get).toHaveBeenNthCalledWith(
    2,
    '/formulas?limit=100&cursor=formula-100',
    { signal: undefined },
  );
});

it('fails closed on a repeated formula catalog cursor', async () => {
  const get = vi.fn().mockResolvedValue({
    items: [summaryPayload('formula-1')],
    next_cursor: 'repeat',
  });
  const api = createFormulaApi({
    get,
    post: vi.fn(),
    put: vi.fn(),
  });

  await expect(api.listFormulas()).rejects.toBeInstanceOf(FormulaProtocolError);
  expect(get).toHaveBeenCalledTimes(2);
});

it('lists immutable formula versions through the bounded paginated endpoint', async () => {
  const get = vi
    .fn()
    .mockResolvedValueOnce({
      items: [versionPayload('version-1', 1)],
      next_cursor: 'version-1',
    })
    .mockResolvedValueOnce({
      items: [versionPayload('version-2', 2)],
      next_cursor: null,
    });
  const api = createFormulaApi({
    get,
    post: vi.fn(),
    put: vi.fn(),
  });

  const versions = await api.listVersions('formula-1');

  expect(versions.map((item) => item.version)).toEqual([1, 2]);
  expect(get).toHaveBeenNthCalledWith(
    2,
    '/formulas/formula-1/versions?limit=100&cursor=version-1',
    { signal: undefined },
  );
});
