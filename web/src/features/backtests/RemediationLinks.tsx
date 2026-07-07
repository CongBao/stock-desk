import { Link, useInRouterContext } from 'react-router-dom';

export function RemediationLinks() {
  const inRouter = useInRouterContext();
  const links = [
    ['/market', '更新行情数据'],
    ['/settings', '检查数据源'],
    ['/formulas', '检查交易公式'],
  ] as const;
  return (
    <nav aria-label="修复回测配置">
      {links.map(([to, label], index) => (
        <span key={to}>
          {index > 0 ? ' · ' : null}
          {inRouter ? <Link to={to}>{label}</Link> : <a href={to}>{label}</a>}
        </span>
      ))}
    </nav>
  );
}
