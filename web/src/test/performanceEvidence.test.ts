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
    const tracker = new ProcessIdentityTracker();
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
  });
});
