import {
  ApiError,
  createApiClient,
  type ApiClient,
  type ApiGetOptions,
  type JsonValue,
} from '../../shared/api/client';

export type ModelProvider = 'deepseek' | 'openai_compatible' | 'ollama';
export type ModelStatus = 'unverified' | 'verified' | 'failed' | 'disabled';

export type ModelConfig = {
  readonly id: string;
  readonly displayName: string;
  readonly provider: ModelProvider;
  readonly baseUrl: string;
  readonly model: string;
  readonly temperature: number;
  readonly timeout: number;
  readonly maxOutput: number;
  readonly apiKeyConfigured: boolean;
  readonly maskedApiKey: string | null;
  readonly status: ModelStatus;
  readonly revision: number;
  readonly verifiedAt: string | null;
  readonly lastTestedAt: string | null;
  readonly errorCode: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
};

export type ModelDraft = {
  readonly displayName: string;
  readonly provider: ModelProvider;
  readonly baseUrl: string | null;
  readonly model: string;
  readonly apiKey?: string;
  readonly temperature: number;
  readonly timeout: number;
  readonly maxOutput: number;
};

export type AnalysisStage = {
  readonly stage: string;
  readonly ordinal: number;
  readonly kind: 'data' | 'role';
  readonly status: string;
  readonly attemptCount: number;
  readonly sourceRunId: string | null;
  readonly failureCode: string | null;
  readonly retryable: boolean | null;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly durationMs: number | null;
  readonly retryAllowed: boolean;
};

export type AnalysisOverview = {
  readonly runId: string;
  readonly taskId: string;
  readonly symbol: string;
  readonly parentRunId: string | null;
  readonly requestedStage: string | null;
  readonly status: string;
  readonly taskStatus: string;
  readonly progress: number;
  readonly cancelRequested: boolean;
  readonly currentStage: string | null;
  readonly snapshotId: string | null;
  readonly reportId: string | null;
  readonly failureCode: string | null;
  readonly modelConfigId: string;
  readonly modelProvider: string;
  readonly modelName: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly durationMs: number | null;
};

export type AnalysisDetail = AnalysisOverview & {
  readonly stages: readonly AnalysisStage[];
};

export type AnalysisSubmission = {
  readonly runId: string;
  readonly taskId: string;
  readonly parentRunId: string | null;
  readonly requestedStage: string | null;
  readonly status: 'queued';
  readonly snapshotId: string | null;
};

export type PreflightCategory = {
  readonly kind: string;
  readonly critical: boolean;
  readonly connectionState: 'available' | 'degraded' | 'missing';
  readonly routeSource: string;
  readonly actualSource: string | null;
  readonly orderedCandidates: readonly Readonly<Record<string, JsonValue>>[];
  readonly attemptedSources: readonly string[];
  readonly missingReason: string | null;
  readonly recoveryCode: string | null;
  readonly permissionGap: boolean;
  readonly dataCutoff: string | null;
  readonly fetchedAt: string | null;
  readonly datasetVersion: string | null;
  readonly qualityFlags: readonly string[];
};

export type PreflightResult = {
  readonly symbol: string;
  readonly previewSnapshotId: string;
  readonly reservation: false;
  readonly ratingEligible: boolean;
  readonly checkedAt: string;
  readonly categories: readonly PreflightCategory[];
};

export type EvidenceItem = {
  readonly evidenceId: string;
  readonly snapshotId: string;
  readonly sectionId: string;
  readonly sectionKind: 'market' | 'fundamentals' | 'announcements' | 'news';
  readonly canonicalSource: string;
  readonly sourceRecord: string;
  readonly sourceUrl: string | null;
  readonly publishedAt: string | null;
  readonly dataCutoff: string;
  readonly fetchedAt: string;
  readonly datasetVersion: string;
  readonly excerpt: string;
  readonly qualityFlags: readonly string[];
  readonly route?: Readonly<Record<string, JsonValue>> | null;
};

export type AnalysisClaim = {
  readonly text: string;
  readonly evidenceIds: readonly string[];
  readonly stance: 'support' | 'oppose' | 'uncertain';
};

export type AnalysisReport = {
  readonly schemaVersion: 'analysis-report-v1';
  readonly reportId: string;
  readonly snapshotId: string;
  readonly status: 'complete' | 'partial' | 'insufficient_evidence';
  readonly rating:
    | 'strong_bullish'
    | 'bullish'
    | 'neutral'
    | 'bearish'
    | 'strong_bearish'
    | null;
  readonly confidence: number;
  readonly confidenceExplanation: string;
  readonly coreJudgments: readonly AnalysisClaim[];
  readonly bullClaims: readonly AnalysisClaim[];
  readonly bearClaims: readonly AnalysisClaim[];
  readonly risks: readonly AnalysisClaim[];
  readonly evidenceItems: readonly EvidenceItem[];
  readonly roleOutputs: readonly JsonValue[];
  readonly modelMetadata: readonly JsonValue[];
  readonly qualityFlags: readonly string[];
  readonly qualityNotes: readonly string[];
  readonly missingModules: readonly string[];
  readonly missingSections: readonly string[];
  readonly recoveryActions: readonly string[];
  readonly generatedAt: string;
  readonly disclaimer: string;
  readonly retryActions: readonly {
    readonly stage: string;
    readonly action: 'retry_stage';
  }[];
  readonly failedModules: readonly string[];
  readonly blockedModules: readonly string[];
  readonly stageFailures: readonly {
    readonly stage: string;
    readonly code: string;
    readonly attemptCount: number;
  }[];
};

type RequestOptions = { readonly signal?: AbortSignal };

export type ModelConnectionResult = {
  readonly configId: string;
  readonly connected: boolean;
  readonly provider: ModelProvider;
  readonly model: string;
  readonly errorCode: string | null;
  readonly status: ModelStatus;
  readonly revision: number;
  readonly testedAt: string;
  readonly lastTestedAt: string;
};

export type AnalysisApi = {
  readonly listModels: (
    options?: RequestOptions & { cursor?: string },
  ) => Promise<{ items: readonly ModelConfig[]; nextCursor: string | null }>;
  readonly createModel: (
    draft: ModelDraft,
    options?: RequestOptions,
  ) => Promise<ModelConfig>;
  readonly createModelSuccessor: (
    id: string,
    draft: ModelDraft,
    options?: RequestOptions,
  ) => Promise<ModelConfig>;
  readonly testModel: (
    id: string,
    revision: number,
    options?: RequestOptions,
  ) => Promise<ModelConnectionResult>;
  readonly disableModel: (
    id: string,
    revision: number,
    options?: RequestOptions,
  ) => Promise<ModelConfig>;
  readonly preflight: (
    symbol: string,
    options?: RequestOptions,
  ) => Promise<PreflightResult>;
  readonly start: (
    input: { symbol: string; modelConfigId: string; maxRetries: number },
    options?: RequestOptions,
  ) => Promise<AnalysisSubmission>;
  readonly listRuns: (
    options?: RequestOptions & { cursor?: string; symbol?: string },
  ) => Promise<{
    items: readonly AnalysisOverview[];
    nextCursor: string | null;
  }>;
  readonly getRun: (
    runId: string,
    options?: RequestOptions,
  ) => Promise<AnalysisDetail>;
  readonly cancelRun: (
    runId: string,
    options?: RequestOptions,
  ) => Promise<AnalysisDetail>;
  readonly getReport: (
    runId: string,
    options?: RequestOptions,
  ) => Promise<AnalysisReport>;
  readonly getEvidence: (
    runId: string,
    evidenceId: string,
    options?: RequestOptions,
  ) => Promise<EvidenceItem>;
  readonly retryStage: (
    runId: string,
    stage: string,
    options?: RequestOptions,
  ) => Promise<AnalysisSubmission>;
};

export class AnalysisProtocolError extends Error {
  constructor(readonly path: string) {
    super(`Analysis API protocol violation at ${path}`);
    this.name = 'AnalysisProtocolError';
  }
}

const digestPattern = /^sha256:[0-9a-f]{64}$/u;
const runPattern = /^[0-9a-f-]{36}$/u;
const errorCodePattern = /^[a-z][a-z0-9_]{0,63}$/u;
const providers = ['deepseek', 'openai_compatible', 'ollama'] as const;
const modelStatuses = ['unverified', 'verified', 'failed', 'disabled'] as const;

function record(value: unknown, path: string): Record<string, JsonValue> {
  if (value === null || Array.isArray(value) || typeof value !== 'object')
    throw new AnalysisProtocolError(path);
  return value as Record<string, JsonValue>;
}

function string(value: JsonValue | undefined, path: string): string {
  if (typeof value !== 'string') throw new AnalysisProtocolError(path);
  return value;
}

function nullableString(
  value: JsonValue | undefined,
  path: string,
): string | null {
  if (value === null) return null;
  return string(value, path);
}

function nullableErrorCode(
  value: JsonValue | undefined,
  path: string,
): string | null {
  const parsed = nullableString(value, path);
  if (parsed !== null && !errorCodePattern.test(parsed))
    throw new AnalysisProtocolError(path);
  return parsed;
}

function number(value: JsonValue | undefined, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value))
    throw new AnalysisProtocolError(path);
  return value;
}

function boolean(value: JsonValue | undefined, path: string): boolean {
  if (typeof value !== 'boolean') throw new AnalysisProtocolError(path);
  return value;
}

function array(
  value: JsonValue | undefined,
  path: string,
): readonly JsonValue[] {
  if (!Array.isArray(value)) throw new AnalysisProtocolError(path);
  return value as readonly JsonValue[];
}

function enumValue<const T extends readonly string[]>(
  value: JsonValue | undefined,
  allowed: T,
  path: string,
): T[number] {
  const parsed = string(value, path);
  if (!(allowed as readonly string[]).includes(parsed))
    throw new AnalysisProtocolError(path);
  return parsed;
}

function digest(value: JsonValue | undefined, path: string): string {
  const parsed = string(value, path);
  if (!digestPattern.test(parsed)) throw new AnalysisProtocolError(path);
  return parsed;
}

function ensureNoSecretFields(value: Record<string, JsonValue>, path: string) {
  for (const key of Object.keys(value)) {
    if (['api_key', 'secret_reference_id', 'plaintext'].includes(key))
      throw new AnalysisProtocolError(`${path}.${key}`);
  }
}

function validMaskedApiKey(value: string | null): boolean {
  if (value === null) return true;
  if (value === '•••••••' || value === '[MASKED]') return true;
  return value.length === 15 && value.slice(4, 11) === '•••••••';
}

function parseModel(value: JsonValue | undefined, path: string): ModelConfig {
  const item = record(value, path);
  ensureNoSecretFields(item, path);
  const status = enumValue(item.status, modelStatuses, `${path}.status`);
  const masked = nullableString(item.masked_api_key, `${path}.masked_api_key`);
  const configured = boolean(
    item.api_key_configured,
    `${path}.api_key_configured`,
  );
  if (configured !== (masked !== null) || !validMaskedApiKey(masked))
    throw new AnalysisProtocolError(`${path}.masked_api_key`);
  return {
    id: digest(item.id, `${path}.id`),
    displayName: string(item.display_name, `${path}.display_name`),
    provider: enumValue(item.provider, providers, `${path}.provider`),
    baseUrl: string(item.base_url, `${path}.base_url`),
    model: string(item.model, `${path}.model`),
    temperature: number(item.temperature, `${path}.temperature`),
    timeout: number(item.timeout, `${path}.timeout`),
    maxOutput: number(item.max_output, `${path}.max_output`),
    apiKeyConfigured: configured,
    maskedApiKey: masked,
    status,
    revision: number(item.revision, `${path}.revision`),
    verifiedAt: nullableString(item.verified_at, `${path}.verified_at`),
    lastTestedAt: nullableString(item.last_tested_at, `${path}.last_tested_at`),
    errorCode: nullableErrorCode(item.error_code, `${path}.error_code`),
    createdAt: string(item.created_at, `${path}.created_at`),
    updatedAt: string(item.updated_at, `${path}.updated_at`),
  };
}

function strictFloatToken(value: number, path: string): string {
  if (!Number.isFinite(value)) throw new AnalysisProtocolError(path);
  if (Number.isInteger(value)) return `${String(value)}.0`;
  return JSON.stringify(value);
}

function strictIntegerToken(value: number, path: string): string {
  if (!Number.isSafeInteger(value)) throw new AnalysisProtocolError(path);
  return String(value);
}

function serializeModelDraft(draft: ModelDraft): string {
  const fields = [
    `"display_name":${JSON.stringify(draft.displayName)}`,
    `"provider":${JSON.stringify(draft.provider)}`,
    `"base_url":${JSON.stringify(draft.baseUrl)}`,
    `"model":${JSON.stringify(draft.model)}`,
  ];
  if (draft.apiKey !== undefined && draft.apiKey !== '')
    fields.push(`"api_key":${JSON.stringify(draft.apiKey)}`);
  fields.push(
    `"temperature":${strictFloatToken(draft.temperature, 'model.temperature')}`,
    `"timeout":${strictFloatToken(draft.timeout, 'model.timeout')}`,
    `"max_output":${strictIntegerToken(draft.maxOutput, 'model.max_output')}`,
  );
  return `{${fields.join(',')}}`;
}

function parseOverview(
  value: JsonValue | undefined,
  path: string,
): AnalysisOverview {
  const item = record(value, path);
  const runId = string(item.run_id, `${path}.run_id`);
  if (!runPattern.test(runId))
    throw new AnalysisProtocolError(`${path}.run_id`);
  return {
    runId,
    taskId: string(item.task_id, `${path}.task_id`),
    symbol: string(item.symbol, `${path}.symbol`),
    parentRunId: nullableString(item.parent_run_id, `${path}.parent_run_id`),
    requestedStage: nullableString(
      item.requested_stage,
      `${path}.requested_stage`,
    ),
    status: string(item.status, `${path}.status`),
    taskStatus: string(item.task_status, `${path}.task_status`),
    progress: number(item.progress, `${path}.progress`),
    cancelRequested: boolean(item.cancel_requested, `${path}.cancel_requested`),
    currentStage: nullableString(item.current_stage, `${path}.current_stage`),
    snapshotId:
      item.snapshot_id === null
        ? null
        : digest(item.snapshot_id, `${path}.snapshot_id`),
    reportId:
      item.report_id === null
        ? null
        : digest(item.report_id, `${path}.report_id`),
    failureCode: nullableString(item.failure_code, `${path}.failure_code`),
    modelConfigId: digest(item.model_config_id, `${path}.model_config_id`),
    modelProvider: string(item.model_provider, `${path}.model_provider`),
    modelName: string(item.model_name, `${path}.model_name`),
    createdAt: string(item.created_at, `${path}.created_at`),
    updatedAt: string(item.updated_at, `${path}.updated_at`),
    startedAt: nullableString(item.started_at, `${path}.started_at`),
    finishedAt: nullableString(item.finished_at, `${path}.finished_at`),
    durationMs:
      item.duration_ms === null
        ? null
        : number(item.duration_ms, `${path}.duration_ms`),
  };
}

function parseStage(value: JsonValue, path: string): AnalysisStage {
  const item = record(value, path);
  return {
    stage: string(item.stage, `${path}.stage`),
    ordinal: number(item.ordinal, `${path}.ordinal`),
    kind: enumValue(item.kind, ['data', 'role'] as const, `${path}.kind`),
    status: string(item.status, `${path}.status`),
    attemptCount: number(item.attempt_count, `${path}.attempt_count`),
    sourceRunId: nullableString(item.source_run_id, `${path}.source_run_id`),
    failureCode: nullableString(item.failure_code, `${path}.failure_code`),
    retryable:
      item.retryable === null
        ? null
        : boolean(item.retryable, `${path}.retryable`),
    startedAt: nullableString(item.started_at, `${path}.started_at`),
    finishedAt: nullableString(item.finished_at, `${path}.finished_at`),
    durationMs:
      item.duration_ms === null
        ? null
        : number(item.duration_ms, `${path}.duration_ms`),
    retryAllowed: boolean(item.retry_allowed, `${path}.retry_allowed`),
  };
}

function parseSubmission(
  value: JsonValue | undefined,
  path: string,
): AnalysisSubmission {
  const item = record(value, path);
  return {
    runId: string(item.run_id, `${path}.run_id`),
    taskId: string(item.task_id, `${path}.task_id`),
    parentRunId: nullableString(item.parent_run_id, `${path}.parent_run_id`),
    requestedStage: nullableString(
      item.requested_stage,
      `${path}.requested_stage`,
    ),
    status: enumValue(item.status, ['queued'] as const, `${path}.status`),
    snapshotId:
      item.snapshot_id === null
        ? null
        : digest(item.snapshot_id, `${path}.snapshot_id`),
  };
}

function parseEvidence(
  value: JsonValue | undefined,
  path: string,
): EvidenceItem {
  const item = record(value, path);
  return {
    evidenceId: digest(item.evidence_id, `${path}.evidence_id`),
    snapshotId: digest(item.snapshot_id, `${path}.snapshot_id`),
    sectionId: digest(item.section_id, `${path}.section_id`),
    sectionKind: enumValue(
      item.section_kind,
      ['market', 'fundamentals', 'announcements', 'news'] as const,
      `${path}.section_kind`,
    ),
    canonicalSource: string(item.canonical_source, `${path}.canonical_source`),
    sourceRecord: string(item.source_record, `${path}.source_record`),
    sourceUrl: nullableString(item.source_url, `${path}.source_url`),
    publishedAt: nullableString(item.published_at, `${path}.published_at`),
    dataCutoff: string(item.data_cutoff, `${path}.data_cutoff`),
    fetchedAt: string(item.fetched_at, `${path}.fetched_at`),
    datasetVersion: string(item.dataset_version, `${path}.dataset_version`),
    excerpt: string(item.excerpt, `${path}.excerpt`),
    qualityFlags: array(item.quality_flags, `${path}.quality_flags`).map(
      (flag, index) => string(flag, `${path}.quality_flags.${String(index)}`),
    ),
    ...(item.route === undefined
      ? {}
      : {
          route:
            item.route === null ? null : record(item.route, `${path}.route`),
        }),
  };
}

function parseClaim(value: JsonValue, path: string): AnalysisClaim {
  const item = record(value, path);
  return {
    text: string(item.text, `${path}.text`),
    evidenceIds: array(item.evidence_ids, `${path}.evidence_ids`).map(
      (id, index) => digest(id, `${path}.evidence_ids.${String(index)}`),
    ),
    stance: enumValue(
      item.stance,
      ['support', 'oppose', 'uncertain'] as const,
      `${path}.stance`,
    ),
  };
}

function stringList(
  value: JsonValue | undefined,
  path: string,
): readonly string[] {
  return array(value, path).map((item, index) =>
    string(item, `${path}.${String(index)}`),
  );
}

function parseReport(
  value: JsonValue | undefined,
  path: string,
): AnalysisReport {
  const item = record(value, path);
  const status = enumValue(
    item.status,
    ['complete', 'partial', 'insufficient_evidence'] as const,
    `${path}.status`,
  );
  const rating =
    item.rating === null
      ? null
      : enumValue(
          item.rating,
          [
            'strong_bullish',
            'bullish',
            'neutral',
            'bearish',
            'strong_bearish',
          ] as const,
          `${path}.rating`,
        );
  if ((status === 'complete') !== (rating !== null))
    throw new AnalysisProtocolError(`${path}.rating`);
  const retries = array(item.retry_actions, `${path}.retry_actions`).map(
    (value, index) => {
      const retry = record(value, `${path}.retry_actions.${String(index)}`);
      return {
        stage: string(
          retry.stage,
          `${path}.retry_actions.${String(index)}.stage`,
        ),
        action: enumValue(
          retry.action,
          ['retry_stage'] as const,
          `${path}.retry_actions.${String(index)}.action`,
        ),
      };
    },
  );
  return {
    schemaVersion: enumValue(
      item.schema_version,
      ['analysis-report-v1'] as const,
      `${path}.schema_version`,
    ),
    reportId: digest(item.report_id, `${path}.report_id`),
    snapshotId: digest(item.snapshot_id, `${path}.snapshot_id`),
    status,
    rating,
    confidence: number(item.confidence, `${path}.confidence`),
    confidenceExplanation: string(
      item.confidence_explanation,
      `${path}.confidence_explanation`,
    ),
    coreJudgments: array(item.core_judgments, `${path}.core_judgments`).map(
      (claim, index) =>
        parseClaim(claim, `${path}.core_judgments.${String(index)}`),
    ),
    bullClaims: array(item.bull_claims, `${path}.bull_claims`).map(
      (claim, index) =>
        parseClaim(claim, `${path}.bull_claims.${String(index)}`),
    ),
    bearClaims: array(item.bear_claims, `${path}.bear_claims`).map(
      (claim, index) =>
        parseClaim(claim, `${path}.bear_claims.${String(index)}`),
    ),
    risks: array(item.risks, `${path}.risks`).map((claim, index) =>
      parseClaim(claim, `${path}.risks.${String(index)}`),
    ),
    evidenceItems: array(item.evidence_items, `${path}.evidence_items`).map(
      (evidence, index) =>
        parseEvidence(evidence, `${path}.evidence_items.${String(index)}`),
    ),
    roleOutputs: array(item.role_outputs, `${path}.role_outputs`),
    modelMetadata: array(item.model_metadata, `${path}.model_metadata`),
    qualityFlags: stringList(item.quality_flags, `${path}.quality_flags`),
    qualityNotes: stringList(item.quality_notes, `${path}.quality_notes`),
    missingModules: stringList(item.missing_modules, `${path}.missing_modules`),
    missingSections: stringList(
      item.missing_sections,
      `${path}.missing_sections`,
    ),
    recoveryActions: stringList(
      item.recovery_actions,
      `${path}.recovery_actions`,
    ),
    generatedAt: string(item.generated_at, `${path}.generated_at`),
    disclaimer: string(item.disclaimer, `${path}.disclaimer`),
    retryActions: retries,
    failedModules: stringList(item.failed_modules, `${path}.failed_modules`),
    blockedModules: stringList(item.blocked_modules, `${path}.blocked_modules`),
    stageFailures: array(item.stage_failures, `${path}.stage_failures`).map(
      (value, index) => {
        const failure = record(
          value,
          `${path}.stage_failures.${String(index)}`,
        );
        return {
          stage: string(
            failure.stage,
            `${path}.stage_failures.${String(index)}.stage`,
          ),
          code: string(
            failure.code,
            `${path}.stage_failures.${String(index)}.code`,
          ),
          attemptCount: number(
            failure.attempt_count,
            `${path}.stage_failures.${String(index)}.attempt_count`,
          ),
        };
      },
    ),
  };
}

const safeMessages: Readonly<Record<string, string>> = {
  invalid_request: '请求参数无效，请检查后重试',
  invalid_cursor: '历史记录游标已失效，请刷新列表',
  model_not_verified: '所选模型尚未通过连接测试',
  report_not_ready: '报告仍在生成中',
  report_unavailable: '本次分析没有可用报告',
  state_conflict: '任务状态已变化，请刷新后重试',
  not_found: '请求的分析记录不存在',
  evidence_not_found: '请求的证据不存在',
  secure_storage_unavailable: '安全凭证存储暂不可用',
  storage_unavailable: '分析存储暂不可用',
  service_unavailable: '分析服务暂不可用',
};

async function safe<T>(request: () => Promise<T>): Promise<T> {
  try {
    return await request();
  } catch (error) {
    if (error instanceof AnalysisProtocolError) throw error;
    const details =
      error instanceof ApiError
        ? error.details
        : typeof error === 'object' && error !== null && 'details' in error
          ? error.details
          : undefined;
    const detailRecord =
      typeof details === 'object' && details !== null && !Array.isArray(details)
        ? (details as Readonly<Record<string, unknown>>)
        : undefined;
    const code =
      typeof detailRecord?.['code'] === 'string'
        ? detailRecord['code']
        : undefined;
    throw new Error(
      code === undefined
        ? '分析服务请求失败，请稍后重试'
        : (safeMessages[code] ?? '分析服务请求失败，请稍后重试'),
    );
  }
}

function query(
  path: string,
  values: Readonly<Record<string, string | undefined>>,
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined) params.set(key, value);
  const suffix = params.toString();
  return suffix === '' ? path : `${path}?${suffix}`;
}

export function createAnalysisApi(
  client: ApiClient = createApiClient(),
): AnalysisApi {
  const get = (path: string, options?: ApiGetOptions) =>
    safe(() => client.get(path, options));
  return {
    async listModels(options = {}) {
      const value = await get(
        query('/settings/models', { cursor: options.cursor }),
        { signal: options.signal },
      );
      const page = record(value, 'models');
      return {
        items: array(page.items, 'models.items').map((item, index) =>
          parseModel(item, `models.items.${String(index)}`),
        ),
        nextCursor: nullableString(page.next_cursor, 'models.next_cursor'),
      };
    },
    async createModel(draft, options = {}) {
      return parseModel(
        await safe(() =>
          client.post('/settings/models', {
            serializedBody: serializeModelDraft(draft),
            signal: options.signal,
          }),
        ),
        'model',
      );
    },
    async createModelSuccessor(id, draft, options = {}) {
      return parseModel(
        await safe(() =>
          client.put(`/settings/models/${encodeURIComponent(id)}`, {
            serializedBody: serializeModelDraft(draft),
            signal: options.signal,
          }),
        ),
        'model',
      );
    },
    async testModel(id, revision, options = {}) {
      const value = record(
        await safe(() =>
          client.post(`/settings/models/${encodeURIComponent(id)}/test`, {
            body: { expected_revision: revision },
            signal: options.signal,
          }),
        ),
        'connection',
      );
      return {
        configId: digest(value.config_id, 'connection.config_id'),
        connected: boolean(value.connected, 'connection.connected'),
        provider: enumValue(value.provider, providers, 'connection.provider'),
        model: string(value.model, 'connection.model'),
        status: enumValue(value.status, modelStatuses, 'connection.status'),
        errorCode: nullableErrorCode(value.error_code, 'connection.error_code'),
        revision: number(value.revision, 'connection.revision'),
        testedAt: string(value.tested_at, 'connection.tested_at'),
        lastTestedAt: string(value.last_tested_at, 'connection.last_tested_at'),
      };
    },
    async disableModel(id, revision, options = {}) {
      return parseModel(
        await safe(() =>
          client.post(`/settings/models/${encodeURIComponent(id)}/disable`, {
            body: { expected_revision: revision },
            signal: options.signal,
          }),
        ),
        'model',
      );
    },
    async preflight(symbol, options = {}) {
      const item = record(
        await safe(() =>
          client.post('/analysis/preflight', {
            body: { symbol },
            signal: options.signal,
          }),
        ),
        'preflight',
      );
      const categories = array(item.categories, 'preflight.categories').map(
        (value, index): PreflightCategory => {
          const category = record(
            value,
            `preflight.categories.${String(index)}`,
          );
          return {
            kind: string(category.kind, 'category.kind'),
            critical: boolean(category.critical, 'category.critical'),
            connectionState: enumValue(
              category.connection_state,
              ['available', 'degraded', 'missing'] as const,
              'category.connection_state',
            ),
            routeSource: string(category.route_source, 'category.route_source'),
            actualSource: nullableString(
              category.actual_source,
              'category.actual_source',
            ),
            orderedCandidates: array(
              category.ordered_candidates,
              'category.ordered_candidates',
            ).map((candidate) => record(candidate, 'category.candidate')),
            attemptedSources: stringList(
              category.attempted_sources,
              'category.attempted_sources',
            ),
            missingReason: nullableString(
              category.missing_reason,
              'category.missing_reason',
            ),
            recoveryCode: nullableString(
              category.recovery_code,
              'category.recovery_code',
            ),
            permissionGap: boolean(
              category.permission_gap,
              'category.permission_gap',
            ),
            dataCutoff: nullableString(
              category.data_cutoff,
              'category.data_cutoff',
            ),
            fetchedAt: nullableString(
              category.fetched_at,
              'category.fetched_at',
            ),
            datasetVersion: nullableString(
              category.dataset_version,
              'category.dataset_version',
            ),
            qualityFlags: stringList(
              category.quality_flags,
              'category.quality_flags',
            ),
          };
        },
      );
      if (categories.length !== 4)
        throw new AnalysisProtocolError('preflight.categories');
      if (item.reservation !== false)
        throw new AnalysisProtocolError('preflight.reservation');
      return {
        symbol: string(item.symbol, 'preflight.symbol'),
        previewSnapshotId: string(
          item.preview_snapshot_id,
          'preflight.preview_snapshot_id',
        ),
        reservation: false,
        ratingEligible: boolean(
          item.rating_eligible,
          'preflight.rating_eligible',
        ),
        checkedAt: string(item.checked_at, 'preflight.checked_at'),
        categories,
      };
    },
    async start(input, options = {}) {
      return parseSubmission(
        await safe(() =>
          client.post('/analysis', {
            body: {
              symbol: input.symbol,
              model_config_id: input.modelConfigId,
              retry: { max_retries: input.maxRetries },
            },
            signal: options.signal,
          }),
        ),
        'submission',
      );
    },
    async listRuns(options = {}) {
      const page = record(
        await get(
          query('/analysis', {
            cursor: options.cursor,
            symbol: options.symbol,
          }),
          { signal: options.signal },
        ),
        'history',
      );
      return {
        items: array(page.items, 'history.items').map((item, index) =>
          parseOverview(item, `history.items.${String(index)}`),
        ),
        nextCursor: nullableString(page.next_cursor, 'history.next_cursor'),
      };
    },
    async getRun(runId, options = {}) {
      const value = await get(`/analysis/${encodeURIComponent(runId)}`, {
        signal: options.signal,
      });
      const item = record(value, 'run');
      return {
        ...parseOverview(value, 'run'),
        stages: array(item.stages, 'run.stages')
          .map((stage, index) =>
            parseStage(stage, `run.stages.${String(index)}`),
          )
          .sort((left, right) => left.ordinal - right.ordinal),
      };
    },
    async cancelRun(runId, options = {}) {
      const value = await safe(() =>
        client.post(`/analysis/${encodeURIComponent(runId)}/cancel`, {
          signal: options.signal,
        }),
      );
      const item = record(value, 'run');
      return {
        ...parseOverview(value, 'run'),
        stages: array(item.stages, 'run.stages')
          .map((stage, index) =>
            parseStage(stage, `run.stages.${String(index)}`),
          )
          .sort((left, right) => left.ordinal - right.ordinal),
      };
    },
    async getReport(runId, options = {}) {
      return parseReport(
        await get(`/analysis/${encodeURIComponent(runId)}/report`, {
          signal: options.signal,
        }),
        'report',
      );
    },
    async getEvidence(runId, evidenceId, options = {}) {
      return parseEvidence(
        await get(
          `/analysis/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(evidenceId)}`,
          { signal: options.signal },
        ),
        'evidence',
      );
    },
    async retryStage(runId, stage, options = {}) {
      return parseSubmission(
        await safe(() =>
          client.post(
            `/analysis/${encodeURIComponent(runId)}/stages/${encodeURIComponent(stage)}/retry`,
            { signal: options.signal },
          ),
        ),
        'submission',
      );
    },
  };
}

export const analysisApi = createAnalysisApi();
