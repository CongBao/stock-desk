import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { marketApi, type MarketApi, type MarketPoolDetail } from './marketApi';
import type { MarketInstrumentSelection } from './marketStore';

type StockPoolPanelProps = {
  readonly api?: MarketApi;
  readonly onSelectInstrument: (instrument: MarketInstrumentSelection) => void;
  readonly onSelectPool: (poolId: string) => void;
  readonly selectedPoolId: string | null;
  readonly onPoolDetail?: (detail: MarketPoolDetail | null) => void;
};

const poolKindLabels = { preset: '预设', custom: '自定义' } as const;
const poolCategoryLabels = {
  all_a: '全 A',
  index: '指数',
  industry: '行业',
} as const;
const MEMBER_BATCH_SIZE = 100;

function compositionTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Shanghai',
  }).format(new Date(value));
}

function PoolMembers({
  detail,
  onSelectInstrument,
}: {
  readonly detail: MarketPoolDetail;
  readonly onSelectInstrument: (instrument: MarketInstrumentSelection) => void;
}) {
  const [pageIndex, setPageIndex] = useState(0);
  const pageCount = Math.ceil(detail.members.length / MEMBER_BATCH_SIZE);
  const pageStart = pageIndex * MEMBER_BATCH_SIZE;
  const visibleMembers = detail.members.slice(
    pageStart,
    pageStart + MEMBER_BATCH_SIZE,
  );
  const composition = detail.provenance.composition;

  return (
    <div className="pool-members">
      <h4>{detail.name} · 成员</h4>
      {detail.kind === 'preset' && composition !== undefined ? (
        <dl
          className="pool-composition-context"
          role="group"
          aria-label={`${detail.name}成分信息`}
        >
          <div>
            <dt>分类</dt>
            <dd>{poolCategoryLabels[composition.category]}</dd>
          </div>
          <div>
            <dt>成分截至</dt>
            <dd>
              <time dateTime={composition.dataCutoff}>
                {compositionTime(composition.dataCutoff)}
              </time>
            </dd>
          </div>
          <div>
            <dt>更新于</dt>
            <dd>
              <time dateTime={composition.fetchedAt}>
                {compositionTime(composition.fetchedAt)}
              </time>
            </dd>
          </div>
          <div>
            <dt>来源</dt>
            <dd>来源 {composition.source}</dd>
          </div>
        </dl>
      ) : detail.kind === 'custom' ? (
        <p className="pool-custom-revision">自定义成员版本 {detail.revision}</p>
      ) : null}
      <ul aria-label={`${detail.name}成员`}>
        {visibleMembers.map((member) => (
          <li key={member.symbol}>
            <button
              type="button"
              onClick={() =>
                onSelectInstrument({ symbol: member.symbol, name: member.name })
              }
            >
              <strong>{member.name}</strong>
              <span>{member.symbol}</span>
            </button>
          </li>
        ))}
      </ul>
      {pageCount > 1 ? (
        <div
          className="pool-pagination"
          role="group"
          aria-label="股票池成员分页"
        >
          <button
            type="button"
            disabled={pageIndex === 0}
            onClick={() => setPageIndex(0)}
          >
            首页
          </button>
          <button
            type="button"
            disabled={pageIndex === 0}
            onClick={() => setPageIndex((index) => Math.max(0, index - 1))}
          >
            上一页
          </button>
          <span aria-live="polite">
            第 {pageIndex + 1} / {pageCount} 页
          </span>
          <button
            type="button"
            disabled={pageIndex === pageCount - 1}
            onClick={() =>
              setPageIndex((index) => Math.min(pageCount - 1, index + 1))
            }
          >
            下一页
          </button>
          <button
            type="button"
            disabled={pageIndex === pageCount - 1}
            onClick={() => setPageIndex(pageCount - 1)}
          >
            末页
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function StockPoolPanel({
  api = marketApi,
  onSelectInstrument,
  onSelectPool,
  selectedPoolId,
  onPoolDetail,
}: StockPoolPanelProps) {
  const [localPoolId, setLocalPoolId] = useState<string | null>(null);
  const activePoolId = localPoolId ?? selectedPoolId;
  useEffect(() => {
    if (selectedPoolId === null) setLocalPoolId(null);
  }, [selectedPoolId]);
  const pools = useInfiniteQuery({
    queryKey: ['market', 'pools'],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      api.getPools({ cursor: pageParam, limit: 20, signal }),
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const detail = useQuery({
    queryKey: ['market', 'pool', activePoolId],
    enabled: activePoolId !== null,
    queryFn: ({ signal }) => {
      if (activePoolId === null) throw new Error('Pool selection is missing');
      return api.getPool(activePoolId, { signal });
    },
  });
  const summaries = pools.data?.pages.flatMap((page) => page.items) ?? [];

  useEffect(() => {
    if (activePoolId === null || detail.data?.poolId !== activePoolId) {
      onPoolDetail?.(null);
      return;
    }
    onPoolDetail?.(detail.data);
  }, [activePoolId, detail.data, onPoolDetail]);

  function selectPool(poolId: string) {
    setLocalPoolId(poolId);
    onSelectPool(poolId);
  }

  return (
    <section className="stock-pool-panel" aria-labelledby="stock-pools-title">
      <header>
        <div>
          <span className="panel-kicker">POOLS</span>
          <h3 id="stock-pools-title">股票池</h3>
        </div>
        <span className="read-only-badge">只读选择</span>
      </header>

      {pools.isPending ? (
        <p role="status" className="market-inline-state">
          正在读取本地股票池…
        </p>
      ) : pools.isError ? (
        <p role="alert" className="market-inline-state">
          股票池暂不可用
        </p>
      ) : summaries.length === 0 ? (
        <p role="status" className="market-inline-state">
          暂无本地股票池
        </p>
      ) : (
        <ul className="pool-list" aria-label="本地股票池">
          {summaries.map((pool) => (
            <li key={pool.poolId}>
              <button
                type="button"
                aria-pressed={activePoolId === pool.poolId}
                onClick={() => selectPool(pool.poolId)}
              >
                <span>
                  <strong>{pool.name}</strong>
                  <small>
                    {pool.memberCount.toLocaleString('zh-CN')} 只证券
                  </small>
                </span>
                <em data-kind={pool.kind}>{poolKindLabels[pool.kind]}</em>
              </button>
            </li>
          ))}
        </ul>
      )}

      {pools.hasNextPage ? (
        <button
          className="pool-more"
          type="button"
          disabled={pools.isFetchingNextPage}
          onClick={() => void pools.fetchNextPage()}
        >
          {pools.isFetchingNextPage ? '正在加载…' : '加载更多股票池'}
        </button>
      ) : null}

      {activePoolId === null ? (
        <p className="pool-guidance">选择股票池后查看其中证券。</p>
      ) : detail.isPending ? (
        <p role="status" className="market-inline-state">
          正在读取股票池成员…
        </p>
      ) : detail.isError ? (
        <p role="alert" className="market-inline-state">
          股票池详情暂不可用
        </p>
      ) : detail.data.members.length === 0 ? (
        <p role="status" className="market-inline-state">
          该股票池没有证券
        </p>
      ) : (
        <PoolMembers
          key={activePoolId}
          detail={detail.data}
          onSelectInstrument={onSelectInstrument}
        />
      )}
    </section>
  );
}
