import type { MarketBarsResponse } from './marketApi';

type ProvenancePanelProps = {
  readonly data: MarketBarsResponse | undefined;
};

const adjustmentLabels = {
  none: '不复权',
  qfq: '前复权',
  hfq: '后复权',
} as const;

const providerLabels: Readonly<Record<string, string>> = {
  akshare: 'AKShare',
  baostock: 'BaoStock',
  eastmoney: '东方财富',
  tdx_local: '通达信本地',
  stock_desk_demo: 'Stock Desk 合成演示 · CC0-1.0',
  tushare: 'Tushare',
};

const reasonLabels: Readonly<Record<string, string>> = {
  permission_denied: '权限不足',
  unsupported: '能力不支持',
  missing: '数据覆盖不完整',
  no_data: '来源无数据',
  provider_unavailable: '来源不可用',
  transient_failure: '暂时失败',
  timeout: '请求超时',
  corrupt: '数据损坏',
  invalid_response: '响应无效',
  no_provider: '无可用来源',
};

const transitionLabels: Readonly<Record<string, string>> = {
  fallback_after_failure: '失败后回退',
  higher_priority_recovered: '高优先级来源恢复',
  priority_changed: '来源优先级变更',
};

function providerLabel(provider: string): string {
  return providerLabels[provider] ?? provider;
}

function reasonLabel(reason: string): string {
  return reasonLabels[reason] ?? reason;
}

function shortVersion(version: string): string {
  return `${version.slice(0, 15)}…${version.slice(-6)}`;
}

export function ProvenancePanel({ data }: ProvenancePanelProps) {
  return (
    <section className="provenance-panel" aria-labelledby="provenance-title">
      <header>
        <div>
          <span className="panel-kicker">PROVENANCE</span>
          <h3 id="provenance-title">数据来源</h3>
        </div>
        <span className="cache-only-badge">本地缓存</span>
      </header>
      {data === undefined ? (
        <p className="provenance-empty">
          选择证券后显示来源、截止时间与路由证据。
        </p>
      ) : (
        <>
          <div className="provenance-summary">
            <p title={data.provenance.source}>
              数据来源：{providerLabel(data.provenance.source)}
            </p>
            <p title={data.provenance.dataCutoff}>
              截至：{data.provenance.dataCutoff}
            </p>
          </div>
          <dl className="provenance-list">
            <div>
              <dt>抓取时间</dt>
              <dd>
                <time dateTime={data.provenance.fetchedAt}>
                  {data.provenance.fetchedAt}
                </time>
              </dd>
            </div>
            <div>
              <dt>复权口径</dt>
              <dd>{adjustmentLabels[data.provenance.adjustment]}</dd>
            </div>
            <div>
              <dt>数据版本</dt>
              <dd title={data.datasetVersion}>
                {shortVersion(data.datasetVersion)}
              </dd>
            </div>
            <div>
              <dt>路由版本</dt>
              <dd title={data.routeVersion}>
                {shortVersion(data.routeVersion)}
              </dd>
            </div>
          </dl>

          <div className="routing-evidence">
            <h4>路由尝试</h4>
            {data.routingManifest.attempts.length === 0 ? (
              <p>首选来源直接命中，无回退。</p>
            ) : (
              <ol>
                {data.routingManifest.attempts.map((attempt) => (
                  <li key={`${String(attempt.ordinal)}-${attempt.source}`}>
                    <strong title={`${attempt.source} · ${attempt.reason}`}>
                      {providerLabel(attempt.source)} ·{' '}
                      {reasonLabel(attempt.reason)}
                    </strong>
                    <span title={attempt.detail}>
                      {reasonLabel(attempt.reason)}
                    </span>
                  </li>
                ))}
              </ol>
            )}
            {data.routingManifest.transition === null ? null : (
              <p className="fallback-note">
                已从 {providerLabel(data.routingManifest.transition.fromSource)}{' '}
                切换到 {providerLabel(data.routingManifest.transition.toSource)}
                （
                {transitionLabels[data.routingManifest.transition.reason] ??
                  data.routingManifest.transition.reason}
                ）
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}
