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
  readonly startedAt: string;
  readonly command: string;
};

export type RuntimeRole =
  'api' | 'playwright' | 'supervisor' | 'web' | 'worker';

export type RootExpectation = {
  readonly role: RuntimeRole;
  readonly commandTokens?: readonly string[];
};

export type ProgressEvidenceState = {
  readonly status: string;
  readonly stage: string;
  readonly processed: number;
  readonly total: number;
  readonly failed: number;
};

const PS_HELPER =
  /(?:^|\s)(?:\/\S*\/)?ps\s+-axo\s+pid=,ppid=,rss=,lstart=,command=(?:\s|$)/u;

export function parseProcessRows(output: string): ProcessRow[] {
  return output
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      const match =
        /^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)$/u.exec(
          line,
        );
      if (match === null) throw new Error('process-list output is malformed');
      return {
        pid: Number(match[1]),
        parent: Number(match[2]),
        rssBytes: Number(match[3]) * 1024,
        startedAt: (match[4] ?? '').replace(/\s+/gu, ' '),
        command: match[5] ?? '',
      };
    });
}

export function parseProcProcessRow(
  pid: number,
  stat: string,
  status: string,
  cmdline: string,
): ProcessRow | null {
  const commandEnd = stat.lastIndexOf(') ');
  const commandStart = stat.indexOf('(');
  if (commandStart < 1 || commandEnd <= commandStart) {
    return null;
  }
  const reportedPid = Number(stat.slice(0, commandStart).trim());
  const fields = stat
    .slice(commandEnd + 2)
    .trim()
    .split(/\s+/u);
  const parent = Number(fields[1]);
  const startTicks = fields[19];
  const rssMatch = /^VmRSS:\s+(\d+)\s+kB$/mu.exec(status);
  if (
    reportedPid !== pid ||
    !Number.isSafeInteger(parent) ||
    parent < 0 ||
    startTicks === undefined ||
    !/^\d+$/u.test(startTicks) ||
    rssMatch === null
  ) {
    return null;
  }
  const rssKilobytes = Number(rssMatch[1]);
  if (!Number.isSafeInteger(rssKilobytes) || rssKilobytes < 0) {
    return null;
  }
  const command = cmdline
    .split('\0')
    .filter((token) => token.length > 0)
    .join(' ');
  const fallback = stat.slice(commandStart + 1, commandEnd);
  return {
    pid,
    parent,
    rssBytes: rssKilobytes * 1024,
    startedAt: `linux-ticks:${startTicks}`,
    command: command || `[${fallback}]`,
  };
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
  private anchors:
    ReadonlyMap<number, { startedAt: string; command: string }> | undefined;
  private readonly incarnations = new Map<string, Set<string>>();

  constructor(
    private readonly expectedRoots: ReadonlyMap<number, RootExpectation>,
  ) {
    if (expectedRoots.size === 0) {
      throw new Error('declared root expectations cannot be empty');
    }
  }

  observe(rows: readonly ProcessRow[]): void {
    const byPid = new Map(rows.map((row) => [row.pid, row]));
    for (const row of rows) {
      const identity = `${row.pid}\u0000${row.startedAt}`;
      const commands = this.incarnations.get(identity) ?? new Set<string>();
      commands.add(row.command);
      this.incarnations.set(identity, commands);
    }
    if (this.anchors === undefined) {
      const anchors = new Map<number, { startedAt: string; command: string }>();
      for (const [pid, expectation] of this.expectedRoots) {
        const row = byPid.get(pid);
        if (row === undefined) {
          throw new Error(
            `declared root ${pid} is missing from first snapshot`,
          );
        }
        if (!commandMatchesRole(row.command, expectation.role)) {
          throw new Error(
            `declared root ${pid} does not match expected ${expectation.role} role`,
          );
        }
        if (
          expectation.commandTokens !== undefined &&
          !commandContainsDeclaredTokens(row.command, expectation.commandTokens)
        ) {
          throw new Error(
            `declared root ${pid} does not match its declared command`,
          );
        }
        anchors.set(pid, {
          startedAt: row.startedAt,
          command: row.command,
        });
      }
      this.anchors = anchors;
      return;
    }
    for (const [pid, anchor] of this.anchors) {
      const row = byPid.get(pid);
      if (row === undefined) {
        throw new Error(`declared root disappeared during sample: ${pid}`);
      }
      if (
        row.startedAt !== anchor.startedAt ||
        row.command !== anchor.command
      ) {
        throw new Error(
          `PID command identity changed for ${pid} with parent ${row.parent} at ${row.startedAt}: ${JSON.stringify(anchor.command)} -> ${JSON.stringify(row.command)}`,
        );
      }
    }
  }
}

function executableToken(token: string): string {
  const basename = token.split('/').at(-1) ?? token;
  return basename.replace(/\.(?:cjs|mjs|js)$/u, '');
}

export function commandContainsDeclaredTokens(
  command: string,
  declared: readonly string[],
): boolean {
  if (declared.length === 0) return false;
  const actual = command.trim().split(/\s+/u);
  for (let start = 0; start <= actual.length - declared.length; start += 1) {
    if (
      declared.every((token, index) => {
        const observed = actual[start + index] ?? '';
        if (index !== 0 || token.includes('/')) return observed === token;
        return executableToken(observed) === executableToken(token);
      })
    ) {
      return true;
    }
  }
  return false;
}

export function portableCommandTokens(
  command: readonly string[],
): readonly string[] {
  const [executable, ...arguments_] = command;
  if (executable === undefined) return [];
  const basename = executable.replaceAll('\\', '/').split('/').at(-1);
  return [basename ?? executable, ...arguments_];
}

export function commandMatchesRole(
  command: string,
  role: RuntimeRole,
): boolean {
  const lower = command.toLowerCase();
  if (role === 'api') return lower.includes('uvicorn');
  if (role === 'worker') return lower.includes('scripts.e2e_dev --worker');
  if (role === 'web') {
    if (lower.includes('vite')) return true;
    const tokens = lower.split(/\s+/u);
    const directory = tokens.indexOf('--dir');
    return (
      directory > 0 &&
      tokens.slice(0, directory).some((token) => token.includes('pnpm')) &&
      tokens[directory + 1] === 'web' &&
      tokens[directory + 2] === 'dev'
    );
  }
  if (role === 'supervisor') {
    return (
      lower.includes('scripts/e2e_dev.py') || lower.includes('scripts.e2e_dev')
    );
  }
  return lower.includes('playwright');
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

export function progressWindowsDemonstrateChange(
  initialKey: string,
  windowKeys: readonly string[],
): boolean {
  return (
    windowKeys.some((key) => key !== initialKey) &&
    new Set(windowKeys).size >= 2
  );
}

export function completedGenerationAfter(
  previousGeneration: number,
  ready: string | null,
  rawGeneration: string | null,
): number | null {
  if (ready !== 'true') return null;
  const generation = Number(rawGeneration);
  return Number.isSafeInteger(generation) && generation > previousGeneration
    ? generation
    : null;
}

function progressEvidenceKey(state: ProgressEvidenceState): string {
  return [
    state.status,
    state.stage,
    state.processed,
    state.total,
    state.failed,
  ].join('|');
}

export function progressEvidenceState(
  value: unknown,
): ProgressEvidenceState | null {
  if (typeof value !== 'object' || value === null) return null;
  const candidate = value as Record<string, unknown>;
  const { status, stage, processed, total, failed } = candidate;
  if (
    typeof status !== 'string' ||
    typeof stage !== 'string' ||
    !Number.isInteger(processed) ||
    !Number.isInteger(total) ||
    !Number.isInteger(failed) ||
    (processed as number) < 0 ||
    (total as number) < 0 ||
    (failed as number) < 0 ||
    (failed as number) > (processed as number) ||
    (processed as number) > (total as number)
  ) {
    return null;
  }
  return {
    status,
    stage,
    processed: processed as number,
    total: total as number,
    failed: failed as number,
  };
}

export class ProgressResponseLedger {
  private readonly byRun = new Map<
    string,
    Map<string, ProgressEvidenceState>
  >();

  record(runId: string, value: unknown): boolean {
    const state = progressEvidenceState(value);
    if (!runId || state === null) return false;
    const states =
      this.byRun.get(runId) ?? new Map<string, ProgressEvidenceState>();
    states.set(progressEvidenceKey(state), state);
    this.byRun.set(runId, states);
    return true;
  }

  match(
    runId: string,
    rendered: ProgressEvidenceState,
  ): ProgressEvidenceState | null {
    return this.byRun.get(runId)?.get(progressEvidenceKey(rendered)) ?? null;
  }
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
