import { useStore, type StoreApi } from 'zustand';
import { createStore } from 'zustand/vanilla';

export type MarketPeriod = '1d' | '1w' | '60m';
export type MarketAdjustment = 'none' | 'qfq' | 'hfq';
export type MarketZoom = { readonly start: number; readonly end: number };
export type MarketChartPreference = 'candlestick';
export type MarketSubchartPreference =
  | { readonly kind: 'none' | 'volume' }
  | { readonly kind: 'formula'; readonly formulaVersionId: string };

export type MarketInstrumentSelection = {
  readonly symbol: string;
  readonly name: string;
  readonly exchange?: 'SH' | 'SZ' | 'BJ';
  readonly instrumentKind?: 'stock' | 'index' | 'etf' | 'fund' | 'bond';
};

export type MarketState = {
  readonly adjustment: MarketAdjustment;
  readonly period: MarketPeriod;
  readonly selectedInstrument: MarketInstrumentSelection | null;
  readonly selectedPoolId: string | null;
  readonly zoom: MarketZoom;
  readonly mainChart: MarketChartPreference;
  readonly subchart: MarketSubchartPreference;
  readonly selectInstrument: (instrument: MarketInstrumentSelection) => void;
  readonly selectPool: (poolId: string | null) => void;
  readonly setAdjustment: (adjustment: MarketAdjustment) => void;
  readonly setPeriod: (period: MarketPeriod) => void;
  readonly setZoom: (zoom: MarketZoom) => void;
  readonly setSubchart: (subchart: MarketSubchartPreference) => void;
  readonly restoreWorkspace: (workspace: {
    readonly adjustment: MarketAdjustment;
    readonly instrument: MarketInstrumentSelection;
    readonly mainChart: MarketChartPreference;
    readonly period: MarketPeriod;
    readonly subchart: MarketSubchartPreference;
    readonly zoom: MarketZoom;
  }) => void;
};

export function createMarketStore(): StoreApi<MarketState> {
  return createStore<MarketState>((set) => ({
    adjustment: 'qfq',
    period: '1d',
    selectedInstrument: null,
    selectedPoolId: null,
    zoom: { start: 0, end: 100 },
    mainChart: 'candlestick',
    subchart: { kind: 'volume' },
    selectInstrument: (selectedInstrument) => set({ selectedInstrument }),
    selectPool: (selectedPoolId) => set({ selectedPoolId }),
    setAdjustment: (adjustment) => set({ adjustment }),
    setPeriod: (period) => set({ period }),
    setZoom: (zoom) => set({ zoom }),
    setSubchart: (subchart) => set({ subchart }),
    restoreWorkspace: (workspace) =>
      set({
        adjustment: workspace.adjustment,
        period: workspace.period,
        selectedInstrument: workspace.instrument,
        zoom: workspace.zoom,
        mainChart: workspace.mainChart,
        subchart: workspace.subchart,
      }),
  }));
}

const marketStore = createMarketStore();

export function resetMarketStore(): void {
  marketStore.setState({
    adjustment: 'qfq',
    period: '1d',
    selectedInstrument: null,
    selectedPoolId: null,
    zoom: { start: 0, end: 100 },
    mainChart: 'candlestick',
    subchart: { kind: 'volume' },
  });
}

export function useMarketStore<T>(selector: (state: MarketState) => T): T {
  return useStore(marketStore, selector);
}
