import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';
import type { MarketBar } from '../market/marketApi';

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

export type BacktestHistogramBin = {
  readonly code: string;
  readonly count: number;
  readonly share: string | null;
};

export type BacktestMetrics = {
  readonly label: string;
  readonly realizedCount: number;
  readonly winRateDenominator: number;
  readonly positiveCount: number;
  readonly negativeCount: number;
  readonly zeroCount: number;
  readonly winRate: string | null;
  readonly winRateReason: string | null;
  readonly meanNetReturn: string | null;
  readonly meanNetReturnReason: string | null;
  readonly medianNetReturn: string | null;
  readonly medianNetReturnReason: string | null;
  readonly payoffRatio: string | null;
  readonly payoffRatioReason: string | null;
  readonly maxWinReturn: string | null;
  readonly maxWinReturnReason: string | null;
  readonly maxLossReturn: string | null;
  readonly maxLossReturnReason: string | null;
  readonly realizedNetPnlTotal: string;
  readonly averageHoldingBars: string | null;
  readonly averageHoldingBarsReason: string | null;
  readonly averageHoldingDays: string | null;
  readonly averageHoldingDaysReason: string | null;
  readonly histogram: readonly BacktestHistogramBin[];
  readonly openTrades: {
    readonly count: number;
    readonly floatingPnlTotal: string;
    readonly meanFloatingReturn: string | null;
    readonly meanFloatingReturnReason: string | null;
  };
  readonly reliability: {
    readonly level: 'low' | 'medium' | 'high';
    readonly reason: string | null;
    readonly realizedCount: number;
    readonly largestSymbolShare: string | null;
  };
};

export type BacktestReport = {
  readonly overview: BacktestOverview;
  readonly formulaVersionId: string;
  readonly formulaChecksum: string;
  readonly formulaEngineVersion: string;
  readonly compatibilityVersion: string;
  readonly backtestEngineVersion: string;
  readonly formulaParameters: readonly {
    readonly name: string;
    readonly kind: 'integer' | 'number';
    readonly value: string;
  }[];
  readonly provenance: {
    readonly instrumentDatasetVersion: string;
    readonly symbolCount: number;
    readonly runnableCount: number;
    readonly gapCount: number;
    readonly sourceIds: {
      readonly signal: readonly string[];
      readonly execution: readonly string[];
      readonly status: readonly string[];
    };
    readonly digest: string;
  };
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly quantityShares: number;
  readonly costs: {
    readonly commissionBps: string;
    readonly minimumCommission: string;
    readonly sellTaxBps: string;
    readonly slippageBps: string;
  };
  readonly executionRulesVersion: string;
  readonly costModelVersion: string;
  readonly sizingVersion: string;
  readonly warmupPolicyVersion: string;
  readonly metrics: BacktestMetrics | null;
  readonly disclaimer: string;
  readonly outcomes: {
    readonly total: number;
    readonly succeeded: number;
    readonly failed: number;
    readonly dataInsufficient: number;
    readonly unprocessed: number;
  };
};

export type BacktestGroup = {
  readonly dimension: 'symbol' | 'entry_month' | 'entry_year';
  readonly key: string;
  readonly realizedCount: number;
  readonly realizedDenominator: number;
  readonly positiveCount: number;
  readonly negativeCount: number;
  readonly zeroCount: number;
  readonly shareOfAll: string;
  readonly winRate: string;
  readonly meanNetReturn: string;
  readonly medianNetReturn: string;
  readonly payoffRatio: string | null;
  readonly netPnlTotal: string;
  readonly averageHoldingDays: string;
};

type OrderSide = 'buy' | 'sell';

export type BacktestOrderEvent =
  | {
      readonly eventType: 'OrderPending';
      readonly side: OrderSide;
      readonly signalAt: string;
      readonly eligibleAt: string;
    }
  | {
      readonly eventType: 'IgnoredSignal';
      readonly signal: OrderSide | null;
      readonly at: string;
      readonly reason:
        | 'already_holding'
        | 'not_holding'
        | 'same_side_order_pending'
        | 'conflicting_signals';
    }
  | {
      readonly eventType: 'OrderCancelled';
      readonly side: OrderSide;
      readonly at: string;
      readonly reason: 'opposite_signal';
    }
  | {
      readonly eventType: 'OrderBlocked';
      readonly side: OrderSide;
      readonly at: string;
      readonly reason: string;
    }
  | {
      readonly eventType: 'OrderFilled';
      readonly side: OrderSide;
      readonly signalAt: string;
      readonly filledAt: string;
      readonly price: string;
      readonly quantity: number;
    }
  | {
      readonly eventType: 'OrderUnfilled';
      readonly side: OrderSide;
      readonly signalAt: string;
      readonly eligibleAt: string;
      readonly endedAt: string;
      readonly reason: 'range_ended_unfilled';
    }
  | {
      readonly eventType: 'OpenTradeMarked';
      readonly entryAt: string;
      readonly entryPrice: string;
      readonly quantity: number;
      readonly markAt: string;
      readonly markPrice: string;
      readonly floatingPnl: string;
    };

export type BacktestTrade = {
  readonly symbol: string;
  readonly ordinal: number;
  readonly realized: boolean;
  readonly entrySignalAt: string;
  readonly entryFillAt: string;
  readonly exitSignalAt: string | null;
  readonly exitFillAt: string | null;
  readonly markAt: string | null;
  readonly quantity: number;
  readonly buyCommission: string;
  readonly sellCommission: string;
  readonly sellTax: string;
  readonly slippageCost: string;
  readonly referenceGrossPnl: string;
  readonly fillGrossPnl: string;
  readonly investedCost: string;
  readonly netPnl: string | null;
  readonly netReturn: string | null;
  readonly floatingPnl: string | null;
  readonly floatingReturn: string | null;
  readonly holdingBars: number;
  readonly holdingDays: number;
  readonly orderEvents: readonly BacktestOrderEvent[];
};

export type BacktestFailure = {
  readonly symbol: string;
  readonly ordinal: number;
  readonly reason: string;
  readonly detail: Readonly<Record<string, JsonValue>>;
};

export type BacktestCursorPage<T> = {
  readonly items: readonly T[];
  readonly nextCursor: string | null;
};

export type BacktestReplay = {
  readonly runId: string;
  readonly snapshotId: string;
  readonly resultHash: string | null;
  readonly symbol: string;
  readonly tradeOrdinal: number;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly bars: readonly MarketBar[];
  readonly formula: {
    readonly signalSeriesId: string;
    readonly formulaVersionId: string;
    readonly formulaChecksum: string;
    readonly engineVersion: string;
    readonly compatibilityVersion: string;
    readonly numericOutputs: readonly {
      readonly name: string;
      readonly values: readonly (number | null)[];
    }[];
    readonly signals: readonly {
      readonly name: 'BUY' | 'SELL';
      readonly values: readonly (boolean | null)[];
    }[];
  };
  readonly trade: BacktestTrade;
  readonly fillMarkers: readonly {
    readonly side: 'buy' | 'sell';
    readonly signalAt: string;
    readonly filledAt: string;
    readonly anchorOrdinal: number;
    readonly referenceOpen: string;
    readonly fillPrice: string;
    readonly quantity: number;
  }[];
  readonly executionEvidence: readonly {
    readonly side: 'buy' | 'sell';
    readonly filledAt: string;
    readonly bar: MarketBar;
  }[];
  readonly provenance: {
    readonly signal: BacktestPinnedIdentity;
    readonly execution: BacktestPinnedIdentity;
    readonly status: BacktestPinnedIdentity;
  };
  readonly nextCursor: string | null;
};

type BacktestPinnedIdentity = {
  readonly manifestRecordId: string;
  readonly datasetVersion: string;
  readonly routeVersion: string;
  readonly source: string;
  readonly dataCutoff: string;
};

type CursorOptions = SignalOptions & { readonly cursor?: string | null };

export type BacktestReportApi = {
  readonly getReport: (
    runId: string,
    options?: SignalOptions,
  ) => Promise<BacktestReport>;
  readonly getGroups: (
    runId: string,
    dimension: BacktestGroup['dimension'],
    options?: CursorOptions,
  ) => Promise<BacktestCursorPage<BacktestGroup>>;
  readonly getTrades: (
    runId: string,
    kind: 'realized' | 'open',
    options?: CursorOptions,
  ) => Promise<BacktestCursorPage<BacktestTrade>>;
  readonly getFailures: (
    runId: string,
    options?: CursorOptions,
  ) => Promise<BacktestCursorPage<BacktestFailure>>;
  readonly getReportLogs: (
    runId: string,
    options?: CursorOptions,
  ) => Promise<BacktestCursorPage<BacktestLog>>;
  readonly getReplay: (
    runId: string,
    symbol: string,
    tradeOrdinal: number,
    options?: CursorOptions,
  ) => Promise<BacktestReplay>;
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

const reportLabel = 'independent trade samples, not portfolio return';
const histogramCodes = [
  'lt_neg_20pct',
  'neg_20_to_10pct',
  'neg_10_to_5pct',
  'neg_5_to_0pct',
  'zero',
  'pos_0_to_5pct',
  'pos_5_to_10pct',
  'pos_10_to_20pct',
  'gt_20pct',
] as const;

function signedDecimal(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 64);
  if (
    !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$/u.test(result) ||
    result === '-0'
  )
    throw new BacktestProtocolError(path);
  return result;
}

function nullableSignedDecimal(value: JsonValue | undefined, path: string) {
  return value === null ? null : signedDecimal(value, path);
}

function boundedRatio(value: string, path: string) {
  const ratio = Number(value);
  if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1)
    throw new BacktestProtocolError(path);
  return ratio;
}

function ratiosDiffer(left: number, right: number, tolerance = 0.000001) {
  return Math.abs(left - right) > tolerance;
}

function boundedTextArray(value: JsonValue | undefined, path: string) {
  return array(value, path, 5).map((entry, index) =>
    text(entry, `${path}[${String(index)}]`, 64),
  );
}

function optionalMetric(
  item: Record<string, JsonValue>,
  valueKey: string,
  reasonKey: string,
  path: string,
) {
  const value = nullableSignedDecimal(item[valueKey], `${path}.${valueKey}`);
  const reason = nullableText(item[reasonKey], `${path}.${reasonKey}`);
  if ((value === null) === (reason === null))
    throw new BacktestProtocolError(`${path}.${valueKey}`);
  return { value, reason };
}

function decodeMetrics(
  value: JsonValue | undefined,
  status: string,
): BacktestMetrics | null {
  const item = object(value, '$.metrics');
  if (Object.keys(item).length === 0) {
    if (status !== 'failed' && status !== 'cancelled')
      throw new BacktestProtocolError('$.metrics');
    return null;
  }
  if (text(item['label'], '$.metrics.label', 128) !== reportLabel)
    throw new BacktestProtocolError('$.metrics.label');
  if (item['equity_curve'] !== null)
    throw new BacktestProtocolError('$.metrics.equity_curve');
  const realizedCount = integer(
    item['realized_count'],
    '$.metrics.realized_count',
  );
  const denominator = integer(
    item['win_rate_denominator'],
    '$.metrics.win_rate_denominator',
  );
  const positive = integer(item['positive_count'], '$.metrics.positive_count');
  const negative = integer(item['negative_count'], '$.metrics.negative_count');
  const zero = integer(item['zero_count'], '$.metrics.zero_count');
  if (
    denominator !== realizedCount ||
    positive + negative + zero !== realizedCount
  )
    throw new BacktestProtocolError('$.metrics.realized_count');
  const winRate = optionalMetric(
    item,
    'win_rate',
    'win_rate_reason',
    '$.metrics',
  );
  const mean = optionalMetric(
    item,
    'mean_net_return',
    'mean_net_return_reason',
    '$.metrics',
  );
  const median = optionalMetric(
    item,
    'median_net_return',
    'median_net_return_reason',
    '$.metrics',
  );
  const payoff = optionalMetric(
    item,
    'payoff_ratio',
    'payoff_ratio_reason',
    '$.metrics',
  );
  const maxWin = optionalMetric(
    item,
    'max_win_return',
    'max_win_return_reason',
    '$.metrics',
  );
  const maxLoss = optionalMetric(
    item,
    'max_loss_return',
    'max_loss_return_reason',
    '$.metrics',
  );
  const averageBars = optionalMetric(
    item,
    'average_holding_bars',
    'average_holding_bars_reason',
    '$.metrics',
  );
  const averageDays = optionalMetric(
    item,
    'average_holding_days',
    'average_holding_days_reason',
    '$.metrics',
  );
  if (realizedCount === 0) {
    if (winRate.value !== null || winRate.reason !== 'no_realized_samples')
      throw new BacktestProtocolError('$.metrics.win_rate');
  } else if (
    winRate.value === null ||
    winRate.reason !== null ||
    ratiosDiffer(
      boundedRatio(winRate.value, '$.metrics.win_rate'),
      positive / realizedCount,
    )
  ) {
    throw new BacktestProtocolError('$.metrics.win_rate');
  }
  const realizedDerived = [mean, median, averageBars, averageDays] as const;
  if (
    realizedDerived.some((metric) =>
      realizedCount === 0
        ? metric.value !== null || metric.reason !== 'no_realized_samples'
        : metric.value === null || metric.reason !== null,
    )
  )
    throw new BacktestProtocolError('$.metrics.realized_derivations');
  const expectedPayoffReason =
    positive === 0 && negative === 0
      ? 'no_positive_or_negative_returns'
      : positive === 0
        ? 'no_positive_returns'
        : negative === 0
          ? 'no_negative_returns'
          : null;
  if (
    payoff.reason !== expectedPayoffReason ||
    (expectedPayoffReason === null) !== (payoff.value !== null) ||
    (payoff.value !== null &&
      (!Number.isFinite(Number(payoff.value)) || Number(payoff.value) < 0))
  )
    throw new BacktestProtocolError('$.metrics.payoff_ratio');
  if (
    (positive === 0
      ? maxWin.value !== null || maxWin.reason !== 'no_positive_returns'
      : maxWin.value === null ||
        maxWin.reason !== null ||
        !Number.isFinite(Number(maxWin.value)) ||
        Number(maxWin.value) <= 0) ||
    (negative === 0
      ? maxLoss.value !== null || maxLoss.reason !== 'no_negative_returns'
      : maxLoss.value === null ||
        maxLoss.reason !== null ||
        !Number.isFinite(Number(maxLoss.value)) ||
        Number(maxLoss.value) >= 0)
  )
    throw new BacktestProtocolError('$.metrics.extreme_returns');
  const decodedHistogram = array(
    item['histogram'],
    '$.metrics.histogram',
    9,
  ).map((entry, index) => {
    const bin = object(entry, `$.metrics.histogram[${String(index)}]`);
    const code = text(bin['code'], '$.metrics.histogram.code', 32);
    if (code !== histogramCodes[index])
      throw new BacktestProtocolError('$.metrics.histogram.code');
    const count = integer(bin['count'], '$.metrics.histogram.count');
    const share =
      bin['share'] === null
        ? null
        : signedDecimal(bin['share'], '$.metrics.histogram.share');
    const shareReason = nullableText(
      bin['share_reason'],
      '$.metrics.histogram.share_reason',
    );
    if (realizedCount === 0) {
      if (share !== null || shareReason !== 'no_realized_samples')
        throw new BacktestProtocolError('$.metrics.histogram.share');
    } else if (
      share === null ||
      shareReason !== null ||
      ratiosDiffer(
        boundedRatio(share, '$.metrics.histogram.share'),
        count / realizedCount,
      )
    ) {
      throw new BacktestProtocolError('$.metrics.histogram.share');
    }
    return {
      code,
      count,
      share,
    };
  });
  if (
    decodedHistogram.length !== 9 ||
    decodedHistogram.reduce((total, bin) => total + bin.count, 0) !==
      realizedCount
  )
    throw new BacktestProtocolError('$.metrics.histogram');
  if (
    realizedCount > 0 &&
    ratiosDiffer(
      decodedHistogram.reduce(
        (total, bin) =>
          total + boundedRatio(bin.share ?? '', '$.metrics.histogram.share'),
        0,
      ),
      1,
      0.0000045,
    )
  )
    throw new BacktestProtocolError('$.metrics.histogram.share');
  const open = object(item['open_trades'], '$.metrics.open_trades');
  const openCount = integer(open['count'], '$.metrics.open_trades.count');
  const openMean = optionalMetric(
    open,
    'mean_floating_return',
    'mean_floating_return_reason',
    '$.metrics.open_trades',
  );
  if (openCount === 0) {
    if (openMean.value !== null || openMean.reason !== 'no_open_samples')
      throw new BacktestProtocolError(
        '$.metrics.open_trades.mean_floating_return',
      );
  } else if (openMean.value === null || openMean.reason !== null) {
    throw new BacktestProtocolError(
      '$.metrics.open_trades.mean_floating_return',
    );
  }
  const reliability = object(item['reliability'], '$.metrics.reliability');
  const reliabilityLevel = enumeration(
    reliability['level'],
    new Set(['low', 'medium', 'high']),
    '$.metrics.reliability.level',
  ) as 'low' | 'medium' | 'high';
  const reliabilityCount = integer(
    reliability['realized_count'],
    '$.metrics.reliability.realized_count',
  );
  if (reliabilityCount !== realizedCount)
    throw new BacktestProtocolError('$.metrics.reliability.realized_count');
  const reliabilityReason = nullableText(
    reliability['reason'],
    '$.metrics.reliability.reason',
  );
  const largestSymbolShare = nullableSignedDecimal(
    reliability['largest_symbol_share'],
    '$.metrics.reliability.largest_symbol_share',
  );
  const concentration =
    largestSymbolShare === null
      ? null
      : boundedRatio(
          largestSymbolShare,
          '$.metrics.reliability.largest_symbol_share',
        );
  const expectedReliability: readonly [typeof reliabilityLevel, string | null] =
    realizedCount === 0
      ? ['low', 'no_realized_samples']
      : realizedCount < 30
        ? ['low', 'small_sample']
        : concentration !== null && concentration > 0.5
          ? ['low', 'high_symbol_concentration']
          : realizedCount < 100
            ? ['medium', 'moderate_sample']
            : ['high', null];
  if (
    (realizedCount === 0) !== (concentration === null) ||
    reliabilityLevel !== expectedReliability[0] ||
    reliabilityReason !== expectedReliability[1]
  )
    throw new BacktestProtocolError('$.metrics.reliability');
  return {
    label: reportLabel,
    realizedCount,
    winRateDenominator: denominator,
    positiveCount: positive,
    negativeCount: negative,
    zeroCount: zero,
    winRate: winRate.value,
    winRateReason: winRate.reason,
    meanNetReturn: mean.value,
    meanNetReturnReason: mean.reason,
    medianNetReturn: median.value,
    medianNetReturnReason: median.reason,
    payoffRatio: payoff.value,
    payoffRatioReason: payoff.reason,
    maxWinReturn: maxWin.value,
    maxWinReturnReason: maxWin.reason,
    maxLossReturn: maxLoss.value,
    maxLossReturnReason: maxLoss.reason,
    realizedNetPnlTotal: signedDecimal(
      item['realized_net_pnl_total'],
      '$.metrics.realized_net_pnl_total',
    ),
    averageHoldingBars: averageBars.value,
    averageHoldingBarsReason: averageBars.reason,
    averageHoldingDays: averageDays.value,
    averageHoldingDaysReason: averageDays.reason,
    histogram: decodedHistogram,
    openTrades: {
      count: openCount,
      floatingPnlTotal: signedDecimal(
        open['floating_pnl_total'],
        '$.metrics.open_trades.floating_pnl_total',
      ),
      meanFloatingReturn: openMean.value,
      meanFloatingReturnReason: openMean.reason,
    },
    reliability: {
      level: reliabilityLevel,
      reason: reliabilityReason,
      realizedCount: reliabilityCount,
      largestSymbolShare,
    },
  };
}

function decodeReport(
  value: JsonValue | undefined,
  expectedRunId: string,
): BacktestReport {
  const root = object(value, '$');
  const overview = decodeOverview(root['overview'], '$.overview');
  if (overview.runId !== expectedRunId)
    throw new BacktestProtocolError('$.overview.run_id');
  if (
    !['succeeded', 'partial_failed', 'failed', 'cancelled'].includes(
      overview.status,
    )
  )
    throw new BacktestProtocolError('$.overview.status');
  const provenance = object(root['provenance'], '$.provenance');
  const sources = object(provenance['source_ids'], '$.provenance.source_ids');
  const symbolCount = integer(
    provenance['symbol_count'],
    '$.provenance.symbol_count',
    1,
  );
  const runnableCount = integer(
    provenance['runnable_count'],
    '$.provenance.runnable_count',
  );
  const gapCount = integer(provenance['gap_count'], '$.provenance.gap_count');
  if (
    runnableCount + gapCount !== symbolCount ||
    symbolCount !== overview.total
  )
    throw new BacktestProtocolError('$.provenance.symbol_count');
  const period = enumeration(
    root['period'],
    new Set(['1d', '1w', '60m']),
    '$.period',
  ) as MarketPeriod;
  const adjustment = enumeration(
    root['adjustment'],
    new Set(['none', 'qfq', 'hfq']),
    '$.adjustment',
  ) as MarketAdjustment;
  const costs = object(root['costs'], '$.costs');
  const outcomes = object(root['outcomes'], '$.outcomes');
  const outcomeTotal = integer(outcomes['total'], '$.outcomes.total', 1);
  const outcomeSucceeded = integer(
    outcomes['succeeded'],
    '$.outcomes.succeeded',
  );
  const outcomeFailed = integer(outcomes['failed'], '$.outcomes.failed');
  const dataInsufficient = integer(
    outcomes['data_insufficient'],
    '$.outcomes.data_insufficient',
  );
  const unprocessed = integer(
    outcomes['unprocessed'],
    '$.outcomes.unprocessed',
  );
  if (
    outcomeTotal !== symbolCount ||
    outcomeSucceeded + outcomeFailed + dataInsufficient + unprocessed !==
      outcomeTotal ||
    outcomeSucceeded + outcomeFailed + dataInsufficient !==
      overview.processed ||
    outcomeFailed + dataInsufficient !== overview.failed ||
    dataInsufficient > gapCount
  )
    throw new BacktestProtocolError('$.outcomes');
  const disclaimer = text(root['disclaimer'], '$.disclaimer', 2048);
  if (disclaimer !== reportLabel)
    throw new BacktestProtocolError('$.disclaimer');
  return {
    overview,
    formulaVersionId: identity(
      root['formula_version_id'],
      '$.formula_version_id',
    ),
    formulaChecksum: digest(root['formula_checksum'], '$.formula_checksum'),
    formulaEngineVersion: text(
      root['formula_engine_version'],
      '$.formula_engine_version',
    ),
    compatibilityVersion: text(
      root['compatibility_version'],
      '$.compatibility_version',
    ),
    backtestEngineVersion: text(
      root['backtest_engine_version'],
      '$.backtest_engine_version',
    ),
    formulaParameters: array(
      root['formula_parameters'],
      '$.formula_parameters',
      64,
    ).map((entry, index) => {
      const parameter = object(entry, `$.formula_parameters[${String(index)}]`);
      return {
        name: text(parameter['name'], '$.formula_parameters.name', 64),
        kind: enumeration(
          parameter['kind'],
          new Set(['integer', 'number']),
          '$.formula_parameters.kind',
        ) as 'integer' | 'number',
        value: signedDecimal(parameter['value'], '$.formula_parameters.value'),
      };
    }),
    provenance: {
      instrumentDatasetVersion: digest(
        provenance['instrument_dataset_version'],
        '$.provenance.instrument_dataset_version',
      ),
      symbolCount,
      runnableCount,
      gapCount,
      sourceIds: {
        signal: boundedTextArray(
          sources['signal'],
          '$.provenance.source_ids.signal',
        ),
        execution: boundedTextArray(
          sources['execution'],
          '$.provenance.source_ids.execution',
        ),
        status: boundedTextArray(
          sources['status'],
          '$.provenance.source_ids.status',
        ),
      },
      digest: digest(provenance['digest'], '$.provenance.digest'),
    },
    period,
    adjustment,
    quantityShares: integer(root['quantity_shares'], '$.quantity_shares', 100),
    costs: {
      commissionBps: decimal(costs['commission_bps'], '$.costs.commission_bps'),
      minimumCommission: decimal(
        costs['minimum_commission'],
        '$.costs.minimum_commission',
      ),
      sellTaxBps: decimal(costs['sell_tax_bps'], '$.costs.sell_tax_bps'),
      slippageBps: decimal(costs['slippage_bps'], '$.costs.slippage_bps'),
    },
    executionRulesVersion: text(
      root['execution_rules_version'],
      '$.execution_rules_version',
    ),
    costModelVersion: text(root['cost_model_version'], '$.cost_model_version'),
    sizingVersion: text(root['sizing_version'], '$.sizing_version'),
    warmupPolicyVersion: text(
      root['warmup_policy_version'],
      '$.warmup_policy_version',
    ),
    metrics: decodeMetrics(root['metrics'], overview.status),
    disclaimer,
    outcomes: {
      total: outcomeTotal,
      succeeded: outcomeSucceeded,
      failed: outcomeFailed,
      dataInsufficient,
      unprocessed,
    },
  };
}

function decodeTradePayload(
  value: JsonValue | undefined,
  symbolValue: string,
  ordinalValue: number,
  expectedRealized?: boolean,
): BacktestTrade {
  const item = object(value, '$.items.payload');
  const realized = flag(item['realized'], '$.items.payload.realized');
  const symbolInPayload =
    item['symbol'] === undefined
      ? symbolValue
      : text(item['symbol'], '$.items.payload.symbol', 16);
  if (symbolInPayload !== symbolValue)
    throw new BacktestProtocolError('$.items.payload.symbol');
  const exitSignalAt = nullableTimestamp(
    item['exit_signal_at'],
    '$.items.payload.exit_signal_at',
  );
  const exitFillAt = nullableTimestamp(
    item['exit_fill_at'],
    '$.items.payload.exit_fill_at',
  );
  if (realized !== (exitSignalAt !== null && exitFillAt !== null))
    throw new BacktestProtocolError('$.items.payload.realized');
  if (expectedRealized !== undefined && realized !== expectedRealized)
    throw new BacktestProtocolError('$.items.payload.realized');
  const markAt = nullableTimestamp(item['mark_at'], '$.items.payload.mark_at');
  const netPnl = nullableSignedDecimal(
    item['net_pnl'],
    '$.items.payload.net_pnl',
  );
  const netReturn = nullableSignedDecimal(
    item['net_return'],
    '$.items.payload.net_return',
  );
  const floatingPnl = nullableSignedDecimal(
    item['floating_pnl'],
    '$.items.payload.floating_pnl',
  );
  const floatingReturn = nullableSignedDecimal(
    item['floating_return'],
    '$.items.payload.floating_return',
  );
  if (
    realized
      ? markAt !== null ||
        netPnl === null ||
        netReturn === null ||
        floatingPnl !== null ||
        floatingReturn !== null
      : markAt === null ||
        netPnl !== null ||
        netReturn !== null ||
        floatingPnl === null ||
        floatingReturn === null
  )
    throw new BacktestProtocolError('$.items.payload.pnl_semantics');
  const orderEvents = decodeOrderEvents(item['order_events']);
  return {
    symbol: symbolValue,
    ordinal: ordinalValue,
    realized,
    entrySignalAt: timestamp(
      item['entry_signal_at'],
      '$.items.payload.entry_signal_at',
    ),
    entryFillAt: timestamp(
      item['entry_fill_at'],
      '$.items.payload.entry_fill_at',
    ),
    exitSignalAt,
    exitFillAt,
    markAt,
    quantity: integer(item['quantity'], '$.items.payload.quantity', 1),
    buyCommission: decimal(
      item['buy_commission'],
      '$.items.payload.buy_commission',
    ),
    sellCommission: decimal(
      item['sell_commission'],
      '$.items.payload.sell_commission',
    ),
    sellTax: decimal(item['sell_tax'], '$.items.payload.sell_tax'),
    slippageCost: decimal(
      item['slippage_cost'],
      '$.items.payload.slippage_cost',
    ),
    referenceGrossPnl: signedDecimal(
      item['reference_gross_pnl'],
      '$.items.payload.reference_gross_pnl',
    ),
    fillGrossPnl: signedDecimal(
      item['fill_gross_pnl'],
      '$.items.payload.fill_gross_pnl',
    ),
    investedCost: positiveDecimal(
      item['invested_cost'],
      '$.items.payload.invested_cost',
    ),
    netPnl,
    netReturn,
    floatingPnl,
    floatingReturn,
    holdingBars: integer(item['holding_bars'], '$.items.payload.holding_bars'),
    holdingDays: integer(item['holding_days'], '$.items.payload.holding_days'),
    orderEvents,
  };
}

function orderSide(value: JsonValue | undefined, path: string): OrderSide {
  return enumeration(value, new Set(['buy', 'sell']), path) as OrderSide;
}

function reasonCode(value: JsonValue | undefined, path: string) {
  const result = text(value, path, 64);
  if (!/^[a-z][a-z0-9_]*$/u.test(result)) throw new BacktestProtocolError(path);
  return result;
}

function positiveDecimal(value: JsonValue | undefined, path: string) {
  const result = decimal(value, path);
  if (result === '0') throw new BacktestProtocolError(path);
  return result;
}

function eventTime(event: BacktestOrderEvent) {
  switch (event.eventType) {
    case 'OrderPending':
      return event.signalAt;
    case 'OrderFilled':
      return event.filledAt;
    case 'OrderUnfilled':
      return event.endedAt;
    case 'OpenTradeMarked':
      return event.markAt;
    default:
      return event.at;
  }
}

function decodeOrderEvents(
  value: JsonValue | undefined,
): readonly BacktestOrderEvent[] {
  const events = array(value, '$.items.payload.order_events', 5000).map(
    (entry, index): BacktestOrderEvent => {
      const path = `$.items.payload.order_events[${String(index)}]`;
      const item = object(entry, path);
      const eventType = text(item['event_type'], `${path}.event_type`, 32);
      const payload = object(item['payload'], `${path}.payload`);
      if (eventType === 'OrderPending') {
        const signalAt = timestamp(
          payload['signal_at'],
          `${path}.payload.signal_at`,
        );
        const eligibleAt = timestamp(
          payload['eligible_at'],
          `${path}.payload.eligible_at`,
        );
        if (Date.parse(eligibleAt) < Date.parse(signalAt))
          throw new BacktestProtocolError(`${path}.payload.eligible_at`);
        return {
          eventType,
          side: orderSide(payload['side'], `${path}.payload.side`),
          signalAt,
          eligibleAt,
        };
      }
      if (eventType === 'IgnoredSignal') {
        const reason = enumeration(
          payload['reason'],
          new Set([
            'already_holding',
            'not_holding',
            'same_side_order_pending',
            'conflicting_signals',
          ]),
          `${path}.payload.reason`,
        ) as Extract<
          BacktestOrderEvent,
          { eventType: 'IgnoredSignal' }
        >['reason'];
        const signal =
          payload['signal'] === null
            ? null
            : orderSide(payload['signal'], `${path}.payload.signal`);
        if ((reason === 'conflicting_signals') !== (signal === null))
          throw new BacktestProtocolError(`${path}.payload.signal`);
        return {
          eventType,
          signal,
          at: timestamp(payload['at'], `${path}.payload.at`),
          reason,
        };
      }
      if (eventType === 'OrderCancelled') {
        const reason = enumeration(
          payload['reason'],
          new Set(['opposite_signal']),
          `${path}.payload.reason`,
        ) as 'opposite_signal';
        return {
          eventType,
          side: orderSide(payload['side'], `${path}.payload.side`),
          at: timestamp(payload['at'], `${path}.payload.at`),
          reason,
        };
      }
      if (eventType === 'OrderBlocked') {
        return {
          eventType,
          side: orderSide(payload['side'], `${path}.payload.side`),
          at: timestamp(payload['at'], `${path}.payload.at`),
          reason: reasonCode(payload['reason'], `${path}.payload.reason`),
        };
      }
      if (eventType === 'OrderFilled') {
        const signalAt = timestamp(
          payload['signal_at'],
          `${path}.payload.signal_at`,
        );
        const filledAt = timestamp(
          payload['filled_at'],
          `${path}.payload.filled_at`,
        );
        if (Date.parse(filledAt) < Date.parse(signalAt))
          throw new BacktestProtocolError(`${path}.payload.filled_at`);
        return {
          eventType,
          side: orderSide(payload['side'], `${path}.payload.side`),
          signalAt,
          filledAt,
          price: positiveDecimal(payload['price'], `${path}.payload.price`),
          quantity: integer(payload['quantity'], `${path}.payload.quantity`, 1),
        };
      }
      if (eventType === 'OrderUnfilled') {
        const signalAt = timestamp(
          payload['signal_at'],
          `${path}.payload.signal_at`,
        );
        const eligibleAt = timestamp(
          payload['eligible_at'],
          `${path}.payload.eligible_at`,
        );
        const endedAt = timestamp(
          payload['ended_at'],
          `${path}.payload.ended_at`,
        );
        if (
          Date.parse(eligibleAt) < Date.parse(signalAt) ||
          Date.parse(endedAt) < Date.parse(signalAt)
        )
          throw new BacktestProtocolError(`${path}.payload.ended_at`);
        return {
          eventType,
          side: orderSide(payload['side'], `${path}.payload.side`),
          signalAt,
          eligibleAt,
          endedAt,
          reason: enumeration(
            payload['reason'],
            new Set(['range_ended_unfilled']),
            `${path}.payload.reason`,
          ) as 'range_ended_unfilled',
        };
      }
      if (eventType === 'OpenTradeMarked') {
        const entryAt = timestamp(
          payload['entry_at'],
          `${path}.payload.entry_at`,
        );
        const markAt = timestamp(payload['mark_at'], `${path}.payload.mark_at`);
        if (Date.parse(markAt) < Date.parse(entryAt))
          throw new BacktestProtocolError(`${path}.payload.mark_at`);
        return {
          eventType,
          entryAt,
          entryPrice: positiveDecimal(
            payload['entry_price'],
            `${path}.payload.entry_price`,
          ),
          quantity: integer(payload['quantity'], `${path}.payload.quantity`, 1),
          markAt,
          markPrice: positiveDecimal(
            payload['mark_price'],
            `${path}.payload.mark_price`,
          ),
          floatingPnl: signedDecimal(
            payload['floating_pnl'],
            `${path}.payload.floating_pnl`,
          ),
        };
      }
      throw new BacktestProtocolError(`${path}.event_type`);
    },
  );
  if (events.length === 0)
    throw new BacktestProtocolError('$.items.payload.order_events');
  for (let index = 1; index < events.length; index += 1) {
    if (
      Date.parse(eventTime(events[index - 1])) >
      Date.parse(eventTime(events[index]))
    )
      throw new BacktestProtocolError('$.items.payload.order_events');
  }
  return events;
}

function decodeTradePage(
  value: JsonValue | undefined,
  expectedRealized: boolean,
): BacktestCursorPage<BacktestTrade> {
  const root = object(value, '$');
  return {
    items: array(root['items'], '$.items', 100).map((entry, index) => {
      const item = object(entry, `$.items[${String(index)}]`);
      const symbolValue = text(item['symbol'], '$.items.symbol', 16);
      if (!symbolPattern.test(symbolValue))
        throw new BacktestProtocolError('$.items.symbol');
      const ordinalValue = integer(item['ordinal'], '$.items.ordinal');
      return decodeTradePayload(
        item['payload'],
        symbolValue,
        ordinalValue,
        expectedRealized,
      );
    }),
    nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
  };
}

function decodeGroupPage(
  value: JsonValue | undefined,
  expectedDimension: BacktestGroup['dimension'],
): BacktestCursorPage<BacktestGroup> {
  const root = object(value, '$');
  return {
    items: array(root['items'], '$.items', 100).map((entry, index) => {
      const item = object(entry, `$.items[${String(index)}]`);
      const dimension = enumeration(
        item['dimension'],
        new Set(['symbol', 'entry_month', 'entry_year']),
        '$.items.dimension',
      ) as BacktestGroup['dimension'];
      if (dimension !== expectedDimension)
        throw new BacktestProtocolError('$.items.dimension');
      const payload = object(item['payload'], '$.items.payload');
      const realizedCount = integer(
        payload['realized_count'],
        '$.items.payload.realized_count',
      );
      const denominator = integer(
        payload['realized_denominator'],
        '$.items.payload.realized_denominator',
      );
      const positive = integer(
        payload['positive_count'],
        '$.items.payload.positive_count',
      );
      const negative = integer(
        payload['negative_count'],
        '$.items.payload.negative_count',
      );
      const zero = integer(payload['zero_count'], '$.items.payload.zero_count');
      if (
        realizedCount <= 0 ||
        realizedCount > denominator ||
        positive + negative + zero !== realizedCount
      )
        throw new BacktestProtocolError('$.items.payload.realized_count');
      const shareOfAll = signedDecimal(
        payload['share_of_all'],
        '$.items.payload.share_of_all',
      );
      const winRate = signedDecimal(
        payload['win_rate'],
        '$.items.payload.win_rate',
      );
      if (
        ratiosDiffer(
          boundedRatio(shareOfAll, '$.items.payload.share_of_all'),
          realizedCount / denominator,
        ) ||
        ratiosDiffer(
          boundedRatio(winRate, '$.items.payload.win_rate'),
          positive / realizedCount,
        )
      )
        throw new BacktestProtocolError('$.items.payload.ratios');
      const payoffRatio = nullableSignedDecimal(
        payload['payoff_ratio'],
        '$.items.payload.payoff_ratio',
      );
      const payoffReason = nullableText(
        payload['payoff_ratio_reason'],
        '$.items.payload.payoff_ratio_reason',
      );
      const expectedPayoffReason =
        positive === 0 && negative === 0
          ? 'no_positive_or_negative_returns'
          : positive === 0
            ? 'no_positive_returns'
            : negative === 0
              ? 'no_negative_returns'
              : null;
      if (
        (expectedPayoffReason === null) !== (payoffRatio !== null) ||
        payoffReason !== expectedPayoffReason ||
        (payoffRatio !== null && Number(payoffRatio) < 0)
      )
        throw new BacktestProtocolError('$.items.payload.payoff_ratio');
      return {
        dimension,
        key: text(item['key'], '$.items.key', 64),
        realizedCount,
        realizedDenominator: denominator,
        positiveCount: positive,
        negativeCount: negative,
        zeroCount: zero,
        shareOfAll,
        winRate,
        meanNetReturn: signedDecimal(
          payload['mean_net_return'],
          '$.items.payload.mean_net_return',
        ),
        medianNetReturn: signedDecimal(
          payload['median_net_return'],
          '$.items.payload.median_net_return',
        ),
        payoffRatio,
        netPnlTotal: signedDecimal(
          payload['net_pnl_total'],
          '$.items.payload.net_pnl_total',
        ),
        averageHoldingDays: signedDecimal(
          payload['average_holding_days'],
          '$.items.payload.average_holding_days',
        ),
      };
    }),
    nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
  };
}

function decodeFailurePage(
  value: JsonValue | undefined,
): BacktestCursorPage<BacktestFailure> {
  const root = object(value, '$');
  return {
    items: array(root['items'], '$.items', 100).map((entry, index) => {
      const item = object(entry, `$.items[${String(index)}]`);
      const symbolValue = text(item['symbol'], '$.items.symbol', 16);
      if (!symbolPattern.test(symbolValue))
        throw new BacktestProtocolError('$.items.symbol');
      return {
        symbol: symbolValue,
        ordinal: integer(item['ordinal'], '$.items.ordinal'),
        reason: text(item['reason'], '$.items.reason', 128),
        detail: object(item['detail'], '$.items.detail'),
      };
    }),
    nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
  };
}

function decodeReportLogPage(
  value: JsonValue | undefined,
): BacktestCursorPage<BacktestLog> {
  const root = object(value, '$');
  return {
    items: array(root['items'], '$.items', 100).map((entry, index) => {
      const item = object(entry, `$.items[${String(index)}]`);
      return {
        ordinal: integer(item['ordinal'], '$.items.ordinal'),
        level: text(item['level'], '$.items.level', 32),
        message: text(item['message'], '$.items.message', 2048),
        detail: object(item['detail'], '$.items.detail'),
      };
    }),
    nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
  };
}

function chartNumber(value: JsonValue | undefined, path: string) {
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new BacktestProtocolError(path);
    return { numeric: value, text: value.toString() };
  }
  if (typeof value !== 'string' || !/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/u.test(value))
    throw new BacktestProtocolError(path);
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) throw new BacktestProtocolError(path);
  return { numeric, text: value };
}

function decodeReplayBar(
  value: JsonValue | undefined,
  path: string,
  expected: {
    readonly symbol: string;
    readonly adjustment: MarketAdjustment;
    readonly period?: MarketPeriod;
  },
): MarketBar {
  const item = object(value, path);
  const symbolValue = text(item['symbol'], `${path}.symbol`, 16);
  const period = enumeration(
    item['period'],
    new Set(['1d', '1w', '60m']),
    `${path}.period`,
  ) as MarketPeriod;
  const adjustment = enumeration(
    item['adjustment'],
    new Set(['none', 'qfq', 'hfq']),
    `${path}.adjustment`,
  ) as MarketAdjustment;
  if (
    symbolValue !== expected.symbol ||
    adjustment !== expected.adjustment ||
    (expected.period !== undefined && period !== expected.period)
  )
    throw new BacktestProtocolError(`${path}.identity`);
  const open = chartNumber(item['open'], `${path}.open`);
  const high = chartNumber(item['high'], `${path}.high`);
  const low = chartNumber(item['low'], `${path}.low`);
  const close = chartNumber(item['close'], `${path}.close`);
  if (
    high.numeric < Math.max(open.numeric, close.numeric) ||
    low.numeric > Math.min(open.numeric, close.numeric) ||
    low.numeric > high.numeric
  )
    throw new BacktestProtocolError(`${path}.ohlc`);
  return {
    symbol: symbolValue,
    timestamp: timestamp(item['timestamp'], `${path}.timestamp`),
    period,
    adjustment,
    open: open.numeric,
    high: high.numeric,
    low: low.numeric,
    close: close.numeric,
    priceText: {
      open: open.text,
      high: high.text,
      low: low.text,
      close: close.text,
    },
    volume: integer(item['volume'], `${path}.volume`),
    status: enumeration(
      item['status'] ?? 'unknown',
      new Set(['unknown', 'normal', 'suspended', 'limit_up', 'limit_down']),
      `${path}.status`,
    ) as MarketBar['status'],
    direction:
      close.numeric > open.numeric
        ? 'rise'
        : close.numeric < open.numeric
          ? 'fall'
          : 'flat',
  };
}

function decodePinnedIdentity(
  value: JsonValue | undefined,
  path: string,
): BacktestPinnedIdentity {
  const item = object(value, path);
  return {
    manifestRecordId: digest(
      item['manifest_record_id'],
      `${path}.manifest_record_id`,
    ),
    datasetVersion: digest(item['dataset_version'], `${path}.dataset_version`),
    routeVersion: digest(item['route_version'], `${path}.route_version`),
    source: text(item['source'], `${path}.source`, 64),
    dataCutoff: timestamp(item['data_cutoff'], `${path}.data_cutoff`),
  };
}

function digestArray(
  value: JsonValue | undefined,
  path: string,
  maximum: number,
) {
  return array(value, path, maximum).map((entry, index) =>
    digest(entry, `${path}[${String(index)}]`),
  );
}

function decodeReplay(
  value: JsonValue | undefined,
  expectedRunId: string,
  expectedSymbol: string,
  expectedOrdinal: number,
): BacktestReplay {
  const root = object(value, '$');
  const runId = identity(root['run_id'], '$.run_id');
  const symbolValue = text(root['symbol'], '$.symbol', 16);
  const tradeOrdinal = integer(root['trade_ordinal'], '$.trade_ordinal');
  if (
    runId !== expectedRunId ||
    symbolValue !== expectedSymbol ||
    tradeOrdinal !== expectedOrdinal
  )
    throw new BacktestProtocolError('$.request_binding');
  const period = enumeration(
    root['period'],
    new Set(['1d', '1w', '60m']),
    '$.period',
  ) as MarketPeriod;
  const adjustment = enumeration(
    root['adjustment'],
    new Set(['none', 'qfq', 'hfq']),
    '$.adjustment',
  ) as MarketAdjustment;
  const bars = array(root['bars'], '$.bars', 500).map((entry, index) =>
    decodeReplayBar(entry, `$.bars[${String(index)}]`, {
      symbol: symbolValue,
      period,
      adjustment,
    }),
  );
  if (bars.length === 0) throw new BacktestProtocolError('$.bars');
  for (let index = 1; index < bars.length; index += 1) {
    if (
      Date.parse(bars[index - 1].timestamp) >= Date.parse(bars[index].timestamp)
    )
      throw new BacktestProtocolError('$.bars.timestamp');
  }
  const formula = object(root['formula'], '$.formula');
  const signalSeriesId = digest(
    formula['signal_series_id'],
    '$.formula.signal_series_id',
  );
  const formulaVersionId = identity(
    formula['formula_version_id'],
    '$.formula.formula_version_id',
  );
  const numericOutputs = array(
    formula['numeric_outputs'],
    '$.formula.numeric_outputs',
    32,
  ).map((entry, index) => {
    const output = object(entry, `$.formula.numeric_outputs[${String(index)}]`);
    const values = array(
      output['values'],
      `$.formula.numeric_outputs[${String(index)}].values`,
      500,
    ).map((item, itemIndex) =>
      item === null
        ? null
        : chartNumber(
            item,
            `$.formula.numeric_outputs[${String(index)}].values[${String(itemIndex)}]`,
          ).numeric,
    );
    if (values.length !== bars.length)
      throw new BacktestProtocolError('$.formula.numeric_outputs.values');
    return {
      name: text(output['name'], '$.formula.numeric_outputs.name', 64),
      values,
    };
  });
  const signals = array(formula['signals'], '$.formula.signals', 2).map(
    (entry, index) => {
      const signal = object(entry, `$.formula.signals[${String(index)}]`);
      const name = enumeration(
        signal['name'],
        new Set(['BUY', 'SELL']),
        '$.formula.signals.name',
      ) as 'BUY' | 'SELL';
      const values = array(
        signal['values'],
        '$.formula.signals.values',
        500,
      ).map((item) =>
        item === null ? null : flag(item, '$.formula.signals.values'),
      );
      if (values.length !== bars.length)
        throw new BacktestProtocolError('$.formula.signals.values');
      return { name, values };
    },
  );
  if (
    signals.length !== 2 ||
    signals[0]?.name !== 'BUY' ||
    signals[1]?.name !== 'SELL'
  )
    throw new BacktestProtocolError('$.formula.signals');
  const provenance = object(root['provenance'], '$.provenance');
  const pinned = {
    signal: decodePinnedIdentity(provenance['signal'], '$.provenance.signal'),
    execution: decodePinnedIdentity(
      provenance['execution'],
      '$.provenance.execution',
    ),
    status: decodePinnedIdentity(provenance['status'], '$.provenance.status'),
  };
  const rawTrade = object(root['trade'], '$.trade');
  const trade = decodeTradePayload(rawTrade, symbolValue, tradeOrdinal);
  const tradeSignalSeries = digest(
    rawTrade['signal_series_id'],
    '$.trade.signal_series_id',
  );
  const tradeFormula = identity(
    rawTrade['formula_version_id'],
    '$.trade.formula_version_id',
  );
  const marketManifests = digestArray(
    rawTrade['market_manifest_ids'],
    '$.trade.market_manifest_ids',
    2,
  );
  const statusManifests = digestArray(
    rawTrade['status_manifest_ids'],
    '$.trade.status_manifest_ids',
    1,
  );
  if (
    tradeSignalSeries !== signalSeriesId ||
    tradeFormula !== formulaVersionId ||
    marketManifests.length !== 2 ||
    marketManifests[0] !== pinned.signal.manifestRecordId ||
    marketManifests[1] !== pinned.execution.manifestRecordId ||
    statusManifests.length !== 1 ||
    statusManifests[0] !== pinned.status.manifestRecordId
  )
    throw new BacktestProtocolError('$.trade.provenance');
  const fillMarkers = array(root['fill_markers'], '$.fill_markers', 2).map(
    (entry, index) => {
      const marker = object(entry, `$.fill_markers[${String(index)}]`);
      const anchorOrdinal = integer(
        marker['anchor_ordinal'],
        '$.fill_markers.anchor_ordinal',
      );
      return {
        side: enumeration(
          marker['side'],
          new Set(['buy', 'sell']),
          '$.fill_markers.side',
        ) as 'buy' | 'sell',
        signalAt: timestamp(marker['signal_at'], '$.fill_markers.signal_at'),
        filledAt: timestamp(marker['filled_at'], '$.fill_markers.filled_at'),
        anchorOrdinal,
        referenceOpen: signedDecimal(
          marker['reference_open'],
          '$.fill_markers.reference_open',
        ),
        fillPrice: signedDecimal(
          marker['fill_price'],
          '$.fill_markers.fill_price',
        ),
        quantity: integer(marker['quantity'], '$.fill_markers.quantity', 1),
      };
    },
  );
  const executionEvidence = array(
    root['execution_evidence'],
    '$.execution_evidence',
    2,
  ).map((entry, index) => {
    const evidence = object(entry, `$.execution_evidence[${String(index)}]`);
    return {
      side: enumeration(
        evidence['side'],
        new Set(['buy', 'sell']),
        '$.execution_evidence.side',
      ) as 'buy' | 'sell',
      filledAt: timestamp(
        evidence['filled_at'],
        '$.execution_evidence.filled_at',
      ),
      bar: decodeReplayBar(evidence['bar'], '$.execution_evidence.bar', {
        symbol: symbolValue,
        adjustment,
        period: period === '1w' ? '1d' : period,
      }),
    };
  });
  if (
    executionEvidence.length !== fillMarkers.length ||
    executionEvidence.some(
      (evidence) =>
        !fillMarkers.some(
          (marker) =>
            marker.side === evidence.side &&
            marker.filledAt === evidence.filledAt,
        ),
    )
  )
    throw new BacktestProtocolError('$.execution_evidence');
  return {
    runId,
    snapshotId: digest(root['snapshot_id'], '$.snapshot_id'),
    resultHash:
      root['result_hash'] === null
        ? null
        : digest(root['result_hash'], '$.result_hash'),
    symbol: symbolValue,
    tradeOrdinal,
    period,
    adjustment,
    bars,
    formula: {
      signalSeriesId,
      formulaVersionId,
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
      numericOutputs,
      signals,
    },
    trade,
    fillMarkers,
    executionEvidence,
    provenance: pinned,
    nextCursor: nullableText(root['next_cursor'], '$.next_cursor'),
  };
}

export function backtestExportUrl(
  runId: string,
  section: 'groups' | 'trades' | 'open' | 'failures' | 'logs',
  format: 'json' | 'csv',
) {
  if (!uuidPattern.test(runId)) throw new Error('Invalid backtest export run');
  return `/api/backtests/${runId}/export/${section}.${format}`;
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
): BacktestApi & BacktestReportApi {
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
    async getReport(runId, { signal } = {}) {
      return decodeReport(
        await client.get(`/backtests/${encodeURIComponent(runId)}/report`, {
          signal,
        }),
        runId,
      );
    },
    async getTrades(runId, kind, { cursor, signal } = {}) {
      return decodeTradePage(
        await client.get(
          query(
            `/backtests/${encodeURIComponent(runId)}/${
              kind === 'realized' ? 'trades' : 'open'
            }`,
            { cursor: cursor ?? undefined, limit: '100' },
          ),
          { signal },
        ),
        kind === 'realized',
      );
    },
    async getGroups(runId, dimension, { cursor, signal } = {}) {
      return decodeGroupPage(
        await client.get(
          query(`/backtests/${encodeURIComponent(runId)}/groups`, {
            dimension,
            cursor: cursor ?? undefined,
            limit: '100',
          }),
          { signal },
        ),
        dimension,
      );
    },
    async getFailures(runId, { cursor, signal } = {}) {
      return decodeFailurePage(
        await client.get(
          query(`/backtests/${encodeURIComponent(runId)}/failures`, {
            cursor: cursor ?? undefined,
            limit: '100',
          }),
          { signal },
        ),
      );
    },
    async getReportLogs(runId, { cursor, signal } = {}) {
      return decodeReportLogPage(
        await client.get(
          query(`/backtests/${encodeURIComponent(runId)}/logs`, {
            cursor: cursor ?? undefined,
            limit: '100',
          }),
          { signal },
        ),
      );
    },
    async getReplay(runId, symbol, tradeOrdinal, { cursor, signal } = {}) {
      if (!uuidPattern.test(runId) || !symbolPattern.test(symbol))
        throw new BacktestProtocolError('request.replay');
      if (!Number.isSafeInteger(tradeOrdinal) || tradeOrdinal < 0)
        throw new BacktestProtocolError('request.trade_ordinal');
      return decodeReplay(
        await client.get(
          query(
            `/backtests/${encodeURIComponent(runId)}/trades/${encodeURIComponent(
              symbol,
            )}/${String(tradeOrdinal)}/replay`,
            { limit: '500', cursor: cursor ?? undefined },
          ),
          { signal },
        ),
        runId,
        symbol,
        tradeOrdinal,
      );
    },
  };
}

export const backtestApi = createBacktestApi();
