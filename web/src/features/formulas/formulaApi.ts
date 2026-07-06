import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';

const MAX_TEXT = 64_000;
const MAX_ITEMS = 100_000;
const PAGE_SIZE = 100;
const MAX_CATALOG_PAGES = 100;

export class FormulaProtocolError extends Error {
  constructor(readonly path: string) {
    super(`Formula API protocol violation at ${path}`);
    this.name = 'FormulaProtocolError';
  }
}

export type FormulaType = 'indicator' | 'trading';
export type FormulaPlacement = 'main' | 'subchart';
export type ParameterDeclaration = {
  readonly kind: 'integer' | 'number';
  readonly default: number;
  readonly label?: string;
  readonly description?: string;
};
export type ParameterSchema = Readonly<Record<string, ParameterDeclaration>>;

export type FormulaDiagnostic = {
  readonly code: string;
  readonly functionName: string | null;
  readonly explanation: string;
  readonly span: {
    readonly line: number;
    readonly column: number;
    readonly endLine: number;
    readonly endColumn: number;
  };
  readonly blocksPreview: boolean;
  readonly blocksSave: boolean;
  readonly blocksBacktest: boolean;
};

export type FormulaValidation = {
  readonly valid: boolean;
  readonly diagnostics: readonly FormulaDiagnostic[];
};

export type FormulaFunction = {
  readonly category: 'math' | 'logic' | 'series' | 'statistics' | 'signal';
  readonly futureBehavior:
    'current_only' | 'past_only' | 'future' | 'repainting';
  readonly name: string;
  readonly signature: string;
  readonly summaryZh: string;
  readonly semanticsZh: string;
  readonly parameters: readonly {
    readonly name: string;
    readonly required: boolean;
    readonly constraintsZh: string;
  }[];
};

export type FormulaField = {
  readonly canonicalName: string;
  readonly name: string;
  readonly sourceName: string;
  readonly summaryZh: string;
  readonly unit: 'price' | 'shares' | 'hands';
  readonly valueType: 'number_series' | 'boolean_series';
};

export type FormulaFunctionCatalog = {
  readonly compatibilityVersion: string;
  readonly officialReference: string;
  readonly functions: readonly FormulaFunction[];
  readonly fields: readonly FormulaField[];
};

export type FormulaTemplate = {
  readonly templateId: string;
  readonly name: string;
  readonly formulaType: FormulaType;
  readonly placement: FormulaPlacement;
  readonly source: string;
  readonly parameterSchema: ParameterSchema;
};

export type FormulaSummary = {
  readonly id: string;
  readonly name: string;
  readonly formulaType: FormulaType;
  readonly placement: FormulaPlacement;
  readonly latestVersion: number;
  readonly createdAt: string;
  readonly updatedAt: string;
};

export type FormulaDraft = {
  readonly formulaId: string;
  readonly revision: number;
  readonly source: string;
  readonly sourceChecksum: string;
  readonly parameterSchema: ParameterSchema;
  readonly diagnostics: readonly FormulaDiagnostic[];
  readonly executableVersionId: string | null;
  readonly updatedAt: string;
};

export type FormulaDetail = FormulaSummary & { readonly draft: FormulaDraft };

export type FormulaVersion = {
  readonly id: string;
  readonly formulaId: string;
  readonly version: number;
  readonly name: string;
  readonly formulaType: FormulaType;
  readonly placement: FormulaPlacement;
  readonly source: string;
  readonly parameterSchema: ParameterSchema;
  readonly compatibilityVersion: string;
  readonly engineVersion: string;
  readonly checksum: string;
  readonly createdAt: string;
};

export type FormulaPreview = {
  readonly schemaVersion: 'stock-desk-signal-series-v1';
  readonly signalSeriesId: string;
  readonly formulaId: string;
  readonly formulaVersionId: string;
  readonly formulaVersion: number;
  readonly formulaChecksum: string;
  readonly engineVersion: string;
  readonly compatibilityVersion: string;
  readonly symbol: string;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly source: string;
  readonly datasetVersion: string;
  readonly routeVersion: string;
  readonly manifestRecordId: string;
  readonly dataCutoff: string;
  readonly queryStart: string;
  readonly queryEnd: string;
  readonly timestamps: readonly string[];
  readonly parameters: readonly {
    readonly name: string;
    readonly kind: 'integer' | 'number';
    readonly value: string;
  }[];
  readonly numericOutputs: readonly {
    readonly name: string;
    readonly values: readonly (number | null)[];
    readonly warmupNullCount: number;
  }[];
  readonly signals: readonly {
    readonly name: 'BUY' | 'SELL';
    readonly values: readonly (boolean | null)[];
    readonly warmupNullCount: number;
  }[];
  readonly runtimeDiagnostics: readonly {
    readonly code: string;
    readonly count: number;
    readonly firstIndex: number;
    readonly output: string | null;
  }[];
};

export type FormulaMutationInput = {
  readonly source: string;
  readonly parameterSchema: ParameterSchema;
};
export type FormulaCreateInput = FormulaMutationInput & {
  readonly name: string;
  readonly formulaType: FormulaType;
  readonly placement: FormulaPlacement;
};
export type FormulaValidateInput = FormulaMutationInput & {
  readonly formulaType: FormulaType;
};
export type FormulaPreviewInput = {
  readonly symbol: string;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly start: string;
  readonly end: string;
  readonly parameters: Readonly<Record<string, number>>;
};

type SignalOptions = { readonly signal?: AbortSignal };

export type FormulaApi = {
  readonly listFunctions: (
    options?: SignalOptions,
  ) => Promise<FormulaFunctionCatalog>;
  readonly listTemplates: (
    options?: SignalOptions,
  ) => Promise<readonly FormulaTemplate[]>;
  readonly listFormulas: (options?: SignalOptions) => Promise<{
    readonly items: readonly FormulaSummary[];
    readonly nextCursor: string | null;
  }>;
  readonly getFormula: (
    formulaId: string,
    options?: SignalOptions,
  ) => Promise<FormulaDetail>;
  readonly listVersions: (
    formulaId: string,
    options?: SignalOptions,
  ) => Promise<readonly FormulaVersion[]>;
  readonly validateFormula: (
    input: FormulaValidateInput,
    options?: SignalOptions,
  ) => Promise<FormulaValidation>;
  readonly createFormula: (
    input: FormulaCreateInput,
    options?: SignalOptions,
  ) => Promise<FormulaDetail>;
  readonly updateDraft: (
    formulaId: string,
    input: FormulaMutationInput & { readonly expectedRevision: number },
    options?: SignalOptions,
  ) => Promise<FormulaDraft>;
  readonly saveFormula: (
    formulaId: string,
    input: FormulaMutationInput & { readonly expectedRevision: number },
    options?: SignalOptions,
  ) => Promise<FormulaVersion>;
  readonly copyFormula: (
    formulaId: string,
    input: { readonly name: string; readonly sourceVersionId?: string },
    options?: SignalOptions,
  ) => Promise<FormulaVersion>;
  readonly previewFormula: (
    versionId: string,
    input: FormulaPreviewInput,
    options?: SignalOptions,
  ) => Promise<FormulaPreview>;
};

function object(
  value: JsonValue | undefined,
  path: string,
): Record<string, JsonValue> {
  if (
    value === null ||
    value === undefined ||
    Array.isArray(value) ||
    typeof value !== 'object'
  ) {
    throw new FormulaProtocolError(path);
  }
  return value as Record<string, JsonValue>;
}

function array(
  value: JsonValue | undefined,
  path: string,
  max = MAX_ITEMS,
): readonly JsonValue[] {
  if (!Array.isArray(value) || value.length > max)
    throw new FormulaProtocolError(path);
  return value as readonly JsonValue[];
}

function text(
  value: JsonValue | undefined,
  path: string,
  max = MAX_TEXT,
): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) {
    throw new FormulaProtocolError(path);
  }
  return value;
}

function boundedText(
  value: JsonValue | undefined,
  path: string,
  max: number,
): string {
  if (typeof value !== 'string' || value.length > max) {
    throw new FormulaProtocolError(path);
  }
  return value;
}

function nullableText(
  value: JsonValue | undefined,
  path: string,
): string | null {
  return value === null ? null : text(value, path, 128);
}

function integer(
  value: JsonValue | undefined,
  path: string,
  minimum = 0,
): number {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < minimum
  ) {
    throw new FormulaProtocolError(path);
  }
  return value;
}

function flag(value: JsonValue | undefined, path: string): boolean {
  if (typeof value !== 'boolean') throw new FormulaProtocolError(path);
  return value;
}

function enumValue<const T extends string>(
  value: JsonValue | undefined,
  allowed: readonly T[],
  path: string,
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new FormulaProtocolError(path);
  }
  return value as T;
}

function timestamp(value: JsonValue | undefined, path: string): string {
  const result = text(value, path, 40);
  if (!Number.isFinite(Date.parse(result)))
    throw new FormulaProtocolError(path);
  return result;
}

function checksum(value: JsonValue | undefined, path: string): string {
  const result = text(value, path, 71);
  if (!/^sha256:[0-9a-f]{64}$/u.test(result))
    throw new FormulaProtocolError(path);
  return result;
}

function decodeParameterSchema(
  value: JsonValue | undefined,
  path: string,
): ParameterSchema {
  const raw = object(value, path);
  if (Object.keys(raw).length > 64) throw new FormulaProtocolError(path);
  return Object.fromEntries(
    Object.entries(raw).map(([name, declaration]) => {
      if (!/^[A-Z][A-Z0-9_]{0,63}$/u.test(name))
        throw new FormulaProtocolError(`${path}.${name}`);
      const item = object(declaration, `${path}.${name}`);
      const kind = enumValue(
        item['kind'],
        ['integer', 'number'],
        `${path}.${name}.kind`,
      );
      const defaultValue = item['default'];
      if (
        typeof defaultValue !== 'number' ||
        !Number.isFinite(defaultValue) ||
        (kind === 'integer' && !Number.isSafeInteger(defaultValue))
      ) {
        throw new FormulaProtocolError(`${path}.${name}.default`);
      }
      const label =
        item['label'] === null || item['label'] === undefined
          ? undefined
          : text(item['label'], `${path}.${name}.label`, 256);
      const description =
        item['description'] === null || item['description'] === undefined
          ? undefined
          : text(item['description'], `${path}.${name}.description`, 1024);
      return [
        name,
        {
          kind,
          default: defaultValue,
          ...(label === undefined ? {} : { label }),
          ...(description === undefined ? {} : { description }),
        },
      ];
    }),
  );
}

function decodeDiagnostic(
  value: JsonValue,
  path: string,
): FormulaDiagnostic | null {
  const item = object(value, path);
  if (item['code'] === 'validated') return null;
  const span = object(item['span'], `${path}.span`);
  const line = integer(span['line'], `${path}.span.line`, 1);
  const column = integer(span['column'], `${path}.span.column`, 1);
  const endLine = integer(span['end_line'], `${path}.span.end_line`, 1);
  const endColumn = integer(span['end_column'], `${path}.span.end_column`, 1);
  if (endLine < line || (endLine === line && endColumn < column)) {
    throw new FormulaProtocolError(`${path}.span`);
  }
  return {
    code: text(item['code'], `${path}.code`, 64),
    functionName: nullableText(item['function'], `${path}.function`),
    explanation: text(item['explanation'], `${path}.explanation`, 1024),
    span: { line, column, endLine, endColumn },
    blocksPreview: flag(item['blocks_preview'], `${path}.blocks_preview`),
    blocksSave: flag(item['blocks_save'], `${path}.blocks_save`),
    blocksBacktest: flag(item['blocks_backtest'], `${path}.blocks_backtest`),
  };
}

function diagnostics(
  value: JsonValue | undefined,
  path: string,
): readonly FormulaDiagnostic[] {
  return array(value, path, 64)
    .map((item, index) => decodeDiagnostic(item, `${path}[${String(index)}]`))
    .filter((item): item is FormulaDiagnostic => item !== null);
}

function decodeSummary(value: JsonValue, path: string): FormulaSummary {
  const item = object(value, path);
  return {
    id: text(item['id'], `${path}.id`, 128),
    name: text(item['name'], `${path}.name`, 64),
    formulaType: enumValue(
      item['formula_type'],
      ['indicator', 'trading'],
      `${path}.formula_type`,
    ),
    placement: enumValue(
      item['placement'],
      ['main', 'subchart'],
      `${path}.placement`,
    ),
    latestVersion: integer(item['latest_version'], `${path}.latest_version`),
    createdAt: timestamp(item['created_at'], `${path}.created_at`),
    updatedAt: timestamp(item['updated_at'], `${path}.updated_at`),
  };
}

function decodeDraft(value: JsonValue | undefined, path: string): FormulaDraft {
  const item = object(value, path);
  return {
    formulaId: text(item['formula_id'], `${path}.formula_id`, 128),
    revision: integer(item['revision'], `${path}.revision`, 1),
    source: text(item['source'], `${path}.source`),
    sourceChecksum: checksum(
      item['source_checksum'],
      `${path}.source_checksum`,
    ),
    parameterSchema: decodeParameterSchema(
      item['parameter_schema'],
      `${path}.parameter_schema`,
    ),
    diagnostics: diagnostics(item['diagnostics'], `${path}.diagnostics`),
    executableVersionId: nullableText(
      item['executable_version_id'],
      `${path}.executable_version_id`,
    ),
    updatedAt: timestamp(item['updated_at'], `${path}.updated_at`),
  };
}

function decodeDetail(
  value: JsonValue | undefined,
  path: string,
): FormulaDetail {
  const item = object(value, path);
  const summary = decodeSummary(value as JsonValue, path);
  const draft = decodeDraft(item['draft'], `${path}.draft`);
  if (draft.formulaId !== summary.id)
    throw new FormulaProtocolError(`${path}.draft.formula_id`);
  return { ...summary, draft };
}

function decodeVersion(
  value: JsonValue | undefined,
  path: string,
): FormulaVersion {
  const item = object(value, path);
  return {
    id: text(item['id'], `${path}.id`, 128),
    formulaId: text(item['formula_id'], `${path}.formula_id`, 128),
    version: integer(item['version'], `${path}.version`, 1),
    name: text(item['name'], `${path}.name`, 64),
    formulaType: enumValue(
      item['formula_type'],
      ['indicator', 'trading'],
      `${path}.formula_type`,
    ),
    placement: enumValue(
      item['placement'],
      ['main', 'subchart'],
      `${path}.placement`,
    ),
    source: text(item['source'], `${path}.source`),
    parameterSchema: decodeParameterSchema(
      item['parameter_schema'],
      `${path}.parameter_schema`,
    ),
    compatibilityVersion: text(
      item['compatibility_version'],
      `${path}.compatibility_version`,
      32,
    ),
    engineVersion: text(item['engine_version'], `${path}.engine_version`, 32),
    checksum: checksum(item['checksum'], `${path}.checksum`),
    createdAt: timestamp(item['created_at'], `${path}.created_at`),
  };
}

function decodeCatalog(value: JsonValue | undefined): FormulaFunctionCatalog {
  const root = object(value, '$');
  return {
    compatibilityVersion: text(
      root['compatibility_version'],
      '$.compatibility_version',
      32,
    ),
    officialReference: text(
      root['official_reference'],
      '$.official_reference',
      2048,
    ),
    fields: array(root['fields'], '$.fields', 64).map((value, index) => {
      const path = `$.fields[${String(index)}]`;
      const item = object(value, path);
      return {
        canonicalName: text(
          item['canonical_name'],
          `${path}.canonical_name`,
          64,
        ),
        name: text(item['name'], `${path}.name`, 64),
        sourceName: text(item['source_name'], `${path}.source_name`, 64),
        summaryZh: text(item['summary_zh'], `${path}.summary_zh`, 1024),
        unit: enumValue(
          item['unit'],
          ['price', 'shares', 'hands'],
          `${path}.unit`,
        ),
        valueType: enumValue(
          item['value_type'],
          ['number_series', 'boolean_series'],
          `${path}.value_type`,
        ),
      };
    }),
    functions: array(root['functions'], '$.functions', 128).map(
      (value, index) => {
        const path = `$.functions[${String(index)}]`;
        const item = object(value, path);
        return {
          category: enumValue(
            item['category'],
            ['math', 'logic', 'series', 'statistics', 'signal'],
            `${path}.category`,
          ),
          futureBehavior: enumValue(
            item['future_behavior'],
            ['current_only', 'past_only', 'future', 'repainting'],
            `${path}.future_behavior`,
          ),
          name: text(item['name'], `${path}.name`, 64),
          signature: text(item['signature'], `${path}.signature`, 256),
          summaryZh: text(item['summary_zh'], `${path}.summary_zh`, 1024),
          semanticsZh: text(item['semantics_zh'], `${path}.semantics_zh`, 2048),
          parameters: array(item['parameters'], `${path}.parameters`, 16).map(
            (parameter, parameterIndex) => {
              const parameterPath = `${path}.parameters[${String(parameterIndex)}]`;
              const parameterItem = object(parameter, parameterPath);
              return {
                name: text(parameterItem['name'], `${parameterPath}.name`, 64),
                required: flag(
                  parameterItem['required'],
                  `${parameterPath}.required`,
                ),
                constraintsZh: boundedText(
                  parameterItem['constraints_zh'],
                  `${parameterPath}.constraints_zh`,
                  1024,
                ),
              };
            },
          ),
        };
      },
    ),
  };
}

function decodeTemplate(value: JsonValue, path: string): FormulaTemplate {
  const item = object(value, path);
  return {
    templateId: text(item['template_id'], `${path}.template_id`, 128),
    name: text(item['name'], `${path}.name`, 64),
    formulaType: enumValue(
      item['formula_type'],
      ['indicator', 'trading'],
      `${path}.formula_type`,
    ),
    placement: enumValue(
      item['placement'],
      ['main', 'subchart'],
      `${path}.placement`,
    ),
    source: text(item['source'], `${path}.source`),
    parameterSchema: decodeParameterSchema(
      item['parameter_schema'],
      `${path}.parameter_schema`,
    ),
  };
}

function nullableNumber(value: JsonValue, path: string): number | null {
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isFinite(value))
    throw new FormulaProtocolError(path);
  return value;
}

function decodeNormalizedParameter(value: JsonValue, path: string) {
  const item = object(value, path);
  const name = text(item['name'], `${path}.name`, 64);
  if (!/^[A-Z][A-Z0-9_]{0,63}$/u.test(name)) {
    throw new FormulaProtocolError(`${path}.name`);
  }
  const kind = enumValue(item['kind'], ['integer', 'number'], `${path}.kind`);
  const normalizedValue = text(item['value'], `${path}.value`, 128);
  if (kind === 'integer') {
    if (!/^-?(?:0|[1-9][0-9]*)$/u.test(normalizedValue)) {
      throw new FormulaProtocolError(`${path}.value`);
    }
    const parsed = Number(normalizedValue);
    if (!Number.isSafeInteger(parsed) || String(parsed) !== normalizedValue) {
      throw new FormulaProtocolError(`${path}.value`);
    }
  } else {
    if (
      !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-](?:0|[1-9][0-9]*))?$/u.test(
        normalizedValue,
      )
    ) {
      throw new FormulaProtocolError(`${path}.value`);
    }
    const parsed = Number(normalizedValue);
    if (
      !Number.isFinite(parsed) ||
      (parsed === 0 && normalizedValue !== '0') ||
      (parsed !== 0 && !/[.e]/u.test(normalizedValue))
    ) {
      throw new FormulaProtocolError(`${path}.value`);
    }
  }
  return { name, kind, value: normalizedValue };
}

export function decodeFormulaPreview(
  value: JsonValue | undefined,
): FormulaPreview {
  const root = object(value, '$');
  const timestamps = array(root['timestamps'], '$.timestamps', MAX_ITEMS).map(
    (value, index) => timestamp(value, `$.timestamps[${String(index)}]`),
  );
  const numericOutputs = array(
    root['numeric_outputs'],
    '$.numeric_outputs',
    32,
  ).map((value, index) => {
    const path = `$.numeric_outputs[${String(index)}]`;
    const item = object(value, path);
    const values = array(item['values'], `${path}.values`, MAX_ITEMS).map(
      (value, valueIndex) =>
        nullableNumber(value, `${path}.values[${String(valueIndex)}]`),
    );
    if (values.length !== timestamps.length)
      throw new FormulaProtocolError(`${path}.values`);
    return {
      name: text(item['name'], `${path}.name`, 64),
      values,
      warmupNullCount: integer(
        item['warmup_null_count'],
        `${path}.warmup_null_count`,
      ),
    };
  });
  const signals = array(root['signals'], '$.signals', 2).map((value, index) => {
    const path = `$.signals[${String(index)}]`;
    const item = object(value, path);
    const values = array(item['values'], `${path}.values`, MAX_ITEMS).map(
      (value, valueIndex) => {
        if (value !== null && typeof value !== 'boolean')
          throw new FormulaProtocolError(
            `${path}.values[${String(valueIndex)}]`,
          );
        return value;
      },
    );
    if (values.length !== timestamps.length)
      throw new FormulaProtocolError(`${path}.values`);
    return {
      name: enumValue(item['name'], ['BUY', 'SELL'], `${path}.name`),
      values,
      warmupNullCount: integer(
        item['warmup_null_count'],
        `${path}.warmup_null_count`,
      ),
    };
  });
  if (signals.map((item) => item.name).join(',') !== 'BUY,SELL')
    throw new FormulaProtocolError('$.signals');
  const parameters = array(root['parameters'], '$.parameters', 64).map(
    (value, index) =>
      decodeNormalizedParameter(value, `$.parameters[${String(index)}]`),
  );
  if (
    parameters.some(
      (parameter, index) =>
        index > 0 && parameter.name <= (parameters[index - 1]?.name ?? ''),
    )
  ) {
    throw new FormulaProtocolError('$.parameters');
  }
  return {
    schemaVersion: enumValue(
      root['schema_version'],
      ['stock-desk-signal-series-v1'],
      '$.schema_version',
    ),
    signalSeriesId: checksum(root['signal_series_id'], '$.signal_series_id'),
    formulaId: text(root['formula_id'], '$.formula_id', 128),
    formulaVersionId: text(
      root['formula_version_id'],
      '$.formula_version_id',
      128,
    ),
    formulaVersion: integer(root['formula_version'], '$.formula_version', 1),
    formulaChecksum: checksum(root['formula_checksum'], '$.formula_checksum'),
    engineVersion: text(root['engine_version'], '$.engine_version', 32),
    compatibilityVersion: text(
      root['compatibility_version'],
      '$.compatibility_version',
      32,
    ),
    symbol: text(root['symbol'], '$.symbol', 16),
    period: enumValue(root['period'], ['1d', '1w', '60m'], '$.period'),
    adjustment: enumValue(
      root['adjustment'],
      ['none', 'qfq', 'hfq'],
      '$.adjustment',
    ),
    source: text(root['source'], '$.source', 32),
    datasetVersion: checksum(root['dataset_version'], '$.dataset_version'),
    routeVersion: checksum(root['route_version'], '$.route_version'),
    manifestRecordId: checksum(
      root['manifest_record_id'],
      '$.manifest_record_id',
    ),
    dataCutoff: timestamp(root['data_cutoff'], '$.data_cutoff'),
    queryStart: timestamp(root['query_start'], '$.query_start'),
    queryEnd: timestamp(root['query_end'], '$.query_end'),
    timestamps,
    parameters,
    numericOutputs,
    signals,
    runtimeDiagnostics: array(
      root['runtime_diagnostics'],
      '$.runtime_diagnostics',
      32,
    ).map((value, index) => {
      const path = `$.runtime_diagnostics[${String(index)}]`;
      const item = object(value, path);
      return {
        code: text(item['code'], `${path}.code`, 64),
        count: integer(item['count'], `${path}.count`, 1),
        firstIndex: integer(item['first_index'], `${path}.first_index`),
        output: nullableText(item['output'], `${path}.output`),
      };
    }),
  };
}

function mutationBody(input: FormulaMutationInput): Record<string, JsonValue> {
  return { source: input.source, parameter_schema: input.parameterSchema };
}

async function decodeAllPages<T>(
  client: ApiClient,
  path: string,
  decodeItem: (value: JsonValue, path: string) => T,
  options: SignalOptions,
): Promise<readonly T[]> {
  const items: T[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  for (let page = 0; page < MAX_CATALOG_PAGES; page += 1) {
    const pagePath = `${path}?limit=${String(PAGE_SIZE)}${
      cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`
    }`;
    const root = object(await client.get(pagePath, options), '$');
    const pageItems = array(root['items'], '$.items', PAGE_SIZE);
    pageItems.forEach((value, index) =>
      items.push(decodeItem(value, `$.items[${String(index)}]`)),
    );
    const nextCursor = nullableText(root['next_cursor'], '$.next_cursor');
    if (nextCursor === null) return items;
    if (seenCursors.has(nextCursor)) {
      throw new FormulaProtocolError('$.next_cursor');
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
  throw new FormulaProtocolError('$.next_cursor');
}

export function createFormulaApi(
  client: ApiClient = createApiClient(),
): FormulaApi {
  return {
    async listFunctions(options = {}) {
      return decodeCatalog(await client.get('/formulas/functions', options));
    },
    async listTemplates(options = {}) {
      const root = object(
        await client.get('/formulas/templates', options),
        '$',
      );
      return array(root['items'], '$.items', 64).map((value, index) =>
        decodeTemplate(value, `$.items[${String(index)}]`),
      );
    },
    async listFormulas(options = {}) {
      return {
        items: await decodeAllPages(
          client,
          '/formulas',
          decodeSummary,
          options,
        ),
        nextCursor: null,
      };
    },
    async getFormula(formulaId, options = {}) {
      return decodeDetail(
        await client.get(`/formulas/${encodeURIComponent(formulaId)}`, options),
        '$',
      );
    },
    async listVersions(formulaId, options = {}) {
      return decodeAllPages(
        client,
        `/formulas/${encodeURIComponent(formulaId)}/versions`,
        decodeVersion,
        options,
      );
    },
    async validateFormula(input, options = {}) {
      const root = object(
        await client.post('/formulas/validate', {
          ...options,
          body: { ...mutationBody(input), formula_type: input.formulaType },
        }),
        '$',
      );
      const result = {
        valid: flag(root['valid'], '$.valid'),
        diagnostics: diagnostics(root['diagnostics'], '$.diagnostics'),
      };
      if (result.valid !== (result.diagnostics.length === 0))
        throw new FormulaProtocolError('$.valid');
      return result;
    },
    async createFormula(input, options = {}) {
      return decodeDetail(
        await client.post('/formulas', {
          ...options,
          body: {
            ...mutationBody(input),
            name: input.name,
            formula_type: input.formulaType,
            placement: input.placement,
          },
        }),
        '$',
      );
    },
    async updateDraft(formulaId, input, options = {}) {
      return decodeDraft(
        await client.put(`/formulas/${encodeURIComponent(formulaId)}/draft`, {
          ...options,
          body: {
            ...mutationBody(input),
            expected_revision: input.expectedRevision,
          },
        }),
        '$',
      );
    },
    async saveFormula(formulaId, input, options = {}) {
      return decodeVersion(
        await client.post(`/formulas/${encodeURIComponent(formulaId)}/save`, {
          ...options,
          body: {
            ...mutationBody(input),
            expected_revision: input.expectedRevision,
          },
        }),
        '$',
      );
    },
    async copyFormula(formulaId, input, options = {}) {
      return decodeVersion(
        await client.post(`/formulas/${encodeURIComponent(formulaId)}/copy`, {
          ...options,
          body: {
            name: input.name,
            source_version_id: input.sourceVersionId ?? null,
          },
        }),
        '$',
      );
    },
    async previewFormula(versionId, input, options = {}) {
      return decodeFormulaPreview(
        await client.post(
          `/formulas/${encodeURIComponent(versionId)}/preview`,
          {
            ...options,
            body: {
              symbol: input.symbol,
              period: input.period,
              adjustment: input.adjustment,
              start: input.start,
              end: input.end,
              parameters: input.parameters as JsonValue,
            },
          },
        ),
      );
    },
  };
}

export const formulaApi = createFormulaApi();
