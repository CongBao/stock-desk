import type { CSSProperties } from 'react';

type CandleStyle = CSSProperties & {
  '--body-bottom': string;
  '--body-height': string;
  '--wick-bottom': string;
  '--wick-height': string;
};

const previewCandles = [
  ['rise', '20%', '22%', '10%', '46%'],
  ['fall', '31%', '16%', '18%', '41%'],
  ['fall', '25%', '20%', '14%', '44%'],
  ['rise', '38%', '18%', '25%', '43%'],
  ['rise', '43%', '25%', '30%', '50%'],
  ['fall', '52%', '18%', '39%', '39%'],
  ['rise', '48%', '22%', '34%', '48%'],
  ['rise', '58%', '20%', '44%', '43%'],
  ['fall', '55%', '26%', '42%', '48%'],
  ['fall', '47%', '19%', '34%', '42%'],
  ['rise', '51%', '21%', '36%', '46%'],
  ['rise', '64%', '17%', '50%', '40%'],
  ['fall', '60%', '22%', '47%', '44%'],
  ['rise', '68%', '20%', '55%', '41%'],
  ['fall', '62%', '19%', '50%', '39%'],
  ['fall', '56%', '24%', '43%', '46%'],
  ['rise', '61%', '18%', '48%', '39%'],
  ['rise', '70%', '17%', '57%', '38%'],
] as const;

const histogramBars = [
  31, 46, 38, 58, 72, 50, 35, 18, 25, 47, 61, 74, 55, 39, 22, 34, 52, 68,
];

export function MarketPage() {
  return (
    <article className="market-page">
      <header className="page-heading market-heading">
        <div>
          <span className="page-kicker">MARKET WORKSPACE</span>
          <h2 data-page-heading tabIndex={-1}>
            行情工作区
          </h2>
          <p>K 线为主图，公式和指标作为紧邻的副图。</p>
        </div>
        <span className="preview-badge">布局预览 / 非实时数据</span>
      </header>

      <div
        className="market-toolbar"
        role="group"
        aria-label="行情布局工具栏预览"
      >
        <div className="instrument-placeholder">
          <span className="instrument-code">— — — — — —</span>
          <span>证券标的待数据源接入</span>
        </div>
        <div
          className="toolbar-chips"
          role="group"
          aria-label="预设周期与复权方式"
        >
          <span>日线</span>
          <span>前复权</span>
          <span>公式副图</span>
        </div>
      </div>

      <section
        className="chart-card primary-chart"
        aria-label="K 线主图布局预览"
      >
        <header className="chart-card-header">
          <div>
            <span className="chart-sequence">01 / PRIMARY</span>
            <h3>K 线主图</h3>
          </div>
          <div className="chart-legend" role="group" aria-label="图例">
            <span className="legend-rise">上涨样式</span>
            <span className="legend-fall">下跌样式</span>
            <span className="legend-line">均线样式</span>
          </div>
        </header>

        <div className="chart-stage" aria-hidden="true">
          <div className="chart-axis-labels">
            <span>结构上沿</span>
            <span>结构中轴</span>
            <span>结构下沿</span>
          </div>
          <div className="candlestick-grid">
            <span className="average-line average-line-one" />
            <span className="average-line average-line-two" />
            {previewCandles.map(
              (
                [direction, bodyBottom, bodyHeight, wickBottom, wickHeight],
                index,
              ) => (
                <span
                  className={`preview-candle ${direction}`}
                  key={`${direction}-${String(index)}`}
                  style={
                    {
                      '--body-bottom': bodyBottom,
                      '--body-height': bodyHeight,
                      '--wick-bottom': wickBottom,
                      '--wick-height': wickHeight,
                    } as CandleStyle
                  }
                >
                  <span className="candle-wick" />
                  <span className="candle-body" />
                </span>
              ),
            )}
          </div>
        </div>
        <p className="chart-disclaimer">
          图形仅用于展示区域层级，不代表任何证券、价格或交易信号。
        </p>
      </section>

      <section
        className="chart-card indicator-chart"
        aria-label="公式副图布局预览"
      >
        <header className="chart-card-header">
          <div>
            <span className="chart-sequence">02 / SECONDARY</span>
            <h3>公式 / 指标副图</h3>
          </div>
          <span className="planned-inline">公式输出 · v0.3.0</span>
        </header>
        <div className="indicator-stage" aria-hidden="true">
          <span className="indicator-zero-line" />
          <span className="indicator-signal indicator-signal-one" />
          <span className="indicator-signal indicator-signal-two" />
          <div className="histogram">
            {histogramBars.map((height, index) => (
              <span
                className={index % 4 < 2 ? 'rise' : 'fall'}
                key={`${String(height)}-${String(index)}`}
                style={{ height: `${String(height)}%` }}
              />
            ))}
          </div>
        </div>
      </section>
    </article>
  );
}
