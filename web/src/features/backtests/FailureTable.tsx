/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- Scrollable table regions need keyboard focus. */
import type { BacktestFailure } from './backtestApi';

export function FailureTable({
  items,
}: {
  readonly items: readonly BacktestFailure[];
}) {
  if (items.length === 0) return <p>当前页没有失败记录。</p>;
  return (
    <div
      className="report-table-scroll"
      tabIndex={0}
      role="region"
      aria-label="可横向滚动的失败表"
    >
      <table>
        <thead>
          <tr>
            <th scope="col">序号</th>
            <th scope="col">证券</th>
            <th scope="col">原因</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={`${String(item.ordinal)}-${item.symbol}`}>
              <td>{item.ordinal + 1}</td>
              <th scope="row">{item.symbol}</th>
              <td>{item.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
