export type CostValues = {
  readonly quantityShares: number;
  readonly commissionBps: string;
  readonly minimumCommission: string;
  readonly sellTaxBps: string;
  readonly slippageBps: string;
};
function canonical(value: string) {
  if (!/^\d+(?:\.\d+)?$/u.test(value)) return value;
  const [whole = '0', fraction] = value.split('.');
  const normalizedWhole = whole.replace(/^0+(?=\d)/u, '');
  const normalizedFraction = fraction?.replace(/0+$/u, '');
  return normalizedFraction === undefined || normalizedFraction === ''
    ? normalizedWhole
    : `${normalizedWhole}.${normalizedFraction}`;
}
export function CostsStep({
  values,
  onChange,
}: {
  readonly values: CostValues;
  readonly onChange: (change: Partial<CostValues>) => void;
}) {
  return (
    <section className="backtest-step" aria-labelledby="backtest-costs-heading">
      <h3 id="backtest-costs-heading" tabIndex={-1}>
        4. 成本
      </h3>
      <p>固定手数按 100 股整数倍模拟；金额与费率使用精确小数字符串提交。</p>
      <div className="backtest-fields">
        <label>
          每次买入股数
          <input
            type="number"
            min="100"
            step="100"
            value={values.quantityShares}
            onChange={(event) =>
              onChange({ quantityShares: Number(event.target.value) })
            }
          />
        </label>
        <label>
          佣金（基点）
          <input
            inputMode="decimal"
            value={values.commissionBps}
            onChange={(event) =>
              onChange({ commissionBps: event.target.value })
            }
            onBlur={(event) =>
              onChange({ commissionBps: canonical(event.target.value) })
            }
          />
        </label>
        <label>
          最低佣金（元）
          <input
            inputMode="decimal"
            value={values.minimumCommission}
            onChange={(event) =>
              onChange({ minimumCommission: event.target.value })
            }
            onBlur={(event) =>
              onChange({ minimumCommission: canonical(event.target.value) })
            }
          />
        </label>
        <label>
          卖出印花税（基点）
          <input
            inputMode="decimal"
            value={values.sellTaxBps}
            onChange={(event) => onChange({ sellTaxBps: event.target.value })}
            onBlur={(event) =>
              onChange({ sellTaxBps: canonical(event.target.value) })
            }
          />
        </label>
        <label>
          滑点（基点）
          <input
            inputMode="decimal"
            value={values.slippageBps}
            onChange={(event) => onChange({ slippageBps: event.target.value })}
            onBlur={(event) =>
              onChange({ slippageBps: canonical(event.target.value) })
            }
          />
        </label>
      </div>
    </section>
  );
}
