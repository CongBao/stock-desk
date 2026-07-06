import {
  BarChart,
  CandlestickChart,
  LineChart,
  ScatterChart,
} from 'echarts/charts';
import {
  AriaComponent,
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components';
import {
  init,
  use,
  type EChartsCoreOption,
  type EChartsType,
} from 'echarts/core';
import { CanvasRenderer } from 'echarts/renderers';
import { useEffect, useRef, useState } from 'react';

import type { MarketBar } from './marketApi';

use([
  CandlestickChart,
  BarChart,
  LineChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  DataZoomComponent,
  AxisPointerComponent,
  AriaComponent,
  CanvasRenderer,
]);

const RISE_COLOR = '#ef4444';
const FALL_COLOR = '#22c55e';
const FLAT_COLOR = '#94a3b8';
const valueFormatter = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 8,
});
const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'Asia/Shanghai',
});

type ChartSeriesDataItem = {
  readonly value: readonly number[] | number;
  readonly rawBar: MarketBar;
  readonly itemStyle?: {
    readonly color: string;
    readonly decal: { readonly symbol: 'rect' | 'triangle' | 'circle' };
  };
};

export type MarketChartOption = {
  readonly animation: false;
  readonly aria: object;
  readonly axisPointer: {
    readonly link: readonly [{ readonly xAxisIndex: readonly [0, 1] }];
  };
  readonly dataZoom: readonly [
    {
      readonly type: 'inside';
      readonly xAxisIndex: readonly [0, 1];
      readonly zoomOnMouseWheel: true;
      readonly moveOnMouseMove: true;
      readonly moveOnMouseWheel: false;
      readonly start: number;
      readonly end: 100;
    },
    {
      readonly type: 'slider';
      readonly xAxisIndex: readonly [0, 1];
      readonly start: number;
      readonly end: 100;
      readonly bottom: number;
      readonly height: number;
    },
  ];
  readonly grid: readonly [object, object];
  readonly tooltip: object;
  readonly xAxis: readonly [object, object];
  readonly yAxis: readonly [object, object];
  readonly series: readonly [
    {
      readonly name: string;
      readonly type: 'candlestick';
      readonly data: readonly ChartSeriesDataItem[];
      readonly itemStyle: {
        readonly color: string;
        readonly color0: string;
        readonly borderColor: string;
        readonly borderColor0: string;
      };
    },
    {
      readonly name: string;
      readonly type: 'bar';
      readonly xAxisIndex: 1;
      readonly yAxisIndex: 1;
      readonly data: readonly ChartSeriesDataItem[];
    },
  ];
};

export type FormulaChartLayer = {
  readonly placement: 'main' | 'subchart';
  readonly timestamps: readonly string[];
  readonly numericOutputs: readonly {
    readonly name: string;
    readonly values: readonly (number | null)[];
  }[];
  readonly signals: readonly {
    readonly name: 'BUY' | 'SELL';
    readonly values: readonly (boolean | null)[];
  }[];
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/gu, (character) => {
    const replacements: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;',
    };
    return replacements[character] ?? character;
  });
}

function formatTime(timestamp: string): string {
  return timeFormatter.format(new Date(timestamp));
}

function formatCanonicalPrice(value: string): string {
  const negative = value.startsWith('-');
  const unsigned = negative ? value.slice(1) : value;
  const [integer = '', fraction = ''] = unsigned.split('.');
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${negative ? '-' : ''}${grouped}${
    fraction.length > 0 ? `.${fraction}` : ''
  }`;
}

// Pure option helpers are exported beside the component for direct chart-contract tests.
// eslint-disable-next-line react-refresh/only-export-components
export function formatMarketTooltip(bar: MarketBar): string {
  const direction =
    bar.direction === 'rise'
      ? '上涨 ▲'
      : bar.direction === 'fall'
        ? '下跌 ▼'
        : '平盘 ◆';
  return [
    `<strong>${escapeHtml(bar.symbol)} · ${escapeHtml(direction)}</strong>`,
    `<span>${escapeHtml(formatTime(bar.timestamp))}</span>`,
    `开 ${formatCanonicalPrice(bar.priceText.open)}`,
    `高 ${formatCanonicalPrice(bar.priceText.high)}`,
    `低 ${formatCanonicalPrice(bar.priceText.low)}`,
    `收 ${formatCanonicalPrice(bar.priceText.close)}`,
    `量 ${valueFormatter.format(bar.volume)}`,
  ].join('<br/>');
}

function tooltipFormatter(parameters: unknown): string {
  if (!Array.isArray(parameters)) return '';
  for (const parameter of parameters as unknown[]) {
    if (
      typeof parameter !== 'object' ||
      parameter === null ||
      !('data' in parameter)
    )
      continue;
    const data = (parameter as Record<string, unknown>)['data'];
    if (typeof data !== 'object' || data === null || !('rawBar' in data))
      continue;
    return formatMarketTooltip(
      (data as Record<string, unknown>)['rawBar'] as MarketBar,
    );
  }
  return '';
}

function volumeStyle(bar: MarketBar) {
  if (bar.direction === 'rise') {
    return { color: RISE_COLOR, decal: { symbol: 'rect' as const } };
  }
  if (bar.direction === 'fall') {
    return { color: FALL_COLOR, decal: { symbol: 'triangle' as const } };
  }
  return { color: FLAT_COLOR, decal: { symbol: 'circle' as const } };
}

function axisPointerIndex(
  event: unknown,
  bars: readonly MarketBar[],
): number | null {
  if (typeof event !== 'object' || event === null) return null;
  const raw = event as Record<string, unknown>;
  if (
    typeof raw['dataIndex'] === 'number' &&
    Number.isInteger(raw['dataIndex'])
  ) {
    return raw['dataIndex'];
  }
  const axesInfo = raw['axesInfo'];
  if (!Array.isArray(axesInfo)) return null;
  const first: unknown = (axesInfo as unknown[])[0];
  if (typeof first !== 'object' || first === null) return null;
  const value = (first as Record<string, unknown>)['value'];
  if (typeof value === 'number' && Number.isInteger(value)) return value;
  if (typeof value === 'string') {
    if (/^\d+$/u.test(value)) return Number(value);
    const timestampIndex = bars.findIndex((bar) => bar.timestamp === value);
    return timestampIndex >= 0 ? timestampIndex : null;
  }
  return null;
}

type ZoomRange = { readonly start: number; readonly end: number };

function dataZoomRange(event: unknown): ZoomRange | null {
  if (typeof event !== 'object' || event === null) return null;
  const raw = event as Record<string, unknown>;
  const batch = raw['batch'];
  const candidate: unknown =
    Array.isArray(batch) && batch.length > 0 ? (batch as unknown[])[0] : raw;
  if (typeof candidate !== 'object' || candidate === null) return null;
  const values = candidate as Record<string, unknown>;
  const start = values['start'];
  const end = values['end'];
  if (
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end > 100 ||
    start > end
  )
    return null;
  return { start, end };
}

function directionLabel(bar: MarketBar): string {
  return bar.direction === 'rise'
    ? '上涨 ▲'
    : bar.direction === 'fall'
      ? '下跌 ▼'
      : '平盘 ◆';
}

function signalMarkerOffset(bar: MarketBar): number {
  return Math.max(
    Math.abs(bar.high - bar.low) * 0.05,
    Math.max(Math.abs(bar.high), Math.abs(bar.low)) * 0.005,
    1e-8,
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function buildMarketChartOption(
  bars: readonly MarketBar[],
): MarketChartOption {
  const categories = bars.map((bar) => bar.timestamp);
  const visibleStart =
    bars.length > 160 ? Math.max(0, 100 - (160 / bars.length) * 100) : 0;
  return {
    animation: false,
    aria: {
      enabled: true,
      decal: { show: true },
      description:
        '证券 K 线和成交量图。上涨使用红色与向上符号，下跌使用绿色与向下符号。',
    },
    axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
    tooltip: {
      trigger: 'axis',
      confine: true,
      axisPointer: { type: 'cross', snap: true },
      formatter: tooltipFormatter,
      backgroundColor: 'rgba(7, 17, 31, 0.96)',
      borderColor: '#27415f',
      textStyle: { color: '#dbeafe', fontSize: 12 },
    },
    grid: [
      { left: 62, right: 24, top: 26, height: '55%' },
      { left: 62, right: 24, top: '69%', height: '14%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: categories,
        gridIndex: 0,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#29425e' } },
        axisLabel: {
          show: false,
          formatter: (value: string) => formatTime(value),
        },
        axisTick: { show: false },
        min: 'dataMin',
        max: 'dataMax',
      },
      {
        type: 'category',
        data: categories,
        gridIndex: 1,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#29425e' } },
        axisLabel: {
          color: '#71849c',
          hideOverlap: true,
          formatter: (value: string) => formatTime(value),
        },
        axisTick: { show: false },
        min: 'dataMin',
        max: 'dataMax',
      },
    ],
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        position: 'right',
        axisLabel: { color: '#71849c' },
        splitLine: { lineStyle: { color: '#1e3550' } },
      },
      {
        scale: true,
        gridIndex: 1,
        position: 'right',
        axisLabel: { color: '#71849c', formatter: '{value}' },
        splitNumber: 2,
        splitLine: { lineStyle: { color: '#1e3550' } },
      },
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1],
        start: visibleStart,
        end: 100,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
      },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        start: visibleStart,
        end: 100,
        bottom: 8,
        height: 18,
      },
    ],
    series: [
      {
        name: 'K 线（红涨绿跌）',
        type: 'candlestick',
        data: bars.map((bar) => ({
          value: [bar.open, bar.close, bar.low, bar.high],
          rawBar: bar,
        })),
        itemStyle: {
          color: RISE_COLOR,
          color0: FALL_COLOR,
          borderColor: RISE_COLOR,
          borderColor0: FALL_COLOR,
        },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: bars.map((bar) => ({
          value: bar.volume,
          rawBar: bar,
          itemStyle: volumeStyle(bar),
        })),
      },
    ],
  };
}

// Extends the market contract without creating a second candlestick implementation.
// Formula timestamps are aligned explicitly so a sparse preview cannot shift signals.
// eslint-disable-next-line react-refresh/only-export-components
export function buildFormulaMarketChartOption(
  bars: readonly MarketBar[],
  formula: FormulaChartLayer,
): EChartsCoreOption {
  const base = buildMarketChartOption(bars);
  const byTimestamp = new Map(
    formula.timestamps.map((timestamp, index) => [timestamp, index] as const),
  );
  const isSubchart = formula.placement === 'subchart';
  const grid = isSubchart
    ? [
        { left: 62, right: 24, top: 30, height: '43%' },
        { left: 62, right: 24, top: '53%', height: '10%' },
        { left: 62, right: 24, top: '68%', height: '18%' },
      ]
    : base.grid;
  const xAxis = isSubchart
    ? [
        ...(base.xAxis as unknown as object[]).map((axis, index) => ({
          ...axis,
          axisLabel:
            index === 1
              ? { show: false }
              : (axis as { axisLabel?: object }).axisLabel,
        })),
        {
          type: 'category',
          data: bars.map((bar) => bar.timestamp),
          gridIndex: 2,
          boundaryGap: true,
          axisLine: { lineStyle: { color: '#29425e' } },
          axisLabel: {
            color: '#71849c',
            hideOverlap: true,
            formatter: (value: string) => formatTime(value),
          },
          axisTick: { show: false },
        },
      ]
    : base.xAxis;
  const yAxis = isSubchart
    ? [
        ...(base.yAxis as unknown as object[]),
        {
          scale: true,
          gridIndex: 2,
          position: 'right',
          axisLabel: { color: '#71849c' },
          splitNumber: 3,
          splitLine: { lineStyle: { color: '#1e3550' } },
        },
      ]
    : base.yAxis;
  const palette = ['#38bdf8', '#f59e0b', '#a78bfa', '#fb7185', '#2dd4bf'];
  const formulaSeries = formula.numericOutputs.map((output, outputIndex) => ({
    name: output.name,
    type: 'line' as const,
    xAxisIndex: isSubchart ? 2 : 0,
    yAxisIndex: isSubchart ? 2 : 0,
    showSymbol: false,
    connectNulls: false,
    smooth: false,
    lineStyle: { width: 1.5, color: palette[outputIndex % palette.length] },
    itemStyle: { color: palette[outputIndex % palette.length] },
    data: bars.map((bar) => {
      const index = byTimestamp.get(bar.timestamp);
      return index === undefined ? null : (output.values[index] ?? null);
    }),
  }));
  const signalSeries = formula.signals.map((signal) => {
    const isBuy = signal.name === 'BUY';
    return {
      name: isBuy ? 'BUY 买点' : 'SELL 卖点',
      type: 'scatter' as const,
      xAxisIndex: 0,
      yAxisIndex: 0,
      symbol: isBuy ? 'triangle' : 'pin',
      symbolRotate: isBuy ? 0 : 180,
      symbolSize: isBuy ? 12 : 15,
      itemStyle: { color: isBuy ? RISE_COLOR : FALL_COLOR },
      data: bars.flatMap((bar) => {
        const index = byTimestamp.get(bar.timestamp);
        if (index === undefined || signal.values[index] !== true) return [];
        const offset = signalMarkerOffset(bar);
        return [[bar.timestamp, isBuy ? bar.low - offset : bar.high + offset]];
      }),
    };
  });
  const visibleStart = base.dataZoom[0].start;
  return {
    ...base,
    aria: {
      enabled: true,
      decal: { show: true },
      description:
        '证券 K 线主图、成交量、公式输出及买卖信号。上涨与买点使用红色，下跌与卖点使用绿色。',
    },
    legend: {
      top: 5,
      right: 24,
      textStyle: { color: '#8296ae', fontSize: 9 },
      data: [
        ...formula.numericOutputs.map((output) => output.name),
        'BUY 买点',
        'SELL 卖点',
      ],
    },
    grid,
    xAxis,
    yAxis,
    axisPointer: { link: [{ xAxisIndex: isSubchart ? [0, 1, 2] : [0, 1] }] },
    dataZoom: [
      {
        ...base.dataZoom[0],
        xAxisIndex: isSubchart ? [0, 1, 2] : [0, 1],
        start: visibleStart,
      },
      {
        ...base.dataZoom[1],
        xAxisIndex: isSubchart ? [0, 1, 2] : [0, 1],
        start: visibleStart,
      },
    ],
    series: [...base.series, ...formulaSeries, ...signalSeries],
  };
}

type MarketChartProps = {
  readonly bars: readonly MarketBar[] | undefined;
  readonly errorMessage?: string;
  readonly formula?: FormulaChartLayer;
  readonly formulaEmptyMessage?: string;
  readonly formulaEmptyPlacement?: FormulaChartLayer['placement'];
  readonly isLoading?: boolean;
};

export function MarketChart({
  bars,
  errorMessage,
  formula,
  formulaEmptyMessage,
  formulaEmptyPlacement = 'subchart',
  isLoading = false,
}: MarketChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<EChartsType | null>(null);
  const barsRef = useRef(bars);
  const [pointer, setPointer] = useState<{
    readonly bars: readonly MarketBar[];
    readonly index: number;
  } | null>(null);
  const [zoom, setZoom] = useState<ZoomRange>({ start: 0, end: 100 });
  barsRef.current = bars;
  const hasBars = bars !== undefined && bars.length > 0;
  const activeBar =
    bars !== undefined && pointer?.bars === bars && pointer.index < bars.length
      ? bars[pointer.index]
      : bars?.at(-1);

  useEffect(() => {
    if (!hasBars || containerRef.current === null) return undefined;
    const chart = init(containerRef.current, undefined, { renderer: 'canvas' });
    instanceRef.current = chart;
    const handleAxisPointer = (event: unknown) => {
      const currentBars = barsRef.current;
      const index = axisPointerIndex(event, currentBars ?? []);
      if (
        currentBars !== undefined &&
        index !== null &&
        index >= 0 &&
        index < currentBars.length
      ) {
        setPointer({ bars: currentBars, index });
      }
    };
    const handleDataZoom = (event: unknown) => {
      const next = dataZoomRange(event);
      if (next !== null) setZoom(next);
    };
    chart.on('updateAxisPointer', handleAxisPointer);
    chart.on('dataZoom', handleDataZoom);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      chart.off('updateAxisPointer', handleAxisPointer);
      chart.off('dataZoom', handleDataZoom);
      chart.dispose();
      instanceRef.current = null;
    };
  }, [hasBars]);

  useEffect(() => {
    if (!hasBars || instanceRef.current === null || bars === undefined) return;
    const option =
      formula === undefined
        ? buildMarketChartOption(bars)
        : buildFormulaMarketChartOption(bars, formula);
    instanceRef.current.setOption(option as EChartsCoreOption, {
      lazyUpdate: true,
      notMerge: true,
    });
    const zoomOptions = option.dataZoom as readonly {
      readonly start?: number;
      readonly end?: number;
    }[];
    setZoom({
      start: zoomOptions[0]?.start ?? 0,
      end: zoomOptions[0]?.end ?? 100,
    });
  }, [bars, formula, hasBars]);

  return (
    <div className="market-chart-stack">
      <section
        className="market-chart-card"
        aria-labelledby="market-chart-title"
      >
        <header className="market-chart-header">
          <div>
            <span className="chart-sequence">MAIN / CACHE ONLY</span>
            <h3 id="market-chart-title">
              {formula === undefined
                ? 'K 线与成交量'
                : formula.placement === 'main'
                  ? 'K 线主图与公式叠加'
                  : 'K 线主图与公式副图'}
            </h3>
          </div>
          <div className="market-chart-tools">
            <div className="market-chart-legend" aria-label="涨跌图例">
              <span data-direction="rise">▲ 上涨（红）</span>
              <span data-direction="fall">▼ 下跌（绿）</span>
            </div>
            {hasBars ? (
              <span
                className="market-chart-zoom-state"
                role="status"
                aria-label="图表缩放范围"
                aria-live="polite"
              >
                可见范围 {Math.round(zoom.start)}%–{Math.round(zoom.end)}%
              </span>
            ) : null}
            <button
              type="button"
              disabled={!hasBars}
              aria-label="重置图表缩放"
              onClick={() => {
                instanceRef.current?.dispatchAction({
                  type: 'dataZoom',
                  start: 0,
                  end: 100,
                });
                setZoom({ start: 0, end: 100 });
              }}
            >
              重置视图
            </button>
          </div>
        </header>
        {activeBar === undefined ? null : (
          <div
            className="market-ohlcv-readout"
            role="status"
            aria-label="当前 K 线 OHLCV"
            aria-live="polite"
            aria-atomic="true"
          >
            <time dateTime={activeBar.timestamp}>
              {formatTime(activeBar.timestamp)}
            </time>
            <strong data-direction={activeBar.direction}>
              {directionLabel(activeBar)}
            </strong>
            <span>开 {formatCanonicalPrice(activeBar.priceText.open)}</span>
            <span>高 {formatCanonicalPrice(activeBar.priceText.high)}</span>
            <span>低 {formatCanonicalPrice(activeBar.priceText.low)}</span>
            <span>收 {formatCanonicalPrice(activeBar.priceText.close)}</span>
            <span>量 {valueFormatter.format(activeBar.volume)}</span>
          </div>
        )}
        <p className="chart-interaction-hint">
          十字光标查看 OHLCV · 滚轮/双指缩放 · 拖动平移
        </p>
        {errorMessage && hasBars ? (
          <p className="market-chart-refresh-alert" role="alert">
            {errorMessage} 已保留上次成功读取的缓存图表。
          </p>
        ) : null}
        <div className="market-chart-viewport">
          {hasBars && bars !== undefined ? (
            <div
              ref={containerRef}
              className="market-chart-canvas"
              role="img"
              aria-label={`${bars[0]?.symbol ?? '证券'} ${
                formula === undefined
                  ? 'K 线与成交量'
                  : 'K 线、公式输出与买卖信号'
              }交互图`}
            />
          ) : errorMessage ? (
            <p className="market-chart-state" role="alert">
              {errorMessage}
            </p>
          ) : isLoading ? (
            <p className="market-chart-state" role="status">
              正在读取本地 K 线缓存…
            </p>
          ) : bars === undefined ? (
            <p className="market-chart-state">先从搜索或股票池选择证券</p>
          ) : bars.length === 0 ? (
            <p className="market-chart-state">本地暂无缓存</p>
          ) : null}
        </div>
      </section>

      {formula === undefined ? (
        <section
          className="formula-subchart"
          aria-label={
            formulaEmptyPlacement === 'main' ? '公式主图叠加' : '公式结果副图'
          }
        >
          <header>
            <div>
              <span className="chart-sequence">
                {formulaEmptyPlacement === 'main'
                  ? 'MAIN / FORMULA'
                  : 'SUB / FORMULA'}
              </span>
              <h3>
                {formulaEmptyPlacement === 'main'
                  ? '公式主图叠加'
                  : '公式结果副图'}
              </h3>
            </div>
            <span className="planned-inline">
              {formulaEmptyMessage === undefined ? '后续阶段' : '待预览'}
            </span>
          </header>
          <p>
            {formulaEmptyMessage ??
              '公式能力将在后续阶段接入；当前不生成指标线或交易信号。'}
          </p>
        </section>
      ) : null}
    </div>
  );
}
