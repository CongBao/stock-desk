import type { MarketAdjustment, MarketPeriod } from '../../market/marketStore';

export type PeriodStepProps = {
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly startDate: string;
  readonly endDate: string;
  readonly onChange: (
    change: Partial<{
      period: MarketPeriod;
      adjustment: MarketAdjustment;
      startDate: string;
      endDate: string;
    }>,
  ) => void;
};

export function PeriodStep(props: PeriodStepProps) {
  return (
    <section
      className="backtest-step"
      aria-labelledby="backtest-period-heading"
    >
      <h3 id="backtest-period-heading" tabIndex={-1}>
        3. 周期
      </h3>
      <fieldset>
        <legend>回测周期</legend>
        {(
          [
            ['1d', '日线'],
            ['1w', '周线'],
            ['60m', '60 分钟'],
          ] as const
        ).map(([value, label]) => (
          <label key={value}>
            <input
              type="radio"
              name="period"
              checked={props.period === value}
              onChange={() => props.onChange({ period: value })}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <label>
        复权方式
        <select
          value={props.adjustment}
          onChange={(event) =>
            props.onChange({
              adjustment: event.target.value as MarketAdjustment,
            })
          }
        >
          <option value="none">不复权</option>
          <option value="qfq">前复权</option>
          <option value="hfq">后复权</option>
        </select>
      </label>
      <div className="backtest-fields">
        <label>
          开始日期（上海时区，含）
          <input
            type="date"
            value={props.startDate}
            onChange={(event) =>
              props.onChange({ startDate: event.target.value })
            }
          />
        </label>
        <label>
          结束日期（上海时区，不含）
          <input
            type="date"
            value={props.endDate}
            onChange={(event) =>
              props.onChange({ endDate: event.target.value })
            }
          />
        </label>
      </div>
      <p className="field-help">
        日期按 Asia/Shanghai 的半开区间解释；日线、周线和 60
        分钟线均只使用对应周期已完成的收盘信号。
      </p>
    </section>
  );
}
