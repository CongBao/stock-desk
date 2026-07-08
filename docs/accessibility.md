# Accessibility and responsive layout

stock-desk targets WCAG 2.1 AA for its browser interface. The release gate scans
all six core workspaces with axe and rejects serious or critical violations.
Automated checks supplement, rather than replace, keyboard and visual review.

## Keyboard and assistive technology

- The first document control is a skip link to the main workspace. Route changes
  move focus to the new page heading and announce the destination; keyboard
  users can focus the skip link to move directly to the workspace.
- The navigation toggle, context drawer, dialogs, tabs, editor actions, task
  cancellation, and primary workflow actions are keyboard operable with a
  visible focus indicator.
- Long-running task lifecycle changes use a dedicated polite live region. Error
  and recovery messages use alerts only when immediate attention is required.
- Charts retain labelled summaries and data tables so color and canvas rendering
  are not the only ways to obtain values. Rise, fall, BUY, and SELL states also
  have text or shape labels.
- `prefers-reduced-motion: reduce` reduces transitions and animations to the
  shortest safe duration and disables smooth scrolling.

## Responsive behavior

The automated browser matrix covers 1600×900, 1100×700, 1024×768, 768×1024,
390×844, plus 640×450 and 640×360 effective viewports representing 200% zoom
and short landscape windows. Every core route must remain free of document-level
horizontal clipping and shell overlap.

At 1200 pixels and below, the left navigation automatically becomes a compact
icon rail. Crossing the breakpoint resets it to the appropriate mode; a manual
expand or collapse remains stable while the viewport stays on the same side of
the breakpoint. Collapsed items show full product icons, never letter
abbreviations, and preserve full accessible names and tooltips.
On short landscape windows, the rail keeps 44-pixel navigation targets and
scrolls locally so later destinations remain reachable without covering the
workspace. Expanded labels truncate safely instead of widening the shell.

Wide tables and charts scroll within their own bounded region. Secondary panels
reflow into the document or become labelled, keyboard-operable drawers before
they can cover primary controls. Content uses `min-width: 0`, wrapping, and
local overflow boundaries so supported screen proportions do not cause
components to overlap.

## Verification

Run the focused checks from the repository root:

```bash
pnpm exec playwright test web/e2e/accessibility.spec.ts web/e2e/responsive.spec.ts
pnpm --dir web test --run src/app/RouteEffects.test.tsx src/app/App.test.tsx src/app/responsiveLayout.test.ts
```

For manual review, use browser zoom at 100% and 200%, traverse each route with
Tab and Shift+Tab, expand and collapse the icon rail, open and close secondary
drawers with the keyboard, and enable the operating system's reduced-motion
preference.

Accessibility defects can be reported through the private process documented in
`SECURITY.md` when they expose sensitive information; other usability defects
belong in the public issue tracker.
