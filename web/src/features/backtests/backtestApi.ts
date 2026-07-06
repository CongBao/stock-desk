import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';

export class BacktestProtocolError extends Error {
  constructor(readonly path: string) {
    super(`Backtest API protocol violation at ${path}`);
    this.name = 'BacktestProtocolError';
  }
}

export type BacktestScope =
  | { readonly kind: 'single'; readonly symbol: string }
  | {
      readonly kind: 'preset';
      readonly poolId: string;
      readonly snapshotId: string;
    }
  | {
      readonly kind: 'custom';
      readonly poolId: string;
      readonly revision: number;
    };

export type BacktestIntent = {
  readonly scope: BacktestScope;
  readonly formulaVersionId: string;
  readonly formulaParameters: Readonly<Record<string, number>>;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly scoringStart: string;
  readonly scoringEnd: string;
  readonly quantityShares: number;
  readonly commissionBps: string;
  readonly minimumCommission: string;
  readonly sellTaxBps: string;
  readonly slippageBps: string;
};

export type BacktestSubmission = {
  readonly runId: string;
  readonly taskId: string;
  readonly snapshotId: string;
  readonly warnings: readonly string[];
};

export type BacktestOverview = {
  readonly runId: string;
  readonly taskId: string;
  readonly snapshotId: string;
  readonly status: string;
  readonly stage: string;
  readonly total: number;
  readonly processed: number;
  readonly failed: number;
  readonly progress: number;
  readonly resultHash: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
};

export type BacktestLog = {
  readonly ordinal: number;
  readonly level: string;
  readonly message: string;
  readonly detail: Readonly<Record<string, JsonValue>>;
};

export type BacktestLogPage = {
  readonly items: readonly BacktestLog[];
  readonly nextCursor: string | null;
  readonly afterCursor: string | null;
};

export type BacktestRunPage = {
  readonly items: readonly BacktestOverview[];
  readonly nextCursor: string | null;
};

export type BacktestPreflight = {
  readonly previewSnapshotId: string;
  readonly reservation: false;
  readonly formula: {
    readonly formulaId: string;
    readonly formulaVersionId: string;
    readonly formulaChecksum: string;
    readonly engineVersion: string;
    readonly compatibilityVersion: string;
    readonly normalizedParameters: readonly {
      readonly name: string;
      readonly kind: 'integer' | 'number';
      readonly value: string;
    }[];
  };
  readonly scope: {
    readonly kind: string;
    readonly symbol: string | null;
    readonly poolId: string | null;
    readonly revisionOrSnapshotId: string | null;
    readonly total: number;
    readonly runnable: number;
    readonly gapCount: number;
    readonly gapSample: readonly {
      readonly symbol: string;
      readonly reason: string;
    }[];
    readonly gapsTruncated: boolean;
    readonly warnings: readonly string[];
  };
  readonly period: string;
  readonly adjustment: string;
  readonly scoringStart: string;
  readonly scoringEnd: string;
  readonly warmup: {
    readonly policyVersion: string;
    readonly lookbackBars: number | null;
    readonly unboundedDependency: boolean;
  };
  readonly coverage: {
    readonly signal: number;
    readonly execution: number;
    readonly status: number;
  };
  readonly rules: {
    readonly executionRulesVersion: string;
    readonly costModelVersion: string;
    readonly sizingVersion: string;
  };
  readonly quantityShares: number;
  readonly costs: {
    readonly commissionBps: string;
    readonly minimumCommission: string;
    readonly sellTaxBps: string;
    readonly slippageBps: string;
  };
  readonly estimatedWorkload: {
    readonly symbols: number;
    readonly runnableSymbols: number;
    readonly formulaRows: number;
  };
  readonly disclaimer: string;
};

type SignalOptions = { readonly signal?: AbortSignal };

export type BacktestApi = {
  readonly preflight: (
    intent: BacktestIntent,
    options?: SignalOptions,
  ) => Promise<BacktestPreflight>;
  readonly create: (
    intent: BacktestIntent,
    options?: SignalOptions,
  ) => Promise<BacktestSubmission>;
  readonly getRun: (
    runId: string,
    options?: SignalOptions,
  ) => Promise<BacktestOverview>;
  readonly getLogs: (
    runId: string,
    options?: SignalOptions & { readonly afterCursor?: string | null },
  ) => Promise<BacktestLogPage>;
  readonly cancel: (
    runId: string,
    options?: SignalOptions,
  ) => Promise<BacktestSubmission>;
  readonly listRuns: (options?: SignalOptions) => Promise<BacktestRunPage>;
};

const uuidPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const digestPattern = /^sha256:[0-9a-f]{64}$/u;
const statuses = new Set([
  'queued',
  'running',
  'succeeded',
  'partial_failed',
  'failed',
  'cancelled',
]);
const stages = new Set([
  'queued',
  'executing',
  'completed',
  'failed',
  'cancelled',
]);
const symbolPattern = /^\d{6}\.(?:SH|SZ|BJ)$/u;
const presetPattern = /^preset:[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/u;

function object(value: JsonValue | undefined, path: string) {
  if (
    value === null ||
    value === undefined ||
    Array.isArray(value) ||
    typeof value !== 'object'
  ) {
    throw new BacktestProtocolError(path);
  }
  return value as Record<string, JsonValue>;
}

function array(value: JsonValue | undefined, path: string, max = 100) {
  if (!Array.isArray(value) || value.length > max) {
    throw new BacktestProtocolError(path);
  }
  return value as readonly JsonValue[];
}

function text(value: JsonValue | undefined, path: string, max = 512) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) {
    throw new BacktestProtocolError(path);
  }
  return value;
}

function nullableText(value: JsonValue | undefined, path: string) {
  return value === null ? null : text(value, path);
}

function integer(value: JsonValue | undefined, path: string, min = 0) {
  if (!Number.isSafeInteger(value) || (value as number) < min) {
    throw new BacktestProtocolError(path);
  }
  return value as number;
}

function finite(value: JsonValue | undefined, path: string) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new BacktestProtocolError(path);
  }
  return value;
}

function flag(value: JsonValue | undefined, path: string) {
  if (typeof value !== 'boolean') throw new BacktestProtocolError(path);
  return value;
}

function timestamp(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 40);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u.test(
      result,
    ) ||
    !Number.isFinite(Date.parse(result))
  )
    throw new BacktestProtocolError(path);
  return result;
}

function nullableTimestamp(value: JsonValue | undefined, path: string) {
  return value === null ? null : timestamp(value, path);
}

function identity(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 36);
  if (!uuidPattern.test(result)) throw new BacktestProtocolError(path);
  return result;
}

function digest(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 71);
  if (!digestPattern.test(result)) throw new BacktestProtocolError(path);
  return result;
}

function enumeration(
  value: JsonValue | undefined,
  allowed: ReadonlySet<string>,
  path: string,
) {
  const result = text(value, path, 32);
  if (!allowed.has(result)) throw new BacktestProtocolError(path);
  return result;
}

function decimal(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 64);
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$/u.test(result)) {
    throw new BacktestProtocolError(path);
  }
  return result;
}

function decodeOverview(
  value: JsonValue | undefined,
  path = '$',
): BacktestOverview {
  const item = object(value, path);
  const total = integer(item['total'], `${path}.total`, 1);
  const processed = integer(item['processed'], `${path}.processed`);
  const failed = integer(item['failed'], `${path}.failed`);
  const progress = finite(item['progress'], `${path}.progress`);
  const status = enumeration(item['status'], statuses, `${path}.status`);
  const stage = enumeration(item['stage'], stages, `${path}.stage`);
  const resultHash =
    item['result_hash'] === null
      ? null
      : digest(item['result_hash'], `${path}.result_hash`);
  const createdAt = timestamp(item['created_at'], `${path}.created_at`);
  const updatedAt = timestamp(item['updated_at'], `${path}.updated_at`);
  const startedAt = nullableTimestamp(item['started_at'], `${path}.started_at`);
  const finishedAt = nullableTimestamp(
    item['finished_at'],
    `${path}.finished_at`,
  );
  const expectedProgress = processed / total;
  if (
    processed > total ||
    failed > processed ||
    progress < 0 ||
    progress > 1 ||
    Math.abs(progress - expectedProgress) > 1e-12
  ) {
    throw new BacktestProtocolError(`${path}.progress`);
  }
  const createdTime = Date.parse(createdAt);
  const updatedTime = Date.parse(updatedAt);
  const startedTime = startedAt === null ? null : Date.parse(startedAt);
  const finishedTime = finishedAt === null ? null : Date.parse(finishedAt);
  if (
    createdTime > updatedTime ||
    (startedTime !== null &&
      (startedTime < createdTime || startedTime > updatedTime)) ||
    (finishedTime !== null && finishedTime < updatedTime)
  ) {
    throw new BacktestProtocolError(`${path}.timestamps`);
  }
  const invalidState =
    (status === 'queued' &&
      (stage !== 'queued' ||
        processed !== 0 ||
        failed !== 0 ||
        startedAt !== null ||
        finishedAt !== null ||
        resultHash !== null)) ||
    (status === 'running' &&
      (stage !== 'executing' ||
        startedAt === null ||
        finishedAt !== null ||
        resultHash !== null)) ||
    (status === 'succeeded' &&
      (stage !== 'completed' ||
        processed !== total ||
        failed !== 0 ||
        startedAt === null ||
        finishedAt === null ||
        resultHash === null)) ||
    (status === 'partial_failed' &&
      (stage !== 'completed' ||
        processed !== total ||
        failed <= 0 ||
        startedAt === null ||
        finishedAt === null ||
        resultHash === null)) ||
    (status === 'failed' &&
      (stage !== 'failed' || finishedAt === null || resultHash !== null)) ||
    (status === 'cancelled' &&
      (stage !== 'cancelled' || finishedAt === null || resultHash !== null));
  if (invalidState) throw new BacktestProtocolError(`${path}.status`);
  return {
    runId: identity(item['run_id'], `${path}.run_id`),
    taskId: identity(item['task_id'], `${path}.task_id`),
    snapshotId: digest(item['snapshot_id'], `${path}.snapshot_id`),
    status,
    stage,
    total,
    processed,
    failed,
    progress,
    resultHash,
    createdAt,
    updatedAt,
    startedAt,
    finishedAt,
  };
}

function decodeSubmission(
  value: JsonValue | undefined,
  expectedRunId?: string,
): BacktestSubmission {
  const item = object(value, '$');
  const runId = identity(item['run_id'], '$.run_id');
  if (expectedRunId !== undefined && runId !== expectedRunId)
    throw new BacktestProtocolError('$.run_id');
  return {
    runId,
    taskId: identity(item['task_id'], '$.task_id'),
    snapshotId: digest(item['snapshot_id'], '$.snapshot_id'),
    warnings: array(item['warnings'], '$.warnings').map((value, index) =>
      text(value, `$.warnings[${String(index)}]`, 1024),
    ),
  };
}

function decodePreflight(value: JsonValue | undefined): BacktestPreflight {
  const root = object(value, '$');
  const formula = object(root['formula'], '$.formula');
  const scope = object(root['scope'], '$.scope');
  const warmup = object(root['warmup'], '$.warmup');
  const coverage = object(root['coverage'], '$.coverage');
  const rules = object(root['rules'], '$.rules');
  const costs = object(root['costs'], '$.costs');
  const workload = object(root['estimated_workload'], '$.estimated_workload');
  if (root['reservation'] !== false)
    throw new BacktestProtocolError('$.reservation');
  const scopeKind = enumeration(
    scope['kind'],
    new Set(['single', 'preset', 'custom']),
    '$.scope.kind',
  );
  const total = integer(scope['total'], '$.scope.total', 1);
  const runnable = integer(scope['runnable'], '$.scope.runnable');
  const gapCount = integer(scope['gap_count'], '$.scope.gap_count');
  if (runnable + gapCount !== total)
    throw new BacktestProtocolError('$.scope.total');
  const period = enumeration(
    root['period'],
    new Set(['1d', '1w', '60m']),
    '$.period',
  );
  const adjustment = enumeration(
    root['adjustment'],
    new Set(['none', 'qfq', 'hfq']),
    '$.adjustment',
  );
  const scopeSymbol = nullableText(scope['symbol'], '$.scope.symbol');
  const scopePoolId = nullableText(scope['pool_id'], '$.scope.pool_id');
  const scopeRevision = nullableText(
    scope['revision_or_snapshot_id'],
    '$.scope.revision_or_snapshot_id',
  );
  if (
    scopeKind === 'single' &&
    (scopeSymbol === null ||
      !symbolPattern.test(scopeSymbol) ||
      scopePoolId !== null ||
      scopeRevision !== null)
  )
    throw new BacktestProtocolError('$.scope');
  if (
    scopeKind === 'preset' &&
    (scopeSymbol !== null ||
      scopePoolId === null ||
      scopePoolId.length < 8 ||
      !presetPattern.test(scopePoolId) ||
      scopeRevision === null ||
      !digestPattern.test(scopeRevision))
  )
    throw new BacktestProtocolError('$.scope');
  if (
    scopeKind === 'custom' &&
    (scopeSymbol !== null ||
      scopePoolId === null ||
      !uuidPattern.test(scopePoolId) ||
      scopeRevision === null ||
      !/^[1-9]\d*$/u.test(scopeRevision))
  )
    throw new BacktestProtocolError('$.scope');
  const gapSample = array(scope['gap_sample'], '$.scope.gap_sample').map(
    (entry, index) => {
      const gap = object(entry, `$.scope.gap_sample[${String(index)}]`);
      const symbol = text(gap['symbol'], '$.scope.gap_sample.symbol');
      if (!symbolPattern.test(symbol))
        throw new BacktestProtocolError('$.scope.gap_sample.symbol');
      return {
        symbol,
        reason: text(gap['reason'], '$.scope.gap_sample.reason'),
      };
    },
  );
  const gapsTruncated = flag(scope['gaps_truncated'], '$.scope.gaps_truncated');
  if (
    gapSample.length > gapCount ||
    gapsTruncated !== gapCount > gapSample.length
  )
    throw new BacktestProtocolError('$.scope.gap_sample');
  const signalCoverage = integer(coverage['signal'], '$.coverage.signal');
  const executionCoverage = integer(
    coverage['execution'],
    '$.coverage.execution',
  );
  const statusCoverage = integer(coverage['status'], '$.coverage.status');
  if (
    signalCoverage !== runnable ||
    executionCoverage !== runnable ||
    statusCoverage !== runnable
  )
    throw new BacktestProtocolError('$.coverage');
  const workloadSymbols = integer(
    workload['symbols'],
    '$.estimated_workload.symbols',
  );
  const workloadRunnable = integer(
    workload['runnable_symbols'],
    '$.estimated_workload.runnable_symbols',
  );
  if (workloadSymbols !== total || workloadRunnable !== runnable)
    throw new BacktestProtocolError('$.estimated_workload');
  const scoringStart = timestamp(root['scoring_start'], '$.scoring_start');
  const scoringEnd = timestamp(root['scoring_end'], '$.scoring_end');
  if (Date.parse(scoringStart) >= Date.parse(scoringEnd))
    throw new BacktestProtocolError('$.scoring_end');
  const quantityShares = integer(
    root['quantity_shares'],
    '$.quantity_shares',
    1,
  );
  if (quantityShares > 100_000_000 || quantityShares % 100 !== 0)
    throw new BacktestProtocolError('$.quantity_shares');
  const commissionBps = decimal(
    costs['commission_bps'],
    '$.costs.commission_bps',
  );
  const minimumCommission = decimal(
    costs['minimum_commission'],
    '$.costs.minimum_commission',
  );
  const sellTaxBps = decimal(costs['sell_tax_bps'], '$.costs.sell_tax_bps');
  const slippageBps = decimal(costs['slippage_bps'], '$.costs.slippage_bps');
  if (
    [commissionBps, sellTaxBps, slippageBps].some(
      (cost) => Number(cost) > 10_000,
    )
  )
    throw new BacktestProtocolError('$.costs');
  return {
    previewSnapshotId: digest(
      root['preview_snapshot_id'],
      '$.preview_snapshot_id',
    ),
    reservation: false,
    formula: {
      formulaId: identity(formula['formula_id'], '$.formula.formula_id'),
      formulaVersionId: identity(
        formula['formula_version_id'],
        '$.formula.formula_version_id',
      ),
      formulaChecksum: digest(
        formula['formula_checksum'],
        '$.formula.formula_checksum',
      ),
      engineVersion: text(
        formula['engine_version'],
        '$.formula.engine_version',
      ),
      compatibilityVersion: text(
        formula['compatibility_version'],
        '$.formula.compatibility_version',
      ),
      normalizedParameters: array(
        formula['normalized_parameters'],
        '$.formula.normalized_parameters',
        64,
      ).map((entry, index) => {
        const binding = object(
          entry,
          `$.formula.normalized_parameters[${String(index)}]`,
        );
        const kind = enumeration(
          binding['kind'],
          new Set(['integer', 'number']),
          `$.formula.normalized_parameters[${String(index)}].kind`,
        );
        return {
          name: text(
            binding['name'],
            `$.formula.normalized_parameters[${String(index)}].name`,
            64,
          ),
          kind: kind as 'integer' | 'number',
          value: text(
            binding['value'],
            `$.formula.normalized_parameters[${String(index)}].value`,
            128,
          ),
        };
      }),
    },
    scope: {
      kind: scopeKind,
      symbol: scopeSymbol,
      poolId: scopePoolId,
      revisionOrSnapshotId: scopeRevision,
      total,
      runnable,
      gapCount,
      gapSample,
      gapsTruncated,
      warnings: array(scope['warnings'], '$.scope.warnings').map(
        (entry, index) =>
          text(entry, `$.scope.warnings[${String(index)}]`, 1024),
      ),
    },
    period,
    adjustment,
    scoringStart,
    scoringEnd,
    warmup: {
      policyVersion: text(warmup['policy_version'], '$.warmup.policy_version'),
      lookbackBars:
        warmup['lookback_bars'] === null
          ? null
          : integer(warmup['lookback_bars'], '$.warmup.lookback_bars'),
      unboundedDependency: flag(
        warmup['unbounded_dependency'],
        '$.warmup.unbounded_dependency',
      ),
    },
    coverage: {
      signal: signalCoverage,
      execution: executionCoverage,
      status: statusCoverage,
    },
    rules: {
      executionRulesVersion: text(
        rules['execution_rules_version'],
        '$.rules.execution_rules_version',
      ),
      costModelVersion: text(
        rules['cost_model_version'],
        '$.rules.cost_model_version',
      ),
      sizingVersion: text(rules['sizing_version'], '$.rules.sizing_version'),
    },
    quantityShares,
    costs: {
      commissionBps,
      minimumCommission,
      sellTaxBps,
      slippageBps,
    },
    estimatedWorkload: {
      symbols: workloadSymbols,
      runnableSymbols: workloadRunnable,
      formulaRows: integer(
        workload['formula_rows'],
        '$.estimated_workload.formula_rows',
      ),
    },
    disclaimer: text(root['disclaimer'], '$.disclaimer', 2048),
  };
}

function encodeIntent(intent: BacktestIntent): JsonValue {
  const scope: JsonValue =
    intent.scope.kind === 'single'
      ? { kind: 'single', symbol: intent.scope.symbol }
      : intent.scope.kind === 'preset'
        ? {
            kind: 'preset',
            pool_id: intent.scope.poolId,
            snapshot_id: intent.scope.snapshotId,
          }
        : {
            kind: 'custom',
            pool_id: intent.scope.poolId,
            revision: intent.scope.revision,
          };
  return {
    scope,
    formula_version_id: intent.formulaVersionId,
    formula_parameters: intent.formulaParameters,
    period: intent.period,
    adjustment: intent.adjustment,
    scoring_start: intent.scoringStart,
    scoring_end: intent.scoringEnd,
    quantity_shares: intent.quantityShares,
    commission_bps: intent.commissionBps,
    minimum_commission: intent.minimumCommission,
    sell_tax_bps: intent.sellTaxBps,
    slippage_bps: intent.slippageBps,
  };
}

function assertPreflightMatchesIntent(
  result: BacktestPreflight,
  intent: BacktestIntent,
) {
  if (
    result.formula.formulaVersionId !== intent.formulaVersionId ||
    result.period !== intent.period ||
    result.adjustment !== intent.adjustment ||
    Date.parse(result.scoringStart) !== Date.parse(intent.scoringStart) ||
    Date.parse(result.scoringEnd) !== Date.parse(intent.scoringEnd) ||
    result.quantityShares !== intent.quantityShares ||
    result.costs.commissionBps !== intent.commissionBps ||
    result.costs.minimumCommission !== intent.minimumCommission ||
    result.costs.sellTaxBps !== intent.sellTaxBps ||
    result.costs.slippageBps !== intent.slippageBps
  )
    throw new BacktestProtocolError('$.intent_binding');

  const scope = result.scope;
  if (intent.scope.kind !== scope.kind)
    throw new BacktestProtocolError('$.scope.intent_binding');
  if (intent.scope.kind === 'single' && scope.symbol !== intent.scope.symbol)
    throw new BacktestProtocolError('$.scope.intent_binding');
  if (
    intent.scope.kind === 'preset' &&
    (scope.poolId !== intent.scope.poolId ||
      scope.revisionOrSnapshotId !== intent.scope.snapshotId)
  )
    throw new BacktestProtocolError('$.scope.intent_binding');
  if (
    intent.scope.kind === 'custom' &&
    (scope.poolId !== intent.scope.poolId ||
      scope.revisionOrSnapshotId !== String(intent.scope.revision))
  )
    throw new BacktestProtocolError('$.scope.intent_binding');

  const normalized = new Map<string, number>();
  for (const [
    index,
    binding,
  ] of result.formula.normalizedParameters.entries()) {
    const { name, kind, value } = binding;
    if (
      typeof name !== 'string' ||
      !/^[A-Z][A-Z0-9_]{0,63}$/u.test(name) ||
      (kind !== 'integer' && kind !== 'number') ||
      typeof value !== 'string' ||
      normalized.has(name)
    )
      throw new BacktestProtocolError(
        `$.formula.normalized_parameters[${String(index)}]`,
      );
    const number = Number(value);
    if (
      !Number.isFinite(number) ||
      (kind === 'integer' && !Number.isSafeInteger(number))
    )
      throw new BacktestProtocolError(
        `$.formula.normalized_parameters[${String(index)}].value`,
      );
    normalized.set(name, number);
  }
  for (const [name, value] of Object.entries(intent.formulaParameters)) {
    if (!normalized.has(name) || normalized.get(name) !== value)
      throw new BacktestProtocolError(
        '$.formula.normalized_parameters.intent_binding',
      );
  }
}

function query(path: string, parameters: Record<string, string | undefined>) {
  const values = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== undefined) values.set(key, value);
  }
  const suffix = values.toString();
  return suffix.length > 0 ? `${path}?${suffix}` : path;
}

export function createBacktestApi(
  client: ApiClient = createApiClient(),
): BacktestApi {
  return {
    async preflight(intent, { signal } = {}) {
      const result = decodePreflight(
        await client.post('/backtests/preflight', {
          body: encodeIntent(intent),
          signal,
        }),
      );
      assertPreflightMatchesIntent(result, intent);
      return result;
    },
    async create(intent, { signal } = {}) {
      return decodeSubmission(
        await client.post('/backtests', { body: encodeIntent(intent), signal }),
      );
    },
    async getRun(runId, { signal } = {}) {
      const result = decodeOverview(
        await client.get(`/backtests/${encodeURIComponent(runId)}`, { signal }),
      );
      if (result.runId !== runId) throw new BacktestProtocolError('$.run_id');
      return result;
    },
    async getLogs(runId, { afterCursor, signal } = {}) {
      const root = object(
        await client.get(
          query(`/backtests/${encodeURIComponent(runId)}/logs`, {
            after_cursor: afterCursor ?? undefined,
            limit: '100',
          }),
          { signal },
        ),
        '$',
      );
      return {
        items: array(root['items'], '$.items').map((entry, index) => {
          const item = object(entry, `$.items[${String(index)}]`);
          return {
            ordinal: integer(item['ordinal'], '$.items.ordinal'),
            level: text(item['level'], '$.items.level', 32),
            message: text(item['message'], '$.items.message', 2048),
            detail: object(item['detail'], '$.items.detail'),
          };
        }),
        nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
        afterCursor: nullableText(root['after_cursor'], '$.after_cursor'),
      };
    },
    async cancel(runId, { signal } = {}) {
      return decodeSubmission(
        await client.post(`/backtests/${encodeURIComponent(runId)}/cancel`, {
          signal,
        }),
        runId,
      );
    },
    async listRuns({ signal } = {}) {
      const root = object(
        await client.get('/backtests?limit=20', { signal }),
        '$',
      );
      return {
        items: array(root['items'], '$.items', 100).map((entry, index) =>
          decodeOverview(entry, `$.items[${String(index)}]`),
        ),
        nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
      };
    },
  };
}

export const backtestApi = createBacktestApi();
