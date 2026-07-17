export type ExecutionStatusEvidenceLevel =
  'authoritative' | 'basic_no_price_limits' | 'mixed';

export const basicExecutionStatusWarning =
  '基础成交假设：停牌依据 BaoStock tradestatus；未校验历史涨跌停。T+1、交易日和下一周期开盘仍按规则处理，结果可能高估可成交机会。';

export function executionStatusEvidenceLabel(
  level: ExecutionStatusEvidenceLevel,
) {
  if (level === 'authoritative') return '严格（含历史涨跌停证据）';
  if (level === 'basic_no_price_limits') return '基础（未校验历史涨跌停）';
  return '混合（部分证券未校验历史涨跌停）';
}
