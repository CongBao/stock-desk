import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

import { marketApi, type MarketApi, type MarketInstrument } from './marketApi';

type StockSearchProps = {
  readonly api?: MarketApi;
  readonly focusOnMount?: boolean;
  readonly debounceMs?: number;
  readonly onSelect: (instrument: MarketInstrument) => void;
};

export function StockSearch({
  api = marketApi,
  focusOnMount = false,
  debounceMs = 250,
  onSelect,
}: StockSearchProps) {
  const listboxId = useId();
  const [inputValue, setInputValue] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(-1);
  const [isOpen, setIsOpen] = useState(false);
  const blurTimerRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const normalizedInput = inputValue.trim();

  useEffect(() => {
    const delay = /^\d{6}$/u.test(normalizedInput) ? 0 : debounceMs;
    const timer = window.setTimeout(() => {
      setDebouncedQuery(normalizedInput);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [debounceMs, normalizedInput]);

  useEffect(
    () => () => {
      if (blurTimerRef.current !== null)
        window.clearTimeout(blurTimerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (focusOnMount) inputRef.current?.focus();
  }, [focusOnMount]);

  const result = useQuery({
    queryKey: ['market', 'instrument-search', debouncedQuery],
    enabled: debouncedQuery.length > 0,
    queryFn: ({ signal }) =>
      api.searchInstruments({ query: debouncedQuery, limit: 20, signal }),
  });
  const queryIsCurrent =
    debouncedQuery.length > 0 && debouncedQuery === normalizedInput;
  const instruments = queryIsCurrent ? (result.data ?? []) : [];
  const showResults = isOpen && queryIsCurrent;

  function choose(instrument: MarketInstrument) {
    if (blurTimerRef.current !== null) {
      window.clearTimeout(blurTimerRef.current);
      blurTimerRef.current = null;
    }
    onSelect(instrument);
    setInputValue(`${instrument.name} · ${instrument.symbol}`);
    setDebouncedQuery('');
    setActiveIndex(-1);
    setIsOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      if (!showResults) return;
      event.preventDefault();
      event.stopPropagation();
      setIsOpen(false);
      setActiveIndex(-1);
      return;
    }
    if (!queryIsCurrent || instruments.length === 0) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIsOpen(true);
      setActiveIndex((index) =>
        index < 0 ? 0 : Math.min(index + 1, instruments.length - 1),
      );
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setIsOpen(true);
      setActiveIndex((index) =>
        index <= 0 ? instruments.length - 1 : index - 1,
      );
    } else if (event.key === 'Enter' && showResults && activeIndex >= 0) {
      event.preventDefault();
      const instrument = instruments[activeIndex];
      if (instrument) choose(instrument);
    }
  }

  return (
    <div className="stock-search">
      <label htmlFor={`${listboxId}-input`}>搜索证券</label>
      <div className="stock-search-field">
        <span aria-hidden="true">⌕</span>
        <input
          ref={inputRef}
          id={`${listboxId}-input`}
          role="combobox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-expanded={showResults}
          aria-activedescendant={
            showResults && activeIndex >= 0
              ? `${listboxId}-option-${String(activeIndex)}`
              : undefined
          }
          data-route-primary-focus
          autoComplete="off"
          placeholder="代码 / 中文名 / 拼音"
          value={inputValue}
          onBlur={() => {
            blurTimerRef.current = window.setTimeout(() => {
              setIsOpen(false);
              blurTimerRef.current = null;
            }, 120);
          }}
          onChange={(event) => {
            setInputValue(event.currentTarget.value);
            setActiveIndex(-1);
            setIsOpen(true);
          }}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
        />
      </div>
      {showResults ? (
        <div className="stock-search-popover">
          {result.isPending || result.isFetching ? (
            <p role="status">正在搜索本地证券…</p>
          ) : result.isError ? (
            <p role="alert">证券搜索暂不可用，请稍后重试</p>
          ) : instruments.length === 0 ? (
            <p role="status">未找到匹配的本地证券</p>
          ) : (
            <ul id={listboxId} role="listbox" aria-label="证券搜索结果">
              {instruments.map((instrument, index) => (
                <li
                  id={`${listboxId}-option-${String(index)}`}
                  key={instrument.symbol}
                  role="option"
                  tabIndex={-1}
                  aria-label={`${instrument.name} ${instrument.symbol}`}
                  aria-describedby={`${listboxId}-option-${String(index)}-meta`}
                  aria-selected={activeIndex === index}
                  onMouseDown={(event) => {
                    event.preventDefault();
                  }}
                  onClick={() => choose(instrument)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') choose(instrument);
                  }}
                >
                  <span className="stock-search-identity">
                    <strong>{instrument.name}</strong>
                    <span>{instrument.symbol}</span>
                  </span>
                  <small id={`${listboxId}-option-${String(index)}-meta`}>
                    来源 {instrument.provenance.source} · 截至{' '}
                    {instrument.provenance.dataCutoff.slice(0, 10)}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
