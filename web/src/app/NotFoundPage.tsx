import { Link } from 'react-router-dom';

export function NotFoundPage() {
  return (
    <article className="planned-page not-found-page">
      <header className="page-heading">
        <div>
          <span className="page-kicker">404 / NOT FOUND</span>
          <h2 data-page-heading tabIndex={-1}>
            页面未找到
          </h2>
          <p>这个地址不属于当前 stock-desk 工作区。</p>
        </div>
      </header>

      <section className="not-found-panel" aria-labelledby="not-found-title">
        <span className="not-found-code" aria-hidden="true">
          404
        </span>
        <div>
          <h3 id="not-found-title">返回已交付的工作区入口</h3>
          <p>没有执行重定向或未实现操作，当前地址会保留到你主动离开。</p>
          <Link className="workspace-link" to="/market">
            返回行情工作区
          </Link>
        </div>
      </section>
    </article>
  );
}
