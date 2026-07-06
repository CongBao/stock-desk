import { createMarketStore } from './marketStore';

it('keeps market selection, period, and adjustment explicit', () => {
  const store = createMarketStore();

  expect(store.getState()).toMatchObject({
    selectedInstrument: null,
    selectedPoolId: null,
    period: '1d',
    adjustment: 'qfq',
  });

  store.getState().selectInstrument({ symbol: '600000.SH', name: '浦发银行' });
  store.getState().selectPool('preset-all-a');
  store.getState().setPeriod('60m');
  store.getState().setAdjustment('hfq');

  expect(store.getState()).toMatchObject({
    selectedInstrument: { symbol: '600000.SH', name: '浦发银行' },
    selectedPoolId: 'preset-all-a',
    period: '60m',
    adjustment: 'hfq',
  });
});
