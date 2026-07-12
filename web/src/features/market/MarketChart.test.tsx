import { act, fireEvent, render, screen } from '@testing-library/react';

import type { MarketBar } from './marketApi';
import {
  buildFormulaMarketChartOption,
  buildMarketChartOption,
  formatMarketTooltip,
  MarketChart,
} from './MarketChart';

const chartMocks = vi.hoisted(() => ({
  dispatchAction: vi.fn(),
  dispose: vi.fn(),
  init: vi.fn(),
  off: vi.fn(),
  on: vi.fn(),
  resize: vi.fn(),
  setOption: vi.fn(),
  use: vi.fn(),
}));

vi.mock('echarts/core', () => ({
  init: chartMocks.init,
  use: chartMocks.use,
}));
vi.mock('echarts/charts', () => ({
  BarChart: {},
  CandlestickChart: {},
  LineChart: {},
  ScatterChart: {},
}));
vi.mock('echarts/components', () => ({
  AriaComponent: {},
  AxisPointerComponent: {},
  DataZoomComponent: {},
  GridComponent: {},
  LegendComponent: {},
  TooltipComponent: {},
}));
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }));

const bars = [
  {
    symbol: '600000.SH',
    timestamp: '2024-01-01T16:00:00Z',
    period: '1d',
    adjustment: 'qfq',
    open: 10,
    high: 11,
    low: 9.8,
    close: 10.8,
    priceText: { open: '10', high: '11', low: '9.8', close: '10.8' },
    volume: 1000,
    status: 'normal',
    direction: 'rise',
  },
  {
    symbol: '600000.SH',
    timestamp: '2024-01-02T16:00:00Z',
    period: '1d',
    adjustment: 'qfq',
    open: 10.8,
    high: 11,
    low: 10.1,
    close: 10.2,
    priceText: { open: '10.8', high: '11', low: '10.1', close: '10.2' },
    volume: 1200,
    status: 'normal',
    direction: 'fall',
  },
] as const satisfies readonly MarketBar[];

class ResizeObserverMock {
  static callback: ResizeObserverCallback | null = null;
  constructor(callback: ResizeObserverCallback) {
    ResizeObserverMock.callback = callback;
  }
  disconnect = vi.fn();
  observe = vi.fn();
  unobserve = vi.fn();
}

beforeEach(() => {
  chartMocks.dispatchAction.mockReset();
  chartMocks.dispose.mockReset();
  chartMocks.resize.mockReset();
  chartMocks.setOption.mockReset();
  chartMocks.init.mockReset().mockReturnValue({
    dispatchAction: chartMocks.dispatchAction,
    dispose: chartMocks.dispose,
    resize: chartMocks.resize,
    setOption: chartMocks.setOption,
    on: chartMocks.on,
    off: chartMocks.off,
  });
  chartMocks.on.mockReset();
  chartMocks.off.mockReset();
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
});

afterEach(() => vi.unstubAllGlobals());

it('builds synchronized candlestick and volume grids with explicit rise/fall encoding', () => {
  const option = buildMarketChartOption(bars);

  expect(option).toMatchObject({
    animation: false,
    axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
    grid: [{}, {}],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1],
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
      },
      { type: 'slider', xAxisIndex: [0, 1] },
    ],
    series: [
      {
        type: 'candlestick',
        itemStyle: {
          color: '#ef4444',
          color0: '#22c55e',
          borderColor: '#ef4444',
          borderColor0: '#22c55e',
        },
      },
      { type: 'bar', xAxisIndex: 1, yAxisIndex: 1 },
      { type: 'bar', xAxisIndex: 1, yAxisIndex: 1 },
      { type: 'bar', xAxisIndex: 1, yAxisIndex: 1 },
    ],
  });
  expect(option.series.slice(1).map((series) => series.itemStyle)).toEqual([
    { color: '#ef4444', decal: { symbol: 'rect' } },
    { color: '#22c55e', decal: { symbol: 'triangle' } },
    { color: '#94a3b8', decal: { symbol: 'circle' } },
  ]);
  expect(formatMarketTooltip(bars[0])).toContain('上涨');
  expect(formatMarketTooltip(bars[0])).toContain('开 10');
  expect(formatMarketTooltip(bars[0])).toContain('量 1,000');
});

it('uses an explicit readable ECharts palette for the light desktop theme', () => {
  const option = buildMarketChartOption(bars, 'light');
  expect(option.tooltip).toMatchObject({
    backgroundColor: 'rgba(255, 255, 255, 0.98)',
    borderColor: '#aab8ca',
    textStyle: { color: '#172033' },
  });
  expect(option.yAxis[0]).toMatchObject({
    axisLabel: { color: '#4b5f78' },
    splitLine: { lineStyle: { color: '#d8e0ea' } },
  });
  expect(option.aria).toMatchObject({ decal: { show: true } });
});

it('keeps full market bars out of the ECharts series graph while preserving indexed tooltips', () => {
  const option = buildMarketChartOption(bars);

  expect(option.series[0].data).toEqual([
    [10, 10.8, 9.8, 11],
    [10.8, 10.2, 10.1, 11],
  ]);
  expect(option.series[1].data[0]).toBe(1_000);
  expect(JSON.stringify(option.series)).not.toContain('rawBar');

  const formatter = (
    option.tooltip as { readonly formatter: (parameters: unknown) => string }
  ).formatter;
  expect(formatter([{ dataIndex: 0 }])).toContain('上涨');
  expect(formatter([{ dataIndex: 1 }])).toContain('开 10.8');
  expect(formatter([{ dataIndex: 99 }])).toBe('');
});

it('batches direction-specific volume bars into overlapping large series', () => {
  const flatBar = {
    ...bars[0],
    timestamp: '2024-01-03T16:00:00Z',
    direction: 'flat' as const,
    volume: 900,
  };
  const option = buildMarketChartOption([...bars, flatBar]);
  const volumeSeries = option.series.slice(1);

  expect(volumeSeries).toHaveLength(3);
  expect(volumeSeries.some((series) => 'stack' in series)).toBe(false);
  expect(volumeSeries).toMatchObject([
    {
      name: '成交量·上涨',
      type: 'bar',
      large: true,
      largeThreshold: 400,
      silent: true,
      barGap: '-100%',
      itemStyle: { color: '#ef4444', decal: { symbol: 'rect' } },
      data: [1_000, '-', '-'],
    },
    {
      name: '成交量·下跌',
      type: 'bar',
      large: true,
      largeThreshold: 400,
      silent: true,
      barGap: '-100%',
      itemStyle: { color: '#22c55e', decal: { symbol: 'triangle' } },
      data: ['-', 1_200, '-'],
    },
    {
      name: '成交量·平盘',
      type: 'bar',
      large: true,
      largeThreshold: 400,
      silent: true,
      barGap: '-100%',
      itemStyle: { color: '#94a3b8', decal: { symbol: 'circle' } },
      data: ['-', '-', 900],
    },
  ]);
});

it('aligns formula subchart outputs and BUY/SELL markers by timestamp', () => {
  const option = buildFormulaMarketChartOption(bars, {
    placement: 'subchart',
    timestamps: [bars[0].timestamp, bars[1].timestamp],
    numericOutputs: [
      { name: 'DIF', values: [null, 0.2] },
      { name: 'DEA', values: [null, 0.1] },
    ],
    signals: [
      { name: 'BUY', values: [null, true] },
      { name: 'SELL', values: [null, false] },
    ],
  });
  const grids = option.grid as readonly object[];
  const series = option.series as readonly {
    readonly name?: string;
    readonly xAxisIndex?: number;
    readonly data?: readonly unknown[];
  }[];

  expect(grids).toHaveLength(3);
  expect(series.find((item) => item.name === 'DIF')).toMatchObject({
    xAxisIndex: 2,
    data: [null, 0.2],
  });
  const buyData = series.find((item) => item.name === 'BUY 买点')?.data as
    readonly [string, number][] | undefined;
  expect(buyData?.[0]?.[0]).toBe(bars[1].timestamp);
  expect(buyData?.[0]?.[1]).toBeLessThan(bars[1].low);
  expect(series.find((item) => item.name === 'SELL 卖点')?.data).toEqual([]);
});

it('places BUY and SELL markers with a positive additive offset for zero and negative prices', () => {
  const unusualBars = [
    {
      ...bars[0],
      open: 0,
      high: 0,
      low: 0,
      close: 0,
      priceText: { open: '0', high: '0', low: '0', close: '0' },
    },
    {
      ...bars[1],
      open: -10,
      high: -9,
      low: -11,
      close: -10,
      priceText: { open: '-10', high: '-9', low: '-11', close: '-10' },
    },
  ] satisfies readonly MarketBar[];
  const option = buildFormulaMarketChartOption(unusualBars, {
    placement: 'subchart',
    timestamps: unusualBars.map((bar) => bar.timestamp),
    numericOutputs: [],
    signals: [
      { name: 'BUY', values: [true, true] },
      { name: 'SELL', values: [true, true] },
    ],
  });
  const series = option.series as readonly {
    readonly name?: string;
    readonly data?: readonly [string, number][];
  }[];
  const buys = series.find((item) => item.name === 'BUY 买点')?.data ?? [];
  const sells = series.find((item) => item.name === 'SELL 卖点')?.data ?? [];

  expect(buys[0]?.[1]).toBeLessThan(0);
  expect(sells[0]?.[1]).toBeGreaterThan(0);
  expect(buys[1]?.[1]).toBeLessThan(-11);
  expect(sells[1]?.[1]).toBeGreaterThan(-9);
});

it('allows Formula Studio to replace the Stage 1 empty subchart copy', () => {
  render(
    <MarketChart
      bars={undefined}
      formulaEmptyMessage="保存并运行预览后显示公式副图与买卖点"
    />,
  );

  expect(
    screen.getByText('保存并运行预览后显示公式副图与买卖点'),
  ).toBeVisible();
  expect(screen.queryByText(/将在后续阶段接入/u)).not.toBeInTheDocument();
});

it('labels main-chart overlays and subcharts according to formula placement', () => {
  const formula = {
    timestamps: bars.map((bar) => bar.timestamp),
    numericOutputs: [{ name: 'DIF', values: [null, 0.2] }],
    signals: [
      { name: 'BUY' as const, values: [null, true] },
      { name: 'SELL' as const, values: [null, false] },
    ],
  };
  const { rerender } = render(
    <MarketChart bars={bars} formula={{ ...formula, placement: 'main' }} />,
  );

  expect(
    screen.getByRole('heading', { name: 'K 线主图与公式叠加' }),
  ).toBeVisible();
  expect(screen.queryByText(/公式副图/u)).not.toBeInTheDocument();

  rerender(
    <MarketChart bars={bars} formula={{ ...formula, placement: 'subchart' }} />,
  );
  expect(
    screen.getByRole('heading', { name: 'K 线主图与公式副图' }),
  ).toBeVisible();
});

it('uses a main-overlay empty region when Formula Studio selects main placement', () => {
  render(
    <MarketChart
      bars={undefined}
      formulaEmptyPlacement="main"
      formulaEmptyMessage="保存并运行预览后在 K 线主图叠加公式输出与买卖点"
    />,
  );

  expect(
    screen.getByRole('region', { name: '公式主图叠加' }),
  ).toHaveTextContent('K 线主图叠加');
  expect(screen.queryByText(/副图/u)).not.toBeInTheDocument();
});

it('uses unique raw timestamps as category keys across years', () => {
  const crossYearBars = [
    { ...bars[0], timestamp: '2024-01-01T16:00:00Z' },
    { ...bars[1], timestamp: '2025-01-01T16:00:00Z' },
  ] satisfies readonly MarketBar[];

  expect(buildMarketChartOption(crossYearBars).xAxis).toMatchObject([
    { data: ['2024-01-01T16:00:00Z', '2025-01-01T16:00:00Z'] },
    { data: ['2024-01-01T16:00:00Z', '2025-01-01T16:00:00Z'] },
  ]);
});

it('renders canonical price text without exposing Number rounding', () => {
  const preciseText = '9999999999999999.99999999';
  const preciseBar = {
    ...bars[0],
    open: 10_000_000_000_000_000,
    high: 10_000_000_000_000_000,
    low: 10_000_000_000_000_000,
    close: 10_000_000_000_000_000,
    priceText: {
      open: preciseText,
      high: preciseText,
      low: preciseText,
      close: preciseText,
    },
    direction: 'flat' as const,
  };

  expect(formatMarketTooltip(preciseBar)).toContain(
    '开 9,999,999,999,999,999.99999999',
  );
  render(<MarketChart bars={[preciseBar]} />);
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('收 9,999,999,999,999,999.99999999');
});

it('initializes, resizes, resets, and disposes the tree-shaken chart instance', () => {
  let axisPointerHandler: ((event: unknown) => void) | undefined;
  let dataZoomHandler: ((event: unknown) => void) | undefined;
  chartMocks.on.mockImplementation(
    (eventName: string, handler: (event: unknown) => void) => {
      if (eventName === 'updateAxisPointer') axisPointerHandler = handler;
      if (eventName === 'dataZoom') dataZoomHandler = handler;
    },
  );
  const { unmount } = render(<MarketChart bars={bars} />);

  expect(chartMocks.init).toHaveBeenCalledOnce();
  expect(chartMocks.setOption).toHaveBeenCalledWith(
    expect.objectContaining({ animation: false }),
    { lazyUpdate: true, notMerge: true },
  );
  ResizeObserverMock.callback?.([], {} as ResizeObserver);
  expect(chartMocks.resize).toHaveBeenCalledOnce();
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('下跌 ▼');
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('开 10.8');

  act(() => axisPointerHandler?.({ axesInfo: [{ value: 0 }] }));
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('上涨 ▲');
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('收 10.8');
  act(() => axisPointerHandler?.({ axesInfo: [{ value: 99 }] }));
  expect(
    screen.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toHaveTextContent('上涨 ▲');

  expect(
    screen.getByRole('status', { name: '图表缩放范围' }),
  ).toHaveTextContent('0%–100%');
  expect(screen.getByRole('status', { name: '图表缩放范围' })).toHaveAttribute(
    'data-zoom-start',
    '0',
  );
  act(() => dataZoomHandler?.({ batch: [{ start: 35, end: 80 }] }));
  expect(
    screen.getByRole('status', { name: '图表缩放范围' }),
  ).toHaveTextContent('35%–80%');
  expect(screen.getByRole('status', { name: '图表缩放范围' })).toHaveAttribute(
    'data-zoom-start',
    '35',
  );

  fireEvent.click(screen.getByRole('button', { name: '重置图表缩放' }));
  expect(chartMocks.dispatchAction).toHaveBeenCalledWith({
    type: 'dataZoom',
    start: 0,
    end: 100,
  });
  expect(
    screen.getByRole('status', { name: '图表缩放范围' }),
  ).toHaveTextContent('0%–100%');

  unmount();
  expect(chartMocks.off).toHaveBeenCalledWith(
    'updateAxisPointer',
    expect.any(Function),
  );
  expect(chartMocks.off).toHaveBeenCalledWith('dataZoom', expect.any(Function));
  expect(chartMocks.dispose).toHaveBeenCalledOnce();
});

it('serializes delayed ECharts generations so A cannot mark queued B ready', () => {
  let finishedHandler: ((event: unknown) => void) | undefined;
  chartMocks.on.mockImplementation(
    (eventName: string, handler: (event: unknown) => void) => {
      if (eventName === 'finished') finishedHandler = handler;
    },
  );
  const { rerender, unmount } = render(<MarketChart bars={bars} />);
  const chart = screen.getByRole('img', {
    name: '600000.SH K 线与成交量交互图',
  });

  expect(chart).toHaveAttribute('data-chart-ready', 'false');
  expect(chart).toHaveAttribute('aria-busy', 'true');
  expect(chart).not.toHaveAttribute('data-chart-generation');
  const nextBars = bars.map((bar) => ({ ...bar }));
  rerender(<MarketChart bars={nextBars} />);
  expect(chart).toHaveAttribute('data-chart-ready', 'false');
  expect(chart).not.toHaveAttribute('data-chart-generation');

  // B is queued while A is still the active ECharts render.
  expect(chartMocks.setOption).toHaveBeenCalledTimes(1);
  act(() => finishedHandler?.({}));

  // A completed, which starts B, but only B's own event may mark B ready.
  expect(chartMocks.setOption).toHaveBeenCalledTimes(2);
  expect(chart).toHaveAttribute('data-chart-ready', 'false');
  expect(chart).toHaveAttribute('aria-busy', 'true');
  expect(chart).toHaveAttribute('data-chart-generation', '1');
  act(() => finishedHandler?.({}));
  expect(chart).toHaveAttribute('data-chart-ready', 'true');
  expect(chart).toHaveAttribute('aria-busy', 'false');
  expect(chart).toHaveAttribute('data-chart-generation', '2');

  const thirdBars = nextBars.map((bar) => ({ ...bar }));
  rerender(<MarketChart bars={thirdBars} />);
  expect(chart).toHaveAttribute('data-chart-ready', 'false');
  expect(chart).toHaveAttribute('aria-busy', 'true');
  expect(chart).toHaveAttribute('data-chart-generation', '2');
  act(() => finishedHandler?.({}));
  expect(chart).toHaveAttribute('data-chart-ready', 'true');
  expect(chart).toHaveAttribute('data-chart-generation', '3');

  unmount();
  expect(chartMocks.off).toHaveBeenCalledWith('finished', expect.any(Function));
});

it('keeps the cached canvas and chart instance through a background error and recovery', () => {
  let finishedHandler: ((event: unknown) => void) | undefined;
  chartMocks.on.mockImplementation(
    (eventName: string, handler: (event: unknown) => void) => {
      if (eventName === 'finished') finishedHandler = handler;
    },
  );
  const { rerender, unmount } = render(<MarketChart bars={bars} />);
  const canvas = screen.getByRole('img', {
    name: '600000.SH K 线与成交量交互图',
  });

  rerender(<MarketChart bars={bars} errorMessage="后台刷新失败" />);

  expect(screen.getByRole('alert')).toHaveTextContent('后台刷新失败');
  expect(
    screen.getByRole('img', { name: '600000.SH K 线与成交量交互图' }),
  ).toBe(canvas);
  expect(chartMocks.init).toHaveBeenCalledOnce();
  expect(chartMocks.dispose).not.toHaveBeenCalled();
  expect(chartMocks.off).not.toHaveBeenCalled();

  const recoveredBars = bars.map((bar) => ({ ...bar }));
  rerender(<MarketChart bars={recoveredBars} />);

  expect(
    screen.getByRole('img', { name: '600000.SH K 线与成交量交互图' }),
  ).toBe(canvas);
  expect(chartMocks.init).toHaveBeenCalledOnce();
  expect(chartMocks.setOption).toHaveBeenCalledOnce();
  act(() => finishedHandler?.({}));
  expect(chartMocks.setOption).toHaveBeenCalledTimes(2);

  unmount();
  expect(chartMocks.on).toHaveBeenCalledTimes(3);
  expect(chartMocks.off).toHaveBeenCalledTimes(3);
  expect(chartMocks.dispose).toHaveBeenCalledOnce();
});

it('renders honest idle, loading, empty, and error states plus an independent formula area', () => {
  const { rerender } = render(<MarketChart bars={undefined} />);
  expect(screen.getByText('先从搜索或股票池选择证券')).toBeInTheDocument();
  expect(
    screen.getByRole('region', { name: '公式结果副图' }),
  ).toHaveTextContent('公式能力将在后续阶段接入');

  rerender(<MarketChart bars={undefined} isLoading />);
  expect(screen.getByRole('status')).toHaveTextContent('正在读取本地 K 线缓存');

  rerender(<MarketChart bars={[]} />);
  expect(screen.getByText('本地暂无缓存')).toBeInTheDocument();

  rerender(<MarketChart bars={undefined} errorMessage="行情协议异常" />);
  expect(screen.getByRole('alert')).toHaveTextContent('行情协议异常');
});
