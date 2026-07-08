import type { AppIconName } from './AppIcon';

export type AppRoute = {
  readonly description: string;
  readonly icon: AppIconName;
  readonly label: string;
  readonly path: string;
  readonly release: `v${number}.${number}.${number}`;
  readonly summary: string;
  readonly title: string;
};

export const appRoutes = [
  {
    label: '行情',
    path: '/market',
    icon: 'market',
    title: '行情工作区',
    release: 'v0.2.0',
    summary: '搜索证券或选择股票池，查看本地缓存 K 线、成交量与来源证据。',
    description:
      '支持日线、周线和 60 分钟周期，以及不复权、前复权和后复权切换。',
  },
  {
    label: '自定义公式',
    path: '/formulas',
    icon: 'formulas',
    title: '自定义公式',
    release: 'v0.3.0',
    summary: '以表单和可视化反馈为主的通达信兼容公式工作台。',
    description:
      '从函数与模板库构建公式，保存不可变版本并预览主图、副图与买卖点。',
  },
  {
    label: '策略回测',
    path: '/backtests',
    icon: 'backtests',
    title: '策略回测',
    release: 'v0.4.0',
    summary: '从指标买卖点直接发起的可复现历史回测。',
    description: '五步配置日线、周线和 60 分钟回测，并持续查看运行进度。',
  },
  {
    label: '智能分析',
    path: '/analysis',
    icon: 'analysis',
    title: '智能分析',
    release: 'v0.5.0',
    summary: '面向国内 LLM 服务商的可审计分析工作流。',
    description: 'DeepSeek 等模型适配与人工确认流程将在智能分析阶段交付。',
  },
  {
    label: '任务中心',
    path: '/tasks',
    icon: 'tasks',
    title: '任务中心',
    release: 'v1.0.0',
    summary: '查看本地长任务的安全进度、生命周期与执行状态。',
    description:
      '筛选最近任务、查看股票池进度和安全事件，并取消排队或运行中的任务。',
  },
  {
    label: '设置',
    path: '/settings',
    icon: 'settings',
    title: '数据源设置',
    release: 'v0.2.0',
    summary: '配置市场数据源优先级、Tushare 凭证与本地通达信目录。',
    description: '诊断来源能力与权限缺口，并为不同数据类别设置独立回退顺序。',
  },
] as const satisfies readonly AppRoute[];
