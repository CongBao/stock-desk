export type AppRoute = {
  readonly description: string;
  readonly icon: string;
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
    icon: '市',
    title: '行情工作区',
    release: 'v0.2.0',
    summary: 'K 线主图与公式副图的工作区层级预览。',
    description: '市场数据和真实绘图能力将在行情阶段接入。',
  },
  {
    label: '自定义公式',
    path: '/formulas',
    icon: '式',
    title: '自定义公式',
    release: 'v0.3.0',
    summary: '以表单和可视化反馈为主的通达信兼容公式工作台。',
    description: '公式编辑、校验与信号预览将在公式阶段交付。',
  },
  {
    label: '策略回测',
    path: '/backtests',
    icon: '测',
    title: '策略回测',
    release: 'v0.4.0',
    summary: '从指标买卖点直接发起的可复现历史回测。',
    description: '日线、周线和 60 分钟回测将在回测阶段交付。',
  },
  {
    label: '智能分析',
    path: '/analysis',
    icon: '析',
    title: '智能分析',
    release: 'v0.5.0',
    summary: '面向国内 LLM 服务商的可审计分析工作流。',
    description: 'DeepSeek 等模型适配与人工确认流程将在智能分析阶段交付。',
  },
  {
    label: '任务中心',
    path: '/tasks',
    icon: '任',
    title: '任务中心',
    release: 'v0.1.0',
    summary: '查看本地长任务的生命周期与执行状态。',
    description: '基础任务状态将在本阶段后续任务接入，完整能力随版本推进。',
  },
  {
    label: '设置',
    path: '/settings',
    icon: '设',
    title: '设置',
    release: 'v0.1.0',
    summary: '管理本地应用基础配置与后续数据源连接。',
    description: '本阶段仅建立设置入口，不采集或展示任何密钥。',
  },
] as const satisfies readonly AppRoute[];
