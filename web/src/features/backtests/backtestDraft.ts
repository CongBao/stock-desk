import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';
import type { BacktestScope } from './backtestApi';

export const BACKTEST_DRAFT_KEY = 'stock-desk.backtest-draft.v1';

export type BacktestDraft = {
  readonly formulaId: string;
  readonly formulaVersionId: string;
  readonly formulaParameters: Readonly<Record<string, number>>;
  readonly scope: BacktestScope;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly startDate: string;
  readonly endDate: string;
  readonly quantityShares: number;
  readonly commissionBps: string;
  readonly minimumCommission: string;
  readonly sellTaxBps: string;
  readonly slippageBps: string;
};

const periods = new Set(['1d', '1w', '60m']);
const adjustments = new Set(['none', 'qfq', 'hfq']);
const datePattern = /^\d{4}-\d{2}-\d{2}$/u;
const symbolPattern = /^\d{6}\.(?:SH|SZ|BJ)$/u;
const digestPattern = /^sha256:[0-9a-f]{64}$/u;
const uuidPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const decimalPattern = /^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$/u;
const MAX_SAFE_PARAMETER = 2 ** 53 - 1;
const MAX_QUANTITY_SHARES = 100_000_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function scope(value: unknown): BacktestScope | null {
  if (!isRecord(value) || typeof value.kind !== 'string') return null;
  if (
    value.kind === 'single' &&
    typeof value.symbol === 'string' &&
    symbolPattern.test(value.symbol)
  ) {
    return { kind: 'single', symbol: value.symbol };
  }
  if (
    value.kind === 'preset' &&
    typeof value.poolId === 'string' &&
    value.poolId.length >= 8 &&
    value.poolId.length <= 71 &&
    /^preset:[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/u.test(value.poolId) &&
    typeof value.snapshotId === 'string' &&
    digestPattern.test(value.snapshotId)
  ) {
    return {
      kind: 'preset',
      poolId: value.poolId,
      snapshotId: value.snapshotId,
    };
  }
  if (
    value.kind === 'custom' &&
    typeof value.poolId === 'string' &&
    uuidPattern.test(value.poolId) &&
    Number.isSafeInteger(value.revision) &&
    (value.revision as number) > 0
  ) {
    return {
      kind: 'custom',
      poolId: value.poolId,
      revision: value.revision as number,
    };
  }
  return null;
}

function parseDraft(value: unknown): BacktestDraft | null {
  if (!isRecord(value)) return null;
  const parsedScope = scope(value.scope);
  if (
    typeof value.formulaId !== 'string' ||
    !uuidPattern.test(value.formulaId) ||
    typeof value.formulaVersionId !== 'string' ||
    !uuidPattern.test(value.formulaVersionId) ||
    !isRecord(value.formulaParameters) ||
    Object.keys(value.formulaParameters).length > 64 ||
    parsedScope === null ||
    typeof value.period !== 'string' ||
    !periods.has(value.period) ||
    typeof value.adjustment !== 'string' ||
    !adjustments.has(value.adjustment) ||
    typeof value.startDate !== 'string' ||
    !datePattern.test(value.startDate) ||
    typeof value.endDate !== 'string' ||
    !datePattern.test(value.endDate) ||
    !Number.isSafeInteger(value.quantityShares) ||
    (value.quantityShares as number) <= 0 ||
    (value.quantityShares as number) > MAX_QUANTITY_SHARES ||
    (value.quantityShares as number) % 100 !== 0
  )
    return null;
  if (
    !isRealDate(value.startDate) ||
    !isRealDate(value.endDate) ||
    value.startDate >= value.endDate
  )
    return null;
  const parameters: Record<string, number> = {};
  for (const [name, parameter] of Object.entries(value.formulaParameters)) {
    if (
      !/^[A-Z][A-Z0-9_]{0,63}$/u.test(name) ||
      typeof parameter !== 'number' ||
      !Number.isFinite(parameter) ||
      Math.abs(parameter) > MAX_SAFE_PARAMETER
    )
      return null;
    parameters[name] = parameter;
  }
  for (const name of [
    'commissionBps',
    'minimumCommission',
    'sellTaxBps',
    'slippageBps',
  ] as const) {
    if (
      typeof value[name] !== 'string' ||
      value[name].length > 64 ||
      !decimalPattern.test(value[name])
    )
      return null;
  }
  if (
    [value.commissionBps, value.sellTaxBps, value.slippageBps].some(
      (cost) => Number(cost) > 10_000,
    )
  )
    return null;
  return {
    formulaId: value.formulaId,
    formulaVersionId: value.formulaVersionId,
    formulaParameters: parameters,
    scope: parsedScope,
    period: value.period as MarketPeriod,
    adjustment: value.adjustment as MarketAdjustment,
    startDate: value.startDate,
    endDate: value.endDate,
    quantityShares: value.quantityShares as number,
    commissionBps: value.commissionBps as string,
    minimumCommission: value.minimumCommission as string,
    sellTaxBps: value.sellTaxBps as string,
    slippageBps: value.slippageBps as string,
  };
}

function isRealDate(value: string): boolean {
  if (!datePattern.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year ?? 0, (month ?? 1) - 1, day ?? 0));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() + 1 === month &&
    date.getUTCDate() === day
  );
}

export function validateBacktestDraft(value: unknown): BacktestDraft | null {
  return parseDraft(value);
}

export function saveBacktestDraft(
  draft: BacktestDraft,
  storage: Storage = localStorage,
): boolean {
  const valid = parseDraft(draft);
  if (valid === null) return false;
  try {
    storage.setItem(
      BACKTEST_DRAFT_KEY,
      JSON.stringify({ version: 1, draft: valid }),
    );
    return true;
  } catch {
    return false;
  }
}

export function loadBacktestDraft(
  storage: Storage = localStorage,
): BacktestDraft | null {
  let raw: string | null;
  try {
    raw = storage.getItem(BACKTEST_DRAFT_KEY);
  } catch {
    return null;
  }
  if (raw === null) return null;
  try {
    const envelope: unknown = JSON.parse(raw);
    if (!isRecord(envelope) || envelope.version !== 1) return null;
    return parseDraft(envelope.draft);
  } catch {
    return null;
  }
}

export function clearBacktestDraft(storage: Storage = localStorage) {
  try {
    storage.removeItem(BACKTEST_DRAFT_KEY);
  } catch {
    /* storage can be unavailable */
  }
}
