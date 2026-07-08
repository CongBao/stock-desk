/// <reference types="node" />

import { createHash } from 'node:crypto';

export type RoutingManifest = {
  readonly selected_source: string;
  readonly attempts: readonly {
    readonly source: string;
    readonly decision: string;
  }[];
};

export type ProcessRow = {
  readonly pid: number;
  readonly parent: number;
  readonly rssBytes: number;
  readonly command: string;
};

const PS_HELPER =
  /(?:^|\s)(?:\/\S*\/)?ps\s+-axo\s+pid=,ppid=,rss=,command=(?:\s|$)/u;

export function parseProcessRows(output: string): ProcessRow[] {
  return output
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      const match = /^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)$/u.exec(line);
      if (match === null) throw new Error('process-list output is malformed');
      return {
        pid: Number(match[1]),
        parent: Number(match[2]),
        rssBytes: Number(match[3]) * 1024,
        command: match[4] ?? '',
      };
    });
}

export function selectProcessTree(
  roots: readonly number[],
  rows: readonly ProcessRow[],
): ProcessRow[] {
  const descendants = new Set(roots);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (descendants.has(row.parent) && !descendants.has(row.pid)) {
        descendants.add(row.pid);
        changed = true;
      }
    }
  }
  return rows
    .filter((row) => descendants.has(row.pid) && !PS_HELPER.test(row.command))
    .sort((left, right) => left.pid - right.pid);
}

export class ProcessIdentityTracker {
  private readonly commands = new Map<number, string>();

  observe(rows: readonly ProcessRow[]): void {
    for (const row of rows) {
      const previous = this.commands.get(row.pid);
      if (previous !== undefined && previous !== row.command) {
        throw new Error(`PID command identity changed for ${row.pid}`);
      }
      this.commands.set(row.pid, row.command);
    }
  }
}

function canonicalize(value: unknown): unknown {
  if (
    value === null ||
    typeof value === 'string' ||
    typeof value === 'boolean'
  ) {
    return value;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new TypeError(
        'canonical performance evidence requires finite numbers',
      );
    }
    return value;
  }
  if (Array.isArray(value)) return value.map((item) => canonicalize(item));
  if (typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalize(item)]),
    );
  }
  throw new TypeError(`unsupported canonical evidence value: ${typeof value}`);
}

export function canonicalDigest(value: unknown): string {
  return `sha256:${createHash('sha256')
    .update(JSON.stringify(canonicalize(value)))
    .digest('hex')}`;
}

export function providerEvidence(manifest: RoutingManifest) {
  if (manifest.selected_source !== 'stock_desk_demo') {
    throw new Error('performance route did not select stock_desk_demo');
  }
  const providerSpans = manifest.attempts.map((attempt) => ({
    source: attempt.source,
    decision: attempt.decision,
  }));
  if (providerSpans.length !== 0) {
    throw new Error(
      'provider attempt duration is unavailable; cached performance evidence requires an empty attempt ledger',
    );
  }
  return {
    provider_spans: providerSpans,
    provider_span_count: 0,
    external_wait_seconds: 0,
  };
}
