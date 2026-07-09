from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import zlib

import pytest
import yaml

import scripts.verify_docs as verify_docs_module
from scripts.verify_docs import (
    main,
    verify_repository,
    verify_wiki,
)


EXPECTED_WIKI_PAGE_STEMS = (
    "Home",
    "Feature-Index",
    "Windows-Installation",
    "macOS-Installation",
    "First-Launch-and-Health",
    "Project-Governance-and-Release-Evidence",
    "Data-Sources-and-Tushare",
    "Local-TDX-Data",
    "Data-Updates-and-Provenance",
    "Stock-Pools",
    "Market-Charts",
    "Formula-Studio-Quickstart",
    "Formula-Compatibility-and-Errors",
    "Formula-Versions-and-Safety",
    "MACD-Backtest-Tutorial",
    "A-Share-Execution-and-Costs",
    "Backtest-Metrics-and-Reliability",
    "Backtest-Replay-Export-and-Failures",
    "Model-Provider-Setup",
    "Research-Reports-and-Evidence",
    "Research-Failures-Retries-and-Safety",
    "Task-Center",
    "Responsive-Navigation-and-Accessibility",
    "Credentials-Logs-and-Local-Security",
    "Backup-Restore-Upgrade-and-Uninstall",
    "Troubleshooting",
)

EXPECTED_REPLACED_WIKI_PAGES = (
    "Installation.md",
    "Market-Data-and-Charts.md",
    "Formula-Studio.md",
    "Backtesting.md",
    "Multi-Agent-Research.md",
    "Backup-and-Restore.md",
    "Configuration-and-Security.md",
)

EXPECTED_WIKI_FEATURE_BINDINGS = {
    "R-050": (
        "Credentials-Logs-and-Local-Security#适用场景",
        "Credentials-Logs-and-Local-Security-en#when-to-use-this",
        "适用场景 / When to use this",
        "local-security-settings",
        "app-route:/settings",
    ),
    "R-052": (
        "Project-Governance-and-Release-Evidence#需求边界与验收",
        "Project-Governance-and-Release-Evidence-en#requirements-boundary-and-acceptance",
        "需求边界与验收 / Requirements boundary and acceptance",
        "governance-requirements",
        "repository-audit:requirements-boundary",
    ),
    "R-056": (
        "Formula-Studio-Quickstart#适用场景",
        "Formula-Studio-Quickstart-en#when-to-use-this",
        "适用场景 / When to use this",
        "formula-studio-wide",
        "app-route:/formulas",
    ),
    "R-073": (
        "Project-Governance-and-Release-Evidence#交付与公开边界",
        "Project-Governance-and-Release-Evidence-en#delivery-and-public-boundary",
        "交付与公开边界 / Delivery and public boundary",
        "governance-documentation",
        "repository-audit:documentation-entry",
    ),
    "R-076": (
        "Project-Governance-and-Release-Evidence#发布验证",
        "Project-Governance-and-Release-Evidence-en#release-verification",
        "发布验证 / Release verification",
        "cross-platform-release-assets",
        "github-release:latest",
    ),
}

EXPECTED_WIKI_DOCUMENTATION_ENTRY_MARKERS = {
    "Project-Governance-and-Release-Evidence.md": (
        "README 提供精简的中英双语入口",
        "详细的中英双语 Wiki",
    ),
    "Project-Governance-and-Release-Evidence-en.md": (
        "README provides a concise bilingual entry point",
        "detailed bilingual Wiki",
    ),
}

EXPECTED_WIKI_APP_UI_LABELS = {
    "First-Launch-and-Health": (
        ("About", "关于"),
        ("Data source settings", "数据源设置"),
        ("Worker running", "Worker 运行中"),
        ("Worker not detected", "Worker 未检测"),
        ("Worker status unavailable", "Worker 状态不可用"),
        ("Worker: API offline", "Worker：API 离线"),
    ),
    "Data-Sources-and-Tushare": (
        ("Data source settings", "数据源设置"),
        ("Category priority", "分类优先级"),
        ("Tushare Token", "Tushare Token"),
        ("Save data source settings", "保存数据源设置"),
        ("Test Tushare connection", "测试 Tushare 连接"),
    ),
    "Local-TDX-Data": (
        ("Local TDX", "通达信本地"),
        ("TongdaXin vipdoc directory", "通达信 vipdoc 目录"),
        ("Save data source settings", "保存数据源设置"),
        ("Test Local TDX connection", "测试 通达信本地 连接"),
    ),
    "Data-Updates-and-Provenance": (
        ("Data update", "数据更新"),
        ("Current symbol", "当前证券"),
        ("Current stock pool", "当前股票池"),
        ("Start update", "启动更新"),
        ("Cancel update", "取消更新"),
        ("Save daily schedule", "保存每日计划"),
    ),
    "Stock-Pools": (
        ("Stock pools", "股票池"),
        ("New custom pool", "新建自定义池"),
        ("Create stock pool", "创建股票池"),
        ("Update instrument catalog", "更新证券目录"),
        ("Save stock pool", "保存股票池"),
        ("Delete stock pool", "删除股票池"),
        ("Pool creation failed; check members", "股票池创建失败，请检查成员。"),
        ("Pool save failed; check members", "股票池保存失败，请检查成员。"),
    ),
    "Market-Charts": (
        ("Market workspace", "行情工作区"),
        ("K-line period", "K 线周期"),
        ("Adjustment method", "复权方式"),
        ("Reset chart zoom", "重置图表缩放"),
        ("Reset view", "重置视图"),
        ("Formula Studio", "公式工作台"),
        ("Run preview", "运行预览"),
    ),
    "Formula-Studio-Quickstart": (
        ("Formula Studio", "公式工作台"),
        ("Custom formulas", "自定义公式"),
        ("Validate now", "立即校验"),
        ("Run preview", "运行预览"),
    ),
    "Formula-Compatibility-and-Errors": (
        ("Validate now", "立即校验"),
        ("Open saved formula", "打开已保存公式"),
    ),
    "Formula-Versions-and-Safety": (
        ("Formula version", "公式版本"),
        ("Run preview", "运行预览"),
    ),
    "MACD-Backtest-Tutorial": (
        ("Formula Studio", "公式工作台"),
        ("Strategy backtest", "策略回测"),
        ("Submit backtest", "提交回测"),
        ("Task Center", "任务中心"),
    ),
    "A-Share-Execution-and-Costs": (("Execution rules", "执行规则"),),
    "Backtest-Metrics-and-Reliability": (
        ("Backtest results", "回测结果"),
        ("Conclusion overview", "结论概览"),
    ),
    "Backtest-Replay-Export-and-Failures": (
        ("Pinned replay", "固定回放"),
        ("Export trades CSV", "导出交易 CSV"),
        ("Task Center", "任务中心"),
        ("Cancel task", "取消任务"),
    ),
    "Model-Provider-Setup": (
        ("Smart analysis", "智能分析"),
        ("Model settings", "模型设置"),
        ("Provider", "提供商"),
        ("Display name", "显示名称"),
        ("Base URL", "Base URL"),
        ("Model", "模型"),
        ("API Key", "API Key"),
        ("Save model configuration", "保存模型配置"),
        ("Test connection", "测试连接"),
        ("Verified", "已验证"),
        ("Error code", "错误代码"),
    ),
    "Research-Reports-and-Evidence": (
        ("Smart analysis", "智能分析"),
        ("Start smart analysis", "启动智能分析"),
        ("View evidence", "查看证据"),
    ),
    "Research-Failures-Retries-and-Safety": (
        ("Stage retry child run", "阶段重试子运行"),
    ),
    "Task-Center": (
        ("Task Center", "任务中心"),
        ("Status filter", "状态筛选"),
        ("Type filter", "类型筛选"),
        ("Open backtest report", "打开回测报告"),
        ("Security event timeline", "安全事件时间线"),
        ("Cancel task", "取消任务"),
    ),
    "Responsive-Navigation-and-Accessibility": (
        ("Expand primary navigation", "展开主导航"),
        ("Collapse primary navigation", "收起主导航"),
    ),
    "Credentials-Logs-and-Local-Security": (
        ("Data source settings", "数据源设置"),
        ("Save data source settings", "保存数据源设置"),
    ),
    "Troubleshooting": (
        ("Task Center", "任务中心"),
        ("Safe event timeline", "安全事件时间线"),
    ),
}

EXPECTED_WIKI_EXTERNAL_UI_LABELS = {
    "Project-Governance-and-Release-Evidence": (
        ("github", "Pull Requests", "拉取请求"),
        ("github", "Actions", "自动化"),
        ("github", "Releases", "发行版"),
    ),
    "Windows-Installation": (
        ("github", "Releases", "发行版"),
        ("windows", "Start menu", "“开始”菜单"),
    ),
    "macOS-Installation": (
        ("github", "Releases", "发行版"),
        ("macos", "About This Mac", "关于本机"),
        ("macos", "Applications", "“应用程序”"),
        ("macos", "Gatekeeper", "安全性检查"),
    ),
    "Backup-Restore-Upgrade-and-Uninstall": (
        ("windows", "Installed apps", "已安装的应用"),
        ("macos", "Applications", "“应用程序”"),
    ),
}

EXPECTED_WIKI_APP_UI_SOURCE_FILES = {
    "First-Launch-and-Health": ("web/src/app/App.tsx", "web/src/app/routes.ts"),
    "Data-Sources-and-Tushare": (
        "web/src/app/routes.ts",
        "web/src/features/settings/DataSourcesPage.tsx",
    ),
    "Local-TDX-Data": ("web/src/features/settings/DataSourcesPage.tsx",),
    "Data-Updates-and-Provenance": (
        "web/src/features/market/MarketOperationsPanel.tsx",
    ),
    "Stock-Pools": (
        "web/src/features/market/StockPoolPanel.tsx",
        "web/src/features/market/MarketOperationsPanel.tsx",
    ),
    "Market-Charts": (
        "web/src/features/market/MarketPage.tsx",
        "web/src/features/market/MarketChart.tsx",
        "web/src/app/routes.ts",
        "web/src/features/formulas/FormulaPreview.tsx",
    ),
    "Formula-Studio-Quickstart": (
        "web/src/app/routes.ts",
        "web/src/features/formulas/FormulaStudioPage.tsx",
        "web/src/features/formulas/FormulaPreview.tsx",
    ),
    "Formula-Compatibility-and-Errors": (
        "web/src/features/formulas/FormulaStudioPage.tsx",
    ),
    "Formula-Versions-and-Safety": (
        "web/src/features/formulas/FormulaStudioPage.tsx",
        "web/src/features/formulas/FormulaPreview.tsx",
    ),
    "MACD-Backtest-Tutorial": (
        "web/src/app/routes.ts",
        "web/src/features/backtests/BacktestWizard.tsx",
        "web/src/features/tasks/TaskCenterPage.tsx",
    ),
    "A-Share-Execution-and-Costs": ("web/src/features/backtests/steps/ReviewStep.tsx",),
    "Backtest-Metrics-and-Reliability": (
        "web/src/features/backtests/BacktestReportPage.tsx",
    ),
    "Backtest-Replay-Export-and-Failures": (
        "web/src/features/backtests/TradeTable.tsx",
        "web/src/features/backtests/BacktestReportPage.tsx",
        "web/src/features/tasks/TaskCenterPage.tsx",
    ),
    "Model-Provider-Setup": (
        "web/src/app/routes.ts",
        "web/src/features/analysis/ModelSettings.tsx",
    ),
    "Research-Reports-and-Evidence": (
        "web/src/app/routes.ts",
        "web/src/features/analysis/AnalysisRunPanel.tsx",
        "web/src/features/analysis/AnalysisPage.tsx",
    ),
    "Research-Failures-Retries-and-Safety": (
        "web/src/features/analysis/ProcessRail.tsx",
    ),
    "Task-Center": (
        "web/src/app/routes.ts",
        "web/src/features/tasks/TaskCenterPage.tsx",
    ),
    "Responsive-Navigation-and-Accessibility": ("web/src/app/App.tsx",),
    "Credentials-Logs-and-Local-Security": (
        "web/src/app/routes.ts",
        "web/src/features/settings/DataSourcesPage.tsx",
    ),
    "Troubleshooting": (
        "web/src/app/routes.ts",
        "web/src/features/tasks/TaskCenterPage.tsx",
    ),
}

EXPECTED_WIKI_WORKFLOW_CONTENT = {
    "First-Launch-and-Health.md": (
        (
            "Worker 运行中",
            "Worker 未检测",
            "Worker 状态不可用",
            "Worker：API 离线",
            "API 正常且 Worker 运行中",
        ),
        (),
    ),
    "First-Launch-and-Health-en.md": (
        (
            "Worker running（Worker 运行中）",
            "Worker not detected（Worker 未检测）",
            "Worker status unavailable（Worker 状态不可用）",
            "Worker: API offline（Worker：API 离线）",
            "API is healthy and Worker is running",
        ),
        (),
    ),
    "Data-Sources-and-Tushare.md": (
        (
            "九类数据分别维护优先级",
            "日线行情、周线行情、60 分钟行情、证券目录、交易日历、回测执行状态、基本面、公告和新闻",
            "Tushare Token 不会从服务端回填到浏览器",
            "配置已变更，请重新检测",
            "同一段行情不会跨来源拼接",
            "Eastmoney 当前适配器尚未交付",
        ),
        ("自动拼接行情", "Token 会回填", "Eastmoney 已可用"),
    ),
    "Data-Sources-and-Tushare-en.md": (
        (
            "nine data categories keep independent priority orders",
            "daily bars, weekly bars, 60-minute bars, instruments, trading calendar, execution status, fundamentals, announcements, and news",
            "The Tushare token is never read back into the browser",
            "configuration changed; test again",
            "One bar segment is never spliced across providers",
            "Eastmoney adapter is not delivered",
        ),
        ("automatically splices bars", "token is read back", "Eastmoney is available"),
    ),
    "Local-TDX-Data.md": (
        (
            "通达信 vipdoc 目录",
            "测试 通达信本地 连接",
            "只支持通达信日线 `.day` 文件",
            "`sh/lday`",
            "`sz/lday`",
            "不支持周线或 60 分钟文件",
            "清空路径并保存",
            "绝对路径会由本地设置 API 返回并回填到本机设置页",
            "路径只在本机可见",
            "诊断错误、任务日志和来源证据不得包含路径",
            "公开截图必须完整遮蔽",
        ),
        (
            "目录选择器",
            "目录校验",
            "启用备用源",
            "路径不会由 API 返回",
            "设置页不会回填路径",
        ),
    ),
    "Local-TDX-Data-en.md": (
        (
            "TongdaXin vipdoc directory（通达信 vipdoc 目录）",
            "Test Local TDX connection（测试 通达信本地 连接）",
            "supports only TongdaXin daily `.day` files",
            "`sh/lday`",
            "`sz/lday`",
            "does not support weekly or 60-minute files",
            "clear the path and save",
            "the absolute path is returned by the local settings API and filled back into the local settings page",
            "the path is visible only on the local machine",
            "Diagnostic errors, task logs, and provenance must not contain the path",
            "public screenshots must fully redact it",
        ),
        (
            "directory picker",
            "directory validation",
            "enable the fallback",
            "path is never returned by the API",
            "settings page never fills the path",
        ),
    ),
    "Data-Updates-and-Provenance.md": (
        (
            "当前证券",
            "当前股票池",
            "开始日期",
            "结束日期",
            "启动更新",
            "逐证券更新结果最多显示前 100 项",
            "保存每日计划",
            "图表始终只读本地缓存",
            "成功后在右侧“数据来源”面板核对",
            "数据版本和路由尝试",
            "修复原因后重新提交",
        ),
        ("预检按钮", "选择数据类别", "任务列表直接显示提供方"),
    ),
    "Data-Updates-and-Provenance-en.md": (
        (
            "Current symbol（当前证券）",
            "Current stock pool（当前股票池）",
            "start date",
            "end date",
            "Start update（启动更新）",
            "per-symbol result list shows at most the first 100 items",
            "Save daily schedule（保存每日计划）",
            "Charts always read only local cache",
            "after success, inspect the Data provenance panel on the right",
            "dataset version and routing attempts",
            "Resubmit after fixing the reason",
        ),
        (
            "preflight button",
            "select data categories",
            "task list directly shows provider",
        ),
    ),
    "Stock-Pools.md": (
        (
            "全 A、指数和行业预设",
            "更新证券目录",
            "新建自定义池",
            "创建股票池",
            "编辑当前股票池",
            "保存股票池",
            "删除股票池",
            "使用上移、下移和移除按钮维护顺序",
            "自定义池最多 5,000 只证券",
            "普通 UI 在通用提示后最多附加 20 个 `#序号 issue-code`",
            "高级 API 的成员级响应返回完整 `issues` 数组，不受 20 项 UI 显示上限限制",
            "超过 5,000 只返回 `code=invalid_request` 和空列表 `issues: []`",
            "不包含成员位置，也不是成员级 issues",
            "缩减列表到 5,000 只以内后重试",
            "股票池创建失败，请检查成员。",
            "股票池保存失败，请检查成员。",
            "### 高级：API 诊断",
            "普通 UI 不显示 `code` 或 `issues` 字段",
            "股票池回测",
            "修正失败成员后再次保存",
        ),
        ("拖动排序", "股票池会共享资金", "不返回 issues"),
    ),
    "Stock-Pools-en.md": (
        (
            "all-A, index, and industry presets",
            "Update instrument catalog（更新证券目录）",
            "New custom pool（新建自定义池）",
            "Create stock pool（创建股票池）",
            "edit the current custom pool",
            "Save stock pool（保存股票池）",
            "Delete stock pool（删除股票池）",
            "use the move-up, move-down, and remove buttons",
            "A custom pool is capped at 5,000 symbols",
            "the generic message is followed by at most 20 `#ordinal issue-code` entries",
            "The advanced member-level API response returns the complete `issues` array without the UI's 20-item display limit",
            "More than 5,000 symbols returns `code=invalid_request` with an empty `issues: []` list",
            "It contains no member positions or member-level issues",
            "reduce the list to at most 5,000 symbols and retry",
            "Pool creation failed; check members（股票池创建失败，请检查成员。）",
            "Pool save failed; check members（股票池保存失败，请检查成员。）",
            "### Advanced: API diagnostics",
            "The normal UI does not display `code` or `issues` fields",
            "pool backtest",
            "save again after correcting failed members",
        ),
        (
            "drag to reorder",
            "shared-capital portfolio",
            "does not return issues",
            "no issues array",
        ),
    ),
    "Market-Charts.md": (
        (
            "行情工作区只读本地缓存",
            "日线、周线和 60 分钟",
            "不复权、前复权和后复权",
            "十字光标查看 OHLCV",
            "滚轮/双指缩放",
            "拖动平移",
            "重置视图",
            "公式图层需在公式工作台运行预览",
            "K 线主图与公式副图",
            "BUY 买点",
            "SELL 卖点",
            "视口不超过 1200px 时主导航自动收起",
            "1100px 以下",
            "900px 以下",
            "图标导航轨",
            "market-daily-narrow",
        ),
        ("行情工作区选择公式", "浏览图表会自动下载", "文字缩写导航"),
    ),
    "Market-Charts-en.md": (
        (
            "Market workspace（行情工作区） reads only local cache",
            "daily, weekly, and 60-minute",
            "no adjustment, qfq, and hfq",
            "crosshair to read OHLCV",
            "wheel or pinch to zoom",
            "drag to pan",
            "Reset view（重置视图）",
            "Formula layers require Run preview（运行预览） in Formula Studio（公式工作台）",
            "K-line main chart and formula subchart",
            "BUY 买点",
            "SELL 卖点",
            "At or below 1200px, primary navigation collapses automatically",
            "Below 1100px",
            "below 900px",
            "icon navigation rail",
            "market-daily-narrow",
        ),
        (
            "choose a formula in Market workspace",
            "chart browsing downloads data",
            "text-abbreviation rail",
        ),
    ),
    "Formula-Studio-Quickstart.md": (
        ("K 线主图与公式副图", "BUY 买点", "SELL 卖点", "运行预览"),
        (),
    ),
    "Formula-Studio-Quickstart-en.md": (
        (
            "K-line main chart and formula subchart",
            "BUY 买点",
            "SELL 卖点",
            "Run preview（运行预览）",
        ),
        (),
    ),
    "Model-Provider-Setup.md": (
        ("提供商", "Base URL", "模型", "API Key", "已验证", "错误代码"),
        ("重试次数", "重试延迟"),
    ),
    "Model-Provider-Setup-en.md": (
        (
            "Provider（提供商）",
            "Base URL（Base URL）",
            "Model（模型）",
            "API Key（API Key）",
            "Verified（已验证）",
            "Error code（错误代码）",
        ),
        ("retry count", "retry delay"),
    ),
    "Task-Center.md": (
        (
            "状态筛选",
            "类型筛选",
            "安全任务摘要",
            "安全事件时间线",
            "取消任务",
            "安全事件时间线只显示可见的审计事件，不是运行日志",
            "回测任务使用回测报告深链",
            "其他任务只显示安全摘要和状态",
            "响应包含 `backtest_run` target 时就显示回测报告链接，任务仍在运行时也可以显示",
            "其他不含该 target 的任务不显示此链接",
        ),
        (
            "时间筛选",
            "逐项结果",
            "通用日志",
            "数据分析深链",
            "没有日志控件",
            "仅已完成",
        ),
    ),
    "Task-Center-en.md": (
        (
            "Status filter（状态筛选）",
            "Type filter（类型筛选）",
            "safe task summary",
            "Security event timeline（安全事件时间线）",
            "Open backtest report（打开回测报告）",
            "Cancel task（取消任务）",
            "visible audit events rather than runtime logs",
            "Backtest tasks use the backtest-report deep link",
            "Other task types show only their safe summary and status",
            "The backtest report link appears whenever the response contains a `backtest_run` target, including while the task is still running",
            "Other tasks without that target do not show the link",
        ),
        (
            "time filter",
            "item results",
            "generic logs",
            "data or analysis deep link",
            "no log control",
            "completed backtest targets",
            "only completed",
        ),
    ),
}

EXPECTED_WIKI_LOW_CODE_SECTION_FORBIDDEN = {
    "Stock-Pools.md": (
        ("操作步骤", "预期结果"),
        ("`code", "`issues"),
    ),
    "Stock-Pools-en.md": (
        ("Steps", "Expected result"),
        ("`code", "`issues"),
    ),
}

EXPECTED_WIKI_LOW_CODE_SECTION_REQUIRED = {
    "Stock-Pools.md": {
        "操作步骤": (
            "通用提示后最多附加 20 个 `#序号 issue-code`",
            "逐项修正",
            "整体超限仍只有通用提示，不附加成员条目",
        ),
        "预期结果": (
            "成员级校验最多显示 20 个成员条目",
            "整体超限不显示成员条目",
        ),
    },
    "Stock-Pools-en.md": {
        "Steps": (
            "the generic message is followed by at most 20 `#ordinal issue-code` entries",
            "correct each displayed member",
            "A whole-request limit failure keeps only the generic message and appends no member entries",
        ),
        "Expected result": (
            "member-level validation displays at most 20 member entries",
            "a whole-request limit failure displays no member entries",
        ),
    },
}

EXPECTED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS = {
    "Data-Sources-and-Tushare.md": (
        (
            "九类数据分别维护优先级",
            "web/src/features/settings/sourceSettingsApi.ts",
            "sourceCategories = [",
        ),
        (
            "Eastmoney 当前适配器尚未交付",
            "web/src/features/settings/DataSourcesPage.tsx",
            "当前适配器尚未交付",
        ),
    ),
    "Data-Sources-and-Tushare-en.md": (
        (
            "nine data categories keep independent priority orders",
            "web/src/features/settings/sourceSettingsApi.ts",
            "sourceCategories = [",
        ),
        (
            "Eastmoney adapter is not delivered",
            "web/src/features/settings/DataSourcesPage.tsx",
            "当前适配器尚未交付",
        ),
    ),
    "Local-TDX-Data.md": (
        (
            "只支持通达信日线 `.day` 文件",
            "src/stock_desk/market/providers/tdx_local.py",
            "_TDX_FILE_PATTERNS",
        ),
        (
            "清空路径并保存",
            "web/src/features/settings/DataSourcesPage.tsx",
            "tdxPath.length > 0 ? tdxPath : null",
        ),
        (
            "绝对路径会由本地设置 API 返回并回填到本机设置页",
            "web/src/features/settings/DataSourcesPage.tsx",
            "setTdxPath(value.tdx_path ?? '')",
        ),
    ),
    "Local-TDX-Data-en.md": (
        (
            "supports only TongdaXin daily `.day` files",
            "src/stock_desk/market/providers/tdx_local.py",
            "_TDX_FILE_PATTERNS",
        ),
        (
            "clear the path and save",
            "web/src/features/settings/DataSourcesPage.tsx",
            "tdxPath.length > 0 ? tdxPath : null",
        ),
        (
            "the absolute path is returned by the local settings API and filled back into the local settings page",
            "web/src/features/settings/DataSourcesPage.tsx",
            "setTdxPath(value.tdx_path ?? '')",
        ),
    ),
    "Data-Updates-and-Provenance.md": (
        (
            "逐证券更新结果最多显示前 100 项",
            "web/src/features/market/MarketOperationsPanel.tsx",
            "items.data.slice(0, 100)",
        ),
        (
            "成功后在右侧“数据来源”面板核对",
            "web/src/features/market/MarketPage.tsx",
            "<ProvenancePanel data={bars.data} />",
        ),
    ),
    "Data-Updates-and-Provenance-en.md": (
        (
            "per-symbol result list shows at most the first 100 items",
            "web/src/features/market/MarketOperationsPanel.tsx",
            "items.data.slice(0, 100)",
        ),
        (
            "after success, inspect the Data provenance panel on the right",
            "web/src/features/market/MarketPage.tsx",
            "<ProvenancePanel data={bars.data} />",
        ),
    ),
    "Stock-Pools.md": (
        (
            "自定义池最多 5,000 只证券",
            "src/stock_desk/market/pools.py",
            "MAX_CUSTOM_MEMBERS = 5_000",
        ),
        (
            "使用上移、下移和移除按钮维护顺序",
            "web/src/features/market/MarketOperationsPanel.tsx",
            "上移 ${symbol}",
        ),
        (
            "普通 UI 在通用提示后最多附加 20 个 `#序号 issue-code`",
            "web/src/features/market/MarketOperationsPanel.tsx",
            ".slice(0, 20)",
        ),
        (
            "高级 API 的成员级响应返回完整 `issues` 数组，不受 20 项 UI 显示上限限制",
            "src/stock_desk/api/market.py",
            "for issue in error.issues",
        ),
    ),
    "Stock-Pools-en.md": (
        (
            "A custom pool is capped at 5,000 symbols",
            "src/stock_desk/market/pools.py",
            "MAX_CUSTOM_MEMBERS = 5_000",
        ),
        (
            "use the move-up, move-down, and remove buttons",
            "web/src/features/market/MarketOperationsPanel.tsx",
            "上移 ${symbol}",
        ),
        (
            "the generic message is followed by at most 20 `#ordinal issue-code` entries",
            "web/src/features/market/MarketOperationsPanel.tsx",
            ".slice(0, 20)",
        ),
        (
            "The advanced member-level API response returns the complete `issues` array without the UI's 20-item display limit",
            "src/stock_desk/api/market.py",
            "for issue in error.issues",
        ),
    ),
    "Market-Charts.md": (
        (
            "公式图层需在公式工作台运行预览",
            "web/src/features/formulas/FormulaPreview.tsx",
            "formula={formulaLayer}",
        ),
        (
            "视口不超过 1200px 时主导航自动收起",
            "web/src/app/App.tsx",
            "window.matchMedia('(max-width: 1200px)')",
        ),
    ),
    "Market-Charts-en.md": (
        (
            "Formula layers require Run preview（运行预览） in Formula Studio（公式工作台）",
            "web/src/features/formulas/FormulaPreview.tsx",
            "formula={formulaLayer}",
        ),
        (
            "At or below 1200px, primary navigation collapses automatically",
            "web/src/app/App.tsx",
            "window.matchMedia('(max-width: 1200px)')",
        ),
    ),
}


REPOSITORY_DOCUMENTS = {
    "README.md": """[English](README.en.md)

# Stock Desk

## 产品定位

本地优先的个人 A 股研究工作台。

## 核心功能

使用任务中心、行情图表、公式工作室、回测和研究功能。

## 下载安装

从 https://github.com/CongBao/stock-desk/releases/latest 选择无需源码的
`stock-desk-<version>-windows-x86_64.exe`、
`stock-desk-<version>-macos-x86_64.dmg` 或
`stock-desk-<version>-macos-arm64.dmg` 安装包。

## 使用文档

参阅[配置](docs/configuration.md)和[免责声明](docs/disclaimer.md)。

## 安全与范围

仅供研究，不连接实盘交易。
""",
    "README.en.md": """[简体中文](README.md)

# Stock Desk

## Product positioning

A local-first personal A-share research desk.

## Core features

Use the task center, market charts, Formula Studio, backtesting, and research.

## Download and install

Choose a source-free installer from
https://github.com/CongBao/stock-desk/releases/latest:
`stock-desk-<version>-windows-x86_64.exe`,
`stock-desk-<version>-macos-x86_64.dmg`, or
`stock-desk-<version>-macos-arm64.dmg`.

## Documentation

See [configuration](docs/configuration.md) and the [disclaimer](docs/disclaimer.md).

## Safety and scope

Research only; no live trading.
""",
    "CONTRIBUTING.md": """# Contributing

## Development setup

```bash
make bootstrap
```

## Quality gates

```bash
make test
```

## Pull requests

Keep changes focused.
""",
    "SUPPORT.md": """# Support

## Questions

Open a discussion.

## Bug reports

Include diagnostics.

## Security

Follow SECURITY.md.
""",
    "CHANGELOG.md": """# Changelog

## Unreleased

Documentation improvements.

## 0.5.0

Multi-agent research.
""",
    "ROADMAP.md": """# Roadmap

## Released

Stages 0 through 4.

## Planned

Release readiness.
""",
    "docs/architecture.md": """# Architecture

## Deployment model

Local API and worker.

### Native installer topology

The parent launcher creates API and worker children on a random 127.0.0.1 port.

### Source development topology

Supervised source processes.

### Container topology

Separate API and worker containers.

The native packaged runtime does not self-mutate, but its user-writable install location can be changed by the user.

## Modules and boundaries

Ports and adapters.

## Data and storage

Local data directory.

## Trust and security

Localhost by default.
""",
    "docs/backup-and-restore.md": """# Backup, restore, upgrade, and rollback

## Deployment support

Record the Compose image digest, immutable source commit, or exact macOS installer artifact.

## Upgrade and rollback procedure

Restore each deployment with its recorded identity.
""",
    "docs/configuration.md": """# Configuration

## Native installers

Windows uses `%LOCALAPPDATA%\\stock-desk`; macOS uses
`~/Library/Application Support/stock-desk`; the key is `config/master.key`.

## Source development

Use a local `.env` and explicit master key.

## Native development

Use the sample environment file.

## Container deployment

Use Docker Compose.

## Application settings

`STOCK_DESK_APP_NAME`, `STOCK_DESK_DATA_DIR`, `STOCK_DESK_DATABASE_URL`,
`STOCK_DESK_MASTER_KEY`, and `STOCK_DESK_WEB_DIST_DIR`.

## Container settings

`STOCK_DESK_UID`, `STOCK_DESK_GID`, `STOCK_DESK_IMAGE`, and
`STOCK_DESK_TDX_HOST_PATH`.

## Provider credentials

Store provider keys locally.
""",
    "docs/troubleshooting.md": """# Troubleshooting

## Startup and health

Inspect the health endpoint.

## Data and charts

Check the configured data source.

## Tasks and workers

Inspect task diagnostics.

## Model providers

Test credentials locally.

## Backup and restore

Restore into an empty data directory.
""",
    "docs/disclaimer.md": """# Disclaimer

## Research use only

This software is for research, not investment advice or live trading.

## Data limitations

Data may be delayed or incomplete.

## Model limitations

Generated output may be inaccurate.

## User responsibility

Verify all results independently.
""",
}


def _write_repository(root: Path) -> None:
    for relative_path, content in REPOSITORY_DOCUMENTS.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".env.example").write_text(
        "\n".join(
            (
                "STOCK_DESK_APP_NAME=Stock Desk",
                "STOCK_DESK_DATA_DIR=./.data",
                "STOCK_DESK_DATABASE_URL=sqlite:///stock-desk.db",
                "STOCK_DESK_MASTER_KEY=",
                "STOCK_DESK_UID=1000",
                "STOCK_DESK_GID=1000",
                "STOCK_DESK_IMAGE=stock-desk:local",
                "STOCK_DESK_TDX_HOST_PATH=./.data/tdx",
            )
        ),
        encoding="utf-8",
    )
    (root / "Makefile").write_text(
        ("bootstrap:\n\t@true\ndev:\n\t@true\ntest:\n\t@true\nacceptance:\n\t@true\n"),
        encoding="utf-8",
    )


def _planned_screenshot_id(stem: str) -> str:
    return "planned-home" if stem == "Home" else f"planned-{stem.casefold()}"


def _wiki_fixture_surface(stem: str) -> tuple[str, str]:
    if stem in {
        "Home",
        "First-Launch-and-Health",
        "Stock-Pools",
        "Market-Charts",
        "Responsive-Navigation-and-Accessibility",
    }:
        return "app-route", "/market"
    if stem in {
        "Data-Sources-and-Tushare",
        "Local-TDX-Data",
        "Data-Updates-and-Provenance",
        "Credentials-Logs-and-Local-Security",
    }:
        return "app-route", "/settings"
    if stem.startswith("Formula-"):
        return "app-route", "/formulas"
    if stem.startswith(("MACD-", "A-Share-", "Backtest-")):
        return "app-route", "/backtests"
    if stem.startswith(("Model-", "Research-")):
        return "app-route", "/analysis"
    if stem in {"Task-Center", "Troubleshooting"}:
        return "app-route", "/tasks"
    return "wiki-page", stem


def _write_wiki(root: Path) -> None:
    article_stems = EXPECTED_WIKI_PAGE_STEMS[2:]
    assignments = {stem: [] for stem in EXPECTED_WIKI_PAGE_STEMS}
    assignments["Home"].append("R-079")
    distributable_stems = EXPECTED_WIKI_PAGE_STEMS[1:]
    for number in range(1, 79):
        stem = distributable_stems[(number - 1) % len(distributable_stems)]
        assignments[stem].append(f"R-{number:03d}")
    for requirement_id in EXPECTED_WIKI_FEATURE_BINDINGS:
        for requirement_ids in assignments.values():
            if requirement_id in requirement_ids:
                requirement_ids.remove(requirement_id)
    requirement_stems = {
        requirement_id: stem
        for stem, requirement_ids in assignments.items()
        for requirement_id in requirement_ids
    }
    rows: list[str] = []
    for number in range(1, 80):
        requirement_id = f"R-{number:03d}"
        if requirement_id in EXPECTED_WIKI_FEATURE_BINDINGS:
            (
                chinese_target,
                english_target,
                section,
                screenshot_id,
                surface,
            ) = EXPECTED_WIKI_FEATURE_BINDINGS[requirement_id]
            rows.append(
                f"| {requirement_id} | [\u4e2d\u6587\u9875\u9762]({chinese_target}) | "
                f"[English page]({english_target}) | {section} | "
                f"`{screenshot_id}` | `{surface}` |"
            )
            continue
        stem = requirement_stems[requirement_id]
        if stem == "Home":
            chinese_link = (
                "[\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb)"
            )
            english_link = "[English home](Home-en#start-here)"
            section = "\u4ece\u8fd9\u91cc\u5f00\u59cb / Start here"
        elif stem == "Feature-Index":
            chinese_link = "[\u529f\u80fd\u7d22\u5f15](Feature-Index#\u9700\u6c42\u5230\u9875\u9762)"
            english_link = "[Feature index](Feature-Index-en#requirements-to-pages)"
            section = "\u9700\u6c42\u5230\u9875\u9762 / Requirements to pages"
        else:
            chinese_link = (
                f"[\u4e2d\u6587\u9875\u9762]({stem}#\u9002\u7528\u573a\u666f)"
            )
            english_link = f"[English page]({stem}-en#when-to-use-this)"
            section = "\u9002\u7528\u573a\u666f / When to use this"
        surface_type, locator = _wiki_fixture_surface(stem)
        rows.append(
            f"| {requirement_id} | {chinese_link} | {english_link} | {section} | "
            f"`{_planned_screenshot_id(stem)}` | `{surface_type}:{locator}` |"
        )
    feature_rows = "\n".join(rows)
    semantic_evidence_by_stem: dict[str, list[str]] = {}
    for binding in EXPECTED_WIKI_FEATURE_BINDINGS.values():
        stem = binding[0].partition("#")[0]
        semantic_evidence_by_stem.setdefault(stem, []).append(binding[3])
    for stem in EXPECTED_WIKI_PAGE_STEMS:
        if stem == "Home":
            chinese = """# Stock Desk 使用手册

[English](Home-en)

## 从这里开始

参阅功能指南。
"""
            english = """# Stock Desk User Guide

[简体中文](Home)

## Start here

See the feature guides.
"""
        elif stem == "Feature-Index":
            chinese = f"""# \u529f\u80fd\u7d22\u5f15

[English](Feature-Index-en)

## \u9700\u6c42\u5230\u9875\u9762

| \u529f\u80fd/\u9700\u6c42 | \u4e2d\u6587\u9875\u9762 | English page | \u7ae0\u8282 | \u622a\u56fe ID | \u8def\u7531/\u754c\u9762 |
| --- | --- | --- | --- | --- | --- |
{feature_rows}
"""
            english = f"""# Feature index

[\u7b80\u4f53\u4e2d\u6587](Feature-Index)

## Requirements to pages

| Feature/requirement | Chinese page | English page | Section | Screenshot ID | Route/surface |
| --- | --- | --- | --- | --- | --- |
{feature_rows}
"""
        else:
            position = article_stems.index(stem)
            previous_stem = "Home" if position == 0 else article_stems[position - 1]
            next_stem = (
                "Home"
                if position == len(article_stems) - 1
                else article_stems[position + 1]
            )
            screenshot_ids = [
                _planned_screenshot_id(stem),
                *semantic_evidence_by_stem.get(stem, []),
            ]
            screenshot_text = "、".join(f"`{item}`" for item in screenshot_ids)
            screenshot_text_en = ", ".join(f"`{item}`" for item in screenshot_ids)
            app_labels = verify_docs_module.REQUIRED_WIKI_APP_UI_LABELS.get(stem)
            external_labels = verify_docs_module.REQUIRED_WIKI_EXTERNAL_UI_LABELS.get(
                stem
            )
            ui_labels = (
                app_labels
                if app_labels is not None
                else tuple(
                    (english, chinese)
                    for _kind, english, chinese in external_labels or ()
                )
            )
            english_title = stem.replace("-", " ")
            for english_label, chinese_label in ui_labels:
                if english_label not in english_title:
                    continue
                english_title = english_title.replace(
                    english_label,
                    f"{english_label}（{chinese_label}）",
                    1,
                )
            glossary = "\n".join(
                f"{index}. `{english}（{chinese}）` — This visible label is used by the workflow."
                for index, (english, chinese) in enumerate(ui_labels, 1)
            )
            ui_step_references = ", ".join(
                f"**{english}（{chinese}）**" for english, chinese in ui_labels
            )
            semantic_chinese_sections = ""
            semantic_english_sections = ""
            if stem == "Project-Governance-and-Release-Evidence":
                semantic_chinese_sections = """
## 需求边界与验收

验收范围可核对。

## 发布验证

发行证据可核对。

## 交付与公开边界

README 提供精简的中英双语入口，详细的中英双语 Wiki 保留完整操作说明。
"""
                semantic_english_sections = """
## Requirements boundary and acceptance

The acceptance scope is auditable.

## Release verification

Release evidence is auditable.

## Delivery and public boundary

The README provides a concise bilingual entry point, while the detailed bilingual Wiki preserves complete operating guidance.
"""
            chinese = f"""# {stem.replace("-", " ")}

[English]({stem}-en) · [功能索引](Feature-Index) · [首页](Home)

## 适用场景

完成这一功能工作流。

## 使用前

确认应用健康且所需输入可用。

## 操作步骤

1. 打开 Stock Desk。
2. 完成工作流。

## 预期结果

结果可见。

## 截图

截图证据 ID：{screenshot_text}。证据状态以截图清单为准。

{semantic_chinese_sections}
## 常见问题

输入缺失时先补齐输入。

## 恢复方法

返回任务中心后重试。

[上一页]({previous_stem}) · [下一页]({next_stem})
"""
            english = f"""# {english_title}

[简体中文]({stem}) · [Feature index](Feature-Index-en) · [Home](Home-en)

## When to use this

Complete this feature workflow.

## Before you start

Confirm that the application is healthy and the required inputs are available.

## Chinese UI labels

{glossary}

## Steps

1. Open Stock Desk.
2. Complete the workflow.
3. Use {ui_step_references}; examples use `sample-value` and `/tmp/example`.

## Expected result

The result is visible.

## Screenshot

Screenshot evidence ID: {screenshot_text_en}. The evidence state is tracked in the screenshot manifest.

{semantic_english_sections}
## Common problems

Supply missing inputs before trying again.

## Recovery

Return to the task center and retry.

[Previous]({previous_stem}-en) · [Next]({next_stem}-en)
"""
            chinese_contract = EXPECTED_WIKI_WORKFLOW_CONTENT.get(f"{stem}.md")
            if chinese_contract is not None:
                chinese += "\n" + "；".join(chinese_contract[0]) + "\n"
            english_contract = EXPECTED_WIKI_WORKFLOW_CONTENT.get(f"{stem}-en.md")
            if english_contract is not None:
                english += "\n" + "; ".join(english_contract[0]) + "\n"
            for heading, markers in EXPECTED_WIKI_LOW_CODE_SECTION_REQUIRED.get(
                f"{stem}.md", {}
            ).items():
                marker = f"## {heading}\n"
                chinese = chinese.replace(
                    marker,
                    marker + "\n" + "；".join(markers) + "\n",
                    1,
                )
            for heading, markers in EXPECTED_WIKI_LOW_CODE_SECTION_REQUIRED.get(
                f"{stem}-en.md", {}
            ).items():
                marker = f"## {heading}\n"
                english = english.replace(
                    marker,
                    marker + "\n" + "; ".join(markers) + "\n",
                    1,
                )
        (root / f"{stem}.md").write_text(chinese, encoding="utf-8")
        (root / f"{stem}-en.md").write_text(english, encoding="utf-8")
    chinese_navigation = "\n".join(
        f"- [{stem}]({stem})" for stem in EXPECTED_WIKI_PAGE_STEMS
    )
    english_navigation = "\n".join(
        f"- [{stem}]({stem}-en)" for stem in EXPECTED_WIKI_PAGE_STEMS
    )
    (root / "_Sidebar.md").write_text(
        f"[English](Home-en)\n\n{chinese_navigation}\n", encoding="utf-8"
    )
    (root / "_Sidebar-en.md").write_text(
        f"[简体中文](Home)\n\n{english_navigation}\n", encoding="utf-8"
    )
    entries: list[str] = []
    for stem in EXPECTED_WIKI_PAGE_STEMS:
        screenshot_id = _planned_screenshot_id(stem)
        surface_type, locator = _wiki_fixture_surface(stem)
        contains_market_data = (
            surface_type == "app-route"
            and locator in {"/market", "/formulas", "/backtests"}
            or verify_docs_module._manifest_market_page([f"{stem}.md", f"{stem}-en.md"])
        )
        entries.append(
            f"""  - screenshot_id: {screenshot_id}
    path: images/{screenshot_id}.png
    page_pairs: [{stem}.md, {stem}-en.md]
    caption_locales: {{zh-CN: \u8ba1\u5212\u8bc1\u636e, en: Planned evidence}}
    features: [{", ".join(assignments[stem])}]
    surface: {{type: {surface_type}, locator: {locator}}}
    contains_market_data: {str(contains_market_data).lower()}
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"""
        )
    for requirement_id, binding in EXPECTED_WIKI_FEATURE_BINDINGS.items():
        chinese_target, english_target, _section, screenshot_id, surface = binding
        chinese_page = chinese_target.partition("#")[0]
        english_page = english_target.partition("#")[0]
        surface_type, separator, locator = surface.partition(":")
        assert separator
        contains_market_data = surface_type == "app-route" and locator in {
            "/market",
            "/formulas",
            "/backtests",
        }
        entries.append(
            f"""  - screenshot_id: {screenshot_id}
    path: images/{screenshot_id}.png
    page_pairs: [{chinese_page}.md, {english_page}.md]
    caption_locales: {{zh-CN: \u8bed\u4e49\u8bc1\u636e, en: Semantic evidence}}
    features: [{requirement_id}]
    surface: {{type: {surface_type}, locator: {locator}}}
    contains_market_data: {str(contains_market_data).lower()}
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"""
        )
    (root / "SCREENSHOT-MANIFEST.yml").write_text(
        """schema_version: stock-desk-documentation-screenshots-v1
screenshots:
"""
        + "\n".join(entries)
        + "\n",
        encoding="utf-8",
    )


def _png_bytes(width: int, height: int, *, varied: bool, seed: int = 0) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = (x * 7 + y * 13 + seed * 17) % 256 if varied else 128
            rows.extend((value, (value * 3) % 256, (value * 5) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


def _mark_planned_home_captured(root: Path, payload: bytes) -> None:
    image = root / "images" / "planned-home.png"
    image.parent.mkdir(exist_ok=True)
    image.write_bytes(payload)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = root / "SCREENSHOT-MANIFEST.yml"
    document = manifest.read_text(encoding="utf-8")
    replacements = {
        "    state: pending": "    state: captured",
        "    viewport: null": (
            "    viewport: {width: 1440, height: 1000, device_scale_factor: 1}"
        ),
        "    product: null": (f"    product: {{version: 1.0.0, git_commit: {commit}}}"),
        "    captured_at: null": "    captured_at: 2026-07-09T00:00:00Z",
        "    sha256: null": f"    sha256: {hashlib.sha256(payload).hexdigest()}",
        "    market_data: null": (
            "    market_data:\n"
            "      symbol: 600519.SH\n"
            "      name: \u8d35\u5dde\u8305\u53f0\n"
            "      period: 1d\n"
            "      adjustment: qfq\n"
            "      start: 2021-01-01\n"
            "      end: 2026-07-08\n"
            "      source: tushare\n"
            "      cutoff: 2026-07-08T07:00:00Z\n"
            "      dataset_version: sha256:"
            f"{hashlib.sha256(b'planned-home-dataset').hexdigest()}"
        ),
        "    capture: null": "    capture: playwright",
        "    editing: null": "    editing: none",
        "    redaction: pending": "    redaction: passed",
    }
    for old, new in replacements.items():
        document = document.replace(old, new, 1)
    manifest.write_text(document, encoding="utf-8")


def _finalize_wiki(root: Path) -> None:
    image_dir = root / "images"
    image_dir.mkdir()
    png = _png_bytes(640, 360, varied=True)
    for stem in EXPECTED_WIKI_PAGE_STEMS:
        if stem == "Home":
            continue
        for suffix in ("", "-en"):
            page = root / f"{stem}{suffix}.md"
            image_name = f"{stem}{suffix}.png"
            page.write_text(
                page.read_text(encoding="utf-8").replace(
                    "<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->",
                    f"![Verified release-candidate screenshot](images/{image_name})",
                ),
                encoding="utf-8",
            )
            (image_dir / image_name).write_bytes(png)


def _write_complete_final_wiki(root: Path) -> None:
    _write_wiki(root)
    image_dir = root / "images"
    image_dir.mkdir()
    repo = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest_path = root / "SCREENSHOT-MANIFEST.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    entries = manifest.get("screenshots")
    assert isinstance(entries, list)
    for ordinal, entry in enumerate(entries, start=1):
        assert isinstance(entry, dict)
        screenshot_id = entry.get("screenshot_id")
        relative_path = entry.get("path")
        page_pairs = entry.get("page_pairs")
        assert isinstance(screenshot_id, str)
        assert isinstance(relative_path, str)
        assert isinstance(page_pairs, list)
        payload = _png_bytes(640, 360, varied=True, seed=ordinal)
        image = root / relative_path
        image.write_bytes(payload)
        for page_name in page_pairs:
            assert isinstance(page_name, str)
            page = root / page_name
            document = page.read_text(encoding="utf-8")
            if relative_path not in document:
                document += f"\n![Captured evidence]({relative_path})\n"
            page.write_text(document, encoding="utf-8")
        entry["state"] = "captured"
        entry["viewport"] = {
            "width": 1440,
            "height": 1000,
            "device_scale_factor": 1,
        }
        entry["product"] = {"version": "1.0.0", "git_commit": commit}
        entry["captured_at"] = "2026-07-09T00:00:00Z"
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
        if entry.get("contains_market_data") is True:
            entry["market_data"] = {
                "symbol": "600519.SH",
                "name": "\u8d35\u5dde\u8305\u53f0",
                "period": "1d",
                "adjustment": "qfq",
                "start": "2021-01-01",
                "end": "2026-07-08",
                "source": "tushare",
                "cutoff": "2026-07-08T07:00:00Z",
                "dataset_version": "sha256:"
                + hashlib.sha256(f"dataset:{screenshot_id}".encode()).hexdigest(),
            }
        else:
            entry["market_data"] = None
        entry["capture"] = "playwright"
        entry["editing"] = "none"
        entry["redaction"] = "passed"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def test_repository_documentation_contract_passes_for_complete_tree(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)

    assert verify_repository(tmp_path) == []


def test_complete_final_wiki_fixture_passes_every_publication_gate(
    tmp_path: Path,
) -> None:
    _write_complete_final_wiki(tmp_path)

    assert verify_wiki(tmp_path, final=True) == []


def test_wiki_real_market_sources_match_product_bar_providers() -> None:
    from stock_desk.market.routing import SourcePriorities
    from stock_desk.market.types import BAR_SOURCE_PROVIDER_IDS

    assert verify_docs_module._real_market_source_ids() == frozenset(
        {"tushare", "akshare", "baostock", "tdx_local"}
    )
    assert SourcePriorities().bars == BAR_SOURCE_PROVIDER_IDS


def test_final_wiki_rejects_copied_image_under_another_name(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    first = tmp_path / "images/planned-home.png"
    second = tmp_path / "images/planned-feature-index.png"
    first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
    second.write_bytes(first.read_bytes())
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in document["screenshots"]
        if item["screenshot_id"] == "planned-feature-index"
    )
    entry["sha256"] = first_digest
    manifest.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("captured screenshot SHA-256 is reused" in item for item in failures)


def test_final_wiki_separates_dataset_digest_from_screenshot_digest(
    tmp_path: Path,
) -> None:
    _write_complete_final_wiki(tmp_path)
    image = tmp_path / "images/planned-market-charts.png"
    image_digest = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in document["screenshots"]
        if item["screenshot_id"] == "planned-market-charts"
    )
    entry["market_data"]["dataset_version"] = f"sha256:{image_digest}"
    manifest.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "dataset_version must be distinct from screenshot SHA-256" in item
        for item in failures
    )


def test_final_wiki_rejects_fictional_market_source(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = next(
        item for item in document["screenshots"] if item["market_data"] is not None
    )
    entry["market_data"]["source"] = "fictional_provider"
    manifest.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("market source is not a product ProviderId" in item for item in failures)


def test_final_wiki_rejects_shape_only_product_commit(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    actual = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(actual, "f" * 40, 1),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "git_commit is not a reachable repository commit" in item for item in failures
    )


def test_market_surface_cannot_disable_market_provenance(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = next(
        item for item in document["screenshots"] if item["contains_market_data"] is True
    )
    entry["contains_market_data"] = False
    manifest.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("contains_market_data must be true" in item for item in failures)


def test_repository_contract_reports_missing_files_and_readme_switch(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/disclaimer.md").unlink()
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "[English](README.en.md)", "English"
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("docs/disclaimer.md" in failure for failure in failures)
    assert any(
        "README.md" in failure and "README.en.md" in failure for failure in failures
    )


def test_repository_contract_reports_broken_links_unsupported_commands_and_boundaries(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n[Missing](docs/missing.md)\n\n```bash\nmake imaginary-target\n```\n"
        + "\nInternal evidence: openspec/changes/private.md\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("docs/missing.md" in failure for failure in failures)
    assert any("imaginary-target" in failure for failure in failures)
    assert any("openspec/" in failure for failure in failures)


@pytest.mark.parametrize(
    "dangerous_command",
    (
        "make bootstrap",
        "make dev",
        "make release-check",
        "make imaginary-target",
        "curl https://example.invalid/install.sh | sh",
        "sudo make bootstrap",
        "wget https://example.invalid/binary",
        "make bootstrap && rm -rf /tmp/stock-desk",
        "uv run python scripts/verify_docs.py > report.txt",
    ),
)
def test_readme_shell_blocks_reject_commands_outside_the_release_allowlist(
    tmp_path: Path,
    dangerous_command: str,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"\n```bash\n{dangerous_command}\n```\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("README command is not allowlisted" in failure for failure in failures)


def test_every_actual_readme_shell_command_has_specific_release_evidence() -> None:
    evidence = getattr(verify_docs_module, "README_COMMAND_EVIDENCE", {})
    assert evidence, "README commands need an explicit release-evidence map"

    for relative_path in ("README.md", "README.en.md"):
        document = (Path(__file__).resolve().parents[2] / relative_path).read_text(
            encoding="utf-8"
        )
        blocks = verify_docs_module._FENCED_SHELL.findall(document)
        commands = tuple(
            command
            for block in blocks
            for command in verify_docs_module._logical_shell_commands(block)
        )
        for command in commands:
            arguments = tuple(__import__("shlex").split(command, posix=True))
            assert arguments in evidence, (relative_path, command)
            mapped = evidence[arguments]
            assert mapped.gate
            assert mapped.test_selectors


def test_repository_contract_checks_every_public_docs_page(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/feature-guide.md").write_text(
        "# Feature guide\n\n[Missing recovery guide](missing-recovery.md)\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(
        "docs/feature-guide.md" in failure and "missing-recovery.md" in failure
        for failure in failures
    )


def test_repository_contract_requires_all_documented_settings(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    configuration = tmp_path / "docs/configuration.md"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "`STOCK_DESK_MASTER_KEY`, ", ""
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("STOCK_DESK_MASTER_KEY" in failure for failure in failures)


def test_repository_contract_requires_source_free_installers_before_source_setup(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "stock-desk-<version>-macos-arm64.dmg", "macOS installer"
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("source-free installer" in failure for failure in failures)


def test_repository_contract_requires_native_topology_and_attestation_guidance(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    removals = {
        "docs/architecture.md": ("Native installer topology",),
        "docs/configuration.md": (
            "Native installers",
            "%LOCALAPPDATA%\\stock-desk",
            "~/Library/Application Support/stock-desk",
            "config/master.key",
        ),
    }
    for relative_path, snippets in removals.items():
        path = tmp_path / relative_path
        document = path.read_text(encoding="utf-8")
        for snippet in snippets:
            document = document.replace(snippet, "removed")
        path.write_text(document, encoding="utf-8")

    failures = verify_repository(tmp_path)

    for expected in (
        "Native installers",
        "%LOCALAPPDATA%\\stock-desk",
        "~/Library/Application Support/stock-desk",
        "config/master.key",
    ):
        assert any(expected in failure for failure in failures), expected


def test_repository_contract_requires_mode_specific_rollback_and_native_writability(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    removals = {
        "docs/backup-and-restore.md": (
            "Compose image digest",
            "immutable source commit",
            "exact macOS installer artifact",
        ),
        "docs/architecture.md": ("user-writable install location",),
    }
    for relative_path, snippets in removals.items():
        path = tmp_path / relative_path
        document = path.read_text(encoding="utf-8")
        for snippet in snippets:
            document = document.replace(snippet, "removed")
        path.write_text(document, encoding="utf-8")

    failures = verify_repository(tmp_path)

    for expected in (
        "Compose image digest",
        "immutable source commit",
        "exact macOS installer artifact",
        "user-writable install location",
    ):
        assert any(expected in failure for failure in failures), expected


def test_wiki_staging_requires_complete_pairs_and_procedural_sections(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    assert verify_wiki(tmp_path, final=False) == []

    (tmp_path / "MACD-Backtest-Tutorial-en.md").unlink()
    formula = tmp_path / "Formula-Studio-Quickstart-en.md"
    formula.write_text(
        formula.read_text(encoding="utf-8").replace("## Recovery", "## Notes"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any("MACD-Backtest-Tutorial-en.md" in failure for failure in failures)
    assert any(
        "Formula-Studio-Quickstart-en.md" in failure and "Recovery" in failure
        for failure in failures
    )


def test_wiki_articles_require_the_complete_shared_template(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    chinese = tmp_path / "Market-Charts.md"
    chinese.write_text(
        chinese.read_text(encoding="utf-8")
        .replace("## 使用前", "## 准备")
        .replace("[下一页](Formula-Studio-Quickstart)", "下一页：公式工作室"),
        encoding="utf-8",
    )
    english = tmp_path / "Market-Charts-en.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace("## Common problems", "## Notes"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    for filename, required in (
        ("Market-Charts.md", "使用前"),
        ("Market-Charts.md", "下一页"),
        ("Market-Charts-en.md", "Common problems"),
    ):
        assert any(
            filename in failure and required in failure for failure in failures
        ), required


def test_wiki_page_pairs_require_matching_evidence_and_navigation(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    chinese = tmp_path / "Market-Charts.md"
    chinese.write_text(
        chinese.read_text(encoding="utf-8").replace(
            "[上一页](Stock-Pools)", "[上一页](Stock-Pools-en)"
        ),
        encoding="utf-8",
    )
    english = tmp_path / "Market-Charts-en.md"
    english.write_text(
        english.read_text(encoding="utf-8")
        .replace("`planned-market-charts`", "`unknown-shot`")
        .replace("[Previous](Stock-Pools-en)", "[Previous](Home-en)"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts" in failure and "screenshot evidence order" in failure
        for failure in failures
    )
    assert any(
        "unknown-shot" in failure and "manifest" in failure for failure in failures
    )
    assert any(
        "Market-Charts" in failure and "normalized navigation" in failure
        for failure in failures
    )
    assert any(
        "Market-Charts.md" in failure
        and "cross-language navigation" in failure
        and "Stock-Pools-en" in failure
        for failure in failures
    )


def test_wiki_declared_screenshot_ids_must_belong_to_the_page_pair(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    for filename in ("Market-Charts.md", "Market-Charts-en.md"):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "`planned-market-charts`", "`planned-home`"
            ),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts.md" in failure
        and "planned-home" in failure
        and "page_pairs" in failure
        for failure in failures
    )


def test_english_articles_require_numbered_chinese_ui_label_mappings(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    for filename, label in (
        ("Market-Charts-en.md", "Market workspace（行情工作区）"),
        ("Task-Center-en.md", "Task Center（任务中心）"),
    ):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(label, "Removed label"),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts-en.md" in failure
        and "Market workspace" in failure
        and "行情工作区" in failure
        and "Chinese UI labels" in failure
        for failure in failures
    )
    assert any(
        "Task-Center-en.md" in failure
        and "Task Center" in failure
        and "任务中心" in failure
        and "Chinese UI labels" in failure
        for failure in failures
    )


def test_wiki_app_ui_labels_are_backed_by_tracked_production_source() -> None:
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_APP_UI_LABELS", None)
        == EXPECTED_WIKI_APP_UI_LABELS
    )
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_APP_UI_SOURCE_FILES", None)
        == EXPECTED_WIKI_APP_UI_SOURCE_FILES
    )
    checker = getattr(verify_docs_module, "_app_ui_label_in_page_source", None)
    assert callable(checker)
    repo = Path(__file__).resolve().parents[2]
    tracked = set(
        subprocess.run(
            ("git", "ls-files", "web/src"),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    for paths in EXPECTED_WIKI_APP_UI_SOURCE_FILES.values():
        assert set(paths) <= tracked
        assert all(".test." not in path and ".spec." not in path for path in paths)
    for stem, labels in EXPECTED_WIKI_APP_UI_LABELS.items():
        for _english, chinese in labels:
            assert checker(stem, chinese), f"{stem}: {chinese}"


def test_page_specific_ui_source_rejects_cross_page_borrowing(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Data-Sources-and-Tushare-en.md"
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace(
            "## Steps",
            "4. `Model settings（模型设置）` — borrowed from another page.\n\n## Steps",
            1,
        )
        .replace(
            "1. Open Stock Desk.",
            "1. Open Stock Desk and use **Model settings（模型设置）**.",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Data-Sources-and-Tushare-en.md" in failure
        and "page-specific production source" in failure
        and "模型设置" in failure
        for failure in failures
    )


def test_tushare_dynamic_connection_label_is_page_source_backed() -> None:
    checker = getattr(verify_docs_module, "_app_ui_label_in_page_source", None)
    assert callable(checker)
    assert checker("Data-Sources-and-Tushare", "测试 Tushare 连接")


def test_wiki_external_ui_labels_use_a_typed_allowlist() -> None:
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_EXTERNAL_UI_LABELS", None)
        == EXPECTED_WIKI_EXTERNAL_UI_LABELS
    )
    expected_allowlist: dict[str, frozenset[tuple[str, str]]] = {}
    for labels in EXPECTED_WIKI_EXTERNAL_UI_LABELS.values():
        for kind, english, chinese in labels:
            expected_allowlist.setdefault(kind, frozenset())
            expected_allowlist[kind] = expected_allowlist[kind] | {(english, chinese)}
    assert (
        getattr(verify_docs_module, "WIKI_EXTERNAL_UI_LABEL_ALLOWLIST", None)
        == expected_allowlist
    )


def test_english_ui_label_first_occurrence_is_bilingual(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Market-Charts-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Market workspace（行情工作区）", "Market workspace", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts-en.md" in failure
        and "first occurrence" in failure
        and "Market workspace（行情工作区）" in failure
        for failure in failures
    )


def test_every_english_ui_label_first_occurrence_is_bilingual(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Market-Charts-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Confirm that the application is healthy and the required inputs are available.",
            "Confirm that the application is healthy, then use Reset view if needed.",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts-en.md" in failure
        and "first occurrence" in failure
        and "Reset view（重置视图）" in failure
        for failure in failures
    )


def test_steps_backticked_ui_references_must_exist_in_the_ui_map(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Data-Sources-and-Tushare-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "1. Open Stock Desk.",
            "1. Open Stock Desk and use **Ghost control（幽灵控件）**.",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Data-Sources-and-Tushare-en.md" in failure
        and "Steps UI reference is missing from UI label map" in failure
        and "Ghost control（幽灵控件）" in failure
        for failure in failures
    )


def test_every_ui_label_map_item_must_be_used_in_steps(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Market-Charts-en.md"
    mapping = "**Reset view（重置视图）**"
    before, separator, after = page.read_text(encoding="utf-8").rpartition(mapping)
    assert separator
    page.write_text(
        before + "removed step reference" + after,
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Market-Charts-en.md" in failure
        and "UI label map item is unused in Steps" in failure
        and "Reset view（重置视图）" in failure
        for failure in failures
    )


def test_wiki_rejects_legacy_typed_code_and_path_prefixes(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Windows-Installation-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "1. Open Stock Desk.",
            "1. Open Stock Desk with `path:/private/value` and `code:artifact.exe`.",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Windows-Installation-en.md" in failure
        and "legacy typed prefix" in failure
        and "path:" in failure
        and "code:" in failure
        for failure in failures
    )


@pytest.mark.parametrize(
    ("filename", "mapping"),
    (
        ("First-Launch-and-Health-en.md", "Worker running（Worker 运行中）"),
        ("Local-TDX-Data-en.md", "Save data source settings（保存数据源设置）"),
    ),
)
def test_required_status_and_save_controls_cannot_be_omitted_from_ui_map(
    tmp_path: Path,
    filename: str,
    mapping: str,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / filename
    document = page.read_text(encoding="utf-8")
    page.write_text(
        re.sub(
            rf"^\d+\. `{re.escape(mapping)}` .*\n",
            "",
            document,
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        filename in failure
        and "Chinese UI labels must be the numbered controlled mappings" in failure
        and mapping.partition("（")[0] in failure
        for failure in failures
    )


def test_app_ui_map_rejects_nonexistent_production_control(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Task-Center-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "## Steps",
            "3. `Nonexistent control（不存在控件）` — must not be accepted.\n\n## Steps",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Task-Center-en.md" in failure
        and "application UI label is absent from page-specific production source"
        in failure
        and "不存在控件" in failure
        for failure in failures
    )


def test_english_ui_label_check_ignores_markdown_link_destinations(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Stock-Pools-en.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "# Stock Pools（股票池）", "# Stock pools"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert not [
        failure
        for failure in failures
        if "Stock-Pools-en.md" in failure and "first occurrence" in failure
    ]


def test_wiki_requires_chinese_default_and_english_suffix(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    assert verify_docs_module.REQUIRED_WIKI_PAGE_STEMS == EXPECTED_WIKI_PAGE_STEMS
    assert verify_wiki(tmp_path, final=False) == []

    home = tmp_path / "Home.md"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            "[English](Home-en)", "[English](Home)"
        ),
        encoding="utf-8",
    )
    english = tmp_path / "Market-Charts-en.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace(
            "[简体中文](Market-Charts)", "[简体中文](Market-Charts-en)"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any("Home.md" in failure and "Home-en" in failure for failure in failures)
    assert any(
        "Market-Charts-en.md" in failure and "Market-Charts" in failure
        for failure in failures
    )


def test_wiki_inventory_includes_public_governance_and_release_evidence() -> None:
    assert "Project-Governance-and-Release-Evidence" in (
        verify_docs_module.REQUIRED_WIKI_PAGE_STEMS
    )


def test_wiki_requires_shared_navigation_and_entry_files(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    (tmp_path / "_Sidebar-en.md").unlink()
    (tmp_path / "SCREENSHOT-MANIFEST.yml").unlink()

    failures = verify_wiki(tmp_path, final=False)

    assert any("_Sidebar-en.md" in failure for failure in failures)
    assert any("SCREENSHOT-MANIFEST.yml" in failure for failure in failures)


def test_final_wiki_feature_index_covers_active_requirements(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    failures = verify_wiki(tmp_path, final=True)

    assert not [item for item in failures if "feature index" in item.casefold()]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (
            lambda document: document.replace(
                "| R-079 | [\u4e2d\u6587\u9996\u9875]",
                "| R-080 | [\u4e2d\u6587\u9996\u9875]",
            ),
            "missing requirement ID: R-079",
        ),
        (
            lambda document: document.replace(
                "[\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb)",
                "[\u4e2d\u6587\u9996\u9875](Missing#\u4ece\u8fd9\u91cc\u5f00\u59cb)",
                1,
            ),
            "referenced page does not exist: Missing.md",
        ),
        (
            lambda document: document.replace(
                "Home#\u4ece\u8fd9\u91cc\u5f00\u59cb", "Home#\u4e0d\u5b58\u5728", 1
            ),
            "referenced section does not exist",
        ),
        (
            lambda document: document.replace("`planned-home`", "`missing-shot`", 1),
            "missing screenshot reference: missing-shot",
        ),
    ),
)
def test_wiki_feature_index_rejects_incomplete_or_dangling_rows(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    mutate = mutation
    assert callable(mutate)
    index.write_text(mutate(index.read_text(encoding="utf-8")), encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "feature index" in item.casefold() and expected in item for item in failures
    )


def test_feature_index_has_fixed_semantic_bindings(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_FEATURE_BINDINGS", None)
        == EXPECTED_WIKI_FEATURE_BINDINGS
    )
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "`local-security-settings`",
                "`planned-credentials-logs-and-local-security`",
            ),
            encoding="utf-8",
        )
    failures = verify_wiki(tmp_path, final=False)
    assert any(
        "R-050" in failure and "semantic binding" in failure for failure in failures
    )


def test_r073_documentation_entry_proves_readme_and_wiki_roles(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    assert (
        getattr(
            verify_docs_module,
            "REQUIRED_WIKI_DOCUMENTATION_ENTRY_MARKERS",
            None,
        )
        == EXPECTED_WIKI_DOCUMENTATION_ENTRY_MARKERS
    )
    for filename, markers in EXPECTED_WIKI_DOCUMENTATION_ENTRY_MARKERS.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(markers[1], "generic docs", 1),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in EXPECTED_WIKI_DOCUMENTATION_ENTRY_MARKERS:
        assert any(
            filename in failure and "R-073 documentation entry proof" in failure
            for failure in failures
        )


def test_workflow_pages_reject_fictional_controls_and_fields(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_WORKFLOW_CONTENT", None)
        == EXPECTED_WIKI_WORKFLOW_CONTENT
    )
    for filename, (required, forbidden) in EXPECTED_WIKI_WORKFLOW_CONTENT.items():
        page = tmp_path / filename
        document = page.read_text(encoding="utf-8").replace(
            required[0], "removed required workflow evidence"
        )
        if forbidden:
            document += f"\n{forbidden[0]}\n"
        page.write_text(document, encoding="utf-8")

    failures = verify_wiki(tmp_path, final=False)

    for filename in EXPECTED_WIKI_WORKFLOW_CONTENT:
        assert any(
            filename in failure and "workflow content contract" in failure
            for failure in failures
        )


def test_market_guide_claims_are_backed_by_tracked_product_source() -> None:
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS", None)
        == EXPECTED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS
    )
    repo = Path(__file__).resolve().parents[2]
    tracked = set(
        subprocess.run(
            ("git", "ls-files"),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    for claims in EXPECTED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS.values():
        for _wiki_marker, relative_path, source_marker in claims:
            assert relative_path in tracked
            assert source_marker in (repo / relative_path).read_text(encoding="utf-8")


def test_market_guide_pages_require_source_backed_claims(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    for filename, claims in EXPECTED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                claims[0][0], "removed source-backed claim", 1
            ),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in EXPECTED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS:
        assert any(
            filename in failure and "source-backed market-guide contract" in failure
            for failure in failures
        )


def test_r056_is_proved_only_by_formula_studio_chart_evidence(
    tmp_path: Path,
) -> None:
    assert verify_docs_module.REQUIRED_WIKI_FEATURE_BINDINGS["R-056"] == (
        "Formula-Studio-Quickstart#适用场景",
        "Formula-Studio-Quickstart-en#when-to-use-this",
        "适用场景 / When to use this",
        "formula-studio-wide",
        "app-route:/formulas",
    )
    _write_wiki(tmp_path)
    manifest = yaml.safe_load(
        (tmp_path / "SCREENSHOT-MANIFEST.yml").read_text(encoding="utf-8")
    )
    entries = [
        entry
        for entry in manifest["screenshots"]
        if "R-056" in entry.get("features", [])
    ]

    assert len(entries) == 1
    assert entries[0]["screenshot_id"] == "formula-studio-wide"
    assert entries[0]["page_pairs"] == [
        "Formula-Studio-Quickstart.md",
        "Formula-Studio-Quickstart-en.md",
    ]
    assert entries[0]["surface"] == {
        "type": "app-route",
        "locator": "/formulas",
    }
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        row = next(
            line
            for line in (tmp_path / filename).read_text(encoding="utf-8").splitlines()
            if line.startswith("| R-056 |")
        )
        assert "Formula-Studio-Quickstart" in row
        assert "`formula-studio-wide`" in row
        assert "`app-route:/formulas`" in row
        assert "Market-Charts" not in row
        assert "`app-route:/market`" not in row
    for filename in (
        "Formula-Studio-Quickstart.md",
        "Formula-Studio-Quickstart-en.md",
    ):
        page = (tmp_path / filename).read_text(encoding="utf-8")
        assert "BUY 买点" in page
        assert "SELL 卖点" in page
    assert "K 线主图与公式副图" in (
        tmp_path / "Formula-Studio-Quickstart.md"
    ).read_text(encoding="utf-8")
    assert "K-line main chart and formula subchart" in (
        tmp_path / "Formula-Studio-Quickstart-en.md"
    ).read_text(encoding="utf-8")

    market_entry = next(
        entry
        for entry in manifest["screenshots"]
        if entry["screenshot_id"] == "planned-market-charts"
    )
    market_entry["features"].append("R-056")
    (tmp_path / "SCREENSHOT-MANIFEST.yml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    failures = verify_wiki(tmp_path, final=False)
    assert any(
        "Screenshot manifest planned-market-charts" in failure
        and "do not exactly match Feature index mappings" in failure
        for failure in failures
    )

    _write_wiki(tmp_path)
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        lines = index.read_text(encoding="utf-8").splitlines()
        lines = [
            (
                "| R-056 | [行情与 K 线图](Market-Charts#适用场景) | "
                "[Market charts](Market-Charts-en#when-to-use-this) | "
                "适用场景 / When to use this | `planned-market-charts` | "
                "`app-route:/market` |"
                if line.startswith("| R-056 |")
                else line
            )
            for line in lines
        ]
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    failures = verify_wiki(tmp_path, final=False)
    assert any(
        "R-056" in failure and "Feature index" in failure for failure in failures
    )


def test_stock_pool_docs_separate_member_issues_from_whole_request_limit(
    tmp_path: Path,
) -> None:
    for filename in ("Stock-Pools.md", "Stock-Pools-en.md"):
        assert (
            verify_docs_module.REQUIRED_WIKI_WORKFLOW_CONTENT[filename]
            == (EXPECTED_WIKI_WORKFLOW_CONTENT[filename])
        )
    assert (
        verify_docs_module.REQUIRED_WIKI_LOW_CODE_SECTION_FORBIDDEN
        == EXPECTED_WIKI_LOW_CODE_SECTION_FORBIDDEN
    )
    assert (
        verify_docs_module.REQUIRED_WIKI_LOW_CODE_SECTION_REQUIRED
        == EXPECTED_WIKI_LOW_CODE_SECTION_REQUIRED
    )
    for mapping in (
        ("Pool creation failed; check members", "股票池创建失败，请检查成员。"),
        ("Pool save failed; check members", "股票池保存失败，请检查成员。"),
    ):
        assert mapping in verify_docs_module.REQUIRED_WIKI_APP_UI_LABELS["Stock-Pools"]
        assert verify_docs_module._app_ui_label_in_page_source(
            "Stock-Pools", mapping[1]
        )
    _write_wiki(tmp_path)
    replacements = {
        "Stock-Pools.md": (
            "超过 5,000 只返回 `code=invalid_request` 和空列表 `issues: []`",
            "超过 5,000 只不返回 issues",
        ),
        "Stock-Pools-en.md": (
            "More than 5,000 symbols returns `code=invalid_request` with an empty `issues: []` list",
            "More than 5,000 symbols does not return issues",
        ),
    }
    for filename, (truth, false_claim) in replacements.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(truth, false_claim, 1),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in replacements:
        assert any(
            filename in failure and "workflow content contract" in failure
            for failure in failures
        )

    _write_wiki(tmp_path)
    for filename, heading in (
        ("Stock-Pools.md", "## 操作步骤\n"),
        ("Stock-Pools-en.md", "## Steps\n"),
    ):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                heading,
                heading + "\n1. Require `code=invalid_request` and `issues: []`.\n",
                1,
            ),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in replacements:
        assert any(
            filename in failure
            and "low-code section exposes advanced API fields" in failure
            for failure in failures
        )


def test_tdx_docs_distinguish_local_settings_visibility_from_public_redaction(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    replacements = {
        "Local-TDX-Data.md": (
            "绝对路径会由本地设置 API 返回并回填到本机设置页",
            "路径不会由 API 返回",
        ),
        "Local-TDX-Data-en.md": (
            "the absolute path is returned by the local settings API and filled back into the local settings page",
            "path is never returned by the API",
        ),
    }
    for filename, (truth, false_claim) in replacements.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(truth, false_claim, 1),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in replacements:
        assert any(
            filename in failure
            and (
                "workflow content contract" in failure
                or "source-backed market-guide contract" in failure
            )
            for failure in failures
        )


def test_task_center_backtest_link_depends_on_target_not_completion(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Task-Center-en.md"
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace(
            "The backtest report link appears whenever the response contains a "
            "`backtest_run` target, including while the task is still running",
            "The link is limited to completed backtest targets",
            1,
        )
        .replace(
            "Other tasks without that target do not show the link",
            "Other tasks are unspecified",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Task-Center-en.md" in failure
        and "workflow content contract" in failure
        and "completed backtest targets" in failure
        for failure in failures
    )


def _append_supplemental_windows_evidence(root: Path, screenshot_id: str) -> None:
    manifest = root / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""  - screenshot_id: {screenshot_id}
    path: images/{screenshot_id}.png
    page_pairs: [Windows-Installation.md, Windows-Installation-en.md]
    caption_locales: {{zh-CN: Windows 安装证据, en: Windows installation evidence}}
    features: []
    surface: {{type: windows-installer, locator: stock-desk-<version>-windows-x86_64.exe}}
    contains_market_data: false
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: 仅作功能演示，不构成投资建议
""",
        encoding="utf-8",
    )


def test_supplemental_page_evidence_may_have_no_requirement_mapping(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    supplemental_id = "supplemental-windows-clean-install"
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"""  - screenshot_id: {supplemental_id}
    path: images/{supplemental_id}.png
    page_pairs: [Windows-Installation.md, Windows-Installation-en.md]
    caption_locales: {{zh-CN: Windows \u5b89\u88c5\u8bc1\u636e, en: Windows installation evidence}}
    features: []
    surface: {{type: windows-installer, locator: stock-desk-<version>-windows-x86_64.exe}}
    contains_market_data: false
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae
""",
        encoding="utf-8",
    )
    for filename in ("Windows-Installation.md", "Windows-Installation-en.md"):
        page = tmp_path / filename
        document = page.read_text(encoding="utf-8")
        current_id = _planned_screenshot_id("Windows-Installation")
        page.write_text(
            document.replace(f"`{current_id}`", f"`{current_id}`, `{supplemental_id}`"),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    assert not [failure for failure in failures if supplemental_id in failure]


def test_supplemental_page_evidence_cannot_be_orphaned(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    supplemental_id = "supplemental-windows-orphan"
    _append_supplemental_windows_evidence(tmp_path, supplemental_id)

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        supplemental_id in failure
        and "must be declared by both manifest page_pairs" in failure
        for failure in failures
    )


def test_supplemental_evidence_rejects_replaced_and_cross_page_declarations(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    supplemental_id = "supplemental-windows-replaced"
    _append_supplemental_windows_evidence(tmp_path, supplemental_id)
    windows = tmp_path / "Windows-Installation.md"
    current_id = _planned_screenshot_id("Windows-Installation")
    windows.write_text(
        windows.read_text(encoding="utf-8").replace(
            f"`{current_id}`", f"`{current_id}`, `{supplemental_id}`", 1
        ),
        encoding="utf-8",
    )
    market = tmp_path / "Market-Charts-en.md"
    market.write_text(
        market.read_text(encoding="utf-8").replace(
            f"`{_planned_screenshot_id('Market-Charts')}`",
            f"`{supplemental_id}`",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        supplemental_id in failure
        and "Windows-Installation-en.md" in failure
        and "must be declared by both manifest page_pairs" in failure
        for failure in failures
    )
    assert any(
        supplemental_id in failure
        and "Market-Charts-en.md" in failure
        and "page_pairs does not include" in failure
        for failure in failures
    )


def test_screenshot_manifest_allows_honest_staging_but_blocks_final(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    staging_failures = verify_wiki(tmp_path, final=False)
    final_failures = verify_wiki(tmp_path, final=True)

    assert not [
        item for item in staging_failures if "screenshot manifest" in item.casefold()
    ]
    assert any(
        "screenshot manifest" in item.casefold() and "pending" in item.casefold()
        for item in final_failures
    )


def test_wiki_feature_index_requires_screenshot_to_cover_mapped_requirement(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "    features: [R-079]", "    features: [R-078]"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "R-079" in item
        and "planned-home" in item
        for item in failures
    )


def test_wiki_manifest_features_exactly_match_feature_index(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "| R-079 | [\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb) | "
                "[English home](Home-en#start-here) | \u4ece\u8fd9\u91cc\u5f00\u59cb / Start here | "
                "`planned-home` | `app-route:/market` |",
                "| R-079 | [\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb) | "
                "[English home](Home-en#start-here) | \u4ece\u8fd9\u91cc\u5f00\u59cb / Start here | "
                "`second-shot` | `app-route:/market` |",
            ),
            encoding="utf-8",
        )
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + """  - screenshot_id: second-shot
    path: images/second-shot.png
    page_pairs: [Home.md, Home-en.md]
    caption_locales: {zh-CN: \u7b2c\u4e8c\u5f20\u622a\u56fe, en: Second screenshot}
    features: [R-079]
    surface: {type: app-route, locator: /market}
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae
""",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "features do not exactly match Feature index" in item
        for item in failures
    )


def test_wiki_typed_surface_supports_app_and_non_app_evidence(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "`planned-home` | `app-route:/market`",
                "`planned-home` | `wiki-page:Home`",
            ),
            encoding="utf-8",
        )
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "    surface: {type: app-route, locator: /market}",
            "    surface: {type: wiki-page, locator: Home}",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert not [item for item in failures if "surface" in item.casefold()]


def test_application_routes_use_shared_json_as_the_single_source_of_truth() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "web/src/app/route-paths.json"
    assert contract_path.is_file()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert isinstance(contract, dict)
    source = (repo / "web/src/app/routes.ts").read_text(encoding="utf-8")
    assert "./route-paths.json" in source
    assert verify_docs_module._canonical_app_routes() == frozenset(contract.values())
    for key in contract:
        assert source.count(f"routePaths.{key}") == 1
    assert "/comment-only-route" not in verify_docs_module._canonical_app_routes()


def test_wiki_feature_index_rejects_every_unparsed_table_body_row(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    index.write_text(
        index.read_text(encoding="utf-8") + "\n| R-080 | malformed | row |\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index feature-index.md" in item.casefold()
        and "unparseable table row" in item
        and "R-080" in item
        for item in failures
    )


def test_final_wiki_requires_every_publication_raster_in_captured_manifest(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))
    for filename in ("Home.md", "Home-en.md"):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n![Home evidence](images/planned-home.png)\n",
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "images/MACD-Backtest-Tutorial.png" in item
        and "exactly one valid captured manifest entry" in item
        for item in failures
    )


def test_final_wiki_rejects_rogue_article_raster_reference(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))
    rogue = tmp_path / "images" / "rogue.png"
    rogue.write_bytes(_png_bytes(640, 360, varied=True))
    page = tmp_path / "Market-Charts.md"
    page.write_text(
        page.read_text(encoding="utf-8") + "\n![Rogue evidence](images/rogue.png)\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item
        and "images/rogue.png" in item
        and "valid captured manifest entry" in item
        for item in failures
    )


def test_final_wiki_rejects_unreferenced_raster_outside_images(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    (tmp_path / "root-rogue.png").write_bytes(_png_bytes(640, 360, varied=True))

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "root-rogue.png" in item and "outside Wiki images" in item for item in failures
    )


def test_final_wiki_rejects_cross_page_pair_image_reference(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    payload = _png_bytes(640, 360, varied=True)
    _mark_planned_home_captured(tmp_path, payload)
    for filename in ("Home.md", "Home-en.md"):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n![Home evidence](images/planned-home.png)\n",
            encoding="utf-8",
        )
    market = tmp_path / "Market-Charts.md"
    market.write_text(
        market.read_text(encoding="utf-8")
        + "\n![Wrong page evidence](images/planned-home.png)\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item
        and "planned-home.png" in item
        and "not listed in manifest page_pairs" in item
        for item in failures
    )


def test_final_page_screenshot_gate_requires_valid_manifest_entry(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item and "captured manifest evidence" in item
        for item in failures
    )


def test_wiki_feature_routes_come_from_the_application_route_contract(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "`app-route:/market`", "`app-route:/health`", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "/health" in item
        and "canonical application route" in item
        for item in failures
    )


def test_repository_audit_supports_distinct_ssh_identity_policy_surface() -> None:
    assert (
        verify_docs_module._surface_failure(
            ("repository-audit", "ssh-identity-policy"),
            verify_docs_module._canonical_app_routes(),
        )
        is None
    )


def test_wiki_rejects_private_ssh_material_and_machine_paths(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Project-Governance-and-Release-Evidence.md"
    ssh_directory = "~/.ssh/"
    key_family = "id_" + "ed25519"
    private_key_path = ssh_directory + key_family + "_github"
    private_key_marker = "BEGIN " + "OPENSSH PRIVATE KEY"
    private_key_header = "-----" + private_key_marker + "-----"
    page.write_text(
        page.read_text(encoding="utf-8")
        + f"\n{private_key_path}\n{private_key_header}\n",
        encoding="utf-8",
    )
    written = page.read_text(encoding="utf-8")
    assert private_key_path in written
    assert private_key_header in written

    failures = verify_wiki(tmp_path, final=False)

    for blocked in (ssh_directory, key_family, private_key_marker):
        assert any(blocked in item for item in failures)


def test_wiki_feature_index_section_column_is_bilingual_and_anchor_bound(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    english = tmp_path / "Feature-Index-en.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace(
            "\u4ece\u8fd9\u91cc\u5f00\u59cb / Start here",
            "\u4ece\u8fd9\u91cc\u5f00\u59cb / Wrong section",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Feature index" in item
        and "section" in item.casefold()
        and "Wrong section" in item
        for item in failures
    )


def test_wiki_feature_route_must_match_screenshot_manifest(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "    surface: {type: app-route, locator: /market}",
            "    surface: {type: app-route, locator: /settings}",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "planned-home" in item
        and "surface does not match" in item
        for item in failures
    )


def test_wiki_manifest_rejects_image_path_escape_and_symlink(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "images/planned-home.png", "images/../escape.png"
        ),
        encoding="utf-8",
    )

    traversal_failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest" in item.casefold() and "escapes Wiki images" in item
        for item in traversal_failures
    )

    _write_wiki(tmp_path)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_png_bytes(640, 360, varied=True))
    images = tmp_path / "images"
    images.mkdir()
    (images / "planned-home.png").symlink_to(outside)

    symlink_failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest" in item.casefold() and "symlink" in item.casefold()
        for item in symlink_failures
    )


def test_captured_wiki_manifest_rejects_arbitrary_image_bytes(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, b"not a raster image")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "decode" in item.casefold()
        for item in failures
    )


def test_final_wiki_manifest_rejects_fictional_page_pair(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Home.md, Home-en.md]", "[Missing.md, Missing-en.md]"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "page_pairs page does not exist" in item
        for item in failures
    )


def test_wiki_manifest_page_pair_matches_feature_targets(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Home.md, Home-en.md]", "[Market-Charts.md, Market-Charts-en.md]"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "planned-home" in item
        and "page_pairs do not match" in item
        for item in failures
    )


def test_captured_wiki_manifest_requires_article_image_references(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "Home.md" in item
        and "must reference images/planned-home.png" in item
        for item in failures
    )


def test_wiki_sidebars_link_to_the_other_language_home(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    sidebar = tmp_path / "_Sidebar.md"
    sidebar.write_text("[English](Home)\n\n[首页](Home)\n", encoding="utf-8")
    english_sidebar = tmp_path / "_Sidebar-en.md"
    english_sidebar.write_text("[中文](Home-en)\n\n[Home](Home-en)\n", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "_Sidebar.md" in failure and "Home-en" in failure for failure in failures
    )
    assert any(
        "_Sidebar-en.md" in failure and "简体中文" in failure for failure in failures
    )


def test_final_wiki_sidebars_require_complete_same_language_navigation(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    sidebar = tmp_path / "_Sidebar.md"
    sidebar.write_text(
        sidebar.read_text(encoding="utf-8")
        .replace("- [Data-Sources-and-Tushare](Data-Sources-and-Tushare)\n", "")
        .replace("(Market-Charts)", "(Market-Charts-en)"),
        encoding="utf-8",
    )
    english_sidebar = tmp_path / "_Sidebar-en.md"
    english_sidebar.write_text(
        english_sidebar.read_text(encoding="utf-8")
        .replace("- [Local-TDX-Data](Local-TDX-Data-en)\n", "")
        .replace("(Formula-Studio-Quickstart-en)", "(Formula-Studio-Quickstart)"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for filename, target in (
        ("_Sidebar.md", "Data-Sources-and-Tushare"),
        ("_Sidebar.md", "Market-Charts-en"),
        ("_Sidebar-en.md", "Local-TDX-Data-en"),
        ("_Sidebar-en.md", "Formula-Studio-Quickstart"),
    ):
        assert any(filename in failure and target in failure for failure in failures)


def test_final_wiki_rejects_legacy_language_aliases_and_replaced_pages(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "Market-Charts.zh-CN.md").write_text("# 旧中文别名\n", encoding="utf-8")
    for filename in EXPECTED_REPLACED_WIKI_PAGES:
        (tmp_path / filename).write_text("# Replaced page\n", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.zh-CN.md" in failure and "legacy" in failure.casefold()
        for failure in failures
    )
    for filename in EXPECTED_REPLACED_WIKI_PAGES:
        assert any(
            filename in failure and "replaced" in failure.casefold()
            for failure in failures
        )


def test_wiki_cannot_be_marked_final_with_screenshot_placeholders(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Market-Charts.md"
    page.write_text(
        page.read_text(encoding="utf-8")
        + "\n<!-- SCREENSHOT_PLACEHOLDER: forbidden final marker -->\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("SCREENSHOT_PLACEHOLDER" in failure for failure in failures)


def test_final_wiki_cli_requires_an_explicit_wiki_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_repository(tmp_path)

    with pytest.raises(SystemExit, match="2"):
        main(["--repo-root", str(tmp_path), "--final-wiki"])

    assert "--final-wiki requires --wiki-root" in capsys.readouterr().err


def test_final_wiki_recursively_scans_checklist_and_nested_markdown(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "PUBLISHING-CHECKLIST.md").write_text(
        "# Publishing checklist\n\nStatus: staging\n\nSCREENSHOT_PLACEHOLDER\n",
        encoding="utf-8",
    )
    nested = tmp_path / "guides" / "advanced.md"
    nested.parent.mkdir()
    nested.write_text(
        "# Advanced\n\n[Missing](missing.md)\n\nopenspec/private.md\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "PUBLISHING-CHECKLIST.md" in failure and "placeholder" in failure.lower()
        for failure in failures
    )
    assert any(
        "PUBLISHING-CHECKLIST.md" in failure and "finalized" in failure
        for failure in failures
    )
    assert any(
        "guides/advanced.md" in failure and "missing.md" in failure
        for failure in failures
    )
    assert any(
        "guides/advanced.md" in failure and "openspec/" in failure
        for failure in failures
    )


def test_final_wiki_rejects_symlinks_path_escapes_and_invalid_images(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_wiki(wiki)
    _finalize_wiki(wiki)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not an image")
    (wiki / "images" / "linked.png").symlink_to(outside)
    nested = wiki / "guides.md"
    nested.write_text(
        "# Unsafe\n\n![Escape](../outside.png)\n\n![Symlink](images/linked.png)\n\n![Directory](images/directory.png)\n",
        encoding="utf-8",
    )
    (wiki / "images" / "directory.png").mkdir()
    (wiki / "images" / "invalid.png").write_bytes(b"not a real screenshot")

    failures = verify_wiki(wiki, final=True)

    assert any("guides.md" in failure and "escapes" in failure for failure in failures)
    assert any(
        "images/linked.png" in failure and "symlink" in failure for failure in failures
    )
    assert any(
        "images/invalid.png" in failure and "decode" in failure for failure in failures
    )
    assert any(
        "images/directory.png" in failure and "scanned publication file" in failure
        for failure in failures
    )


def test_final_wiki_rejects_placeholder_and_internal_publishable_path_names(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    png = (tmp_path / "images" / "MACD-Backtest-Tutorial.png").read_bytes()
    (tmp_path / "images" / "SCREENSHOT_PLACEHOLDER.png").write_bytes(png)
    internal = tmp_path / "openspec" / "private.png"
    internal.parent.mkdir()
    internal.write_bytes(png)

    failures = verify_wiki(tmp_path, final=True)

    assert any("images/SCREENSHOT_PLACEHOLDER.png" in failure for failure in failures)
    assert any("openspec/private.png" in failure for failure in failures)


def test_final_wiki_rejects_every_unsupported_regular_path_before_filtering(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "attachment.pdf").write_bytes(b"%PDF harmless")
    (tmp_path / "notes.txt").write_text(
        "SCREENSHOT_PLACEHOLDER openspec/private.md",
        encoding="utf-8",
    )
    (tmp_path / "unexpected.yml").write_text("private: true\n", encoding="utf-8")
    git_metadata = tmp_path / ".git" / "ignored.txt"
    git_metadata.parent.mkdir()
    git_metadata.write_text("SCREENSHOT_PLACEHOLDER", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "attachment.pdf" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "notes.txt" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "unexpected.yml" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "notes.txt" in failure and "placeholder" in failure.lower()
        for failure in failures
    )
    assert any(
        "notes.txt" in failure and "openspec/" in failure for failure in failures
    )
    assert not any(".git/ignored.txt" in failure for failure in failures)


def test_final_wiki_requires_fully_decoded_useful_raster_screenshots(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    image_dir = tmp_path / "images"
    fake = image_dir / "fake.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-decoded-image")
    tiny = image_dir / "tiny.png"
    tiny.write_bytes(_png_bytes(1, 1, varied=False))
    uniform = image_dir / "uniform.png"
    uniform.write_bytes(_png_bytes(640, 360, varied=False))
    svg = image_dir / "fake.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    page = tmp_path / "MACD-Backtest-Tutorial-en.md"
    document = page.read_text(encoding="utf-8")
    document = document.replace(
        "images/MACD-Backtest-Tutorial-en.png", "images/fake.png"
    )
    document += (
        "\n![Tiny](images/tiny.png)\n"
        "![Uniform](images/uniform.png)\n"
        "![Vector](images/fake.svg)\n"
    )
    page.write_text(document, encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "images/fake.png" in failure and "decode" in failure for failure in failures
    )
    assert any(
        "images/tiny.png" in failure and "dimensions" in failure for failure in failures
    )
    assert any(
        "images/uniform.png" in failure and "content" in failure for failure in failures
    )
    assert any(
        "images/fake.svg" in failure and "unsupported" in failure
        for failure in failures
    )
    assert any(
        "MACD-Backtest-Tutorial-en.md" in failure and "real screenshot" in failure
        for failure in failures
    )


def test_ast_link_policy_covers_reference_html_autolink_and_nested_parentheses(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    page = tmp_path / "guides" / "rendered-links.md"
    page.parent.mkdir()
    page.write_text(
        """# Rendered links

[Reference][missing-reference]

![Reference image][missing-image]

<a href="../escaped.html">escaped HTML</a>

<img src="images/missing-html.png" alt="missing HTML image">

<ftp://example.com/private>

[Nested](missing_(guide).md)

[missing-reference]: missing_(reference).md
[missing-image]: images/missing_(reference).png
""",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for target in (
        "missing_(reference).md",
        "images/missing_(reference).png",
        "../escaped.html",
        "images/missing-html.png",
        "ftp://example.com/private",
        "missing_(guide).md",
    ):
        assert any(
            "guides/rendered-links.md" in failure and target in failure
            for failure in failures
        ), target


def test_wiki_targets_must_be_scanned_and_screenshots_must_resolve_under_images(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    png = _png_bytes(640, 360, varied=True)
    (tmp_path / "root.png").write_bytes(png)
    (tmp_path / "notes.txt").write_text("not publishable", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "private.md").write_text("# Private", encoding="utf-8")
    (git_dir / "private.png").write_bytes(png)

    english = tmp_path / "MACD-Backtest-Tutorial-en.md"
    english.write_text(
        english.read_text(encoding="utf-8")
        .replace("images/MACD-Backtest-Tutorial-en.png", "images/../root.png")
        .replace(
            "## Recovery",
            """[Ignored](notes.txt)
[Literal traversal](images/../notes.txt)
[Git metadata](.git/private.md)
![Git image](.git/private.png)

## Recovery""",
        ),
        encoding="utf-8",
    )
    chinese = tmp_path / "MACD-Backtest-Tutorial.md"
    chinese.write_text(
        chinese.read_text(encoding="utf-8")
        .replace("images/MACD-Backtest-Tutorial.png", "images/%2e%2e/root.png")
        .replace(
            "## 恢复方法",
            """[编码穿越](images/%2e%2e/notes.txt)

## 恢复方法""",
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for target in (
        "notes.txt",
        "images/../notes.txt",
        "images/%2e%2e/notes.txt",
        ".git/private.md",
        ".git/private.png",
    ):
        assert any(
            target in failure and "scanned publication file" in failure
            for failure in failures
        ), target
    assert any(
        "MACD-Backtest-Tutorial-en.md" in failure and "real screenshot" in failure
        for failure in failures
    )
    assert any(
        "MACD-Backtest-Tutorial.md" in failure and "real screenshot" in failure
        for failure in failures
    )


def test_wiki_backup_commands_require_posix_source_or_container_scope(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    backup = tmp_path / "Backup-Restore-Upgrade-and-Uninstall-en.md"
    backup.write_text(
        backup.read_text(encoding="utf-8")
        + "\n`uv run python scripts/backup.py backup.stockdesk-backup`\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Backup-Restore-Upgrade-and-Uninstall-en.md" in failure
        and "source/container POSIX" in failure
        for failure in failures
    )
