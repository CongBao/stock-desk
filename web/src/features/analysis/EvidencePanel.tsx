import type { AnalysisClaim, EvidenceItem } from './analysisApi';

const sectionLabels: Readonly<Record<string, string>> = {
  market: '行情',
  fundamentals: '基本面',
  announcements: '公告',
  news: '新闻',
};

export function EvidencePanel({
  claim,
  items,
}: {
  readonly claim: AnalysisClaim | null;
  readonly items: readonly EvidenceItem[];
}) {
  return (
    <aside className="analysis-evidence" aria-label="证据详情">
      <header>
        <span className="panel-kicker">EVIDENCE</span>
        <h3>事实证据</h3>
      </header>
      {claim === null ? null : (
        <div className="evidence-claim-context">
          <strong>{claim.text}</strong>
          <span>
            立场：
            {claim.stance === 'support'
              ? '支持'
              : claim.stance === 'oppose'
                ? '反对'
                : '不确定'}
          </span>
        </div>
      )}
      {items.length === 0 ? (
        <p className="analysis-empty">
          选择一条事实判断后，在这里核对持久化证据。
        </p>
      ) : (
        <div className="evidence-scroll" aria-label="证据记录滚动区">
          {items.map((item) => (
            <article key={item.evidenceId} className="evidence-card">
              <div className="evidence-card-heading">
                <span>{sectionLabels[item.sectionKind]}</span>
                <span>{item.canonicalSource}</span>
              </div>
              <p>
                <strong>摘录：</strong>
                {item.excerpt}
              </p>
              <dl>
                <div>
                  <dt>记录</dt>
                  <dd>{item.sourceRecord}</dd>
                </div>
                <div>
                  <dt>数据版本</dt>
                  <dd>{item.datasetVersion}</dd>
                </div>
                <div>
                  <dt>发布时间：</dt>
                  <dd>
                    {item.publishedAt === null
                      ? '未提供'
                      : new Date(item.publishedAt).toLocaleString('zh-CN')}
                  </dd>
                </div>
                <div>
                  <dt>数据截止</dt>
                  <dd>{new Date(item.dataCutoff).toLocaleString('zh-CN')}</dd>
                </div>
                <div>
                  <dt>采集时间</dt>
                  <dd>{new Date(item.fetchedAt).toLocaleString('zh-CN')}</dd>
                </div>
                <div>
                  <dt>质量标记：</dt>
                  <dd>
                    {item.qualityFlags.length === 0
                      ? '无'
                      : item.qualityFlags.join('、')}
                  </dd>
                </div>
                <div>
                  <dt>来源路由：</dt>
                  <dd>
                    {item.route === null || item.route === undefined
                      ? '未提供'
                      : JSON.stringify(item.route)}
                  </dd>
                </div>
              </dl>
              {item.sourceUrl === null ? null : (
                <a href={item.sourceUrl} target="_blank" rel="noreferrer">
                  打开来源页面（不会自动抓取）
                </a>
              )}
            </article>
          ))}
        </div>
      )}
    </aside>
  );
}
