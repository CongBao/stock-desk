import { act, fireEvent, render, screen } from '@testing-library/react';

import type { MarketBar } from './marketApi';
import {
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
vi.mock('echarts/charts', () => ({ BarChart: {}, CandlestickChart: {} }));
vi.mock('echarts/components', () => ({
  AriaComponent: {},
  AxisPointerComponent: {},
  DataZoomComponent: {},
  GridComponent: {},
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
    ],
  });
  expect(option.series[1]?.data).toMatchObject([
    { itemStyle: { color: '#ef4444', decal: { symbol: 'rect' } } },
    { itemStyle: { color: '#22c55e', decal: { symbol: 'triangle' } } },
  ]);
  expect(formatMarketTooltip(bars[0])).toContain('上涨');
  expect(formatMarketTooltip(bars[0])).toContain('开 10');
  expect(formatMarketTooltip(bars[0])).toContain('量 1,000');
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
  chartMocks.on.mockImplementation(
    (eventName: string, handler: (event: unknown) => void) => {
      if (eventName === 'updateAxisPointer') axisPointerHandler = handler;
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

  fireEvent.click(screen.getByRole('button', { name: '重置图表缩放' }));
  expect(chartMocks.dispatchAction).toHaveBeenCalledWith({
    type: 'dataZoom',
    start: 0,
    end: 100,
  });

  unmount();
  expect(chartMocks.off).toHaveBeenCalledWith(
    'updateAxisPointer',
    expect.any(Function),
  );
  expect(chartMocks.dispose).toHaveBeenCalledOnce();
});

it('keeps the cached canvas and chart instance through a background error and recovery', () => {
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
  expect(chartMocks.setOption).toHaveBeenCalledTimes(2);

  unmount();
  expect(chartMocks.on).toHaveBeenCalledOnce();
  expect(chartMocks.off).toHaveBeenCalledOnce();
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
