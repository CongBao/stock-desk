import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { formulaApi, type FormulaApi } from '../formulas/formulaApi';
import {
  marketApi,
  type MarketApi,
  type MarketPoolSummary,
} from '../market/marketApi';
import {
  backtestApi,
  type BacktestApi,
  type BacktestOverview,
} from './backtestApi';
import {
  loadBacktestDraft,
  parseBacktestPrefill,
  resolvedBacktestPrefill,
  type BacktestPrefillResolution,
  type BacktestDraft,
} from './backtestDraft';
import { BacktestWizard } from './BacktestWizard';
import type { FormulaChoice } from './steps/FormulaStep';

const runLabels: Readonly<Record<string, string>> = {
  queued: '等待执行',
  running: '运行中',
  succeeded: '已完成',
  partial_failed: '部分完成',
  failed: '失败',
  cancelled: '已取消',
};

export type BacktestWorkspacePageProps = {
  readonly api?: BacktestApi;
  readonly formulaClient?: Pick<FormulaApi, 'listFormulas' | 'listVersions'>;
  readonly marketClient?: Pick<MarketApi, 'getPools' | 'searchInstruments'>;
};

export function BacktestWorkspacePage({
  api = backtestApi,
  formulaClient = formulaApi,
  marketClient = marketApi,
}: BacktestWorkspacePageProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const parsedPrefill = useMemo(
    () => parseBacktestPrefill(location.search),
    [location.search],
  );
  const [formulaChoices, setFormulaChoices] = useState<
    readonly FormulaChoice[]
  >([]);
  const [pools, setPools] = useState<readonly MarketPoolSummary[]>([]);
  const [runs, setRuns] = useState<readonly BacktestOverview[] | null>(null);
  const [formulaError, setFormulaError] = useState(false);
  const [poolError, setPoolError] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [storedDraft] = useState(() => loadBacktestDraft());
  const [restoredDraft, setRestoredDraft] = useState<
    BacktestDraft | undefined
  >();
  const [verifiedStoredSingle, setVerifiedStoredSingle] = useState(false);
  const [wizardKey, setWizardKey] = useState(0);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [prefillResolution, setPrefillResolution] =
    useState<BacktestPrefillResolution>(() => ({
      search: location.search,
      verified: parsedPrefill.kind !== 'valid',
      draft: null,
    }));

  function refresh() {
    setFormulaError(false);
    setPoolError(false);
    setHistoryError(false);
    setRuns(null);
    setRefreshGeneration((value) => value + 1);
  }

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const formulaPage = await formulaClient.listFormulas({
          signal: controller.signal,
        });
        const trading = formulaPage.items.filter(
          (item) => item.formulaType === 'trading',
        );
        const choices = await Promise.all(
          trading.map(async (item) => ({
            ...item,
            versions: (
              await formulaClient.listVersions(item.id, {
                signal: controller.signal,
              })
            )
              .filter((version) => version.formulaType === 'trading')
              .sort((left, right) => right.version - left.version),
          })),
        );
        if (!active) return;
        setFormulaChoices(
          choices.filter((choice) => choice.versions.length > 0),
        );
      } catch {
        if (!active || controller.signal.aborted) return;
        setFormulaError(true);
      }
    })();
    void (async () => {
      try {
        const allPools: MarketPoolSummary[] = [];
        let cursor: string | undefined;
        let complete = false;
        for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
          const page = await marketClient.getPools({
            cursor,
            limit: 50,
            signal: controller.signal,
          });
          allPools.push(...page.items);
          if (page.nextCursor === null) {
            complete = true;
            break;
          }
          cursor = page.nextCursor;
        }
        if (active) {
          setPools(allPools);
          setPoolError(!complete);
        }
      } catch {
        if (active && !controller.signal.aborted) setPoolError(true);
      }
    })();
    void (async () => {
      try {
        const page = await api.listRuns({ signal: controller.signal });
        if (active) setRuns(page.items.slice(0, 20));
      } catch {
        if (active && !controller.signal.aborted) setHistoryError(true);
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [api, formulaClient, marketClient, refreshGeneration]);

  useEffect(() => {
    const storedScope = storedDraft?.scope;
    if (storedScope?.kind !== 'single') {
      setVerifiedStoredSingle(false);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setVerifiedStoredSingle(false);
    void marketClient
      .searchInstruments({
        query: storedScope.symbol,
        limit: 10,
        signal: controller.signal,
      })
      .then((items) => {
        if (active)
          setVerifiedStoredSingle(
            items.some(
              (item) =>
                item.symbol === storedScope.symbol &&
                item.instrumentKind === 'stock' &&
                item.listingStatus !== 'delisted',
            ),
          );
      })
      .catch(() => undefined);
    return () => {
      active = false;
      controller.abort();
    };
  }, [marketClient, refreshGeneration, storedDraft]);

  useEffect(() => {
    if (parsedPrefill.kind !== 'valid') {
      setPrefillResolution({
        search: location.search,
        verified: true,
        draft: null,
      });
      return undefined;
    }
    const controller = new AbortController();
    let active = true;
    const draft = parsedPrefill.draft;
    const symbol = draft.scope.kind === 'single' ? draft.scope.symbol : '';
    setPrefillResolution({
      search: location.search,
      verified: false,
      draft: null,
    });
    void marketClient
      .searchInstruments({
        query: symbol,
        limit: 10,
        signal: controller.signal,
      })
      .then((items) => {
        if (!active) return;
        const listed = items.some(
          (item) =>
            item.symbol === symbol &&
            item.instrumentKind === 'stock' &&
            item.listingStatus !== 'delisted',
        );
        setPrefillResolution({
          search: location.search,
          verified: true,
          draft: listed ? draft : null,
        });
      })
      .catch(() => {
        if (active && !controller.signal.aborted)
          setPrefillResolution({
            search: location.search,
            verified: true,
            draft: null,
          });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [location.search, marketClient, parsedPrefill]);

  const restorable = useMemo(() => {
    if (storedDraft === null) return null;
    const choice = formulaChoices.find(
      (item) => item.id === storedDraft.formulaId,
    );
    if (
      choice?.versions.some(
        (version) => version.id === storedDraft.formulaVersionId,
      ) !== true
    )
      return null;
    const storedScope = storedDraft.scope;
    if (storedScope.kind === 'single' && !verifiedStoredSingle) return null;
    if (
      storedScope.kind === 'preset' &&
      !pools.some(
        (pool) =>
          pool.kind === 'preset' &&
          pool.poolId === storedScope.poolId &&
          pool.snapshotId === storedScope.snapshotId,
      )
    )
      return null;
    if (
      storedScope.kind === 'custom' &&
      !pools.some(
        (pool) =>
          pool.kind === 'custom' &&
          pool.poolId === storedScope.poolId &&
          pool.revision === storedScope.revision,
      )
    )
      return null;
    return storedDraft;
  }, [formulaChoices, pools, storedDraft, verifiedStoredSingle]);
  const prefillPending =
    parsedPrefill.kind === 'valid' &&
    (prefillResolution.search !== location.search ||
      !prefillResolution.verified);
  const prefillInvalid =
    parsedPrefill.kind === 'invalid' ||
    (parsedPrefill.kind === 'valid' &&
      prefillResolution.search === location.search &&
      prefillResolution.verified &&
      prefillResolution.draft === null);
  const currentPrefill = resolvedBacktestPrefill(
    parsedPrefill,
    prefillResolution,
    location.search,
  );

  return (
    <article className="backtest-workspace-page">
      <header className="page-heading">
        <div>
          <span className="eyebrow">STRATEGY BACKTEST / v0.4.0</span>
          <h2 data-page-heading tabIndex={-1}>
            策略回测
          </h2>
          <p>从已保存的通达信兼容交易公式出发，通过五步向导创建可复现回测。</p>
        </div>
        <div className="page-heading-actions">
          {restorable !== null && restoredDraft === undefined ? (
            <button
              type="button"
              className="secondary-action"
              onClick={() => {
                setRestoredDraft(restorable);
                setWizardKey((value) => value + 1);
              }}
            >
              恢复上次草稿
            </button>
          ) : null}
          <button type="button" className="secondary-action" onClick={refresh}>
            刷新公式、股票池与历史
          </button>
        </div>
      </header>
      {formulaError ? (
        <p className="workspace-notice" role="status">
          交易公式目录暂时不可用；请稍后重试，其他配置仍可继续查看。
        </p>
      ) : null}
      {poolError ? (
        <p className="workspace-notice" role="status">
          股票池目录暂时不可用；仍可选择单只证券。
        </p>
      ) : null}
      {prefillInvalid ? (
        <p className="workspace-notice" role="status">
          行情预填参数无效或已失效，未应用任何预填内容。
        </p>
      ) : null}
      {prefillPending ? (
        <p className="workspace-notice" role="status">
          正在核对行情预填…
        </p>
      ) : (
        <div
          className="guidance-anchor-contents"
          data-guidance-target="backtest-wizard"
        >
          <BacktestWizard
            key={`${String(wizardKey)}:${location.search}`}
            api={api}
            formulaChoices={formulaChoices}
            initialState={restoredDraft ?? currentPrefill ?? undefined}
            marketApiClient={marketClient}
            pools={pools}
            catalogRevision={refreshGeneration}
            onSubmitted={(submission, notice) =>
              void navigate(`/backtests/${submission.runId}`, {
                state: { submissionNotice: notice },
              })
            }
          />
        </div>
      )}
      <section
        className="backtest-history"
        data-guidance-target="backtest-history"
        aria-labelledby="backtest-history-heading"
      >
        <div className="section-heading">
          <h3 id="backtest-history-heading">最近回测</h3>
          <span>最多显示最近 20 条</span>
        </div>
        {historyError ? (
          <p role="status">暂时无法读取最近回测</p>
        ) : runs === null ? (
          <p role="status">正在读取最近回测…</p>
        ) : runs.length === 0 ? (
          <p>还没有回测记录，完成上方配置即可创建第一条。</p>
        ) : (
          <ul>
            {runs.map((run) => (
              <li key={run.runId}>
                <Link to={`/backtests/${run.runId}`}>
                  <strong>{runLabels[run.status] ?? '未知状态'}</strong>
                  <span>
                    {run.processed} / {run.total}
                  </span>
                  <time dateTime={run.createdAt}>
                    {run.createdAt.slice(0, 10)}
                  </time>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}
