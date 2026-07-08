import { describe, expect, it } from 'vitest';

import {
  canonicalDigest,
  ProcessIdentityTracker,
  parseProcessRows,
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
});

describe('process-tree evidence', () => {
  it('selects roots and descendants while excluding the ps sampling helper', () => {
    const rows = parseProcessRows(`
      10 1 100 node playwright
      11 10 200 /usr/bin/chromium --headless
      12 10 50 /bin/ps -axo pid=,ppid=,rss=,command=
      99 1 300 unrelated
    `);

    expect(selectProcessTree([10], rows)).toEqual([
      { pid: 10, parent: 1, rssBytes: 102_400, command: 'node playwright' },
      {
        pid: 11,
        parent: 10,
        rssBytes: 204_800,
        command: '/usr/bin/chromium --headless',
      },
    ]);
  });

  it('rejects a changed command identity for a reused PID', () => {
    const tracker = new ProcessIdentityTracker(new Map([[10, 'playwright']]));
    tracker.observe([
      { pid: 10, parent: 1, rssBytes: 1, command: 'node playwright' },
    ]);
    tracker.observe([
      { pid: 10, parent: 1, rssBytes: 2, command: 'node playwright' },
    ]);

    expect(() =>
      tracker.observe([
        { pid: 10, parent: 1, rssBytes: 2, command: 'python replacement' },
      ]),
    ).toThrow(/PID command identity changed/u);
    expect(() => tracker.observe([])).toThrow(/declared root disappeared/u);
  });

  it('freezes identity anchors at the first snapshot and ignores later transient PID reuse', () => {
    const tracker = new ProcessIdentityTracker(new Map([[10, 'playwright']]));
    const root = {
      pid: 10,
      parent: 1,
      rssBytes: 1,
      command: 'node playwright',
    };
    tracker.observe([root]);
    tracker.observe([
      root,
      { pid: 20, parent: 10, rssBytes: 1, command: 'transient child A' },
    ]);

    expect(() =>
      tracker.observe([
        root,
        { pid: 20, parent: 10, rssBytes: 1, command: 'transient child B' },
      ]),
    ).not.toThrow();
  });

  it('requires every declared root to match its expected runtime role', () => {
    const tracker = new ProcessIdentityTracker(new Map([[10, 'api']]));

    expect(() =>
      tracker.observe([
        { pid: 10, parent: 1, rssBytes: 1, command: 'node playwright' },
      ]),
    ).toThrow(/expected api role/u);
  });

  it('recognizes the recorded pnpm web-dev launcher as the web service root', () => {
    const tracker = new ProcessIdentityTracker(new Map([[10, 'web']]));

    expect(() =>
      tracker.observe([
        {
          pid: 10,
          parent: 1,
          rssBytes: 1,
          command: 'node pnpm --dir web dev',
        },
      ]),
    ).not.toThrow();

    expect(() =>
      new ProcessIdentityTracker(new Map([[11, 'web']])).observe([
        {
          pid: 11,
          parent: 1,
          rssBytes: 1,
          command: 'node pnpm dev',
        },
      ]),
    ).toThrow(/expected web role/u);
  });
});
