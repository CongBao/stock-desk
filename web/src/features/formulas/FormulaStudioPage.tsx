import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, type JsonValue } from '../../shared/api/client';
import {
  isMarketNotFound,
  MarketProtocolError,
  marketApi,
  type MarketApi,
  type MarketBar,
} from '../market/marketApi';
import type { MarketAdjustment, MarketPeriod } from '../market/marketStore';
import { FormulaEditor, type FormulaEditorHandle } from './FormulaEditor';
import { FormulaPreview } from './FormulaPreview';
import { FunctionLibrary } from './FunctionLibrary';
import {
  formulaApi,
  FormulaProtocolError,
  type FormulaApi,
  type FormulaDetail,
  type FormulaDiagnostic,
  type FormulaField,
  type FormulaFunction,
  type FormulaPlacement,
  type FormulaPreview as FormulaPreviewResult,
  type FormulaTemplate,
  type FormulaType,
  type FormulaValidation,
  type FormulaVersion,
  type ParameterSchema,
} from './formulaApi';
import { ParameterPanel } from './ParameterPanel';
import type { TdxDocumentationEntry } from './tdxLanguage';

const EMPTY_SCHEMA: ParameterSchema = {};
type OperationName = 'validation' | 'load' | 'preview' | 'save' | 'copy';

function schemaFingerprint(schema: ParameterSchema): string {
  return JSON.stringify(
    Object.fromEntries(
      Object.entries(schema).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    ),
  );
}

function draftFingerprint(source: string, schema: ParameterSchema): string {
  return `${source}\u0000${schemaFingerprint(schema)}`;
}

function parameterValues(
  schema: ParameterSchema,
): Readonly<Record<string, number>> {
  return Object.fromEntries(
    Object.entries(schema).map(([name, declaration]) => [
      name,
      declaration.default,
    ]),
  );
}

function errorMessage(error: unknown): string {
  if (
    error instanceof FormulaProtocolError ||
    error instanceof MarketProtocolError
  ) {
    return '本地服务返回了不兼容的数据，已停止渲染。';
  }
  if (isMarketNotFound(error)) {
    return '本地缓存中没有该证券/周期/复权的数据，请先在行情页更新数据。';
  }
  if (error instanceof ApiError) {
    const details = error.details;
    const code =
      details !== null &&
      typeof details === 'object' &&
      !Array.isArray(details) &&
      typeof (details as Readonly<Record<string, JsonValue>>)['code'] ===
        'string'
        ? (details as Readonly<Record<string, JsonValue>>)['code']
        : null;
    if (code === 'preview_timeout' || error.status === 504) {
      return '公式预览超过 3 秒执行上限，已安全终止；请缩短数据范围或简化公式。';
    }
    if (code === 'resource_limit_exceeded') {
      return '公式或行情数据超过预览资源上限，请减少输出、参数或数据范围。';
    }
    if (code === 'preview_worker_failed') {
      return '公式预览计算进程异常退出，未产生可用结果，请重试。';
    }
    if (code === 'revision_conflict') {
      return '该公式草稿已在其他位置更新，请重新打开最新版本后再保存。';
    }
    if (code === 'formula_invalid' || code === 'invalid_request') {
      return '公式或预览参数未通过服务端校验，请按诊断修改后重试。';
    }
    if (error.kind === 'network') {
      return '无法连接本地 API，请确认 stock-desk 服务正在运行。';
    }
    if (error.kind === 'http') {
      return `本地 API 拒绝了请求（HTTP ${String(error.status ?? '未知')}），请按页面诊断处理。`;
    }
  }
  if (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    error.name === 'AbortError'
  ) {
    return '';
  }
  return '操作失败，请确认本地 API 已启动并重试。';
}

function snippetFor(
  item: FormulaFunction | FormulaField,
  fallback: string,
): string {
  if ('signature' in item) return item.signature;
  return fallback;
}

export type FormulaStudioPageProps = {
  readonly api?: FormulaApi;
  readonly initialFormula?: FormulaDetail;
  readonly initialSource?: string;
  readonly marketApiClient?: Pick<MarketApi, 'getBars'>;
  readonly validationDebounceMs?: number;
};

export function FormulaStudioPage({
  api = formulaApi,
  initialFormula,
  initialSource,
  marketApiClient = marketApi,
  validationDebounceMs = 350,
}: FormulaStudioPageProps) {
  const editorRef = useRef<FormulaEditorHandle>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const adoptedTemplate = useRef(false);
  const mountedRef = useRef(true);
  const draftEpochRef = useRef(0);
  const operationControllers = useRef(
    new Map<OperationName, AbortController>(),
  );
  const operationGenerations = useRef(new Map<OperationName, number>());
  const initialDraft = initialFormula?.draft;
  const [formulaId, setFormulaId] = useState<string | null>(
    initialFormula?.id ?? null,
  );
  const [name, setName] = useState(initialFormula?.name ?? '我的公式');
  const [formulaType, setFormulaType] = useState<FormulaType>(
    initialFormula?.formulaType ?? 'indicator',
  );
  const [placement, setPlacement] = useState<FormulaPlacement>(
    initialFormula?.placement ?? 'subchart',
  );
  const [source, setSource] = useState(
    initialSource ?? initialDraft?.source ?? '',
  );
  const [parameterSchema, setParameterSchema] = useState<ParameterSchema>(
    initialDraft?.parameterSchema ?? EMPTY_SCHEMA,
  );
  const [revision, setRevision] = useState(initialDraft?.revision ?? 1);
  const [savedVersionId, setSavedVersionId] = useState<string | null>(
    initialDraft?.executableVersionId ?? null,
  );
  const [savedFingerprint, setSavedFingerprint] = useState<string | null>(
    initialDraft?.executableVersionId === null ||
      initialDraft?.executableVersionId === undefined
      ? null
      : draftFingerprint(initialDraft.source, initialDraft.parameterSchema),
  );
  const [persistedFingerprint, setPersistedFingerprint] = useState<
    string | null
  >(
    initialDraft === undefined
      ? null
      : draftFingerprint(initialDraft.source, initialDraft.parameterSchema),
  );
  const [validation, setValidation] = useState<FormulaValidation | null>(
    initialDraft?.executableVersionId !== null &&
      initialDraft?.executableVersionId !== undefined &&
      initialDraft.diagnostics.length === 0
      ? { valid: true, diagnostics: [] }
      : null,
  );
  const [isValidating, setIsValidating] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState('600000.SH');
  const [period, setPeriod] = useState<MarketPeriod>('1d');
  const [adjustment, setAdjustment] = useState<MarketAdjustment>('qfq');
  const [bars, setBars] = useState<readonly MarketBar[] | undefined>();
  const [preview, setPreview] = useState<FormulaPreviewResult | undefined>();
  const [historicalVersionId, setHistoricalVersionId] = useState('');

  const cancelOperation = useCallback((name: OperationName) => {
    operationControllers.current.get(name)?.abort();
    operationControllers.current.delete(name);
    operationGenerations.current.set(
      name,
      (operationGenerations.current.get(name) ?? 0) + 1,
    );
  }, []);

  const beginOperation = useCallback(
    (name: OperationName) => {
      cancelOperation(name);
      const controller = new AbortController();
      const generation = operationGenerations.current.get(name) ?? 0;
      operationControllers.current.set(name, controller);
      return { controller, generation };
    },
    [cancelOperation],
  );

  const isCurrentOperation = useCallback(
    (name: OperationName, generation: number, controller: AbortController) =>
      mountedRef.current &&
      !controller.signal.aborted &&
      operationGenerations.current.get(name) === generation &&
      operationControllers.current.get(name) === controller,
    [],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      for (const controller of operationControllers.current.values()) {
        controller.abort();
      }
      operationControllers.current.clear();
    };
  }, []);

  const catalogQuery = useQuery({
    queryKey: ['formulas', 'functions'],
    queryFn: ({ signal }) => api.listFunctions({ signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const templatesQuery = useQuery({
    queryKey: ['formulas', 'templates'],
    queryFn: ({ signal }) => api.listTemplates({ signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });
  const formulasQuery = useQuery({
    queryKey: ['formulas', 'catalog'],
    queryFn: ({ signal }) => api.listFormulas({ signal }),
  });
  const versionsQuery = useQuery({
    queryKey: ['formulas', formulaId, 'versions'],
    queryFn: ({ signal }) =>
      formulaId === null
        ? Promise.resolve([] as readonly FormulaVersion[])
        : api.listVersions(formulaId, { signal }),
    enabled: formulaId !== null,
  });

  const currentFingerprint = useMemo(
    () => draftFingerprint(source, parameterSchema),
    [parameterSchema, source],
  );
  const isDirty = savedFingerprint !== currentFingerprint;
  const isDraftDirty = persistedFingerprint !== currentFingerprint;
  const historicalVersion = useMemo(
    () =>
      (versionsQuery.data ?? []).find(
        (version) => version.id === historicalVersionId,
      ),
    [historicalVersionId, versionsQuery.data],
  );

  const runValidation = useCallback(async () => {
    const operation = beginOperation('validation');
    const epoch = draftEpochRef.current;
    if (source.trim().length === 0) {
      setValidation(null);
      setIsValidating(false);
      return null;
    }
    setIsValidating(true);
    setOperationError(null);
    try {
      const result = await api.validateFormula(
        { source, parameterSchema, formulaType },
        { signal: operation.controller.signal },
      );
      if (
        isCurrentOperation(
          'validation',
          operation.generation,
          operation.controller,
        ) &&
        draftEpochRef.current === epoch
      ) {
        setValidation(result);
        return result;
      }
      return null;
    } catch (error) {
      if (
        isCurrentOperation(
          'validation',
          operation.generation,
          operation.controller,
        ) &&
        draftEpochRef.current === epoch
      ) {
        setValidation(null);
        const message = errorMessage(error);
        if (message.length > 0) setOperationError(message);
      }
      return null;
    } finally {
      if (
        isCurrentOperation(
          'validation',
          operation.generation,
          operation.controller,
        )
      ) {
        setIsValidating(false);
      }
    }
  }, [
    api,
    beginOperation,
    formulaType,
    isCurrentOperation,
    parameterSchema,
    source,
  ]);

  useEffect(() => {
    setValidation(null);
    const timer = window.setTimeout(
      () => void runValidation(),
      validationDebounceMs,
    );
    return () => {
      window.clearTimeout(timer);
      cancelOperation('validation');
    };
  }, [cancelOperation, runValidation, validationDebounceMs]);

  useEffect(() => {
    if (
      adoptedTemplate.current ||
      initialSource !== undefined ||
      initialFormula !== undefined ||
      source.length > 0 ||
      templatesQuery.data === undefined ||
      templatesQuery.data.length === 0
    ) {
      return;
    }
    const template = templatesQuery.data[0];
    if (template === undefined) return;
    adoptedTemplate.current = true;
    setName(template.name);
    setFormulaType(template.formulaType);
    setPlacement(template.placement);
    setSource(template.source);
    setParameterSchema(template.parameterSchema);
  }, [initialFormula, initialSource, source.length, templatesQuery.data]);

  useEffect(() => {
    if (
      initialSource === undefined ||
      initialFormula !== undefined ||
      source !== initialSource ||
      Object.keys(parameterSchema).length > 0 ||
      templatesQuery.data === undefined
    ) {
      return;
    }
    const matchingTemplate = templatesQuery.data.find(
      (template) => template.source === initialSource,
    );
    if (matchingTemplate === undefined) return;
    setName(matchingTemplate.name);
    setFormulaType(matchingTemplate.formulaType);
    setPlacement(matchingTemplate.placement);
    setParameterSchema(matchingTemplate.parameterSchema);
  }, [
    initialFormula,
    initialSource,
    parameterSchema,
    source,
    templatesQuery.data,
  ]);

  const documentation = useMemo<readonly TdxDocumentationEntry[]>(
    () => [
      ...(catalogQuery.data?.functions ?? []).map((item) => ({
        name: item.name,
        signature: item.signature,
        summary: item.summaryZh,
        details: item.semanticsZh,
        kind: 'function' as const,
      })),
      ...(catalogQuery.data?.fields ?? []).map((item) => ({
        name: item.name,
        signature: item.name,
        summary: item.summaryZh,
        details: `${item.canonicalName} · ${item.unit}`,
        kind: 'field' as const,
      })),
    ],
    [catalogQuery.data],
  );

  function updateSource(next: string) {
    draftEpochRef.current += 1;
    cancelOperation('load');
    cancelOperation('save');
    cancelOperation('copy');
    cancelOperation('preview');
    setIsSaving(false);
    setIsPreviewing(false);
    setSource(next);
    setBars(undefined);
    setPreview(undefined);
    setNotice(null);
  }

  function invalidatePreview() {
    cancelOperation('preview');
    setIsPreviewing(false);
    setBars(undefined);
    setPreview(undefined);
    setNotice(null);
  }

  function applyTemplate(template: FormulaTemplate) {
    draftEpochRef.current += 1;
    for (const operation of ['load', 'save', 'copy', 'preview'] as const) {
      cancelOperation(operation);
    }
    setIsSaving(false);
    setIsPreviewing(false);
    setFormulaId(null);
    setSavedVersionId(null);
    setSavedFingerprint(null);
    setPersistedFingerprint(null);
    setRevision(1);
    setName(template.name);
    setFormulaType(template.formulaType);
    setPlacement(template.placement);
    setSource(template.source);
    setParameterSchema(template.parameterSchema);
    setPreview(undefined);
    setBars(undefined);
    setHistoricalVersionId('');
    setNotice(`已载入模板：${template.name}`);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  async function saveFormula() {
    const currentValidation = validation?.valid
      ? validation
      : await runValidation();
    if (currentValidation?.valid !== true) return;
    const operation = beginOperation('save');
    const epoch = draftEpochRef.current;
    setIsSaving(true);
    setOperationError(null);
    try {
      if (formulaId === null) {
        const created = await api.createFormula(
          { name, formulaType, placement, source, parameterSchema },
          { signal: operation.controller.signal },
        );
        if (
          !isCurrentOperation(
            'save',
            operation.generation,
            operation.controller,
          ) ||
          draftEpochRef.current !== epoch
        )
          return;
        setFormulaId(created.id);
        setRevision(created.draft.revision);
        setSavedVersionId(created.draft.executableVersionId);
        setSavedFingerprint(currentFingerprint);
        setPersistedFingerprint(currentFingerprint);
        setNotice(`已保存版本 v${String(created.latestVersion)}`);
      } else {
        const version = await api.saveFormula(
          formulaId,
          { source, parameterSchema, expectedRevision: revision },
          { signal: operation.controller.signal },
        );
        if (
          !isCurrentOperation(
            'save',
            operation.generation,
            operation.controller,
          ) ||
          draftEpochRef.current !== epoch
        )
          return;
        setRevision((value) => value + 1);
        setSavedVersionId(version.id);
        setSavedFingerprint(currentFingerprint);
        setPersistedFingerprint(currentFingerprint);
        setNotice(`已保存版本 v${String(version.version)}`);
      }
      setPreview(undefined);
      setBars(undefined);
      void formulasQuery.refetch();
      void versionsQuery.refetch();
    } catch (error) {
      if (
        isCurrentOperation('save', operation.generation, operation.controller)
      ) {
        setOperationError(errorMessage(error));
      }
    } finally {
      if (
        isCurrentOperation('save', operation.generation, operation.controller)
      ) {
        setIsSaving(false);
      }
    }
  }

  async function saveDraft() {
    if (formulaId === null || !isDraftDirty) return;
    const operation = beginOperation('save');
    const epoch = draftEpochRef.current;
    setIsSaving(true);
    setOperationError(null);
    try {
      const draft = await api.updateDraft(
        formulaId,
        { source, parameterSchema, expectedRevision: revision },
        { signal: operation.controller.signal },
      );
      if (
        !isCurrentOperation(
          'save',
          operation.generation,
          operation.controller,
        ) ||
        draftEpochRef.current !== epoch
      )
        return;
      setRevision(draft.revision);
      setPersistedFingerprint(currentFingerprint);
      setSavedVersionId(draft.executableVersionId);
      setValidation({
        valid: draft.diagnostics.length === 0,
        diagnostics: draft.diagnostics,
      });
      setNotice(`草稿已保存 · 修订 ${String(draft.revision)}`);
      setBars(undefined);
      setPreview(undefined);
      void formulasQuery.refetch();
    } catch (error) {
      if (
        isCurrentOperation('save', operation.generation, operation.controller)
      ) {
        setOperationError(errorMessage(error));
      }
    } finally {
      if (
        isCurrentOperation('save', operation.generation, operation.controller)
      ) {
        setIsSaving(false);
      }
    }
  }

  async function copyFormula() {
    if (formulaId === null || savedVersionId === null || isDirty) return;
    const operation = beginOperation('copy');
    const epoch = draftEpochRef.current;
    setIsSaving(true);
    setOperationError(null);
    try {
      const copy = await api.copyFormula(
        formulaId,
        { name: `${name} 副本`, sourceVersionId: savedVersionId },
        { signal: operation.controller.signal },
      );
      if (
        !isCurrentOperation(
          'copy',
          operation.generation,
          operation.controller,
        ) ||
        draftEpochRef.current !== epoch
      )
        return;
      setNotice(`已复制为独立公式版本：${copy.name}`);
      void formulasQuery.refetch();
    } catch (error) {
      if (
        isCurrentOperation('copy', operation.generation, operation.controller)
      ) {
        setOperationError(errorMessage(error));
      }
    } finally {
      if (
        isCurrentOperation('copy', operation.generation, operation.controller)
      ) {
        setIsSaving(false);
      }
    }
  }

  async function loadFormula(selectedId: string) {
    if (selectedId.length === 0 || selectedId === formulaId) return;
    const operation = beginOperation('load');
    for (const name of ['save', 'copy', 'preview'] as const) {
      cancelOperation(name);
    }
    setIsSaving(false);
    setIsPreviewing(false);
    setBars(undefined);
    setPreview(undefined);
    setOperationError(null);
    try {
      const selected = await api.getFormula(selectedId, {
        signal: operation.controller.signal,
      });
      if (
        !isCurrentOperation('load', operation.generation, operation.controller)
      ) {
        return;
      }
      draftEpochRef.current += 1;
      cancelOperation('validation');
      setFormulaId(selected.id);
      setName(selected.name);
      setFormulaType(selected.formulaType);
      setPlacement(selected.placement);
      setSource(selected.draft.source);
      setParameterSchema(selected.draft.parameterSchema);
      setRevision(selected.draft.revision);
      setSavedVersionId(selected.draft.executableVersionId);
      setSavedFingerprint(
        selected.draft.executableVersionId === null
          ? null
          : draftFingerprint(
              selected.draft.source,
              selected.draft.parameterSchema,
            ),
      );
      setPersistedFingerprint(
        draftFingerprint(selected.draft.source, selected.draft.parameterSchema),
      );
      setPreview(undefined);
      setHistoricalVersionId('');
      setNotice(`已打开：${selected.name}`);
    } catch (error) {
      if (
        isCurrentOperation('load', operation.generation, operation.controller)
      ) {
        setOperationError(errorMessage(error));
      }
    }
  }

  async function runPreview() {
    if (savedVersionId === null || isDirty || validation?.valid !== true)
      return;
    const operation = beginOperation('preview');
    const epoch = draftEpochRef.current;
    setBars(undefined);
    setPreview(undefined);
    setIsPreviewing(true);
    setOperationError(null);
    try {
      const market = await marketApiClient.getBars({
        symbol,
        period,
        adjustment,
        formulaVersionId: savedVersionId,
        formulaParameters: parameterValues(parameterSchema),
        signal: operation.controller.signal,
      });
      const result = market.formula;
      if (result === undefined) throw new FormulaProtocolError('bars.formula');
      if (
        !isCurrentOperation(
          'preview',
          operation.generation,
          operation.controller,
        ) ||
        draftEpochRef.current !== epoch
      )
        return;
      setBars(market.bars);
      setPreview(result);
      setNotice(`预览已完成：v${String(result.formulaVersion)}`);
    } catch (error) {
      if (
        isCurrentOperation(
          'preview',
          operation.generation,
          operation.controller,
        )
      )
        setOperationError(errorMessage(error));
    } finally {
      if (
        isCurrentOperation(
          'preview',
          operation.generation,
          operation.controller,
        )
      )
        setIsPreviewing(false);
    }
  }

  const diagnostics: readonly FormulaDiagnostic[] =
    validation?.diagnostics ?? [];
  const canSave =
    validation?.valid === true && isDirty && !isSaving && source.length > 0;
  const canPreview =
    savedVersionId !== null &&
    !isDirty &&
    validation?.valid === true &&
    !isPreviewing;
  const canSaveDraft =
    formulaId !== null && isDraftDirty && !isSaving && source.length > 0;
  const serviceError =
    catalogQuery.error ?? templatesQuery.error ?? formulasQuery.error;

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <article className="formula-studio-page">
      <header className="page-heading formula-studio-heading">
        <div>
          <span className="page-kicker">FORMULA / TDX COMPATIBLE</span>
          <h2 ref={headingRef} data-page-heading tabIndex={-1}>
            公式工作台
          </h2>
          <p>用函数、模板和参数表单构建公式，再显式保存并预览买卖点。</p>
        </div>
        <div className="formula-heading-meta">
          <span className="release-badge">v0.3.0 · Formula Studio</span>
          <span>
            {catalogQuery.data?.compatibilityVersion ?? '兼容清单加载中'}
          </span>
        </div>
      </header>

      {serviceError === null || serviceError === undefined ? null : (
        <p className="formula-page-alert" role="alert">
          {errorMessage(serviceError)}
        </p>
      )}

      <div className="formula-studio-grid">
        <FunctionLibrary
          fields={catalogQuery.data?.fields ?? []}
          functions={catalogQuery.data?.functions ?? []}
          templates={templatesQuery.data ?? []}
          onSelectTemplate={applyTemplate}
          onInsert={(fallback, item) =>
            editorRef.current?.insertSnippet(snippetFor(item, fallback))
          }
        />

        <section className="formula-editor-panel" aria-label="公式代码与参数">
          <header className="formula-panel-heading formula-editor-heading">
            <div>
              <span className="panel-kicker">EDITOR / CTRL + ENTER</span>
              <h3>代码与参数</h3>
            </div>
            <span
              className="formula-validation-state"
              data-state={
                validation?.valid === true
                  ? 'valid'
                  : diagnostics.length > 0
                    ? 'invalid'
                    : 'pending'
              }
            >
              {isValidating
                ? '校验中'
                : validation?.valid === true
                  ? '校验通过'
                  : diagnostics.length > 0
                    ? `${String(diagnostics.length)} 个问题`
                    : '等待校验'}
            </span>
          </header>
          <div className="formula-identity-row">
            <label>
              <span>公式名称</span>
              <input
                aria-label="公式名称"
                disabled={formulaId !== null}
                maxLength={64}
                value={name}
                onChange={(event) => {
                  draftEpochRef.current += 1;
                  cancelOperation('load');
                  cancelOperation('save');
                  setIsSaving(false);
                  setName(event.currentTarget.value);
                }}
              />
            </label>
            <label>
              <span>打开公式</span>
              <select
                aria-label="打开已保存公式"
                value={formulaId ?? ''}
                onChange={(event) =>
                  void loadFormula(event.currentTarget.value)
                }
              >
                <option value="">新公式 / 模板</option>
                {(formulasQuery.data?.items ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · v{item.latestVersion}
                  </option>
                ))}
              </select>
            </label>
            {formulaId === null ? null : (
              <label>
                <span>历史版本（只读）</span>
                <select
                  aria-label="查看历史版本"
                  value={historicalVersionId}
                  onChange={(event) =>
                    setHistoricalVersionId(event.currentTarget.value)
                  }
                >
                  <option value="">不查看历史版本</option>
                  {(versionsQuery.data ?? []).map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version} · {version.createdAt}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          {historicalVersion === undefined ? null : (
            <section aria-label="历史版本详情">
              <p>
                v{historicalVersion.version}{' '}
                为不可变历史版本；可复制到当前草稿作参考。
              </p>
              <textarea
                aria-label="历史版本公式源码"
                readOnly
                value={historicalVersion.source}
              />
              <button
                type="button"
                className="formula-secondary-action"
                onClick={() => {
                  updateSource(historicalVersion.source);
                  setParameterSchema(historicalVersion.parameterSchema);
                  setHistoricalVersionId('');
                }}
              >
                复制到当前草稿
              </button>
            </section>
          )}
          <div className="formula-kind-row">
            <div role="radiogroup" aria-label="公式类型">
              <button
                type="button"
                role="radio"
                aria-checked={formulaType === 'indicator'}
                disabled={formulaId !== null}
                onClick={() => {
                  draftEpochRef.current += 1;
                  cancelOperation('load');
                  cancelOperation('validation');
                  cancelOperation('save');
                  setFormulaType('indicator');
                }}
              >
                技术指标
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={formulaType === 'trading'}
                disabled={formulaId !== null}
                onClick={() => {
                  draftEpochRef.current += 1;
                  cancelOperation('load');
                  cancelOperation('validation');
                  cancelOperation('save');
                  setFormulaType('trading');
                }}
              >
                交易系统
              </button>
            </div>
            <div role="radiogroup" aria-label="绘制位置">
              <button
                type="button"
                role="radio"
                aria-checked={placement === 'subchart'}
                disabled={formulaId !== null}
                onClick={() => {
                  draftEpochRef.current += 1;
                  cancelOperation('load');
                  cancelOperation('save');
                  setPlacement('subchart');
                }}
              >
                副图
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={placement === 'main'}
                disabled={formulaId !== null}
                onClick={() => {
                  draftEpochRef.current += 1;
                  cancelOperation('load');
                  cancelOperation('save');
                  setPlacement('main');
                }}
              >
                主图叠加
              </button>
            </div>
          </div>
          <FormulaEditor
            ref={editorRef}
            diagnostics={diagnostics}
            documentation={documentation}
            source={source}
            onChange={updateSource}
            onValidate={() => void runValidation()}
          />
          <div className="formula-diagnostic-panel" aria-live="polite">
            {diagnostics.length === 0 ? (
              <p>
                {validation?.valid === true
                  ? '语法、函数支持度和未来函数检查已通过。'
                  : '输入后会自动校验，也可按 Ctrl/⌘ + Enter 立即校验。'}
              </p>
            ) : (
              <ol>
                {diagnostics.map((diagnostic, index) => (
                  <li key={`${diagnostic.code}-${String(index)}`}>
                    <strong>{diagnostic.explanation}</strong>
                    <span>
                      第 {diagnostic.span.line} 行，第 {diagnostic.span.column}{' '}
                      列
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </div>
          <ParameterPanel
            schema={parameterSchema}
            onChange={(schema) => {
              draftEpochRef.current += 1;
              cancelOperation('load');
              cancelOperation('save');
              cancelOperation('copy');
              cancelOperation('preview');
              setIsSaving(false);
              setIsPreviewing(false);
              setParameterSchema(schema);
              setBars(undefined);
              setPreview(undefined);
              setNotice(null);
            }}
          />
          <footer className="formula-editor-actions">
            <div aria-live="polite">
              {notice ??
                (isDirty && savedVersionId !== null
                  ? '草稿已变更，请先校验并保存新版本'
                  : '保存会创建不可变版本')}
            </div>
            <button
              type="button"
              className="formula-secondary-action"
              onClick={() => void runValidation()}
              disabled={isValidating || source.length === 0}
            >
              立即校验
            </button>
            <button
              type="button"
              className="formula-secondary-action"
              onClick={() => void copyFormula()}
              disabled={
                formulaId === null ||
                savedVersionId === null ||
                isDirty ||
                isSaving
              }
            >
              复制公式
            </button>
            <button
              type="button"
              className="formula-secondary-action"
              onClick={() => void saveDraft()}
              disabled={!canSaveDraft}
            >
              保存草稿
            </button>
            <button
              type="button"
              className="formula-primary-action"
              onClick={() => void saveFormula()}
              disabled={!canSave}
            >
              {isSaving ? '保存中…' : '保存为新版本'}
            </button>
          </footer>
        </section>

        <FormulaPreview
          adjustment={adjustment}
          bars={bars}
          errorMessage={operationError ?? undefined}
          isLoading={isPreviewing}
          onAdjustmentChange={(value) => {
            setAdjustment(value);
            invalidatePreview();
          }}
          onPeriodChange={(value) => {
            setPeriod(value);
            invalidatePreview();
          }}
          onPreview={() => void runPreview()}
          onSymbolChange={(value) => {
            setSymbol(value);
            invalidatePreview();
          }}
          period={period}
          placement={placement}
          preview={preview}
          previewDisabled={!canPreview}
          symbol={symbol}
        />
      </div>
    </article>
  );
}
