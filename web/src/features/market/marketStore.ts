import { useStore, type StoreApi } from 'zustand';
import { createStore } from 'zustand/vanilla';

export type MarketPeriod = '1d' | '1w' | '60m';
export type MarketAdjustment = 'none' | 'qfq' | 'hfq';

export type MarketInstrumentSelection = {
  readonly symbol: string;
  readonly name: string;
};

export type MarketState = {
  readonly adjustment: MarketAdjustment;
  readonly period: MarketPeriod;
  readonly selectedInstrument: MarketInstrumentSelection | null;
  readonly selectedPoolId: string | null;
  readonly selectInstrument: (instrument: MarketInstrumentSelection) => void;
  readonly selectPool: (poolId: string | null) => void;
  readonly setAdjustment: (adjustment: MarketAdjustment) => void;
  readonly setPeriod: (period: MarketPeriod) => void;
};

export function createMarketStore(): StoreApi<MarketState> {
  return createStore<MarketState>((set) => ({
    adjustment: 'qfq',
    period: '1d',
    selectedInstrument: null,
    selectedPoolId: null,
    selectInstrument: (selectedInstrument) => set({ selectedInstrument }),
    selectPool: (selectedPoolId) => set({ selectedPoolId }),
    setAdjustment: (adjustment) => set({ adjustment }),
    setPeriod: (period) => set({ period }),
  }));
}

const marketStore = createMarketStore();

export function resetMarketStore(): void {
  marketStore.setState({
    adjustment: 'qfq',
    period: '1d',
    selectedInstrument: null,
    selectedPoolId: null,
  });
}

export function useMarketStore<T>(selector: (state: MarketState) => T): T {
  return useStore(marketStore, selector);
}
