import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { ModalDialog } from '../../shared/ModalDialog';

import {
  guidanceApi,
  type GuidanceApi,
  type GuidancePage,
  type GuidancePreferences,
  type GuidanceStatus,
} from './guidanceApi';

type GuidanceStep = {
  readonly title: string;
  readonly body: string;
  readonly expectedResult: string;
  readonly target: `[data-guidance-target=${string}]`;
};

type GuidanceTour = {
  readonly contentVersion: number;
  readonly label: string;
  readonly steps: readonly [
    GuidanceStep,
    GuidanceStep,
    GuidanceStep,
    ...GuidanceStep[],
  ];
};

type PersistOperation = {
  readonly controller: AbortController;
};

// Exported for contract tests that verify every core page has an independent,
// versioned 3-5 step definition.
// eslint-disable-next-line react-refresh/only-export-components
export const GUIDANCE_TOURS: Readonly<Record<GuidancePage, GuidanceTour>> = {
  market: {
    contentVersion: 2,
    label: '行情',
    steps: [
      {
        title: '搜索证券',
        body: '按代码、中文名或拼音查找证券。',
        expectedResult: '选择后立即加载对应的真实 K 线。',
        target: '[data-guidance-target=market-search]',
      },
      {
        title: '管理自选',
        body: '在左侧栏查看自选与最近访问。',
        expectedResult: '常用证券会保留在个人工作区。',
        target: '[data-guidance-target=market-watchlist]',
      },
      {
        title: '切换周期',
        body: '选择日线、周线或 60 分钟。',
        expectedResult: '主图按所选周期重新显示。',
        target: '[data-guidance-target=market-period]',
      },
      {
        title: '查看图表',
        body: '主图展示 K 线，副图展示公式结果。',
        expectedResult: '可缩放并检查当前数据来源。',
        target: '[data-guidance-target=market-chart]',
      },
    ],
  },
  formula: {
    contentVersion: 1,
    label: '公式',
    steps: [
      {
        title: '编辑公式',
        body: '在编辑器输入兼容的通达信公式。',
        expectedResult: '语法诊断会定位需要修改的位置。',
        target: '[data-guidance-target=formula-editor]',
      },
      {
        title: '配置参数',
        body: '用表单设置公式参数与默认值。',
        expectedResult: '无需改代码即可尝试不同参数。',
        target: '[data-guidance-target=formula-parameters]',
      },
      {
        title: '预览效果',
        body: '预览主图、副图和买卖点信号。',
        expectedResult: '保存前即可核对公式输出。',
        target: '[data-guidance-target=formula-preview]',
      },
      {
        title: '保存版本',
        body: '验证通过后保存不可变版本。',
        expectedResult: '该版本可直接用于策略回测。',
        target: '[data-guidance-target=formula-save]',
      },
    ],
  },
  backtest: {
    contentVersion: 1,
    label: '回测',
    steps: [
      {
        title: '选择公式',
        body: '选择带买卖点的已保存公式版本。',
        expectedResult: '信号规则被固定到本次回测。',
        target: '[data-guidance-target=backtest-wizard]',
      },
      {
        title: '设置范围',
        body: '选择证券、股票池和回测周期。',
        expectedResult: '系统只计算确认过的历史范围。',
        target: '[data-guidance-target=backtest-wizard]',
      },
      {
        title: '确认成本',
        body: '检查手续费、滑点等假设。',
        expectedResult: '报告会反映更接近实际的交易成本。',
        target: '[data-guidance-target=backtest-wizard]',
      },
      {
        title: '查看历史',
        body: '已提交的任务会进入历史列表。',
        expectedResult: '可打开报告或前往任务中心查看进度。',
        target: '[data-guidance-target=backtest-history]',
      },
    ],
  },
  analysis: {
    contentVersion: 1,
    label: '智能分析',
    steps: [
      {
        title: '创建分析',
        body: '选择证券、模型和分析范围。',
        expectedResult: '确认后创建可审计的分析任务。',
        target: '[data-guidance-target=analysis-run]',
      },
      {
        title: '观察过程',
        body: '过程面板展示各阶段状态。',
        expectedResult: '可识别等待、完成或失败的步骤。',
        target: '[data-guidance-target=analysis-process]',
      },
      {
        title: '核对证据',
        body: '查看结论关联的数据与来源。',
        expectedResult: '每项关键判断都可回到证据核验。',
        target: '[data-guidance-target=analysis-evidence]',
      },
      {
        title: '阅读结论',
        body: '结论区汇总观点、风险与限制。',
        expectedResult: '获得带证据边界的研究结果，而非投资建议。',
        target: '[data-guidance-target=analysis-conclusion]',
      },
    ],
  },
  tasks: {
    contentVersion: 1,
    label: '任务中心',
    steps: [
      {
        title: '查看汇总',
        body: '先了解排队、运行、成功和失败数量。',
        expectedResult: '快速判断后台工作是否正常。',
        target: '[data-guidance-target=tasks-metrics]',
      },
      {
        title: '筛选任务',
        body: '按状态和类型缩小最近任务列表。',
        expectedResult: '更快定位需要关注的任务。',
        target: '[data-guidance-target=tasks-filters]',
      },
      {
        title: '查看详情',
        body: '选择任务查看阶段、事件和失败说明。',
        expectedResult: '获得安全、可操作的当前状态。',
        target: '[data-guidance-target=tasks-list]',
      },
      {
        title: '刷新状态',
        body: '手动刷新可立即同步后台进度。',
        expectedResult: '列表和详情显示最新安全快照。',
        target: '[data-guidance-target=tasks-refresh]',
      },
    ],
  },
};

function pageForPath(pathname: string): GuidancePage | null {
  if (pathname === '/market') return 'market';
  if (pathname === '/formulas') return 'formula';
  if (pathname.startsWith('/backtests')) return 'backtest';
  if (pathname === '/analysis') return 'analysis';
  if (pathname === '/tasks') return 'tasks';
  return null;
}

export function ContextualGuidance({
  api = guidanceApi,
}: {
  readonly api?: GuidanceApi;
}) {
  const location = useLocation();
  const page = pageForPath(location.pathname);
  const [preferences, setPreferences] = useState<GuidancePreferences | null>(
    null,
  );
  const [active, setActive] = useState<{
    page: GuidancePage;
    manual: boolean;
  } | null>(null);
  const [stepIndex, setStepIndex] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [placement, setPlacement] = useState<'top' | 'bottom'>('bottom');
  const [persisting, setPersisting] = useState(false);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const helpRef = useRef<HTMLButtonElement>(null);
  const persistStatusRef = useRef<HTMLParagraphElement>(null);
  const openedAutomatically = useRef(new Set<GuidancePage>());
  const persistOperationRef = useRef<PersistOperation | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .get({ signal: controller.signal })
      .then(setPreferences)
      .catch(() => undefined);
    return () => controller.abort();
  }, [api]);

  useEffect(
    () => () => {
      const operation = persistOperationRef.current;
      persistOperationRef.current = null;
      operation?.controller.abort();
    },
    [],
  );

  useEffect(() => {
    if (persisting) persistStatusRef.current?.focus();
  }, [persisting]);

  useEffect(() => {
    if (!menuOpen || active !== null) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setMenuOpen(false);
      helpRef.current?.focus();
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [active, menuOpen]);

  useEffect(() => {
    if (page === null || preferences === null || active !== null) return;
    const tour = GUIDANCE_TOURS[page];
    const saved = preferences.pages[page];
    if (
      saved?.contentVersion === tour.contentVersion ||
      openedAutomatically.current.has(page)
    )
      return;
    openedAutomatically.current.add(page);
    setMenuOpen(false);
    setStepIndex(0);
    setActive({ page, manual: false });
  }, [active, page, preferences]);

  const close = useCallback(() => {
    setActive(null);
    setStepIndex(0);
  }, []);

  const persistAndClose = useCallback(
    async (status: GuidanceStatus) => {
      if (active === null || preferences === null) return;
      if (active.manual) {
        close();
        return;
      }
      if (persistOperationRef.current !== null) return;
      const controller = new AbortController();
      const operation: PersistOperation = {
        controller,
      };
      persistOperationRef.current = operation;
      setPersisting(true);
      const isCurrent = () =>
        persistOperationRef.current === operation && !controller.signal.aborted;
      try {
        const saved = await api.put(
          {
            expectedRevision: preferences.revision,
            page: active.page,
            contentVersion: GUIDANCE_TOURS[active.page].contentVersion,
            status,
          },
          { signal: controller.signal },
        );
        if (!isCurrent()) return;
        setPreferences(saved);
      } catch {
        if (!isCurrent()) return;
        try {
          const refreshed = await api.get({ signal: controller.signal });
          if (!isCurrent()) return;
          setPreferences(refreshed);
        } catch {
          if (!isCurrent()) return;
          /* keep UI safe and close the one acknowledged action */
        }
      }
      if (!isCurrent()) return;
      persistOperationRef.current = null;
      setPersisting(false);
      close();
    },
    [active, api, close, preferences],
  );

  useEffect(() => {
    if (active === null) return;
    const step = GUIDANCE_TOURS[active.page].steps[stepIndex];
    let target: HTMLElement | null = null;
    const attachTarget = () => {
      if (step === undefined) return false;
      target = document.querySelector<HTMLElement>(step.target);
      if (target === null) return false;
      target.setAttribute('data-guidance-active', 'true');
      const placeAbove =
        target.getBoundingClientRect().top > window.innerHeight / 2;
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({
          block: placeAbove ? 'end' : 'start',
          inline: 'nearest',
        });
      }
      setPlacement(placeAbove ? 'top' : 'bottom');
      return true;
    };
    const observer = new MutationObserver(() => {
      if (attachTarget()) observer.disconnect();
    });
    if (!attachTarget()) {
      observer.observe(document.body, { childList: true, subtree: true });
    }
    primaryRef.current?.focus();
    return () => {
      observer.disconnect();
      target?.removeAttribute('data-guidance-active');
    };
  }, [active, stepIndex]);

  const tour = active === null ? null : GUIDANCE_TOURS[active.page];
  const step = tour?.steps[stepIndex];
  const isManual = active?.manual === true;
  return (
    <>
      <div className="guidance-help">
        <button
          ref={helpRef}
          type="button"
          aria-label="帮助"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((value) => !value)}
        >
          <span aria-hidden="true">?</span> 帮助
        </button>
        {menuOpen ? (
          <div className="guidance-help-menu" role="menu">
            {page === null ? (
              <span>当前页面暂无引导</span>
            ) : (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  setStepIndex(0);
                  setActive({ page, manual: true });
                }}
              >
                重新打开{GUIDANCE_TOURS[page].label}引导
              </button>
            )}
          </div>
        ) : null}
      </div>
      {tour !== null && step !== undefined ? (
        <ModalDialog
          backdropClassName="guidance-layer"
          initialFocusRef={primaryRef}
          returnFocusRef={helpRef}
          fallbackFocusRef={helpRef}
          onEscape={() =>
            isManual ? close() : void persistAndClose('dismissed')
          }
          className="guidance-dialog"
          data-placement={placement}
          aria-label={`${tour.label}快速引导`}
          aria-busy={persisting}
        >
          <section>
            <header>
              <span>
                {stepIndex + 1} / {tour.steps.length}
              </span>
              <strong>{step.title}</strong>
            </header>
            <p>{step.body}</p>
            <p className="guidance-expected">
              <strong>预期结果：</strong>
              {step.expectedResult}
            </p>
            <div className="guidance-actions">
              <button
                ref={primaryRef}
                type="button"
                disabled={persisting}
                onClick={() =>
                  stepIndex + 1 === tour.steps.length
                    ? void persistAndClose('completed')
                    : setStepIndex((value) => value + 1)
                }
              >
                {stepIndex + 1 === tour.steps.length ? '完成引导' : '下一步'}
              </button>
              <button
                type="button"
                className="secondary-action"
                disabled={persisting}
                onClick={() =>
                  isManual ? close() : void persistAndClose('dismissed')
                }
              >
                {isManual ? '关闭引导' : '跳过引导'}
              </button>
            </div>
            {persisting ? (
              <p ref={persistStatusRef} role="status" tabIndex={-1}>
                正在保存引导进度…
              </p>
            ) : null}
          </section>
        </ModalDialog>
      ) : null}
    </>
  );
}
