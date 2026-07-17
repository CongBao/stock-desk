export const settingsResponse = {
  priorities: {
    daily_bars: ['tushare', 'akshare', 'baostock', 'tdx_local', 'eastmoney'],
    weekly_bars: ['tushare', 'akshare', 'baostock', 'eastmoney'],
    minute_bars: ['tushare', 'baostock', 'eastmoney'],
    instruments: ['tushare', 'akshare', 'baostock', 'eastmoney'],
    trading_calendar: ['tushare', 'baostock', 'eastmoney'],
    execution_status: ['tushare', 'baostock'],
    fundamentals: ['tushare', 'akshare'],
    announcements: ['tushare', 'akshare'],
    news: ['akshare'],
  },
  tdx_path: '/safe/vipdoc',
  tushare: {
    source: 'tushare',
    configured: true,
    secure_storage_available: true,
    masked_hint: 'ts-p•••••••3456',
  },
} as const;

export const diagnosticResponse = {
  source: 'tushare',
  status: 'permission_denied',
  capabilities: ['bars', 'execution_status', 'instruments', 'trading_calendar'],
  permissions: [
    { category: 'minute_bars', state: 'permission_denied' },
    { category: 'daily_bars', state: 'available' },
    { category: 'weekly_bars', state: 'available' },
    { category: 'instruments', state: 'available' },
    { category: 'trading_calendar', state: 'available' },
    { category: 'execution_status', state: 'available' },
  ],
  available_periods: ['1d', '1w'],
  markets: [],
  gaps: [
    {
      category: 'minute_bars',
      state: 'permission_denied',
      reason: 'permission_denied',
      detail: 'provider permission was denied',
    },
  ],
  last_checked: '2026-07-06T09:30:00Z',
  last_update: '2026-07-06T08:00:00Z',
  data_cutoff: '2026-07-05T16:00:00Z',
  fallback_reason: {
    reason: 'permission_denied',
    detail: 'provider permission was denied',
  },
} as const;
