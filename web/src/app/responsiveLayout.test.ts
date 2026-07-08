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

it('keeps the 1201-1600 context drawer scoped to Formula Studio and analysis', () => {
  const desktop = mediaSlice(1600, 1200);

  expect(desktop).toContain(".app-shell[data-workspace='formulas'] {");
  expect(desktop).toContain(
    ".app-shell[data-workspace='formulas'] .context-panel {",
  );
  expect(desktop).toContain(".app-shell[data-workspace='analysis'] {");
  expect(desktop).toContain(
    ".app-shell[data-workspace='analysis'] .context-panel {",
  );
  expect(desktop).not.toMatch(/\n\s*\.app-shell\s*\{/u);
  expect(desktop).not.toMatch(/\n\s*\.context-panel\s*\{/u);
});

it('uses the global context drawer only at the 1200px tablet breakpoint', () => {
  const tablet = mediaSlice(1200, 760);

  expect(tablet).toContain('.app-shell {');
  expect(tablet).toContain('.context-panel {');
  expect(tablet).toContain(".context-panel[data-open='true'] {");
  expect(tablet).toContain(".app-shell[data-navigation-collapsed='true']");
});

it('uses a compact vertical rail instead of horizontally clipped navigation', () => {
  expect(theme).toContain('--collapsed-rail-width: 80px');
  expect(theme).toContain(".app-shell[data-navigation-collapsed='true'] {");
  expect(theme).toMatch(
    /\.app-shell\[data-navigation-collapsed='true'\]\s*\{[^}]*grid-template-columns:\s*var\(--collapsed-rail-width\) minmax\(0, 1fr\)/su,
  );
  expect(theme).not.toMatch(
    /data-navigation-collapsed='true'[^}]*grid-template-columns:\s*72px/su,
  );
  expect(theme).toContain("[data-navigation-collapsed='true'] .nav-label");
  const mobile = theme.slice(theme.indexOf('@media (max-width: 760px)'));
  expect(mobile).not.toContain('grid-template-columns: repeat(3');
  expect(mobile).toMatch(
    /\.app-shell\[data-navigation-collapsed='false'\]\s*\{[^}]*display:\s*block/su,
  );
  expect(mobile).toMatch(
    /\.app-shell\[data-navigation-collapsed='false'\] \.navigation-rail\s*\{[^}]*position:\s*relative/su,
  );
});

it('keeps short landscape navigation scrollable and truncates expanded labels safely', () => {
  expect(theme).toMatch(
    /\.primary-navigation\s*\{[^}]*min-height:\s*0[^}]*overflow-y:\s*auto[^}]*scrollbar-gutter:\s*stable/su,
  );
  expect(theme).toMatch(
    /\.nav-label\s*\{[^}]*min-width:\s*0[^}]*overflow:\s*hidden[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/su,
  );
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

it('uses a dominant overflow-safe three-column analysis report on wide screens', () => {
  expect(theme).toMatch(
    /\.analysis-report-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(210px, 0\.72fr\) minmax\(420px, 1\.8fr\) minmax\(\s*260px,\s*0\.9fr\s*\)/su,
  );
  expect(theme).toMatch(
    /\.analysis-process,\s*\.analysis-conclusion,\s*\.analysis-evidence\s*\{[^}]*min-width:\s*0/su,
  );
  expect(theme).toMatch(/\.evidence-scroll\s*\{[^}]*overflow-y:\s*auto/su);
  expect(theme).toMatch(
    /\.analysis-history-scroll\s*\{[^}]*overflow:\s*auto/su,
  );
});

it('turns process and evidence into bounded drawers when three columns no longer fit', () => {
  const start = theme.indexOf('@media (max-width: 1280px)');
  const end = theme.indexOf('@media (max-width: 760px)', start);
  const narrow = theme.slice(start, end);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(narrow).toContain('.analysis-report-workspace');
  expect(narrow).toContain('.analysis-report-toolbar');
  expect(narrow).toContain('.analysis-drawer');
  expect(narrow).toContain(".analysis-drawer[data-open='true']");
  expect(narrow).toContain('position: static');
  expect(narrow).toContain('display: none');
  expect(narrow).toContain('display: block');
});

it('wraps analysis controls and preserves one primary column on mobile', () => {
  const mobile = theme.slice(theme.indexOf('@media (max-width: 760px)'));
  expect(mobile).toContain('.analysis-run-controls');
  expect(mobile).toContain('grid-template-columns: minmax(0, 1fr)');
  expect(mobile).toContain('.analysis-report-toolbar');
  expect(mobile).toContain('flex-wrap: wrap');
});

it('keeps model lifecycle actions and evidence context bounded', () => {
  expect(theme).toMatch(
    /\.saved-model-actions\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap/su,
  );
  expect(theme).toMatch(
    /\.evidence-claim-context\s*\{[^}]*overflow-wrap:\s*anywhere/su,
  );
});

it('keeps the 390px collapsed rail single-column and reserves two columns for manual expansion', () => {
  const mobile = theme.slice(theme.indexOf('@media (max-width: 420px)'));
  expect(mobile).toMatch(
    /\[data-navigation-collapsed='true'\] \.primary-navigation ul\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/su,
  );
  expect(mobile).toMatch(
    /\[data-navigation-collapsed='false'\] \.primary-navigation ul\s*\{[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/su,
  );
  expect(mobile).toMatch(/\.topbar-kicker\s*\{[^}]*display:\s*none/su);
  expect(mobile).toMatch(
    /\.topbar-product-name\s*\{[^}]*white-space:\s*nowrap/su,
  );
});
