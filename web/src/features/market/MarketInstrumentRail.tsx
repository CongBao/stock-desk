import type { RefObject } from 'react';

import type { MarketNavigationInstrument } from './marketNavigationApi';

const SHANGHAI_COMPOSITE = {
  symbol: '000001.SS',
  name: '上证指数',
  instrumentKind: 'index',
} as const satisfies MarketNavigationInstrument;

type MarketInstrumentRailProps = {
  readonly collapsed: boolean;
  readonly watchlist: readonly MarketNavigationInstrument[];
  readonly recent: readonly MarketNavigationInstrument[];
  readonly selectedSymbol: string | null;
  readonly onAdd: (instrument: MarketNavigationInstrument) => void;
  readonly onRemove: (instrument: MarketNavigationInstrument) => void;
  readonly onSelect: (instrument: MarketNavigationInstrument) => void;
  readonly onToggle: () => void;
  readonly toggleRef?: RefObject<HTMLButtonElement | null>;
};

function RailIcon({ name }: { readonly name: 'panel' | 'star' | 'clock' }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
    >
      {name === 'panel' ? (
        <>
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <path d="M9 4v16" />
        </>
      ) : name === 'star' ? (
        <path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" />
      ) : (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </>
      )}
    </svg>
  );
}

function InstrumentList({
  instruments,
  label,
  selectedSymbol,
  onRemove,
  onSelect,
}: {
  readonly instruments: readonly MarketNavigationInstrument[];
  readonly label: string;
  readonly selectedSymbol: string | null;
  readonly onRemove?: (instrument: MarketNavigationInstrument) => void;
  readonly onSelect: (instrument: MarketNavigationInstrument) => void;
}) {
  return (
    <ul className="market-instrument-list" aria-label={label}>
      {instruments.map((instrument) => (
        <li key={instrument.symbol}>
          <button
            className="market-instrument-select"
            type="button"
            aria-current={
              selectedSymbol === instrument.symbol ? 'true' : undefined
            }
            aria-label={`查看${instrument.name} ${instrument.symbol}`}
            onClick={() => onSelect(instrument)}
          >
            <strong>{instrument.name}</strong>
            <span>{instrument.symbol}</span>
          </button>
          {onRemove === undefined ? null : (
            <button
              className="market-instrument-remove"
              type="button"
              aria-label={`从自选移除${instrument.name}`}
              title={`从自选移除${instrument.name}`}
              onClick={() => onRemove(instrument)}
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeWidth="1.8"
              >
                <path d="M6 12h12" />
              </svg>
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

export function MarketInstrumentRail({
  collapsed,
  onAdd,
  onRemove,
  onSelect,
  onToggle,
  recent,
  selectedSymbol,
  toggleRef,
  watchlist,
}: MarketInstrumentRailProps) {
  return (
    <aside
      className="market-instrument-rail"
      data-collapsed={collapsed}
      aria-label="自选与最近访问"
    >
      <header>
        <div className="market-rail-title">
          <RailIcon name="panel" />
          <strong>标的导航</strong>
        </div>
        <button
          ref={toggleRef}
          className="market-rail-toggle"
          type="button"
          aria-controls="market-instrument-rail-content"
          aria-expanded={!collapsed}
          aria-label={collapsed ? '展开自选与最近访问' : '收起自选与最近访问'}
          title={collapsed ? '展开自选与最近访问' : '收起自选与最近访问'}
          onClick={onToggle}
        >
          <RailIcon name="panel" />
        </button>
      </header>

      <div id="market-instrument-rail-content" hidden={collapsed}>
        <section aria-labelledby="market-watchlist-title">
          <h3 id="market-watchlist-title">
            <RailIcon name="star" />
            自选股
          </h3>
          {watchlist.length === 0 ? (
            <div className="market-watchlist-empty">
              <p>还没有自选股</p>
              <button
                type="button"
                aria-label="查看上证指数 000001.SS"
                onClick={() => onSelect(SHANGHAI_COMPOSITE)}
              >
                <strong>上证指数</strong>
                <span>000001.SS</span>
              </button>
              <button
                className="market-add-first"
                type="button"
                onClick={() => onAdd(SHANGHAI_COMPOSITE)}
              >
                添加第一只自选
              </button>
            </div>
          ) : (
            <InstrumentList
              instruments={watchlist}
              label="自选股"
              selectedSymbol={selectedSymbol}
              onRemove={onRemove}
              onSelect={onSelect}
            />
          )}
        </section>

        <section aria-labelledby="market-recent-title">
          <h3 id="market-recent-title">
            <RailIcon name="clock" />
            最近访问
          </h3>
          {recent.length === 0 ? (
            <p className="market-recent-empty">
              搜索或查看标的后会出现在这里。
            </p>
          ) : (
            <InstrumentList
              instruments={recent}
              label="最近访问"
              selectedSymbol={selectedSymbol}
              onSelect={onSelect}
            />
          )}
        </section>
      </div>
    </aside>
  );
}
