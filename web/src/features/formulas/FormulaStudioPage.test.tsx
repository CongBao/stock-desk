import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode, type ComponentProps } from 'react';

import { ApiError } from '../../shared/api/client';
import type { MarketApi, MarketBarsResponse } from '../market/marketApi';
import {
  FormulaStudioPage,
  type FormulaStudioPageProps,
} from './FormulaStudioPage';
import type {
  FormulaApi,
  FormulaDetail,
  FormulaFunctionCatalog,
  FormulaPreview,
  FormulaTemplate,
  FormulaValidation,
} from './formulaApi';

vi.mock('@monaco-editor/react', () => ({
  loader: { config: vi.fn() },
  default: ({
    onChange,
    options,
    value,
  }: {
    readonly onChange?: (value: string | undefined) => void;
    readonly options?: { readonly ariaLabel?: string };
    readonly value?: string;
  }) => (
    <textarea
      aria-label={options?.ariaLabel}
      value={value}
      onChange={(event) => onChange?.(event.currentTarget.value)}
    />
  ),
}));

vi.mock('../market/MarketChart', () => ({
  MarketChart: ({
    errorMessage,
    formula,
  }: {
    readonly errorMessage?: string;
    readonly formula?: FormulaPreview;
  }) =>
    errorMessage === undefined ? (
      <div role="img" aria-label="公式预览图">
        {formula?.numericOutputs.map((output) => output.name).join(' / ')}
        {formula?.signals
          .filter((signal) => signal.values.some(Boolean))
          .map((signal) => signal.name)
          .join(' / ')}
      </div>
    ) : (
      <p role="alert">{errorMessage}</p>
    ),
}));

const template: FormulaTemplate = {
  templateId: 'builtin-macd',
  name: 'MACD 金叉 / 死叉',
  formulaType: 'trading',
  placement: 'subchart',
  source:
    'DIF:EMA(CLOSE,SHORT)-EMA(CLOSE,LONG);\nDEA:EMA(DIF,MID);\nMACD:(DIF-DEA)*2;\nBUY:CROSS(DIF,DEA);\nSELL:CROSS(DEA,DIF);',
  parameterSchema: {
    SHORT: { kind: 'integer', default: 12, label: '短周期' },
    LONG: { kind: 'integer', default: 26, label: '长周期' },
    MID: { kind: 'integer', default: 9, label: '信号周期' },
  },
};

const functions: FormulaFunctionCatalog = {
  compatibilityVersion: 'tdx-v1',
  officialReference: 'https://example.invalid/tdx-v1',
  fields: [
    {
      name: 'CLOSE',
      canonicalName: 'CLOSE',
      sourceName: 'close',
      summaryZh: '收盘价序列',
      unit: 'price',
      valueType: 'number_series',
    },
  ],
  functions: [
    {
      category: 'statistics',
      futureBehavior: 'past_only',
      name: 'EMA',
      signature: 'EMA(系列, 周期)',
      summaryZh: '指数移动平均',
      semanticsZh: '仅使用当前与历史数据。',
      parameters: [
        { name: 'X', required: true, constraintsZh: '数值序列' },
        { name: 'N', required: true, constraintsZh: '正整数周期' },
      ],
    },
    {
      category: 'signal',
      futureBehavior: 'past_only',
      name: 'CROSS',
      signature: 'CROSS(序列A, 序列B)',
      summaryZh: '向上穿越',
      semanticsZh: '当前周期向上穿越。',
      parameters: [],
    },
  ],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

const detail: FormulaDetail = {
  id: 'formula-1',
  name: 'MACD 金叉 / 死叉',
  formulaType: 'trading',
  placement: 'subchart',
  latestVersion: 1,
  createdAt: '2026-07-06T00:00:00Z',
  updatedAt: '2026-07-06T00:00:00Z',
  draft: {
    formulaId: 'formula-1',
    revision: 1,
    source: template.source,
    sourceChecksum: `sha256:${'a'.repeat(64)}`,
    parameterSchema: template.parameterSchema,
    diagnostics: [],
    executableVersionId: 'version-1',
    updatedAt: '2026-07-06T00:00:00Z',
  },
};

const preview: FormulaPreview = {
  schemaVersion: 'stock-desk-signal-series-v1',
  signalSeriesId: `sha256:${'d'.repeat(64)}`,
  formulaId: 'formula-1',
  formulaVersionId: 'version-1',
  formulaVersion: 1,
  formulaChecksum: `sha256:${'a'.repeat(64)}`,
  engineVersion: 'formula-engine-v1',
  compatibilityVersion: 'tdx-v1',
  symbol: '600000.SH',
  period: '1d',
  adjustment: 'qfq',
  source: 'baostock',
  datasetVersion: `sha256:${'1'.repeat(64)}`,
  routeVersion: `sha256:${'2'.repeat(64)}`,
  manifestRecordId: `sha256:${'3'.repeat(64)}`,
  dataCutoff: '2024-01-02T16:00:00Z',
  queryStart: '2024-01-01T16:00:00Z',
  queryEnd: '2024-01-03T16:00:00Z',
  timestamps: ['2024-01-01T16:00:00Z', '2024-01-02T16:00:00Z'],
  parameters: [{ name: 'SHORT', kind: 'integer', value: '12' }],
  numericOutputs: [
    { name: 'DIF', values: [null, 0.2], warmupNullCount: 1 },
    { name: 'DEA', values: [null, 0.1], warmupNullCount: 1 },
    { name: 'MACD', values: [null, 0.2], warmupNullCount: 1 },
  ],
  signals: [
    { name: 'BUY', values: [null, true], warmupNullCount: 1 },
    { name: 'SELL', values: [null, false], warmupNullCount: 1 },
  ],
  runtimeDiagnostics: [],
};

const bars = {
  query: {
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
    start: '2024-01-01T16:00:00Z',
    end: '2024-01-03T16:00:00Z',
  },
  bars: [
    {
      symbol: '600000.SH',
      timestamp: '2024-01-01T16:00:00Z',
      period: '1d',
      adjustment: 'qfq',
      open: 10,
      high: 11,
      low: 9,
      close: 10.5,
      priceText: { open: '10', high: '11', low: '9', close: '10.5' },
      volume: 1000,
      status: 'normal',
      direction: 'rise',
    },
    {
      symbol: '600000.SH',
      timestamp: '2024-01-02T16:00:00Z',
      period: '1d',
      adjustment: 'qfq',
      open: 10.5,
      high: 11,
      low: 10,
      close: 10.2,
      priceText: { open: '10.5', high: '11', low: '10', close: '10.2' },
      volume: 1100,
      status: 'normal',
      direction: 'fall',
    },
  ],
  coverage: {
    start: '2024-01-01T16:00:00Z',
    end: '2024-01-02T16:00:00Z',
  },
  formula: preview,
} as unknown as MarketBarsResponse;

function apiFixture(
  validation: FormulaValidation = { valid: true, diagnostics: [] },
) {
  const api: FormulaApi = {
    listFunctions: vi.fn(() => Promise.resolve(functions)),
    listTemplates: vi.fn(() => Promise.resolve([template])),
    listFormulas: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getFormula: vi.fn(() => Promise.resolve(detail)),
    listVersions: vi.fn(() =>
      Promise.resolve([
        {
          id: 'version-1',
          formulaId: 'formula-1',
          version: 1,
          name: detail.name,
          formulaType: detail.formulaType,
          placement: detail.placement,
          source: detail.draft.source,
          parameterSchema: detail.draft.parameterSchema,
          checksum: `sha256:${'a'.repeat(64)}`,
          engineVersion: 'formula-engine-v1',
          compatibilityVersion: 'tdx-v1',
          createdAt: '2026-07-06T00:00:00Z',
        },
      ]),
    ),
    validateFormula: vi.fn(() => Promise.resolve(validation)),
    createFormula: vi.fn(() => Promise.resolve(detail)),
    updateDraft: vi.fn(() => Promise.resolve(detail.draft)),
    saveFormula: vi.fn(() =>
      Promise.resolve({
        id: 'version-2',
        formulaId: 'formula-1',
        version: 2,
        name: detail.name,
        formulaType: detail.formulaType,
        placement: detail.placement,
        source: detail.draft.source,
        parameterSchema: detail.draft.parameterSchema,
        checksum: `sha256:${'b'.repeat(64)}`,
        engineVersion: 'formula-engine-v1',
        compatibilityVersion: 'tdx-v1',
        createdAt: '2026-07-06T00:01:00Z',
      }),
    ),
    copyFormula: vi.fn(() =>
      Promise.resolve({
        id: 'version-copy',
        formulaId: 'formula-copy',
        version: 1,
        name: `${detail.name} 副本`,
        formulaType: detail.formulaType,
        placement: detail.placement,
        source: detail.draft.source,
        parameterSchema: detail.draft.parameterSchema,
        checksum: `sha256:${'c'.repeat(64)}`,
        engineVersion: 'formula-engine-v1',
        compatibilityVersion: 'tdx-v1',
        createdAt: '2026-07-06T00:02:00Z',
      }),
    ),
    previewFormula: vi.fn(() => Promise.resolve(preview)),
  };
  return api;
}

function marketApiFixture() {
  return {
    getBars: vi.fn<MarketApi['getBars']>(() => Promise.resolve(bars)),
  } satisfies Pick<MarketApi, 'getBars'>;
}

function renderStudio(
  props: Partial<FormulaStudioPageProps> = {},
  api = apiFixture(),
  marketApiClient = marketApiFixture(),
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <FormulaStudioPage
        api={api}
        marketApiClient={marketApiClient}
        validationDebounceMs={1}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { ...result, api, marketApiClient };
}

function renderStudioStrict(
  props: Partial<FormulaStudioPageProps> = {},
  api = apiFixture(),
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <FormulaStudioPage
          api={api}
          marketApiClient={marketApiFixture()}
          validationDebounceMs={1}
          {...props}
        />
      </QueryClientProvider>
    </StrictMode>,
  );
}

it('inserts a function snippet from the searchable categorized library', async () => {
  const user = userEvent.setup();
  renderStudio({ initialSource: 'DIF:' });

  const search = await screen.findByRole('searchbox', {
    name: '搜索函数或模板',
  });
  await user.type(search, 'EMA');
  await user.click(screen.getByRole('button', { name: /EMA/ }));

  expect(screen.getByRole('textbox', { name: '通达信公式代码' })).toHaveValue(
    'DIF: EMA(系列, 周期)',
  );
  expect(screen.getByText('指数移动平均')).toBeVisible();
});

it('debounces validation, locates line diagnostics, and blocks saving invalid source', async () => {
  const user = userEvent.setup();
  const invalid: FormulaValidation = {
    valid: false,
    diagnostics: [
      {
        code: 'unsupported_function',
        functionName: 'UNKNOWN',
        explanation: '不支持函数 UNKNOWN',
        span: { line: 1, column: 3, endLine: 1, endColumn: 10 },
        blocksPreview: true,
        blocksSave: true,
        blocksBacktest: true,
      },
    ],
  };
  const api = apiFixture(invalid);
  renderStudio({ initialSource: 'X:UNKNOWN(CLOSE);' }, api);

  await user.click(screen.getByRole('button', { name: '立即校验' }));

  expect(await screen.findByText('不支持函数 UNKNOWN')).toBeVisible();
  expect(screen.getByText('第 1 行，第 3 列')).toBeVisible();
  expect(screen.getByRole('button', { name: '保存为新版本' })).toBeDisabled();
  expect(api.validateFormula).toHaveBeenCalledWith(
    expect.objectContaining({ source: 'X:UNKNOWN(CLOSE);' }),
    expect.anything(),
  );
});

it('saves a valid formula revision before running an explicit aligned preview', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  const marketApiClient = marketApiFixture();
  renderStudio({ initialSource: template.source }, api, marketApiClient);

  await screen.findByText('EMA');
  await waitFor(() => expect(api.validateFormula).toHaveBeenCalled());
  await user.clear(screen.getByRole('spinbutton', { name: '短周期' }));
  await user.type(screen.getByRole('spinbutton', { name: '短周期' }), '10');
  await user.click(screen.getByRole('button', { name: '保存为新版本' }));
  await user.click(await screen.findByRole('button', { name: '运行预览' }));

  const createCall = vi.mocked(api.createFormula).mock.calls[0];
  expect(createCall?.[0].formulaType).toBe('trading');
  expect(createCall?.[0].placement).toBe('subchart');
  expect(createCall?.[0].parameterSchema['SHORT']?.default).toBe(10);
  expect(marketApiClient.getBars).toHaveBeenCalledWith(
    expect.objectContaining({
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
      formulaVersionId: 'version-1',
    }),
  );
  expect(
    vi.mocked(marketApiClient.getBars).mock.calls[0]?.[0].formulaParameters,
  ).toMatchObject({ SHORT: 10 });
  expect(api.previewFormula).not.toHaveBeenCalled();
  expect(
    await screen.findByRole('img', { name: '公式预览图' }),
  ).toHaveTextContent('DIF / DEA / MACD');
  expect(screen.getByRole('img', { name: '公式预览图' })).toHaveTextContent(
    'BUY',
  );
});

it('persists an invalid existing formula as a revisioned draft without publishing it', async () => {
  const user = userEvent.setup();
  const invalid: FormulaValidation = {
    valid: false,
    diagnostics: [
      {
        code: 'parse_error',
        functionName: null,
        explanation: '语法错误',
        span: { line: 1, column: 1, endLine: 1, endColumn: 2 },
        blocksPreview: true,
        blocksSave: true,
        blocksBacktest: true,
      },
    ],
  };
  const api = apiFixture(invalid);
  vi.mocked(api.updateDraft).mockResolvedValue({
    ...detail.draft,
    revision: 2,
    source: 'INVALID',
    diagnostics: invalid.diagnostics,
    executableVersionId: null,
  });
  renderStudio({ initialFormula: detail }, api);

  const editor = screen.getByRole('textbox', { name: '通达信公式代码' });
  await user.clear(editor);
  await user.type(editor, 'INVALID');
  await waitFor(() => expect(api.validateFormula).toHaveBeenCalled());
  await user.click(screen.getByRole('button', { name: '保存草稿' }));

  const updateCall = vi.mocked(api.updateDraft).mock.calls[0];
  expect(updateCall?.[0]).toBe('formula-1');
  expect(updateCall?.[1]).toMatchObject({
    source: 'INVALID',
    expectedRevision: 1,
  });
  expect(updateCall?.[2]?.signal).toBeInstanceOf(AbortSignal);
  expect(api.saveFormula).not.toHaveBeenCalled();
  expect(await screen.findByText('草稿已保存 · 修订 2')).toBeVisible();
});

it('shows immutable historical versions without replacing the editable draft', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  vi.mocked(api.listVersions).mockResolvedValue([
    {
      id: 'version-old',
      formulaId: detail.id,
      version: 1,
      name: detail.name,
      formulaType: detail.formulaType,
      placement: detail.placement,
      source: 'OLD:CLOSE;',
      parameterSchema: {},
      compatibilityVersion: 'tdx-v1',
      engineVersion: 'formula-engine-v1',
      checksum: `sha256:${'e'.repeat(64)}`,
      createdAt: '2026-07-05T00:00:00Z',
    },
  ]);
  renderStudio({ initialFormula: detail }, api);

  await screen.findByRole('option', { name: /v1 · 2026-07-05/u });
  await user.selectOptions(
    screen.getByRole('combobox', { name: '查看历史版本' }),
    'version-old',
  );

  expect(screen.getByRole('textbox', { name: '历史版本公式源码' })).toHaveValue(
    'OLD:CLOSE;',
  );
  expect(screen.getByRole('textbox', { name: '通达信公式代码' })).toHaveValue(
    detail.draft.source,
  );
});

it('discards an in-flight atomic preview immediately when its query changes', async () => {
  const user = userEvent.setup();
  let resolveBars: ((value: MarketBarsResponse) => void) | undefined;
  const marketApiClient = {
    getBars: vi.fn(
      () =>
        new Promise<MarketBarsResponse>((resolve) => {
          resolveBars = resolve;
        }),
    ),
  } satisfies Pick<MarketApi, 'getBars'>;
  renderStudio({ initialFormula: detail }, apiFixture(), marketApiClient);

  await user.click(screen.getByRole('button', { name: '运行预览' }));
  await user.clear(screen.getByRole('textbox', { name: '预览证券代码' }));
  await user.type(
    screen.getByRole('textbox', { name: '预览证券代码' }),
    '000001.SZ',
  );
  resolveBars?.(bars);

  await waitFor(() => expect(screen.getByText('尚未运行')).toBeVisible());
  expect(screen.queryByText(/1 个买点/u)).not.toBeInTheDocument();
});

it('does not let a stale formula load overwrite edits made while it is pending', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  const secondSummary = {
    ...detail,
    id: 'formula-2',
    name: '第二个公式',
    draft: { ...detail.draft, formulaId: 'formula-2' },
  };
  vi.mocked(api.listFormulas).mockResolvedValue({
    items: [secondSummary],
    nextCursor: null,
  });
  let resolveLoad: ((value: FormulaDetail) => void) | undefined;
  vi.mocked(api.getFormula).mockImplementation(
    () =>
      new Promise<FormulaDetail>((resolve) => {
        resolveLoad = resolve;
      }),
  );
  renderStudio({ initialFormula: detail }, api);

  await screen.findByRole('option', { name: '第二个公式 · v1' });
  await user.selectOptions(
    screen.getByRole('combobox', { name: '打开已保存公式' }),
    'formula-2',
  );
  await user.type(
    screen.getByRole('textbox', { name: '通达信公式代码' }),
    '\nLOCAL:CLOSE;',
  );
  resolveLoad?.(secondSummary);

  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '通达信公式代码' })).toHaveValue(
      `${detail.draft.source}\nLOCAL:CLOSE;`,
    ),
  );
  expect(screen.queryByText('已打开：第二个公式')).not.toBeInTheDocument();
});

it('keeps only the latest deferred validation result and aborts the stale request', async () => {
  const api = apiFixture();
  const first = deferred<FormulaValidation>();
  const second = deferred<FormulaValidation>();
  vi.mocked(api.validateFormula)
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise);
  renderStudio({ initialFormula: detail }, api);

  await waitFor(() => expect(api.validateFormula).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByRole('textbox', { name: '通达信公式代码' }), {
    target: { value: `${detail.draft.source}\nLOCAL:CLOSE;` },
  });
  await waitFor(() => expect(api.validateFormula).toHaveBeenCalledTimes(2));
  const firstSignal = vi.mocked(api.validateFormula).mock.calls[0]?.[1]?.signal;
  expect(firstSignal?.aborted).toBe(true);

  await act(async () => {
    second.resolve({ valid: true, diagnostics: [] });
    await Promise.resolve();
  });
  expect(await screen.findByText('校验通过')).toBeVisible();
  await act(async () => {
    first.resolve({
      valid: false,
      diagnostics: [
        {
          code: 'stale',
          functionName: null,
          explanation: '过期诊断',
          span: { line: 1, column: 1, endLine: 1, endColumn: 2 },
          blocksPreview: true,
          blocksSave: true,
          blocksBacktest: true,
        },
      ],
    });
    await Promise.resolve();
  });
  expect(screen.queryByText('过期诊断')).not.toBeInTheDocument();
});

it('finishes async validation after the StrictMode effect setup-cleanup-setup cycle', async () => {
  const api = apiFixture();
  const pending = deferred<FormulaValidation>();
  vi.mocked(api.validateFormula).mockImplementation(() => pending.promise);
  renderStudioStrict({ initialFormula: detail }, api);

  await waitFor(() => expect(api.validateFormula).toHaveBeenCalled());
  await act(async () => {
    pending.resolve({ valid: true, diagnostics: [] });
    await pending.promise;
  });

  expect(await screen.findByText('校验通过')).toBeVisible();
  expect(screen.getByRole('button', { name: '立即校验' })).toBeEnabled();
});

it('disables copying a saved version while the current draft is dirty', () => {
  const api = apiFixture();
  renderStudio({ initialFormula: detail }, api);

  fireEvent.change(screen.getByRole('textbox', { name: '通达信公式代码' }), {
    target: { value: `${detail.draft.source}\nLOCAL:CLOSE;` },
  });

  expect(screen.getByRole('button', { name: '复制公式' })).toBeDisabled();
  expect(api.copyFormula).not.toHaveBeenCalled();
});

it('copies a saved immutable version as an independent formula with cancellable request context', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  renderStudio({ initialFormula: detail }, api);

  await user.click(screen.getByRole('button', { name: '复制公式' }));

  await waitFor(() =>
    expect(api.copyFormula).toHaveBeenCalledWith(
      detail.id,
      { name: `${detail.name} 副本`, sourceVersionId: 'version-1' },
      { signal: expect.any(AbortSignal) as unknown },
    ),
  );
  expect(
    await screen.findByText(`已复制为独立公式版本：${detail.name} 副本`),
  ).toBeVisible();
});

it('aborts and ignores a deferred save response after the draft changes', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  const saved = deferred<Awaited<ReturnType<FormulaApi['saveFormula']>>>();
  vi.mocked(api.saveFormula).mockImplementation(() => saved.promise);
  renderStudio({ initialFormula: detail }, api);

  const editor = screen.getByRole('textbox', { name: '通达信公式代码' });
  fireEvent.change(editor, {
    target: { value: `${detail.draft.source}\nFIRST:CLOSE;` },
  });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '保存为新版本' })).toBeEnabled(),
  );
  await user.click(screen.getByRole('button', { name: '保存为新版本' }));
  await waitFor(() => expect(api.saveFormula).toHaveBeenCalledOnce());
  const saveSignal = vi.mocked(api.saveFormula).mock.calls[0]?.[2]?.signal;
  fireEvent.change(editor, {
    target: { value: `${detail.draft.source}\nSECOND:CLOSE;` },
  });
  expect(saveSignal?.aborted).toBe(true);
  await act(async () => {
    saved.resolve({
      id: 'version-2',
      formulaId: detail.id,
      version: 2,
      name: detail.name,
      formulaType: detail.formulaType,
      placement: detail.placement,
      source: `${detail.draft.source}\nFIRST:CLOSE;`,
      parameterSchema: detail.draft.parameterSchema,
      compatibilityVersion: 'tdx-v1',
      engineVersion: 'formula-engine-v1',
      checksum: `sha256:${'f'.repeat(64)}`,
      createdAt: '2026-07-06T01:00:00Z',
    });
    await Promise.resolve();
  });
  expect(screen.queryByText('已保存版本 v2')).not.toBeInTheDocument();
});

it('aborts an outstanding manual operation when Formula Studio unmounts', async () => {
  const api = apiFixture();
  const pending = deferred<FormulaValidation>();
  vi.mocked(api.validateFormula).mockImplementation(() => pending.promise);
  const view = renderStudio({ initialFormula: detail }, api);

  await waitFor(() => expect(api.validateFormula).toHaveBeenCalled());
  const signal = vi.mocked(api.validateFormula).mock.calls[0]?.[1]?.signal;
  view.unmount();

  expect(signal?.aborted).toBe(true);
});

it('keeps preview explicit and marks the saved revision stale after editing', async () => {
  const user = userEvent.setup();
  const api = apiFixture();
  renderStudio({ initialFormula: detail }, api);

  await screen.findByText('EMA');
  const previewButton = screen.getByRole('button', { name: '运行预览' });
  expect(previewButton).toBeEnabled();
  expect(api.previewFormula).not.toHaveBeenCalled();

  await user.type(
    screen.getByRole('textbox', { name: '通达信公式代码' }),
    '\nX:CLOSE;',
  );

  expect(previewButton).toBeDisabled();
  expect(screen.getByText('草稿已变更，请先校验并保存新版本')).toBeVisible();
});

it('explains a missing local bar cache without reporting the healthy API as down', async () => {
  const user = userEvent.setup();
  const marketApiClient = {
    getBars: vi.fn(() =>
      Promise.reject(new ApiError('missing', { kind: 'http', status: 404 })),
    ),
  } satisfies Pick<MarketApi, 'getBars'>;
  renderStudio({ initialFormula: detail }, apiFixture(), marketApiClient);

  await user.click(screen.getByRole('button', { name: '运行预览' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '本地缓存中没有该证券/周期/复权的数据，请先在行情页更新数据',
  );
  expect(screen.queryByText(/API 已启动/u)).not.toBeInTheDocument();
});

it('never renders a raw HTTP status for an unmapped formula failure', async () => {
  const user = userEvent.setup();
  const marketApiClient = marketApiFixture();
  vi.mocked(marketApiClient.getBars).mockRejectedValueOnce(
    new ApiError('API request failed with status 503', {
      kind: 'http',
      status: 503,
      details: { code: 'unmapped_failure' },
    }),
  );
  renderStudio({ initialFormula: detail }, apiFixture(), marketApiClient);

  await user.click(screen.getByRole('button', { name: '运行预览' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '本地服务暂时无法完成公式请求',
  );
  expect(document.body).not.toHaveTextContent(/HTTP|503|unmapped_failure/u);
});

it.each([
  ['preview_timeout', 504, '公式预览超过 3 秒执行上限，已安全终止'],
  ['resource_limit_exceeded', 422, '公式或行情数据超过预览资源上限'],
] as const)(
  'surfaces the stable %s preview failure precisely',
  async (code, status, expected) => {
    const user = userEvent.setup();
    const api = apiFixture();
    const marketApiClient = marketApiFixture();
    vi.mocked(marketApiClient.getBars).mockRejectedValue(
      new ApiError(code, {
        kind: 'http',
        status,
        details: { code },
      }),
    );
    renderStudio({ initialFormula: detail }, api, marketApiClient);

    await user.click(screen.getByRole('button', { name: '运行预览' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(expected);
  },
);

it('exposes the three regions and formula actions to keyboard and assistive technology', async () => {
  renderStudio({ initialSource: template.source });

  expect(
    await screen.findByRole('complementary', { name: '函数与模板库' }),
  ).toBeVisible();
  expect(screen.getByRole('region', { name: '公式代码与参数' })).toBeVisible();
  expect(screen.getByRole('region', { name: '公式图表预览' })).toBeVisible();
  expect(screen.getByRole('button', { name: '复制公式' })).toBeVisible();
  expect(screen.getByRole('radio', { name: '日线' })).toBeChecked();
  expect(
    screen.queryByText(/条件选股|五彩 K 线|AI 公式/u),
  ).not.toBeInTheDocument();
});

type MonacoEditorProps = ComponentProps<
  (typeof import('@monaco-editor/react'))['default']
>;
void (undefined as unknown as MonacoEditorProps);
