import type { AppRoute } from './routes';

type PlannedPageProps = {
  readonly route: AppRoute;
};

export function PlannedPage({ route }: PlannedPageProps) {
  return (
    <article className="planned-page">
      <header className="page-heading">
        <div>
          <span className="page-kicker">PLANNED WORKSPACE</span>
          <h2 data-page-heading tabIndex={-1}>
            {route.title}
          </h2>
          <p>{route.summary}</p>
        </div>
        <span className="release-badge">计划版本 {route.release}</span>
      </header>

      <section className="roadmap-panel" aria-labelledby="roadmap-heading">
        <div className="roadmap-visual" aria-hidden="true">
          <span className="roadmap-orbit roadmap-orbit-one" />
          <span className="roadmap-orbit roadmap-orbit-two" />
          <span className="roadmap-core">{route.icon}</span>
        </div>
        <div className="roadmap-copy">
          <span className="roadmap-index">ROADMAP / {route.release}</span>
          <h3 id="roadmap-heading">能力按阶段交付</h3>
          <p>{route.description}</p>
          <div className="roadmap-guardrail">
            <span aria-hidden="true">◇</span>
            当前页面仅说明规划，不会触发未实现的操作。
          </div>
        </div>
      </section>
    </article>
  );
}
