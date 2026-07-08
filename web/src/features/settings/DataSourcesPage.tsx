import { useEffect, useRef, useState, type FormEvent } from 'react';

import {
  sourceSettingsApi,
  sourceCategories,
  type DiagnosticState,
  type SourceCategory,
  type SourceDiagnostic,
  type SourcePriorities,
  type SourceProvider,
  type SourceSettings,
  type SourceSettingsApi,
} from './sourceSettingsApi';

const sourceMetadata = [
  {
    id: 'tushare',
    name: 'Tushare',
    kind: '主数据源',
    description: '覆盖行情、证券目录和交易日历；需要服务端安全保存 Token。',
  },
  {
    id: 'akshare',
    name: 'AKShare',
    kind: '备用数据源',
    description: '适合日线、周线和证券目录降级；不承诺完整交易日历。',
  },
  {
    id: 'baostock',
    name: 'BaoStock',
    kind: '备用数据源',
    description: '无需用户凭证，可用于行情、证券目录和交易日历。',
  },
  {
    id: 'tdx_local',
    name: '通达信本地',
    kind: '本地数据源',
    description: '只读取经过安全检查的绝对 vipdoc 目录，不跟随符号链接。',
  },
  {
    id: 'eastmoney',
    name: 'Eastmoney',
    kind: '预留数据源',
    description: '当前适配器尚未交付，检测会如实显示不可用。',
  },
] as const satisfies readonly {
  readonly id: SourceProvider;
  readonly name: string;
  readonly kind: string;
  readonly description: string;
}[];

const sourceNames: Readonly<Record<SourceProvider, string>> =
  Object.fromEntries(
    sourceMetadata.map((source) => [source.id, source.name]),
  ) as Readonly<Record<SourceProvider, string>>;
const categoryLabels: Readonly<Record<SourceCategory, string>> = {
  daily_bars: '日线行情',
  weekly_bars: '周线行情',
  minute_bars: '60 分钟行情',
  instruments: '证券目录',
  trading_calendar: '交易日历',
  execution_status: '回测执行状态',
  fundamentals: '基本面',
  announcements: '公告',
  news: '新闻',
};
const stateLabels: Readonly<Record<DiagnosticState, string>> = {
  available: '可用',
  unavailable: '不可用',
  permission_denied: '权限不足',
  unsupported: '不支持',
  transient_failure: '暂时失败',
};
const capabilityLabels = {
  bars: '行情',
  execution_status: '回测执行状态',
  instruments: '证券目录',
  trading_calendar: '交易日历',
} as const;
const periodLabels = { '1d': '日线', '1w': '周线', '60m': '60 分钟' } as const;
const marketLabels = { SH: '上交所', SZ: '深交所' } as const;
const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'Asia/Shanghai',
});

function formatTime(value: string | null): string {
  return value === null ? '未知' : dateTimeFormatter.format(new Date(value));
}

function clonePriorities(priorities: SourcePriorities): SourcePriorities {
  return Object.fromEntries(
    sourceCategories.map((category) => [category, [...priorities[category]]]),
  ) as unknown as SourcePriorities;
}

function DiagnosticDetails({
  diagnostic,
}: {
  readonly diagnostic: SourceDiagnostic;
}) {
  const detail =
    diagnostic.fallback_reason?.detail ?? diagnostic.gaps[0]?.detail;
  return (
    <div
      className="source-diagnostic"
      aria-label={`${sourceNames[diagnostic.source]} 检测结果`}
    >
      <div className="diagnostic-summary">
        <span className="source-state" data-state={diagnostic.status}>
          {stateLabels[diagnostic.status]}
        </span>
        <span>检测于 {formatTime(diagnostic.last_checked)}</span>
      </div>
      <dl className="diagnostic-metrics">
        <div>
          <dt>能力</dt>
          <dd>
            {diagnostic.capabilities.length > 0
              ? diagnostic.capabilities
                  .map((item) => capabilityLabels[item])
                  .join('、')
              : '未确认'}
          </dd>
        </div>
        <div>
          <dt>可用周期</dt>
          <dd>
            {diagnostic.available_periods.length > 0
              ? diagnostic.available_periods
                  .map((item) => periodLabels[item])
                  .join('、')
              : '未确认'}
          </dd>
        </div>
        <div>
          <dt>识别市场</dt>
          <dd>
            {diagnostic.markets.length > 0
              ? diagnostic.markets.map((item) => marketLabels[item]).join('、')
              : '未确认'}
          </dd>
        </div>
        <div>
          <dt>最近更新</dt>
          <dd>{formatTime(diagnostic.last_update)}</dd>
        </div>
        <div>
          <dt>数据截至</dt>
          <dd>{formatTime(diagnostic.data_cutoff)}</dd>
        </div>
      </dl>
      {diagnostic.permissions.length > 0 ? (
        <ul className="permission-list" aria-label="权限状态">
          {diagnostic.permissions.map((permission) => (
            <li key={permission.category}>
              {categoryLabels[permission.category]}：
              {stateLabels[permission.state]}
            </li>
          ))}
        </ul>
      ) : null}
      {diagnostic.gaps.length > 0 ? (
        <div className="capability-gaps">
          <strong>能力缺口</strong>
          <ul>
            {diagnostic.gaps.map((gap) => (
              <li key={gap.category}>
                {categoryLabels[gap.category]} · {stateLabels[gap.state]}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {detail !== undefined ? (
        <p className="fallback-reason">{detail}</p>
      ) : null}
    </div>
  );
}

type DataSourcesPageProps = { readonly api?: SourceSettingsApi };

export function DataSourcesPage({
  api = sourceSettingsApi,
}: DataSourcesPageProps) {
  const [settings, setSettings] = useState<SourceSettings | null>(null);
  const [priorities, setPriorities] = useState<SourcePriorities | null>(null);
  const [tdxPath, setTdxPath] = useState('');
  const [token, setToken] = useState('');
  const [loadError, setLoadError] = useState(false);
  const [saveState, setSaveState] = useState<
    'idle' | 'dirty' | 'saving' | 'saved' | 'error'
  >('idle');
  const [diagnostics, setDiagnostics] = useState<
    Partial<Record<SourceProvider, SourceDiagnostic>>
  >({});
  const [diagnosticErrors, setDiagnosticErrors] = useState<Set<SourceProvider>>(
    new Set(),
  );
  const [testingSources, setTestingSources] = useState<Set<SourceProvider>>(
    new Set(),
  );
  const [staleDiagnostics, setStaleDiagnostics] = useState<Set<SourceProvider>>(
    new Set(),
  );
  const saveController = useRef<AbortController | null>(null);
  const editRevision = useRef(0);
  const diagnosticControllers = useRef(
    new Map<SourceProvider, AbortController>(),
  );
  const canTestConnections = saveState === 'idle' || saveState === 'saved';

  useEffect(() => {
    const controller = new AbortController();
    void api
      .getSettings({ signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setSettings(value);
        setPriorities(clonePriorities(value.priorities));
        setTdxPath(value.tdx_path ?? '');
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadError(true);
      });
    return () => {
      controller.abort();
      saveController.current?.abort();
      for (const pending of diagnosticControllers.current.values()) {
        pending.abort();
      }
      diagnosticControllers.current.clear();
    };
  }, [api]);

  function invalidateDiagnostics() {
    const affected = new Set<SourceProvider>([
      ...(Object.keys(diagnostics) as SourceProvider[]),
      ...diagnosticErrors,
      ...testingSources,
      ...diagnosticControllers.current.keys(),
    ]);
    for (const pending of diagnosticControllers.current.values()) {
      pending.abort();
    }
    diagnosticControllers.current.clear();
    setDiagnostics({});
    setDiagnosticErrors(new Set());
    setTestingSources(new Set());
    if (affected.size > 0) {
      setStaleDiagnostics((current) => new Set([...current, ...affected]));
    }
  }

  function markEdited() {
    editRevision.current += 1;
    invalidateDiagnostics();
    setSaveState('dirty');
  }

  function moveSource(
    category: SourceCategory,
    index: number,
    direction: -1 | 1,
  ) {
    setPriorities((current) => {
      if (current === null) return current;
      const reordered = [...current[category]];
      const target = index + direction;
      if (target < 0 || target >= reordered.length) return current;
      [reordered[index], reordered[target]] = [
        reordered[target],
        reordered[index],
      ];
      return { ...current, [category]: reordered };
    });
    markEdited();
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (priorities === null) return;
    invalidateDiagnostics();
    saveController.current?.abort();
    const controller = new AbortController();
    saveController.current = controller;
    const submittedRevision = editRevision.current;
    const pendingToken = token;
    setToken('');
    setSaveState('saving');
    try {
      const publicResult = await api.savePublic(
        { priorities, tdxPath: tdxPath.length > 0 ? tdxPath : null },
        { signal: controller.signal },
      );
      let tushare = publicResult.tushare;
      if (pendingToken.length > 0) {
        tushare = await api.saveTushare(pendingToken, {
          signal: controller.signal,
        });
      }
      if (controller.signal.aborted) return;
      const resolved = { ...publicResult, tushare };
      invalidateDiagnostics();
      if (editRevision.current === submittedRevision) {
        setSettings(resolved);
        setPriorities(clonePriorities(resolved.priorities));
        setTdxPath(resolved.tdx_path ?? '');
        setSaveState('saved');
      } else {
        setSettings((current) =>
          current === null
            ? current
            : { ...current, tushare: resolved.tushare },
        );
        setSaveState('dirty');
      }
    } catch {
      if (!controller.signal.aborted) {
        invalidateDiagnostics();
        setSaveState(
          editRevision.current === submittedRevision ? 'error' : 'dirty',
        );
      }
    } finally {
      if (saveController.current === controller) saveController.current = null;
    }
  }

  function testConnection(source: SourceProvider) {
    if (!canTestConnections) return;
    diagnosticControllers.current.get(source)?.abort();
    const controller = new AbortController();
    const diagnosticRevision = editRevision.current;
    diagnosticControllers.current.set(source, controller);
    setTestingSources((current) => new Set(current).add(source));
    setDiagnosticErrors((current) => {
      const next = new Set(current);
      next.delete(source);
      return next;
    });
    setStaleDiagnostics((current) => {
      const next = new Set(current);
      next.delete(source);
      return next;
    });
    void api
      .testSource(source, { signal: controller.signal })
      .then((diagnostic) => {
        if (
          controller.signal.aborted ||
          editRevision.current !== diagnosticRevision ||
          diagnosticControllers.current.get(source) !== controller
        )
          return;
        setDiagnostics((current) => ({ ...current, [source]: diagnostic }));
      })
      .catch(() => {
        if (
          !controller.signal.aborted &&
          editRevision.current === diagnosticRevision
        ) {
          setDiagnosticErrors((current) => new Set(current).add(source));
        }
      })
      .finally(() => {
        if (diagnosticControllers.current.get(source) === controller) {
          diagnosticControllers.current.delete(source);
          setTestingSources((current) => {
            const next = new Set(current);
            next.delete(source);
            return next;
          });
        }
      });
  }

  return (
    <article className="data-sources-page">
      <header className="page-heading data-sources-heading">
        <div>
          <span className="page-kicker">SETTINGS / DATA SOURCES</span>
          <h2 data-page-heading tabIndex={-1}>
            数据源设置
          </h2>
          <p>
            按数据类别安排降级顺序，安全保存凭证，并在更新数据前完成连接检测。
          </p>
        </div>
        <span className="release-badge">v0.2.0 · 本地优先</span>
      </header>

      {loadError ? (
        <p role="alert" className="settings-banner settings-banner-error">
          数据源设置读取失败，请稍后重试。
        </p>
      ) : settings === null || priorities === null ? (
        <p role="status" className="settings-banner">
          正在读取本地数据源设置…
        </p>
      ) : (
        <form onSubmit={(event) => void save(event)}>
          <section
            className="source-overview"
            aria-labelledby="source-overview-title"
          >
            <div className="settings-section-heading">
              <div>
                <span>01 / CONNECTIONS</span>
                <h3 id="source-overview-title">数据源连接</h3>
              </div>
              <p>检测只读取服务端配置，浏览器不会取回任何明文 Token。</p>
            </div>
            <div className="source-card-grid">
              {sourceMetadata.map((source) => {
                const diagnostic = diagnostics[source.id];
                const isTesting = testingSources.has(source.id);
                const persistenceHelpId = `${source.id}-test-persistence-help`;
                return (
                  <section
                    className="source-card"
                    key={source.id}
                    data-source={source.id}
                  >
                    <header>
                      <div>
                        <span>{source.kind}</span>
                        <h3>{source.name}</h3>
                      </div>
                      <span className="source-card-index">
                        {String(sourceMetadata.indexOf(source) + 1).padStart(
                          2,
                          '0',
                        )}
                      </span>
                    </header>
                    <p>{source.description}</p>

                    {source.id === 'tushare' ? (
                      <div className="source-field-stack">
                        <label>
                          <span>Tushare Token</span>
                          <input
                            type="password"
                            autoComplete="new-password"
                            value={token}
                            maxLength={4096}
                            onChange={(event) => {
                              setToken(event.currentTarget.value);
                              markEdited();
                            }}
                            placeholder="仅在需要更新时输入"
                          />
                        </label>
                        <small>
                          {settings.tushare.configured
                            ? `已配置：${settings.tushare.masked_hint ?? '安全存储不可读'}`
                            : settings.tushare.secure_storage_available
                              ? '尚未配置 Token'
                              : '请先配置 STOCK_DESK_MASTER_KEY'}
                        </small>
                      </div>
                    ) : null}

                    {source.id === 'tdx_local' ? (
                      <label className="source-field-stack">
                        <span>通达信 vipdoc 目录</span>
                        <input
                          aria-label="通达信 vipdoc 目录"
                          value={tdxPath}
                          maxLength={2048}
                          onChange={(event) => {
                            setTdxPath(event.currentTarget.value);
                            markEdited();
                          }}
                          placeholder="/absolute/path/to/vipdoc"
                        />
                      </label>
                    ) : null}

                    <button
                      type="button"
                      className="source-test-button"
                      disabled={isTesting || !canTestConnections}
                      aria-describedby={
                        canTestConnections ? undefined : persistenceHelpId
                      }
                      onClick={() => testConnection(source.id)}
                    >
                      {isTesting ? '检测中…' : `测试 ${source.name} 连接`}
                    </button>
                    <span id={persistenceHelpId} className="visually-hidden">
                      当前配置尚未成功保存，请先保存后再检测连接。
                    </span>
                    {diagnosticErrors.has(source.id) ? (
                      <p role="alert" className="source-inline-error">
                        连接检测失败，请检查本地配置后重试。
                      </p>
                    ) : diagnostic !== undefined ? (
                      <DiagnosticDetails diagnostic={diagnostic} />
                    ) : staleDiagnostics.has(source.id) ? (
                      <p className="source-not-tested">
                        配置已变更，请重新检测
                      </p>
                    ) : (
                      <p className="source-not-tested">尚未检测</p>
                    )}
                  </section>
                );
              })}
            </div>
          </section>

          <section
            className="priority-settings"
            aria-labelledby="priority-settings-title"
          >
            <div className="settings-section-heading">
              <div>
                <span>02 / FALLBACK ORDER</span>
                <h3 id="priority-settings-title">分类优先级</h3>
              </div>
              <p>
                只有失败、无权限、缺失、不支持或无数据时才进入下一来源，不拼接同一段行情。
              </p>
            </div>
            <div className="priority-grid">
              {sourceCategories.map((category) => (
                <section
                  className="priority-lane"
                  key={category}
                  role="group"
                  aria-label={`${categoryLabels[category]}优先级`}
                >
                  <header>
                    <h4>{categoryLabels[category]}</h4>
                    <span>{priorities[category].length} 个来源</span>
                  </header>
                  <ol>
                    {priorities[category].map((source, index) => (
                      <li key={source}>
                        <span className="priority-rank">{index + 1}</span>
                        <strong>{sourceNames[source]}</strong>
                        <div className="priority-actions">
                          <button
                            type="button"
                            disabled={index === 0}
                            aria-label={`上移 ${sourceNames[source]}（${categoryLabels[category]}）`}
                            onClick={() => moveSource(category, index, -1)}
                          >
                            ↑
                          </button>
                          <button
                            type="button"
                            disabled={index === priorities[category].length - 1}
                            aria-label={`下移 ${sourceNames[source]}（${categoryLabels[category]}）`}
                            onClick={() => moveSource(category, index, 1)}
                          >
                            ↓
                          </button>
                        </div>
                      </li>
                    ))}
                  </ol>
                </section>
              ))}
            </div>
          </section>

          <footer className="settings-save-bar">
            <div aria-live="polite">
              {saveState === 'saved' ? (
                <p role="status">设置已安全保存</p>
              ) : saveState === 'dirty' ? (
                <p role="status">存在未保存更改</p>
              ) : saveState === 'error' ? (
                <p role="alert">
                  保存失败；已输入的 Token 未保留，请检查配置后重试。
                </p>
              ) : (
                <p>Token 只写入服务端加密存储，不会回填到当前页面。</p>
              )}
            </div>
            <button type="submit" disabled={saveState === 'saving'}>
              {saveState === 'saving' ? '正在保存…' : '保存数据源设置'}
            </button>
          </footer>
        </form>
      )}
    </article>
  );
}
