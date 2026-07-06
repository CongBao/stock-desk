import theme from './theme.css?raw';

function mediaSlice(maxWidth: number, nextMaxWidth: number): string {
  const start = theme.indexOf(`@media (max-width: ${String(maxWidth)}px)`);
  const end = theme.indexOf(
    `@media (max-width: ${String(nextMaxWidth)}px)`,
    start + 1,
  );
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return theme.slice(start, end);
}

it('keeps the 1201-1600 context drawer scoped to Formula Studio', () => {
  const desktop = mediaSlice(1600, 1200);

  expect(desktop).toContain(".app-shell[data-workspace='formulas'] {");
  expect(desktop).toContain(
    ".app-shell[data-workspace='formulas'] .context-panel {",
  );
  expect(desktop).not.toMatch(/\n\s*\.app-shell\s*\{/u);
  expect(desktop).not.toMatch(/\n\s*\.context-panel\s*\{/u);
});

it('uses the global context drawer only at the 1200px tablet breakpoint', () => {
  const tablet = mediaSlice(1200, 760);

  expect(tablet).toContain('.app-shell {');
  expect(tablet).toContain('.context-panel {');
  expect(tablet).toContain(".context-panel[data-open='true'] {");
});

it('collapses the backtest editor to one overflow-safe column by 1100px', () => {
  const start = theme.indexOf('@media (max-width: 1100px)');
  const end = theme.indexOf('@media (max-width: 760px)', start);
  const tablet = theme.slice(start, end);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(tablet).toContain('.backtest-wizard-layout');
  expect(tablet).toContain('grid-template-columns: minmax(0, 1fr)');
  expect(tablet).toContain(".app-shell[data-workspace='backtests'] .workspace");
  expect(tablet).toContain('overflow-x: clip');
});

it('keeps report grids and replay bounded at 1024px while tables scroll locally', () => {
  const start = theme.indexOf('@media (max-width: 1100px)');
  const end = theme.indexOf('@media (max-width: 760px)', start);
  const tablet = theme.slice(start, end);
  expect(tablet).toContain('.report-metric-grid');
  expect(tablet).toContain('.report-counts');
  expect(tablet).toContain('grid-template-columns: repeat(2, minmax(0, 1fr))');
  expect(theme).toContain('.report-table-scroll');
  expect(theme).toMatch(/\.report-table-scroll\s*\{[^}]*overflow-x: auto/su);
  expect(theme).toMatch(/\.trade-replay\s*\{[^}]*min-width: 0/su);
});
