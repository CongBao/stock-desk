import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import { MarketChart, type FormulaChartLayer } from '../market/MarketChart';
import type { MarketBar } from '../market/marketApi';
import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';
import type {
  FormulaPreview as FormulaPreviewResult,
  FormulaPlacement,
} from './formulaApi';

const periods: readonly {
  readonly value: MarketPeriod;
  readonly label: string;
}[] = [
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '60m', label: '60 分钟' },
];

type FormulaPreviewProps = {
  readonly adjustment: MarketAdjustment;
  readonly bars?: readonly MarketBar[];
  readonly errorMessage?: string;
  readonly isLoading: boolean;
  readonly onAdjustmentChange: (adjustment: MarketAdjustment) => void;
  readonly onPeriodChange: (period: MarketPeriod) => void;
  readonly onPreview: () => void;
  readonly onSymbolChange: (symbol: string) => void;
  readonly period: MarketPeriod;
  readonly placement: FormulaPlacement;
  readonly preview?: FormulaPreviewResult;
  readonly previewDisabled: boolean;
  readonly symbol: string;
};

export function FormulaPreview({
  adjustment,
  bars,
  errorMessage,
  isLoading,
  onAdjustmentChange,
  onPeriodChange,
  onPreview,
  onSymbolChange,
  period,
  placement,
  preview,
  previewDisabled,
  symbol,
}: FormulaPreviewProps) {
  const formulaLayer: FormulaChartLayer | undefined =
    preview === undefined
      ? undefined
      : {
          placement,
          timestamps: preview.timestamps,
          numericOutputs: preview.numericOutputs,
          signals: preview.signals,
        };
  return (
    <section
      className="formula-preview-panel"
      aria-label="公式图表预览"
      data-guidance-target="formula-preview"
    >
      <header className="formula-panel-heading formula-preview-heading">
        <div>
          <span className="panel-kicker">PREVIEW / SAVED REVISION</span>
          <h3>K 线与公式预览</h3>
        </div>
        {preview === undefined ? (
          <span className="preview-revision-badge">尚未运行</span>
        ) : (
          <span className="preview-revision-badge" data-ready="true">
            v{preview.formulaVersion} · {preview.engineVersion}
          </span>
        )}
      </header>
      <div className="formula-preview-controls">
        <label>
          <span>证券代码</span>
          <input
            value={symbol}
            aria-label="预览证券代码"
            spellCheck={false}
            onChange={(event) =>
              onSymbolChange(event.currentTarget.value.toUpperCase())
            }
          />
        </label>
        <div
          className="period-selector"
          role="radiogroup"
          aria-label="预览周期"
        >
          {periods.map((item) => (
            <button
              key={item.value}
              type="button"
              role="radio"
              aria-checked={period === item.value}
              onClick={() => onPeriodChange(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <label>
          <span>复权</span>
          <select
            aria-label="预览复权方式"
            value={adjustment}
            onChange={(event) =>
              onAdjustmentChange(event.currentTarget.value as MarketAdjustment)
            }
          >
            <option value="none">不复权</option>
            <option value="qfq">前复权</option>
            <option value="hfq">后复权</option>
          </select>
        </label>
        <AsyncActionButton
          className="formula-preview-run"
          type="button"
          pending={isLoading}
          disabled={previewDisabled || isLoading}
          onClick={onPreview}
        >
          运行预览
        </AsyncActionButton>
      </div>
      <p className="formula-preview-policy">
        预览只运行已保存且校验通过的不可变版本，不会在输入时自动计算。
      </p>
      <MarketChart
        bars={bars}
        errorMessage={errorMessage}
        formula={formulaLayer}
        formulaEmptyPlacement={placement}
        formulaEmptyMessage={
          placement === 'main'
            ? '保存并运行预览后在 K 线主图叠加公式输出与买卖点'
            : '保存并运行预览后显示公式副图与买卖点'
        }
        isLoading={isLoading}
      />
      {preview === undefined ? null : (
        <footer className="formula-preview-summary" aria-live="polite">
          <span>{preview.numericOutputs.length} 条输出</span>
          <span>
            {preview.signals
              .find((item) => item.name === 'BUY')
              ?.values.filter(Boolean).length ?? 0}{' '}
            个买点
          </span>
          <span>
            {preview.signals
              .find((item) => item.name === 'SELL')
              ?.values.filter(Boolean).length ?? 0}{' '}
            个卖点
          </span>
        </footer>
      )}
    </section>
  );
}
