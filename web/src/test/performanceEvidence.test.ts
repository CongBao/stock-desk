import { describe, expect, it } from 'vitest';

import {
  canonicalDigest,
  completedGenerationAfter,
  progressWindowsDemonstrateChange,
  ProcessIdentityTracker,
  parseProcessRows,
  portableCommandTokens,
  ProgressResponseLedger,
  providerEvidence,
  selectProcessTree,
} from '../../e2e/performanceEvidence';

describe('canonical performance evidence', () => {
  it('recursively sorts object keys while preserving array order', () => {
    const left = {
      z: { second: 2, first: [{ beta: true, alpha: false }] },
      a: ['first', 'second'],
    };
    const right = {
      a: ['first', 'second'],
      z: { first: [{ alpha: false, beta: true }], second: 2 },
    };
    const reversedArray = { ...right, a: ['second', 'first'] };

    expect(canonicalDigest(left)).toBe(canonicalDigest(right));
    expect(canonicalDigest(reversedArray)).not.toBe(canonicalDigest(right));
  });

  it('derives exact zero calls and wait from an empty immutable attempt ledger', () => {
    expect(
      providerEvidence({ selected_source: 'stock_desk_demo', attempts: [] }),
    ).toEqual({
      provider_spans: [],
      provider_span_count: 0,
      external_wait_seconds: 0,
    });
  });

  it('refuses to invent unavailable durations for a nonempty attempt ledger', () => {
    expect(() =>
      providerEvidence({
        selected_source: 'stock_desk_demo',
        attempts: [{ source: 'tushare', decision: 'unavailable' }],
      }),
    ).toThrow(/duration is unavailable/u);
  });

  it('accepts repeated progress windows once the rendered UI has truly changed', () => {
    const initial = 'running|executing|1|5000|0';
    const firstChange = 'running|executing|2|5000|0';
    const secondChange = 'running|executing|3|5000|0';

    expect(
      progressWindowsDemonstrateChange(initial, [
        firstChange,
        firstChange,
        secondChange,
        secondChange,
      ]),
    ).toBe(true);
    expect(
      progressWindowsDemonstrateChange(initial, [
        firstChange,
        firstChange,
        firstChange,
      ]),
    ).toBe(false);
    expect(progressWindowsDemonstrateChange(initial, [initial, initial])).toBe(
      false,
    );
  });

  it('requires a strictly newer completed generation without sampling transient pending DOM', () => {
    expect(completedGenerationAfter(7, 'true', '7')).toBeNull();
    expect(completedGenerationAfter(7, 'false', '8')).toBeNull();
    expect(completedGenerationAfter(7, 'true', 'not-an-integer')).toBeNull();
    expect(completedGenerationAfter(7, 'true', '8')).toBe(8);
  });

  it('matches rendered progress to an exact authoritative page response without a second request', () => {
    const ledger = new ProgressResponseLedger();
    const state = {
      status: 'running',
      stage: 'executing',
      processed: 8,
      total: 5000,
      failed: 3,
    };
    expect(ledger.record('run-1', state)).toBe(true);
    expect(ledger.record('run-1', { ...state, processed: Number.NaN })).toBe(
      false,
    );

    expect(ledger.match('run-1', { ...state })).toEqual(state);
    expect(ledger.match('run-1', { ...state, processed: 9 })).toBeNull();
    expect(ledger.match('run-2', state)).toBeNull();
  });

  it('persists portable service command tokens without weakening raw runtime matching', () => {
    expect(
      portableCommandTokens([
        '/Users/operator/worktree/.venv/bin/python3',
        '-m',
        'uvicorn',
      ]),
    ).toEqual(['python3', '-m', 'uvicorn']);
    expect(portableCommandTokens(['pnpm', '--dir', 'web', 'dev'])).toEqual([
      'pnpm',
      '--dir',
      'web',
      'dev',
    ]);
  });
});

describe('process-tree evidence', () => {
  const rootStart = 'Wed Jul 8 12:00:00 2026';
  const childStart = 'Wed Jul 8 12:00:01 2026';

  it('selects roots and descendants while excluding the ps sampling helper', () => {
    const rows = parseProcessRows(`
      10 1 100 Wed Jul  8 12:00:00 2026 node playwright
      11 10 200 Wed Jul  8 12:00:01 2026 /usr/bin/chromium --headless
      12 10 50 Wed Jul  8 12:00:02 2026 /bin/ps -axo pid=,ppid=,rss=,lstart=,command=
      99 1 300 Wed Jul  8 12:00:03 2026 unrelated
    `);

    expect(selectProcessTree([10], rows)).toEqual([
      {
        pid: 10,
        parent: 1,
        rssBytes: 102_400,
        startedAt: rootStart,
        command: 'node playwright',
      },
      {
        pid: 11,
        parent: 10,
        rssBytes: 204_800,
        startedAt: childStart,
        command: '/usr/bin/chromium --headless',
      },
    ]);
  });

  it('rejects a changed command within the same process incarnation', () => {
    const tracker = new ProcessIdentityTracker(
      new Map([[10, { role: 'playwright' }]]),
    );
    tracker.observe([
      {
        pid: 10,
        parent: 1,
        rssBytes: 1,
        startedAt: rootStart,
        command: 'node playwright',
      },
    ]);
    tracker.observe([
      {
        pid: 10,
        parent: 1,
        rssBytes: 2,
        startedAt: rootStart,
        command: 'node playwright',
      },
    ]);

    expect(() =>
      tracker.observe([
        {
          pid: 10,
          parent: 1,
          rssBytes: 2,
          startedAt: rootStart,
          command: 'python replacement',
        },
      ]),
    ).toThrow(
      /PID command identity changed for 10.*parent 1.*Wed Jul 8 12:00:00 2026.*node playwright.*python replacement/u,
    );
    expect(() => tracker.observe([])).toThrow(/declared root disappeared/u);
  });

  it('tracks legal late-child command evolution and PID reuse by start identity', () => {
    const tracker = new ProcessIdentityTracker(
      new Map([[10, { role: 'playwright' }]]),
    );
    const root = {
      pid: 10,
      parent: 1,
      rssBytes: 1,
      startedAt: rootStart,
      command: 'node playwright',
    };
    tracker.observe([root]);
    tracker.observe([
      root,
      {
        pid: 20,
        parent: 10,
        rssBytes: 1,
        startedAt: childStart,
        command: 'transient child A',
      },
    ]);

    expect(() =>
      tracker.observe([
        root,
        {
          pid: 20,
          parent: 10,
          rssBytes: 1,
          startedAt: childStart,
          command: 'transient child B',
        },
      ]),
    ).not.toThrow();
    expect(() =>
      tracker.observe([
        root,
        {
          pid: 20,
          parent: 10,
          rssBytes: 1,
          startedAt: 'Wed Jul 8 12:01:00 2026',
          command: 'transient child B',
        },
      ]),
    ).not.toThrow();
  });

  it('requires every declared root to match its expected runtime role', () => {
    const tracker = new ProcessIdentityTracker(
      new Map([[10, { role: 'api' }]]),
    );

    expect(() =>
      tracker.observe([
        {
          pid: 10,
          parent: 1,
          rssBytes: 1,
          startedAt: rootStart,
          command: 'node playwright',
        },
      ]),
    ).toThrow(/expected api role/u);
  });

  it('recognizes the recorded pnpm web-dev launcher as the web service root', () => {
    const tracker = new ProcessIdentityTracker(
      new Map([
        [10, { role: 'web', commandTokens: ['pnpm', '--dir', 'web', 'dev'] }],
      ]),
    );

    expect(() =>
      tracker.observe([
        {
          pid: 10,
          parent: 1,
          rssBytes: 1,
          startedAt: rootStart,
          command: 'node /opt/pnpm.cjs --dir web dev',
        },
      ]),
    ).not.toThrow();

    expect(() =>
      new ProcessIdentityTracker(
        new Map([
          [11, { role: 'web', commandTokens: ['pnpm', '--dir', 'web', 'dev'] }],
        ]),
      ).observe([
        {
          pid: 11,
          parent: 1,
          rssBytes: 1,
          startedAt: rootStart,
          command: 'node /opt/vite --host 127.0.0.1',
        },
      ]),
    ).toThrow(/declared command/u);
  });
});
