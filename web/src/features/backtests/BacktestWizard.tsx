import { useEffect, useMemo, useRef, useState } from 'react';

import {
  marketApi,
  type MarketApi,
  type MarketPoolSummary,
} from '../market/marketApi';
import {
  backtestApi,
  type BacktestApi,
  type BacktestIntent,
  type BacktestPreflight,
  type BacktestSubmission,
} from './backtestApi';
import { saveBacktestDraft, type BacktestDraft } from './backtestDraft';
import { RemediationLinks } from './RemediationLinks';
import { CostsStep } from './steps/CostsStep';
import { FormulaStep, type FormulaChoice } from './steps/FormulaStep';
import { PeriodStep } from './steps/PeriodStep';
import { ReviewStep } from './steps/ReviewStep';
import { ScopeStep } from './steps/ScopeStep';

const steps = ['公式', '范围', '周期', '成本', '复核'] as const;
const exactDecimal = /^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$/u;
const canonicalSymbol = /^\d{6}\.(?:SH|SZ|BJ)$/u;

const emptyDraft: BacktestDraft = {
  formulaId: '',
  formulaVersionId: '',
  formulaParameters: {},
  scope: { kind: 'single', symbol: '' },
  period: '1d',
  adjustment: 'qfq',
  startDate: '',
  endDate: '',
  quantityShares: 1000,
  commissionBps: '2.5',
  minimumCommission: '5',
  sellTaxBps: '5',
  slippageBps: '1',
};

function fingerprint(draft: BacktestDraft) {
  return JSON.stringify(draft);
}

function intent(draft: BacktestDraft): BacktestIntent {
  return {
    scope: draft.scope,
    formulaVersionId: draft.formulaVersionId,
    formulaParameters: draft.formulaParameters,
    period: draft.period,
    adjustment: draft.adjustment,
    scoringStart: `${draft.startDate}T00:00:00+08:00`,
    scoringEnd: `${draft.endDate}T00:00:00+08:00`,
    quantityShares: draft.quantityShares,
    commissionBps: draft.commissionBps,
    minimumCommission: draft.minimumCommission,
    sellTaxBps: draft.sellTaxBps,
    slippageBps: draft.slippageBps,
  };
}

function realDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year ?? 0, (month ?? 1) - 1, day ?? 0));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() + 1 === month &&
    date.getUTCDate() === day
  );
}

function validateStep(
  draft: BacktestDraft,
  step: number,
  choices: readonly FormulaChoice[],
  pools: readonly MarketPoolSummary[],
): readonly string[] {
  const errors: string[] = [];
  if (step === 0) {
    const formula = choices.find((item) => item.id === draft.formulaId);
    const version = formula?.versions.find(
      (item) => item.id === draft.formulaVersionId,
    );
    if (
      formula === undefined ||
      version === undefined ||
      version.formulaType !== 'trading'
    )
      errors.push('请选择已保存且可执行的交易公式版本。');
    else {
      const expectedNames = Object.keys(version.parameterSchema).sort();
      const actualNames = Object.keys(draft.formulaParameters).sort();
      if (
        expectedNames.length !== actualNames.length ||
        expectedNames.some((name, index) => name !== actualNames[index])
      ) {
        errors.push('公式参数与所选版本不一致，请重新选择该公式版本。');
      }
      for (const [name, declaration] of Object.entries(
        version.parameterSchema,
      )) {
        const value = draft.formulaParameters[name];
        if (
          value === undefined ||
          !Number.isFinite(value) ||
          Math.abs(value) > Number.MAX_SAFE_INTEGER ||
          (declaration.kind === 'integer' && !Number.isSafeInteger(value))
        )
          errors.push(`请填写有效的公式参数：${declaration.label ?? name}。`);
      }
    }
  }
  if (step === 1) {
    const selectedScope = draft.scope;
    if (
      selectedScope.kind === 'single' &&
      !canonicalSymbol.test(selectedScope.symbol)
    )
      errors.push('请通过证券搜索选择有效的 A 股证券。');
    if (
      selectedScope.kind === 'preset' &&
      !pools.some(
        (pool) =>
          pool.poolId === selectedScope.poolId &&
          pool.kind === 'preset' &&
          pool.snapshotId === selectedScope.snapshotId,
      )
    )
      errors.push('该预设股票池快照已更新，请重新选择当前版本。');
    if (
      selectedScope.kind === 'custom' &&
      !pools.some(
        (pool) =>
          pool.poolId === selectedScope.poolId &&
          pool.kind === 'custom' &&
          pool.revision === selectedScope.revision,
      )
    )
      errors.push('该自定义股票池版本已更新，请重新选择当前版本。');
  }
  if (
    step === 2 &&
    (!realDate(draft.startDate) ||
      !realDate(draft.endDate) ||
      draft.startDate >= draft.endDate)
  )
    errors.push('请选择真实日期，且开始日期必须早于结束日期。');
  if (step === 3) {
    if (
      !Number.isSafeInteger(draft.quantityShares) ||
      draft.quantityShares <= 0 ||
      draft.quantityShares > 100_000_000 ||
      draft.quantityShares % 100 !== 0
    )
      errors.push(
        '买入股数必须在 100 至 100,000,000 之间，且为 100 股整数倍。',
      );
    for (const [index, value] of [
      draft.commissionBps,
      draft.minimumCommission,
      draft.sellTaxBps,
      draft.slippageBps,
    ].entries()) {
      if (
        value.length > 64 ||
        !exactDecimal.test(value) ||
        (index !== 1 && Number(value) > 10_000)
      ) {
        errors.push(
          '成本必须使用不超过 64 个字符的非负规范小数；费率不能超过 10,000 基点。',
        );
        break;
      }
    }
  }
  return errors;
}

function validate(
  draft: BacktestDraft,
  choices: readonly FormulaChoice[],
  pools: readonly MarketPoolSummary[],
) {
  return [0, 1, 2, 3].flatMap((step) =>
    validateStep(draft, step, choices, pools),
  );
}

export type BacktestWizardProps = {
  readonly api?: BacktestApi;
  readonly formulaChoices?: readonly FormulaChoice[];
  readonly initialState?: BacktestDraft;
  readonly marketApiClient?: Pick<MarketApi, 'searchInstruments'>;
  readonly pools?: readonly MarketPoolSummary[];
  readonly catalogRevision?: number;
  readonly onSubmitted?: (
    submission: BacktestSubmission,
    notice: readonly string[],
  ) => void;
};

export function BacktestWizard({
  api = backtestApi,
  formulaChoices = [],
  initialState = emptyDraft,
  marketApiClient = marketApi,
  pools = [],
  catalogRevision = 0,
  onSubmitted,
}: BacktestWizardProps) {
  const [draft, setDraft] = useState(initialState);
  const [step, setStep] = useState(0);
  const [preflight, setPreflight] = useState<BacktestPreflight | null>(null);
  const [preflightFingerprint, setPreflightFingerprint] = useState<
    string | null
  >(null);
  const [preflighting, setPreflighting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [partialConfirmed, setPartialConfirmed] = useState(false);
  const [errors, setErrors] = useState<readonly string[]>([]);
  const errorRef = useRef<HTMLDivElement>(null);
  const requestGeneration = useRef(0);
  const submitLock = useRef(false);
  const preflightController = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const hasUserChanged = useRef(false);
  const catalogFingerprint = useMemo(() => {
    const formula = formulaChoices
      .flatMap((choice) => choice.versions)
      .find((version) => version.id === draft.formulaVersionId);
    const selectedScope = draft.scope;
    const pool =
      selectedScope.kind === 'single'
        ? null
        : pools.find((item) => item.poolId === selectedScope.poolId);
    return JSON.stringify({
      formulaChecksum: formula?.checksum ?? null,
      formulaVersion: formula?.version ?? null,
      poolSnapshot: pool?.snapshotId ?? null,
      poolRevision: pool?.revision ?? null,
      catalogRevision,
    });
  }, [
    catalogRevision,
    draft.formulaVersionId,
    draft.scope,
    formulaChoices,
    pools,
  ]);
  const currentFingerprint = useMemo(
    () => `${fingerprint(draft)}\u0000${catalogFingerprint}`,
    [catalogFingerprint, draft],
  );
  const previousCatalogFingerprint = useRef(catalogFingerprint);

  useEffect(() => {
    if (hasUserChanged.current) saveBacktestDraft(draft);
  }, [draft]);
  useEffect(() => {
    if (previousCatalogFingerprint.current === catalogFingerprint) return;
    previousCatalogFingerprint.current = catalogFingerprint;
    preflightController.current?.abort();
    requestGeneration.current += 1;
    setPreflighting(false);
    setPreflight(null);
    setPreflightFingerprint(null);
    setPartialConfirmed(false);
  }, [catalogFingerprint]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      preflightController.current?.abort();
    };
  }, []);

  function update(updater: (current: BacktestDraft) => BacktestDraft) {
    if (submitLock.current) return;
    hasUserChanged.current = true;
    setDraft((current) => {
      const next = updater(current);
      return next;
    });
    preflightController.current?.abort();
    requestGeneration.current += 1;
    setPreflighting(false);
    setPreflight(null);
    setPartialConfirmed(false);
    setPreflightFingerprint(null);
    setErrors([]);
  }

  function move(next: number) {
    if (submitLock.current) return;
    if (next > step) {
      const validation = Array.from(
        { length: next - step },
        (_, offset) => step + offset,
      ).flatMap((index) => validateStep(draft, index, formulaChoices, pools));
      if (validation.length > 0) {
        setErrors(validation);
        window.setTimeout(() => errorRef.current?.focus(), 0);
        return;
      }
    }
    setStep(next);
    window.setTimeout(
      () =>
        document
          .querySelector<HTMLElement>(
            `#backtest-${['formula', 'scope', 'period', 'costs', 'review'][next]}-heading`,
          )
          ?.focus(),
      0,
    );
  }

  async function runPreflight() {
    if (submitLock.current) return;
    const validation = validate(draft, formulaChoices, pools);
    if (validation.length > 0) {
      setErrors(validation);
      window.setTimeout(() => errorRef.current?.focus(), 0);
      return;
    }
    const generation = ++requestGeneration.current;
    preflightController.current?.abort();
    const controller = new AbortController();
    preflightController.current = controller;
    const requestedFingerprint = currentFingerprint;
    setPreflighting(true);
    setErrors([]);
    try {
      const result = await api.preflight(intent(draft), {
        signal: controller.signal,
      });
      if (requestGeneration.current !== generation) return;
      setPreflight(result);
      setPartialConfirmed(false);
      setPreflightFingerprint(requestedFingerprint);
    } catch {
      if (
        requestGeneration.current === generation &&
        !controller.signal.aborted
      ) {
        setErrors(['预检失败，请检查本地服务和数据覆盖后重试。']);
        window.setTimeout(() => errorRef.current?.focus(), 0);
      }
    } finally {
      if (requestGeneration.current === generation) setPreflighting(false);
    }
  }

  async function submit() {
    if (
      submitLock.current ||
      preflight === null ||
      preflightFingerprint !== currentFingerprint ||
      preflight.scope.runnable === 0 ||
      (preflight.scope.gapCount > 0 && !partialConfirmed)
    )
      return;
    submitLock.current = true;
    setSubmitting(true);
    setErrors([]);
    saveBacktestDraft(draft);
    try {
      const submission = await api.create(intent(draft));
      if (!mountedRef.current) return;
      const warningLabels: Readonly<Record<string, string>> = {
        partial_data: '部分证券数据不足，已按服务端冻结的可运行范围创建任务。',
        snapshot_changed: '数据版本已更新，服务端已冻结新的不可变快照。',
      };
      const notice = submission.warnings.map(
        (warning) =>
          warningLabels[warning] ??
          '服务端返回了回测范围提示，请在运行详情中核对冻结证据。',
      );
      if (submission.snapshotId !== preflight.previewSnapshotId)
        notice.push('提交时数据已更新，服务端已重新校验并冻结新的不可变快照。');
      onSubmitted?.(submission, notice);
    } catch {
      if (!mountedRef.current) return;
      submitLock.current = false;
      setSubmitting(false);
      setErrors(['提交失败，完整草稿已保留；请确认本地服务可用后重试。']);
      window.setTimeout(() => errorRef.current?.focus(), 0);
    }
  }

  const choice = formulaChoices.find((item) => item.id === draft.formulaId);
  const selectedScope = draft.scope;
  const pool =
    selectedScope.kind === 'single'
      ? undefined
      : pools.find((item) => item.poolId === selectedScope.poolId);
  const periodLabel = { '1d': '日线', '1w': '周线', '60m': '60 分钟' }[
    draft.period
  ];
  const adjustmentLabel = { none: '不复权', qfq: '前复权', hfq: '后复权' }[
    draft.adjustment
  ];
  return (
    <div className="backtest-wizard">
      <fieldset className="backtest-wizard-lock" disabled={submitting}>
        <legend className="visually-hidden">回测配置</legend>
        <nav className="backtest-stepper" aria-label="回测配置步骤">
          <ol>
            {steps.map((label, index) => (
              <li key={label}>
                <button
                  type="button"
                  aria-current={step === index ? 'step' : undefined}
                  onClick={() => move(index)}
                >
                  {index + 1}. {label}
                </button>
              </li>
            ))}
          </ol>
        </nav>
        {errors.length > 0 ? (
          <div
            ref={errorRef}
            className="backtest-error-summary"
            role="alert"
            tabIndex={-1}
          >
            <strong>请处理以下问题</strong>
            <ul>
              {errors.map((error) => (
                <li key={error}>{error}</li>
              ))}
            </ul>
            <RemediationLinks />
          </div>
        ) : null}
        <div className="backtest-wizard-layout">
          <div className="backtest-editor-panel">
            {step === 0 ? (
              <FormulaStep
                choices={formulaChoices}
                formulaId={draft.formulaId}
                formulaVersionId={draft.formulaVersionId}
                parameters={draft.formulaParameters}
                onFormulaChange={(formulaId) => {
                  const selected = formulaChoices.find(
                    (item) => item.id === formulaId,
                  );
                  const version = selected?.versions.find(
                    (item) => item.version === selected.latestVersion,
                  );
                  update((current) => ({
                    ...current,
                    formulaId,
                    formulaVersionId: version?.id ?? '',
                    formulaParameters: Object.fromEntries(
                      Object.entries(version?.parameterSchema ?? {}).map(
                        ([name, declaration]) => [name, declaration.default],
                      ),
                    ),
                  }));
                }}
                onVersionChange={(formulaVersionId) => {
                  const version = choice?.versions.find(
                    (item) => item.id === formulaVersionId,
                  );
                  update((current) => ({
                    ...current,
                    formulaVersionId,
                    formulaParameters: Object.fromEntries(
                      Object.entries(version?.parameterSchema ?? {}).map(
                        ([name, declaration]) => [name, declaration.default],
                      ),
                    ),
                  }));
                }}
                onParameterChange={(name, value) =>
                  update((current) => ({
                    ...current,
                    formulaParameters: {
                      ...current.formulaParameters,
                      [name]: value,
                    },
                  }))
                }
              />
            ) : null}
            {step === 1 ? (
              <ScopeStep
                scope={draft.scope}
                pools={pools}
                marketApiClient={marketApiClient}
                onChange={(scope) =>
                  update((current) => ({ ...current, scope }))
                }
              />
            ) : null}
            {step === 2 ? (
              <PeriodStep
                period={draft.period}
                adjustment={draft.adjustment}
                startDate={draft.startDate}
                endDate={draft.endDate}
                onChange={(change) =>
                  update((current) => ({ ...current, ...change }))
                }
              />
            ) : null}
            {step === 3 ? (
              <CostsStep
                values={draft}
                onChange={(change) =>
                  update((current) => ({ ...current, ...change }))
                }
              />
            ) : null}
            {step === 4 ? (
              <ReviewStep
                draft={draft}
                formulaName={choice?.name ?? '未选择'}
                scopeName={
                  draft.scope.kind === 'single'
                    ? draft.scope.symbol
                    : (pool?.name ?? '未选择')
                }
                preflight={preflight}
                busy={preflighting}
                partialConfirmed={partialConfirmed}
                onPartialConfirmed={setPartialConfirmed}
                onPreflight={() => void runPreflight()}
              />
            ) : null}
            <div className="wizard-actions">
              <button
                type="button"
                className="secondary-action"
                disabled={step === 0}
                onClick={() => move(step - 1)}
              >
                上一步
              </button>
              <button
                type="button"
                className="secondary-action"
                disabled={step === steps.length - 1}
                onClick={() => move(step + 1)}
              >
                下一步
              </button>
              <button
                type="button"
                className="primary-action"
                disabled={
                  submitting ||
                  preflight === null ||
                  preflightFingerprint !== currentFingerprint ||
                  preflight.scope.runnable === 0 ||
                  (preflight.scope.gapCount > 0 && !partialConfirmed)
                }
                onClick={() => void submit()}
              >
                {submitting ? '提交中…' : '提交回测'}
              </button>
            </div>
          </div>
          <aside className="backtest-review-panel" aria-label="当前配置摘要">
            <h3>配置摘要</h3>
            <dl>
              <div>
                <dt>公式</dt>
                <dd>{choice?.name ?? '未选择'}</dd>
              </div>
              <div>
                <dt>范围</dt>
                <dd>
                  {draft.scope.kind === 'single'
                    ? draft.scope.symbol || '未选择'
                    : (pool?.name ?? '未选择')}
                </dd>
              </div>
              <div>
                <dt>周期</dt>
                <dd>
                  {periodLabel} · {adjustmentLabel}
                </dd>
              </div>
              <div>
                <dt>区间</dt>
                <dd>
                  {draft.startDate || '—'} → {draft.endDate || '—'}
                </dd>
              </div>
            </dl>
            <p>任何配置修改都会使服务端预检失效。</p>
          </aside>
        </div>
      </fieldset>
    </div>
  );
}
