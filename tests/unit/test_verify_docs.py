from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re
import struct
import subprocess
from typing import Any
import zlib

import pytest
import yaml

import scripts.verify_docs as verify_docs_module
from scripts.verify_docs import (
    main,
    verify_repository,
    verify_wiki,
)
from stock_desk.formula.compiler import FormulaCompileError, compile_formula
from stock_desk.formula.functions.registry import V1_REGISTRY


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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_COMMIT = "17912f5fa8cb43c1df7c41315b8cd60199b9d403"


def _initialize_fixture_git(root: Path) -> None:
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    object_directory = subprocess.check_output(
        ("git", "rev-parse", "--git-path", "objects"),
        cwd=PROJECT_ROOT,
        text=True,
    ).strip()
    alternates = root / ".git/objects/info/alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
        f"{Path(object_directory).resolve()}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ("git", "-C", str(root), "update-ref", "refs/heads/main", CAPTURE_COMMIT),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/main"),
        check=True,
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
        ("Functions and templates", "函数与模板"),
        ("Trading system", "交易系统"),
        ("Subchart", "副图"),
        ("Validate now", "立即校验"),
        ("Save draft", "保存草稿"),
        ("Save as new version", "保存为新版本"),
        ("Run preview", "运行预览"),
    ),
    "Formula-Compatibility-and-Errors": (
        ("Technical indicator", "技术指标"),
        ("Trading system", "交易系统"),
        ("Functions, fields, or descriptions", "函数、字段或说明"),
        ("Validate now", "立即校验"),
        ("Open formula", "打开公式"),
        ("Save draft", "保存草稿"),
    ),
    "Formula-Versions-and-Safety": (
        ("Open formula", "打开公式"),
        ("Read-only historical versions", "历史版本（只读）"),
        ("Copy to current draft", "复制到当前草稿"),
        ("Save draft", "保存草稿"),
        ("Save as new version", "保存为新版本"),
        ("Formula version", "公式版本"),
        ("Run preview", "运行预览"),
    ),
    "MACD-Backtest-Tutorial": (
        ("Strategy backtest", "策略回测"),
        ("Formula version", "公式版本"),
        ("Backtest scope", "回测范围"),
        ("Single symbol", "单只证券"),
        ("Search securities", "搜索证券"),
        ("Adjustment method", "复权方式"),
        ("Run preflight", "运行预检"),
        ("Submit backtest", "提交回测"),
        ("Run progress", "运行进度"),
        ("Backtest results", "回测结果"),
        ("Task Center", "任务中心"),
    ),
    "A-Share-Execution-and-Costs": (
        ("Execution rules", "执行规则"),
        ("Shares per buy", "每次买入股数"),
        ("Commission (bps)", "佣金（基点）"),
        ("Minimum commission (CNY)", "最低佣金（元）"),
        ("Sell stamp duty (bps)", "卖出印花税（基点）"),
        ("Slippage (bps)", "滑点（基点）"),
        ("Order lifecycle", "订单生命周期"),
    ),
    "Backtest-Metrics-and-Reliability": (
        ("Backtest results", "回测结果"),
        ("Backtest conclusion", "回测结论"),
        ("Grouped performance", "分组表现"),
        ("Pinned snapshot and execution assumptions", "固定快照与执行口径"),
    ),
    "Backtest-Replay-Export-and-Failures": (
        ("Pinned replay", "固定回放"),
        ("Order lifecycle", "订单生命周期"),
        ("Export trades CSV", "导出交易 CSV"),
        ("Export open positions CSV", "导出开放仓位 CSV"),
        ("Export failures CSV", "导出失败 CSV"),
        ("Export logs JSON", "导出日志 JSON"),
        ("Cancel backtest", "取消回测"),
        ("Retry reading logs", "重试读取日志"),
        ("Task Center", "任务中心"),
        ("Open backtest report", "打开回测报告"),
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
        "web/src/features/formulas/FunctionLibrary.tsx",
    ),
    "Formula-Versions-and-Safety": (
        "web/src/features/formulas/FormulaStudioPage.tsx",
        "web/src/features/formulas/FormulaPreview.tsx",
        "web/src/features/backtests/steps/FormulaStep.tsx",
    ),
    "MACD-Backtest-Tutorial": (
        "web/src/app/routes.ts",
        "web/src/features/backtests/BacktestWizard.tsx",
        "web/src/features/backtests/BacktestRunPage.tsx",
        "web/src/features/backtests/RunProgress.tsx",
        "web/src/features/backtests/steps/FormulaStep.tsx",
        "web/src/features/backtests/steps/ScopeStep.tsx",
        "web/src/features/backtests/steps/PeriodStep.tsx",
        "web/src/features/backtests/steps/ReviewStep.tsx",
        "web/src/features/tasks/TaskCenterPage.tsx",
    ),
    "A-Share-Execution-and-Costs": (
        "web/src/features/backtests/steps/CostsStep.tsx",
        "web/src/features/backtests/steps/ReviewStep.tsx",
        "web/src/features/backtests/TradeReplay.tsx",
    ),
    "Backtest-Metrics-and-Reliability": (
        "web/src/features/backtests/BacktestRunPage.tsx",
        "web/src/features/backtests/BacktestReportPage.tsx",
        "web/src/features/backtests/ReportOverview.tsx",
        "web/src/features/backtests/GroupedMetrics.tsx",
    ),
    "Backtest-Replay-Export-and-Failures": (
        "web/src/features/backtests/TradeTable.tsx",
        "web/src/features/backtests/TradeReplay.tsx",
        "web/src/features/backtests/BacktestReportPage.tsx",
        "web/src/features/backtests/BacktestRunPage.tsx",
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

EXPECTED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE = {
    "Formula-Studio-Quickstart": {
        "公式工作台": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "自定义公式": ("web/src/app/routes.ts", "route_label"),
        "函数与模板": ("web/src/features/formulas/FunctionLibrary.tsx", "jsx_text"),
        "交易系统": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "副图": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "立即校验": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "保存草稿": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "保存为新版本": (
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "button_expression",
        ),
        "运行预览": (
            "web/src/features/formulas/FormulaPreview.tsx",
            "button_expression",
        ),
    },
    "Formula-Compatibility-and-Errors": {
        "技术指标": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "交易系统": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "函数、字段或说明": (
            "web/src/features/formulas/FunctionLibrary.tsx",
            "placeholder",
        ),
        "立即校验": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "打开公式": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "保存草稿": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
    },
    "Formula-Versions-and-Safety": {
        "打开公式": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "历史版本（只读）": (
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "jsx_text",
        ),
        "复制到当前草稿": (
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "jsx_text",
        ),
        "保存草稿": ("web/src/features/formulas/FormulaStudioPage.tsx", "jsx_text"),
        "保存为新版本": (
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "button_expression",
        ),
        "公式版本": ("web/src/features/backtests/steps/FormulaStep.tsx", "jsx_text"),
        "运行预览": (
            "web/src/features/formulas/FormulaPreview.tsx",
            "button_expression",
        ),
    },
    "MACD-Backtest-Tutorial": {
        "策略回测": ("web/src/app/routes.ts", "route_label"),
        "公式版本": ("web/src/features/backtests/steps/FormulaStep.tsx", "jsx_text"),
        "回测范围": ("web/src/features/backtests/steps/ScopeStep.tsx", "jsx_text"),
        "单只证券": ("web/src/features/backtests/steps/ScopeStep.tsx", "jsx_text"),
        "搜索证券": (
            "web/src/features/backtests/steps/ScopeStep.tsx",
            "button_expression",
        ),
        "复权方式": ("web/src/features/backtests/steps/PeriodStep.tsx", "jsx_text"),
        "运行预检": (
            "web/src/features/backtests/steps/ReviewStep.tsx",
            "button_expression",
        ),
        "提交回测": (
            "web/src/features/backtests/BacktestWizard.tsx",
            "button_expression",
        ),
        "运行进度": ("web/src/features/backtests/RunProgress.tsx", "jsx_text"),
        "回测结果": ("web/src/features/backtests/BacktestRunPage.tsx", "jsx_text"),
        "任务中心": ("web/src/features/tasks/TaskCenterPage.tsx", "jsx_text"),
    },
    "A-Share-Execution-and-Costs": {
        "执行规则": ("web/src/features/backtests/steps/ReviewStep.tsx", "jsx_text"),
        "每次买入股数": ("web/src/features/backtests/steps/CostsStep.tsx", "jsx_text"),
        "佣金（基点）": ("web/src/features/backtests/steps/CostsStep.tsx", "jsx_text"),
        "最低佣金（元）": (
            "web/src/features/backtests/steps/CostsStep.tsx",
            "jsx_text",
        ),
        "卖出印花税（基点）": (
            "web/src/features/backtests/steps/CostsStep.tsx",
            "jsx_text",
        ),
        "滑点（基点）": ("web/src/features/backtests/steps/CostsStep.tsx", "jsx_text"),
        "订单生命周期": ("web/src/features/backtests/TradeReplay.tsx", "jsx_text"),
    },
    "Backtest-Metrics-and-Reliability": {
        "回测结果": ("web/src/features/backtests/BacktestRunPage.tsx", "jsx_text"),
        "回测结论": ("web/src/features/backtests/ReportOverview.tsx", "jsx_text"),
        "分组表现": ("web/src/features/backtests/GroupedMetrics.tsx", "jsx_text"),
        "固定快照与执行口径": (
            "web/src/features/backtests/BacktestReportPage.tsx",
            "jsx_text",
        ),
    },
    "Backtest-Replay-Export-and-Failures": {
        "固定回放": ("web/src/features/backtests/TradeTable.tsx", "button_expression"),
        "订单生命周期": ("web/src/features/backtests/TradeReplay.tsx", "jsx_text"),
        "导出交易 CSV": (
            "web/src/features/backtests/BacktestReportPage.tsx",
            "jsx_text",
        ),
        "导出开放仓位 CSV": (
            "web/src/features/backtests/BacktestReportPage.tsx",
            "jsx_text",
        ),
        "导出失败 CSV": (
            "web/src/features/backtests/BacktestReportPage.tsx",
            "jsx_text",
        ),
        "导出日志 JSON": (
            "web/src/features/backtests/BacktestReportPage.tsx",
            "jsx_text",
        ),
        "取消回测": (
            "web/src/features/backtests/BacktestRunPage.tsx",
            "button_expression",
        ),
        "重试读取日志": (
            "web/src/features/backtests/BacktestRunPage.tsx",
            "button_expression",
        ),
        "任务中心": ("web/src/features/tasks/TaskCenterPage.tsx", "jsx_text"),
        "打开回测报告": ("web/src/features/tasks/TaskCenterPage.tsx", "jsx_text"),
        "取消任务": (
            "web/src/features/tasks/TaskCenterPage.tsx",
            "button_expression",
        ),
    },
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
        (
            "MACD 金叉 / 死叉",
            "函数与模板",
            "交易系统",
            "副图",
            "DIF:EMA(C,12)-EMA(C,26)",
            "输入后会自动校验",
            "Ctrl/⌘ + Enter",
            "保存为新版本",
            "运行预览",
            "K 线主图与公式副图",
            "BUY 买点",
            "SELL 卖点",
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "2026-07-08",
            "54 个买点",
            "55 个卖点",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "待截图元数据",
            "不是已捕获声明",
            "不公开原始行",
            "保存草稿可以保留尚未通过校验的文本",
            "不会生成可预览或可回测版本",
            "复制公式",
            "技术指标主要用于绘图输出",
            "即使技术指标保存了 BUY/SELL",
            "也不会出现在回测向导",
        ),
        ("编辑草稿时会自动运行预览", "直接预览未保存草稿"),
    ),
    "Formula-Studio-Quickstart-en.md": (
        (
            "MACD golden-cross / death-cross",
            "Functions and templates（函数与模板）",
            "Trading system（交易系统）",
            "Subchart（副图）",
            "DIF:EMA(C,12)-EMA(C,26)",
            "validates automatically after input",
            "Ctrl/⌘ + Enter",
            "Save as new version（保存为新版本）",
            "K-line main chart and formula subchart",
            "BUY 买点",
            "SELL 卖点",
            "Run preview（运行预览）",
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "2026-07-08",
            "54 BUY signals",
            "55 SELL signals",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "future-screenshot metadata",
            "not a capture-complete claim",
            "No raw rows",
            "Save draft（保存草稿） can preserve text that has not passed validation",
            "does not create a previewable or backtestable version",
            "Copy formula",
            "Technical indicator（技术指标） is intended for plotted outputs",
            "can still save BUY/SELL outputs",
            "does not appear in the backtest wizard",
        ),
        (
            "preview runs automatically while editing",
            "preview an unsaved draft directly",
        ),
    ),
    "Formula-Compatibility-and-Errors.md": (
        (
            "技术指标",
            "交易系统",
            "`:=` 声明隐藏中间量",
            "`:` 声明公开输出",
            "公开的非信号输出必须是数值",
            "BUY 和 SELL 必须成对且是可见布尔输出",
            "自动补全、函数帮助和参数提示",
            "第 1 行，第 1 列",
            "`formula_syntax_error`",
            "`unsupported_function`",
            "`invalid_argument_count`",
            "`invalid_signal_output`",
            "`future_data`",
            "`repainting`",
            "[完整兼容清单](https://github.com/CongBao/stock-desk/blob/main/docs/formula-compatibility.md)",
            "`tdx-v1`",
            "条件选股、五彩 K 线、平台专有绘图、外部数据",
            "从第一条诊断开始",
            "逐段替换",
            "保存草稿",
            "不能预览、保存为新版本或用于回测",
            "页面不显示稳定诊断码",
            "高级 / API 诊断码参考",
            "当前 `tdx-v1` 的 17 个函数",
            "仅有 `current_only` 或 `past_only`",
            "`repainting` 是为未来兼容登记表保留的安全诊断",
            "未登记的不安全函数先返回 `unsupported_function`",
            "技术指标主要用于绘图输出",
            "即使保存 BUY/SELL",
            "不会进入回测向导",
        ),
        (
            "完全兼容所有通达信公式",
            "自动修复粘贴公式",
            "查看诊断代码",
            "搜索函数或模板",
            "打开已保存公式",
        ),
    ),
    "Formula-Compatibility-and-Errors-en.md": (
        (
            "Technical indicator（技术指标）",
            "Trading system（交易系统）",
            "`:=` declares a hidden intermediate",
            "`:` declares a public output",
            "public non-signal outputs must be numeric",
            "BUY and SELL must appear as a pair of visible Boolean outputs",
            "autocomplete, function help, and parameter hints",
            "line 1, column 1",
            "`formula_syntax_error`",
            "`unsupported_function`",
            "`invalid_argument_count`",
            "`invalid_signal_output`",
            "`future_data`",
            "`repainting`",
            "[complete compatibility list](https://github.com/CongBao/stock-desk/blob/main/docs/formula-compatibility.md)",
            "`tdx-v1`",
            "condition selection, colored K-lines, platform-specific drawing, and external data",
            "start with the first diagnostic",
            "replace one section at a time",
            "Save draft（保存草稿）",
            "cannot be previewed, saved as a new version, or used in a backtest",
            "The visible panel does not show stable diagnostic codes",
            "Advanced / API diagnostic-code reference",
            "The current `tdx-v1` registry has 17 functions",
            "only `current_only` or `past_only`",
            "`repainting` is a reserved safety diagnostic for a future compatibility registry",
            "an unregistered unsafe function first returns `unsupported_function`",
            "Technical indicator（技术指标） is intended for plotted outputs",
            "can still save BUY/SELL",
            "does not enter the backtest wizard",
        ),
        (
            "fully compatible with every TongdaXin formula",
            "automatically repairs pasted formulas",
            "inspect the diagnostic code",
            "Search functions or templates（搜索函数或模板）",
            "Open saved formula（打开已保存公式）",
        ),
    ),
    "Formula-Versions-and-Safety.md": (
        (
            "保存草稿",
            "保存为新版本",
            "历史版本（只读）",
            "复制到当前草稿",
            "公式版本",
            "回测只能选择已保存且可执行的交易公式版本",
            "当前版本没有公式启用、停用或删除控件",
            "也没有对应 API",
            "已保存版本不能从 UI 删除",
            "回测对公式版本的引用不会被用户操作悬空",
            "`future_data`",
            "`repainting`",
            "阻止预览、保存为新版本和回测",
            "受控语法",
            "不执行 Python 或其他任意代码",
            "不提供文件或网络访问",
            "3 秒执行上限",
            "独立计算进程",
            "`revision_conflict`",
            "重新打开最新版本",
            "创建下一个不可变版本",
            "编辑、校验并保存为新版本后，再运行预览",
            "当前 `tdx-v1` 不登记 `future` 或 `repainting` 函数",
            "`repainting` 是未来兼容登记表的保留安全诊断",
            "未登记函数先返回 `unsupported_function`",
        ),
        ("修改历史版本", "点击停用公式", "点击删除版本"),
    ),
    "Formula-Versions-and-Safety-en.md": (
        (
            "Save draft（保存草稿）",
            "Save as new version（保存为新版本）",
            "Read-only historical versions（历史版本（只读））",
            "Copy to current draft（复制到当前草稿）",
            "Formula version（公式版本）",
            "A backtest can select only a saved, executable trading-formula version",
            "The current release has no formula enable, disable, or delete control",
            "and no corresponding API",
            "Saved versions cannot be deleted from the UI",
            "user actions cannot leave a backtest formula-version reference dangling",
            "`future_data`",
            "`repainting`",
            "block preview, saving a new version, and backtesting",
            "controlled grammar",
            "does not execute Python or other arbitrary code",
            "provides no file or network access",
            "3-second execution limit",
            "isolated computation process",
            "`revision_conflict`",
            "reopen the latest formula",
            "create the next immutable version",
            "edit, validate, save as a new version, and only then run preview",
            "The current `tdx-v1` registry contains no `future` or `repainting` function",
            "`repainting` is reserved for a future compatibility registry",
            "an unregistered function first returns `unsupported_function`",
        ),
        (
            "modify a historical version",
            "click Disable formula",
            "click Delete version",
        ),
    ),
    "MACD-Backtest-Tutorial.md": (
        (
            "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "2026-07-08",
            "公式版本 ID 待最终截图固化",
            "待截图元数据",
            "不是已捕获声明",
            "不公开原始行情行",
            "五步向导",
            "公式版本",
            "单只证券",
            "搜索证券",
            "回测周期",
            "日线、周线和 60 分钟",
            "开始日期（上海时区，含）",
            "结束日期（上海时区，不含）",
            "运行预检",
            "可运行 1 / 1",
            "提交回测",
            "回测运行",
            "运行进度",
            "任务中心",
            "回测结果",
            "任何配置修改都会使服务端预检失效",
            "不填写或承诺尚未计算的胜率、收益率和交易笔数",
        ),
        (
            "预检会创建任务",
            "提交后只在任务中心等待",
            "编辑公式代码完成回测",
            "导出回执",
            "回测结果为 100%",
        ),
    ),
    "MACD-Backtest-Tutorial-en.md": (
        (
            "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "2026-07-08",
            "formula-version ID remains pending until final capture",
            "future-screenshot metadata",
            "not a capture-complete claim",
            "No raw market rows",
            "five-step wizard",
            "Formula version（公式版本）",
            "Single symbol（单只证券）",
            "Search securities（搜索证券）",
            "backtest period",
            "daily, weekly, and 60-minute",
            "inclusive Shanghai-time start",
            "exclusive Shanghai-time end",
            "Run preflight（运行预检）",
            "1 runnable / 1 total",
            "Submit backtest（提交回测）",
            "Backtest run（回测运行）",
            "Run progress（运行进度）",
            "Task Center（任务中心）",
            "Backtest results（回测结果）",
            "Any configuration change invalidates the server preflight",
            "does not fill in or promise an uncomputed win rate, return, or trade count",
        ),
        (
            "preflight creates a task",
            "wait only in Task Center after submission",
            "edit formula code to complete the backtest",
            "export receipt",
            "backtest result is 100%",
        ),
    ),
    "A-Share-Execution-and-Costs.md": (
        (
            "收盘信号后下一对应周期开盘尝试成交",
            "日线信号不会在同一根 K 线成交",
            "60 分钟信号可以在同一交易日的下一根可交易 K 线开盘尝试成交",
            "周线信号从下一周的首个可交易开盘开始尝试",
            "T+1 只约束卖出",
            "100 股整数倍",
            "停牌",
            "买入遇涨停开盘受阻",
            "卖出遇跌停开盘受阻",
            "预检只核对执行状态覆盖和冻结规则身份",
            "逐成交点的停牌、涨跌停与 T+1 在运行时判断",
            "仅当股票池仍至少有 1 只可运行证券时，缺失状态覆盖的成员才在预检显示 `missing_execution_status` 数据不足缺口",
            "单股无执行状态覆盖或股票池无任何可运行证券时，预检整体失败",
            "普通界面只显示“预检失败，请检查本地服务和数据覆盖后重试。”，不显示缺口样例",
            "冻结状态引用存在但逐点证据不完整时，当前版本把该证券记为普通失败 `symbol_execution_failed`",
            "待成交订单会保留到首个可成交时点",
            "相反信号会撤销待成交订单",
            "同一证券同一时刻最多一个持仓",
            "重复买入和空仓卖出会被忽略",
            "佣金按买卖两侧分别计算，并分别应用最低佣金",
            "印花税只在卖出侧计算",
            "滑点对买入加价、对卖出减价",
            "开放仓位只展示浮动结果，不进入已实现胜率",
            "执行规则不是可展开或可编辑控件",
            "订单生命周期",
        ),
        (
            "信号价就是成交价",
            "买入也收印花税",
            "T+1 禁止当日买入",
            "可修改执行规则",
            "回测使用共享资金池",
            "逐点证据不完整也记为数据不足",
            "预检逐成交点模拟 T+1",
            "单股无覆盖会显示 missing_execution_status 缺口样例",
            "全池无可运行证券仍返回部分预检",
        ),
    ),
    "A-Share-Execution-and-Costs-en.md": (
        (
            "a close signal attempts execution at the next corresponding-period open",
            "A daily signal never fills on the same bar",
            "A 60-minute signal can attempt a fill at the next tradable bar open on the same trading day",
            "A weekly signal starts attempting fills at the first tradable open of the next week",
            "T+1 constrains sells only",
            "100-share multiple",
            "suspension",
            "a buy is blocked when the open is limit-up",
            "a sell is blocked when the open is limit-down",
            "Preflight checks only execution-status coverage and frozen rule identities",
            "Suspension, price limits, and T+1 are evaluated at each candidate fill during execution",
            "A missing-status member appears as a `missing_execution_status` data-insufficient gap only when the pool still has at least one runnable symbol",
            "A single symbol without status coverage, or a pool with no runnable symbol, fails preflight as a whole",
            "The normal UI shows only `预检失败，请检查本地服务和数据覆盖后重试。` and no gap sample",
            "When a frozen status reference exists but per-point evidence is incomplete, the current release records an ordinary `symbol_execution_failed` failure",
            "A pending order remains until the first executable point",
            "An opposite signal cancels the pending order",
            "one position per symbol at a time",
            "Repeated buys while held and sells while flat are ignored",
            "Commission is calculated independently on both sides and each side applies the minimum commission",
            "Stamp duty is calculated only on the sell side",
            "Slippage raises buy fills and lowers sell fills",
            "Open positions show floating results but do not enter realized win rate",
            "Execution rules（执行规则） is evidence text, not an expandable or editable control",
            "Order lifecycle（订单生命周期）",
        ),
        (
            "signal price is the fill price",
            "stamp duty applies to buys",
            "T+1 prevents same-day buys",
            "edit the execution rules",
            "uses a shared capital pool",
            "incomplete per-point evidence is data insufficient",
            "preflight simulates T+1 at every fill point",
            "a single symbol without coverage shows a missing_execution_status gap sample",
            "a zero-runnable pool still returns a partial preflight",
        ),
    ),
    "Backtest-Metrics-and-Reliability.md": (
        (
            "样本是已平仓的独立交易，不是组合收益",
            "胜率 = 净收益大于 0 的已实现样本数 ÷ 已实现样本数",
            "净收益等于 0 不算胜利",
            "平均单笔净收益",
            "中位单笔净收益",
            "盈亏比是正收益样本的平均净收益除以负收益样本平均净收益的绝对值",
            "最大单笔盈利",
            "最大单笔亏损",
            "平均持有 K 线",
            "平均持有天数",
            "九档收益分布",
            "按股票、按月和按年",
            "失败",
            "数据不足",
            "未处理",
            "开放仓位",
            "少于 30 个已实现样本为低可靠性",
            "30 至 99 个且最大单一证券占比不超过 50% 为中可靠性",
            "至少 100 个且最大单一证券占比不超过 50% 为高可靠性",
            "单一证券占比超过 50% 仍为低可靠性",
            "当前版本不计算组合资金曲线、最大回撤或基准超额收益",
            "最大单笔亏损不能当作最大回撤",
            "固定快照与执行口径",
            "证券数据集",
            "来源证据",
            "聚合结论不可计算",
            "无已实现样本",
            "历史胜率不是预测",
        ),
        (
            "最大单笔亏损就是最大回撤",
            "提供基准超额收益",
            "胜率可以预测未来",
            "失败证券不影响结论范围",
        ),
    ),
    "Backtest-Metrics-and-Reliability-en.md": (
        (
            "Samples are realized independent trades, not portfolio returns",
            "Win rate equals realized samples with net return above zero divided by all realized samples",
            "A zero net return is not a win",
            "average net return per trade",
            "median net return per trade",
            "Payoff ratio is mean positive net return divided by the absolute mean negative net return",
            "best single trade",
            "worst single trade",
            "average holding bars",
            "average holding days",
            "nine-bin return distribution",
            "by symbol, month, and year",
            "failed",
            "data insufficient",
            "unprocessed",
            "open positions",
            "Fewer than 30 realized samples is low reliability",
            "30 to 99 with no symbol above 50% is medium reliability",
            "At least 100 with no symbol above 50% is high reliability",
            "Any symbol above 50% keeps reliability low",
            "The current release does not calculate a portfolio equity curve, maximum drawdown, or benchmark excess return",
            "Worst single trade is not maximum drawdown",
            "Pinned snapshot and execution assumptions（固定快照与执行口径）",
            "instrument dataset",
            "provenance digest",
            "Aggregate conclusion unavailable",
            "No realized samples",
            "Historical win rate is not a forecast",
        ),
        (
            "worst trade is maximum drawdown",
            "provides benchmark excess return",
            "win rate predicts the future",
            "failed symbols do not affect conclusion scope",
        ),
    ),
    "Backtest-Replay-Export-and-Failures.md": (
        (
            "交易明细",
            "固定回放",
            "订单生命周期",
            "信号已忽略、执行受阻、委托已撤销、委托成交、委托待执行、区间结束未成交和开放仓位标记",
            "固定 SignalSeries",
            "固定执行行情证据",
            "导出交易 CSV",
            "导出开放仓位 CSV",
            "导出失败 CSV",
            "导出日志 JSON",
            "浏览器直接下载文件，不显示导出回执",
            "任务中心",
            "股票池回测进度",
            "当前阶段",
            "已处理 / 总数",
            "失败记录",
            "运行日志",
            "安全事件时间线不是运行日志",
            "取消回测",
            "取消任务",
            "取消不会删除已持久化的数据",
            "取消请求结果未知时先刷新状态",
            "重试读取日志",
            "打开回测报告",
            "恢复上次草稿",
            "没有一键重跑或只重试失败证券的控件",
            "新任务会重新预检并冻结新的快照身份",
            "相同可见配置不保证沿用旧数据快照",
            "普通界面只有恢复草稿后重新提交",
            "高级 API：`POST /api/backtests/{run_id}/copy`",
            '请求体 `{"mode":"exact"}` 复用原 `snapshot_id` 和全部冻结输入',
            '请求体 `{"mode":"latest"}` 才按原意图重建最新快照',
            "`exact` 和 `latest` 都创建新的运行 ID 与任务 ID",
            "`mode` 必填，不能省略",
            "旧结果的快照、结果哈希、公式版本和来源证据不会被新任务改写",
        ),
        (
            "页面显示导出回执",
            "点击一键重跑按钮",
            "点击只重试失败证券",
            "编辑 CSV 会更新历史结果",
            "关闭浏览器会取消任务",
            "普通界面提供 exact 复制",
            "exact 重建最新快照",
            "latest 复用原 snapshot_id",
            "省略 mode 自动使用 exact",
        ),
    ),
    "Backtest-Replay-Export-and-Failures-en.md": (
        (
            "Trade details",
            "Pinned replay（固定回放）",
            "Order lifecycle（订单生命周期）",
            "ignored signal, execution blocked, order cancelled, order filled, order pending, range ended unfilled, and open-position mark",
            "pinned SignalSeries",
            "pinned execution-bar evidence",
            "Export trades CSV（导出交易 CSV）",
            "Export open positions CSV（导出开放仓位 CSV）",
            "Export failures CSV（导出失败 CSV）",
            "Export logs JSON（导出日志 JSON）",
            "The browser downloads the file directly and shows no export receipt",
            "Task Center（任务中心）",
            "pool-backtest progress",
            "current stage",
            "processed / total",
            "failure records",
            "runtime logs",
            "Security event timeline is not a runtime log",
            "Cancel backtest（取消回测）",
            "Cancel task（取消任务）",
            "Cancellation does not delete persisted data",
            "If the cancellation outcome is unknown, refresh status first",
            "Retry reading logs（重试读取日志）",
            "Open backtest report（打开回测报告）",
            "Restore last draft（恢复上次草稿）",
            "There is no one-click rerun or retry-failed-symbols control",
            "A new task runs preflight again and freezes a new snapshot identity",
            "The same visible configuration does not guarantee reuse of the old data snapshot",
            "The normal UI only restores a draft and resubmits it",
            "Advanced API: `POST /api/backtests/{run_id}/copy`",
            'Body `{"mode":"exact"}` reuses the original `snapshot_id` and every frozen input',
            'Body `{"mode":"latest"}` is the mode that rebuilds a latest snapshot from the original intent',
            "Both `exact` and `latest` create a new run ID and task ID",
            "`mode` is required and cannot be omitted",
            "A new task never rewrites the old result's snapshot, result hash, formula version, or provenance",
        ),
        (
            "The page shows an export receipt",
            "Select the one-click rerun button",
            "Select the retry failed symbols button",
            "editing a CSV updates historical results",
            "closing the browser cancels the task",
            "the normal UI provides exact copy",
            "exact rebuilds the latest snapshot",
            "latest reuses the original snapshot_id",
            "omitting mode defaults to exact",
        ),
    ),
    "Model-Provider-Setup.md": (
        (
            "当前版本只提供 DeepSeek、OpenAI-compatible 和 Ollama 三种提供商",
            "提供商",
            "Base URL",
            "模型",
            "API Key",
            "Temperature",
            "超时（秒）",
            "最大输出 Tokens",
            "最大重试次数是每次新建分析的 0–5 参数，不是模型配置字段",
            "已验证",
            "错误代码",
        ),
        ("DeepSeek V4 已内置", "Ollama 需要 API Key", "模型设置中的重试次数"),
    ),
    "Model-Provider-Setup-en.md": (
        (
            "The current release offers exactly DeepSeek, OpenAI-compatible, and Ollama",
            "Provider（提供商）",
            "Base URL（Base URL）",
            "Model（模型）",
            "API Key（API Key）",
            "temperature",
            "timeout",
            "maximum output",
            "Maximum retries is a per-run value from 0 to 5, not a model-setting field",
            "Verified（已验证）",
            "Error code（错误代码）",
        ),
        (
            "DeepSeek V4 is built in",
            "Ollama requires an API key",
            "retry count in model settings",
        ),
    ),
    "Task-Center.md": (
        (
            "状态筛选",
            "类型筛选",
            "安全任务摘要",
            "安全事件时间线",
            "取消任务",
            "任务中心读取最近 100 项任务的安全视图",
            "只显示经过约束的可见审计事件，不是运行日志",
            "当前只有包含 `backtest_run` target 的任务显示回测报告深链",
            "任务运行中也可以显示",
            "行情更新与智能分析当前不提供任务中心深链",
            "取消结果未知时先刷新任务状态，再决定是否重试",
        ),
        (
            "时间筛选控件",
            "显示完整运行日志",
            "所有任务都有结果深链",
            "仅已完成任务显示回测报告",
        ),
    ),
    "Task-Center-en.md": (
        (
            "Status filter（状态筛选）",
            "Type filter（类型筛选）",
            "safe summary",
            "Security event timeline（安全事件时间线）",
            "Open backtest report（打开回测报告）",
            "Cancel task（取消任务）",
            "Task Center reads the safe view for the most recent 100 tasks",
            "constrained visible audit events, not runtime logs",
            "Only a task with a `backtest_run` target currently exposes a report deep link",
            "including while a task is still running",
            "Market updates and analysis currently have no Task Center result deep link",
            "When cancellation outcome is unknown, refresh task state before deciding whether to retry",
        ),
        (
            "time-filter control",
            "shows complete runtime logs",
            "every task has a result deep link",
            "only completed tasks show a backtest report",
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


EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS = {
    "Formula-Studio-Quickstart.md": (
        (
            "保存为新版本后才能运行预览",
            "web/src/features/formulas/FormulaPreview.tsx",
            "预览只运行已保存且校验通过的不可变版本",
        ),
        (
            "保存草稿可以保留尚未通过校验的文本",
            "src/stock_desk/formula/repository.py",
            "executable_version_id=None",
        ),
        (
            "预览结果绑定数据集版本、数据截止时间、公式版本和公式摘要",
            "web/src/features/formulas/formulaApi.ts",
            "readonly formulaChecksum: string;",
        ),
        (
            "技术指标主要用于绘图输出；即使技术指标保存了 BUY/SELL，也不会出现在回测向导",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "item.formulaType === 'trading'",
        ),
    ),
    "Formula-Studio-Quickstart-en.md": (
        (
            "Run preview（运行预览） is available only after Save as new version（保存为新版本）",
            "web/src/features/formulas/FormulaPreview.tsx",
            "预览只运行已保存且校验通过的不可变版本",
        ),
        (
            "Save draft（保存草稿） can preserve text that has not passed validation",
            "src/stock_desk/formula/repository.py",
            "executable_version_id=None",
        ),
        (
            "The preview result binds dataset version, data cutoff, formula version, and formula checksum",
            "web/src/features/formulas/formulaApi.ts",
            "readonly formulaChecksum: string;",
        ),
        (
            "Technical indicator（技术指标） is intended for plotted outputs; it can still save BUY/SELL outputs, but it does not appear in the backtest wizard",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "item.formulaType === 'trading'",
        ),
    ),
    "Formula-Compatibility-and-Errors.md": (
        (
            "公开的非信号输出必须是数值",
            "src/stock_desk/formula/compiler.py",
            "public non-signal outputs must be numeric",
        ),
        (
            "BUY 和 SELL 必须成对且是可见布尔输出",
            "src/stock_desk/formula/compiler.py",
            "BUY and SELL must be visible boolean outputs",
        ),
        (
            "自动补全、函数帮助和参数提示",
            "web/src/features/formulas/tdxLanguage.ts",
            "registerSignatureHelpProvider",
        ),
        (
            "页面不显示稳定诊断码",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "<strong>{diagnostic.explanation}</strong>",
        ),
        (
            "`repainting` 是为未来兼容登记表保留的安全诊断",
            "src/stock_desk/formula/analysis.py",
            'elif behavior == "repainting":',
        ),
    ),
    "Formula-Compatibility-and-Errors-en.md": (
        (
            "public non-signal outputs must be numeric",
            "src/stock_desk/formula/compiler.py",
            "public non-signal outputs must be numeric",
        ),
        (
            "BUY and SELL must appear as a pair of visible Boolean outputs",
            "src/stock_desk/formula/compiler.py",
            "BUY and SELL must be visible boolean outputs",
        ),
        (
            "autocomplete, function help, and parameter hints",
            "web/src/features/formulas/tdxLanguage.ts",
            "registerSignatureHelpProvider",
        ),
        (
            "The visible panel does not show stable diagnostic codes",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "<strong>{diagnostic.explanation}</strong>",
        ),
        (
            "`repainting` is a reserved safety diagnostic for a future compatibility registry",
            "src/stock_desk/formula/analysis.py",
            'elif behavior == "repainting":',
        ),
    ),
    "Formula-Versions-and-Safety.md": (
        (
            "历史版本（只读）",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "为不可变历史版本",
        ),
        (
            "回测只能选择已保存且可执行的交易公式版本",
            "web/src/features/backtests/steps/FormulaStep.tsx",
            "选择已保存、可执行的交易公式版本",
        ),
        (
            "3 秒执行上限",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "公式预览超过 3 秒执行上限",
        ),
    ),
    "Formula-Versions-and-Safety-en.md": (
        (
            "Read-only historical versions（历史版本（只读））",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "为不可变历史版本",
        ),
        (
            "A backtest can select only a saved, executable trading-formula version",
            "web/src/features/backtests/steps/FormulaStep.tsx",
            "选择已保存、可执行的交易公式版本",
        ),
        (
            "3-second execution limit",
            "web/src/features/formulas/FormulaStudioPage.tsx",
            "公式预览超过 3 秒执行上限",
        ),
    ),
}


EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS = {
    "MACD-Backtest-Tutorial.md": (
        (
            "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
            "src/stock_desk/formula/service.py",
            "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
        ),
        (
            "任何配置修改都会使服务端预检失效",
            "web/src/features/backtests/BacktestWizard.tsx",
            "任何配置修改都会使服务端预检失效",
        ),
        (
            "提交后自动进入“回测运行”",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "navigate(`/backtests/${submission.runId}`",
        ),
    ),
    "MACD-Backtest-Tutorial-en.md": (
        (
            "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
            "src/stock_desk/formula/service.py",
            "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);",
        ),
        (
            "Any configuration change invalidates the server preflight",
            "web/src/features/backtests/BacktestWizard.tsx",
            "任何配置修改都会使服务端预检失效",
        ),
        (
            "Submission opens Backtest run（回测运行） automatically",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "navigate(`/backtests/${submission.runId}`",
        ),
    ),
    "A-Share-Execution-and-Costs.md": (
        (
            "收盘信号后下一对应周期开盘尝试成交",
            "web/src/features/backtests/steps/ReviewStep.tsx",
            "收盘信号后下一对应周期开盘尝试成交",
        ),
        (
            "T+1 只约束卖出",
            "src/stock_desk/backtest/constraints.py",
            'if side == "sell":',
        ),
        (
            "相反信号会撤销待成交订单",
            "src/stock_desk/backtest/state_machine.py",
            "CancellationReason.OPPOSITE_SIGNAL",
        ),
        (
            "佣金按买卖两侧分别计算，并分别应用最低佣金",
            "src/stock_desk/backtest/costs.py",
            "commission = max(",
        ),
        (
            "仅当股票池仍至少有 1 只可运行证券时，缺失状态覆盖的成员才在预检显示 `missing_execution_status` 数据不足缺口",
            "src/stock_desk/backtest/service.py",
            'else "missing_execution_status"',
        ),
        (
            "单股无执行状态覆盖或股票池无任何可运行证券时，预检整体失败",
            "src/stock_desk/backtest/service.py",
            "if runnable_count == 0:",
        ),
        (
            "普通界面只显示“预检失败，请检查本地服务和数据覆盖后重试。”，不显示缺口样例",
            "web/src/features/backtests/BacktestWizard.tsx",
            "预检失败，请检查本地服务和数据覆盖后重试。",
        ),
        (
            "冻结状态引用存在但逐点证据不完整时，当前版本把该证券记为普通失败 `symbol_execution_failed`",
            "src/stock_desk/backtest/pool_runner.py",
            'reason = "symbol_execution_failed"',
        ),
    ),
    "A-Share-Execution-and-Costs-en.md": (
        (
            "a close signal attempts execution at the next corresponding-period open",
            "web/src/features/backtests/steps/ReviewStep.tsx",
            "收盘信号后下一对应周期开盘尝试成交",
        ),
        (
            "T+1 constrains sells only",
            "src/stock_desk/backtest/constraints.py",
            'if side == "sell":',
        ),
        (
            "An opposite signal cancels the pending order",
            "src/stock_desk/backtest/state_machine.py",
            "CancellationReason.OPPOSITE_SIGNAL",
        ),
        (
            "Commission is calculated independently on both sides and each side applies the minimum commission",
            "src/stock_desk/backtest/costs.py",
            "commission = max(",
        ),
        (
            "A missing-status member appears as a `missing_execution_status` data-insufficient gap only when the pool still has at least one runnable symbol",
            "src/stock_desk/backtest/service.py",
            'else "missing_execution_status"',
        ),
        (
            "A single symbol without status coverage, or a pool with no runnable symbol, fails preflight as a whole",
            "src/stock_desk/backtest/service.py",
            "if runnable_count == 0:",
        ),
        (
            "The normal UI shows only `预检失败，请检查本地服务和数据覆盖后重试。` and no gap sample",
            "web/src/features/backtests/BacktestWizard.tsx",
            "预检失败，请检查本地服务和数据覆盖后重试。",
        ),
        (
            "When a frozen status reference exists but per-point evidence is incomplete, the current release records an ordinary `symbol_execution_failed` failure",
            "src/stock_desk/backtest/pool_runner.py",
            'reason = "symbol_execution_failed"',
        ),
    ),
    "Backtest-Metrics-and-Reliability.md": (
        (
            "胜率 = 净收益大于 0 的已实现样本数 ÷ 已实现样本数",
            "src/stock_desk/backtest/metrics.py",
            "win_rate = _ratio(len(positive), count) if count else None",
        ),
        (
            "少于 30 个已实现样本为低可靠性",
            "src/stock_desk/backtest/metrics.py",
            "if realized_count < 30:",
        ),
        (
            "单一证券占比超过 50% 仍为低可靠性",
            "src/stock_desk/backtest/metrics.py",
            'largest_symbol_share > Decimal("0.500000")',
        ),
        (
            "当前版本不计算组合资金曲线、最大回撤或基准超额收益",
            "src/stock_desk/backtest/metrics.py",
            '"equity_curve": None',
        ),
    ),
    "Backtest-Metrics-and-Reliability-en.md": (
        (
            "Win rate equals realized samples with net return above zero divided by all realized samples",
            "src/stock_desk/backtest/metrics.py",
            "win_rate = _ratio(len(positive), count) if count else None",
        ),
        (
            "Fewer than 30 realized samples is low reliability",
            "src/stock_desk/backtest/metrics.py",
            "if realized_count < 30:",
        ),
        (
            "Any symbol above 50% keeps reliability low",
            "src/stock_desk/backtest/metrics.py",
            'largest_symbol_share > Decimal("0.500000")',
        ),
        (
            "The current release does not calculate a portfolio equity curve, maximum drawdown, or benchmark excess return",
            "src/stock_desk/backtest/metrics.py",
            '"equity_curve": None',
        ),
    ),
    "Backtest-Replay-Export-and-Failures.md": (
        (
            "浏览器直接下载文件，不显示导出回执",
            "web/src/features/backtests/BacktestReportPage.tsx",
            "导出交易 CSV",
        ),
        (
            "取消不会删除已持久化的数据",
            "web/src/features/backtests/BacktestRunPage.tsx",
            "取消不会删除已持久化的数据",
        ),
        (
            "恢复上次草稿",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "恢复上次草稿",
        ),
        (
            "旧结果的快照、结果哈希、公式版本和来源证据不会被新任务改写",
            "web/src/features/backtests/BacktestReportPage.tsx",
            "固定快照与执行口径",
        ),
        (
            "高级 API：`POST /api/backtests/{run_id}/copy`",
            "src/stock_desk/api/backtests.py",
            '"/{run_id}/copy"',
        ),
        (
            '请求体 `{"mode":"exact"}` 复用原 `snapshot_id` 和全部冻结输入',
            "src/stock_desk/backtest/service.py",
            "snapshot=run.snapshot",
        ),
        (
            '请求体 `{"mode":"latest"}` 才按原意图重建最新快照',
            "src/stock_desk/backtest/service.py",
            "return self.submit(",
        ),
        (
            "`mode` 必填，不能省略",
            "src/stock_desk/api/backtests.py",
            'mode: Literal["exact", "latest"]',
        ),
    ),
    "Backtest-Replay-Export-and-Failures-en.md": (
        (
            "The browser downloads the file directly and shows no export receipt",
            "web/src/features/backtests/BacktestReportPage.tsx",
            "导出交易 CSV",
        ),
        (
            "Cancellation does not delete persisted data",
            "web/src/features/backtests/BacktestRunPage.tsx",
            "取消不会删除已持久化的数据",
        ),
        (
            "Restore last draft（恢复上次草稿）",
            "web/src/features/backtests/BacktestWorkspacePage.tsx",
            "恢复上次草稿",
        ),
        (
            "A new task never rewrites the old result's snapshot, result hash, formula version, or provenance",
            "web/src/features/backtests/BacktestReportPage.tsx",
            "固定快照与执行口径",
        ),
        (
            "Advanced API: `POST /api/backtests/{run_id}/copy`",
            "src/stock_desk/api/backtests.py",
            '"/{run_id}/copy"',
        ),
        (
            'Body `{"mode":"exact"}` reuses the original `snapshot_id` and every frozen input',
            "src/stock_desk/backtest/service.py",
            "snapshot=run.snapshot",
        ),
        (
            'Body `{"mode":"latest"}` is the mode that rebuilds a latest snapshot from the original intent',
            "src/stock_desk/backtest/service.py",
            "return self.submit(",
        ),
        (
            "`mode` is required and cannot be omitted",
            "src/stock_desk/api/backtests.py",
            'mode: Literal["exact", "latest"]',
        ),
    ),
}


EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS = {
    "Model-Provider-Setup.md": (
        (
            "当前版本只提供 DeepSeek、OpenAI-compatible 和 Ollama 三种提供商",
            "src/stock_desk/analysis/model_config.py",
            "class ModelProviderKind(StrEnum):",
        ),
        (
            "远程 API Key 是只写字段；Ollama 不接收 API Key",
            "src/stock_desk/analysis/model_config.py",
            'json_schema_extra={"writeOnly": True}',
        ),
        (
            "最大重试次数是每次新建分析的 0–5 参数，不是模型配置字段",
            "web/src/features/analysis/AnalysisRunPanel.tsx",
            "const maxRetriesIsValid = /^[0-5]$/u.test(maxRetries);",
        ),
        (
            "编辑会创建后继配置，原配置保持不可变",
            "web/src/features/analysis/ModelSettings.tsx",
            "后继配置已创建，原配置保持不可变。",
        ),
    ),
    "Model-Provider-Setup-en.md": (
        (
            "The current release offers exactly DeepSeek, OpenAI-compatible, and Ollama",
            "src/stock_desk/analysis/model_config.py",
            "class ModelProviderKind(StrEnum):",
        ),
        (
            "A remote API key is write-only, while Ollama does not accept an API key",
            "src/stock_desk/analysis/model_config.py",
            'json_schema_extra={"writeOnly": True}',
        ),
        (
            "Maximum retries is a per-run value from 0 to 5, not a model-setting field",
            "web/src/features/analysis/AnalysisRunPanel.tsx",
            "const maxRetriesIsValid = /^[0-5]$/u.test(maxRetries);",
        ),
        (
            "Editing creates a successor configuration and leaves the original immutable",
            "web/src/features/analysis/ModelSettings.tsx",
            "后继配置已创建，原配置保持不可变。",
        ),
    ),
    "Research-Reports-and-Evidence.md": (
        (
            "九阶段流程由四个数据快照阶段和五个模型研究阶段组成",
            "src/stock_desk/analysis/models.py",
            "('market','fundamentals','announcements','news','technical',",
        ),
        (
            "五级评级是强烈看多、看多、中性、看空和强烈看空",
            "web/src/features/analysis/ConclusionPanel.tsx",
            "strong_bearish: '强烈看空'",
        ),
        (
            "证据卡显示记录、数据版本、发布时间、数据截止、采集时间、质量标记和来源路由",
            "web/src/features/analysis/EvidencePanel.tsx",
            "JSON.stringify(item.route)",
        ),
    ),
    "Research-Reports-and-Evidence-en.md": (
        (
            "The nine-stage process contains four data-snapshot stages and five model-research stages",
            "src/stock_desk/analysis/models.py",
            "('market','fundamentals','announcements','news','technical',",
        ),
        (
            "The five ratings are Strong bullish, Bullish, Neutral, Bearish, and Strong bearish",
            "web/src/features/analysis/ConclusionPanel.tsx",
            "strong_bearish: '强烈看空'",
        ),
        (
            "Each evidence card shows record, dataset version, publication time, data cutoff, fetch time, quality flags, and source route",
            "web/src/features/analysis/EvidencePanel.tsx",
            "JSON.stringify(item.route)",
        ),
    ),
    "Research-Failures-Retries-and-Safety.md": (
        (
            "部分报告不输出评级，置信度固定为 0",
            "src/stock_desk/analysis/report.py",
            "partial report state is inconsistent",
        ),
        (
            "只有报告列出的失败模型阶段可创建阶段重试",
            "src/stock_desk/analysis/service.py",
            "allowed = frozenset(item.stage.value for item in report.retry_actions)",
        ),
        (
            "阶段重试创建子运行，父运行保持不可变",
            "web/src/features/analysis/ProcessRail.tsx",
            "父运行保持不可变",
        ),
        (
            "新闻、公告与模型输出按不可信数据块处理，不能把其中指令当成控制指令",
            "src/stock_desk/analysis/content_policy.py",
            'UNTRUSTED_DATA_LABEL: Final = "untrusted-data"',
        ),
    ),
    "Research-Failures-Retries-and-Safety-en.md": (
        (
            "A partial report has no rating and its confidence is fixed at 0",
            "src/stock_desk/analysis/report.py",
            "partial report state is inconsistent",
        ),
        (
            "Only failed model stages listed by the report are eligible for stage retry",
            "src/stock_desk/analysis/service.py",
            "allowed = frozenset(item.stage.value for item in report.retry_actions)",
        ),
        (
            "A stage retry creates a child run and keeps the parent run immutable",
            "web/src/features/analysis/ProcessRail.tsx",
            "父运行保持不可变",
        ),
        (
            "News, announcements, and model output are untrusted data blocks; their instructions are not control instructions",
            "src/stock_desk/analysis/content_policy.py",
            'UNTRUSTED_DATA_LABEL: Final = "untrusted-data"',
        ),
    ),
    "Task-Center.md": (
        (
            "任务中心读取最近 100 项任务的安全视图",
            "web/src/features/tasks/taskApi.ts",
            "'/tasks?view=safe&limit=100'",
        ),
        (
            "当前只有包含 `backtest_run` target 的任务显示回测报告深链",
            "web/src/features/tasks/TaskCenterPage.tsx",
            "selectedTask.presentation.target?.type === 'backtest_run'",
        ),
        (
            "取消结果未知时先刷新任务状态，再决定是否重试",
            "web/src/features/tasks/TaskCenterPage.tsx",
            "取消结果未知。请先刷新任务状态，再决定是否重试。",
        ),
    ),
    "Task-Center-en.md": (
        (
            "Task Center reads the safe view for the most recent 100 tasks",
            "web/src/features/tasks/taskApi.ts",
            "'/tasks?view=safe&limit=100'",
        ),
        (
            "Only a task with a `backtest_run` target currently exposes a report deep link",
            "web/src/features/tasks/TaskCenterPage.tsx",
            "selectedTask.presentation.target?.type === 'backtest_run'",
        ),
        (
            "When cancellation outcome is unknown, refresh task state before deciding whether to retry",
            "web/src/features/tasks/TaskCenterPage.tsx",
            "取消结果未知。请先刷新任务状态，再决定是否重试。",
        ),
    ),
    "Responsive-Navigation-and-Accessibility.md": (
        (
            "视口不超过 1200px 时导航自动收成图标栏",
            "web/src/app/App.tsx",
            "window.matchMedia('(max-width: 1200px)')",
        ),
        (
            "收起后使用完整产品图标，不使用文字缩写",
            "web/src/app/App.tsx",
            "<AppIcon name={route.icon} />",
        ),
        (
            "发布矩阵覆盖 200% 缩放等效视口且要求页面无横向裁切和组件重叠",
            "docs/accessibility.md",
            "effective viewports representing 200% zoom",
        ),
    ),
    "Responsive-Navigation-and-Accessibility-en.md": (
        (
            "At or below 1200px, navigation automatically collapses to an icon rail",
            "web/src/app/App.tsx",
            "window.matchMedia('(max-width: 1200px)')",
        ),
        (
            "The collapsed rail uses full product icons, not letter abbreviations",
            "web/src/app/App.tsx",
            "<AppIcon name={route.icon} />",
        ),
        (
            "The release matrix covers 200% zoom-equivalent viewports and requires no horizontal clipping or shell overlap",
            "docs/accessibility.md",
            "effective viewports representing 200% zoom",
        ),
    ),
    "Credentials-Logs-and-Local-Security.md": (
        (
            "提供方密钥使用 Fernet 在本地加密，界面只显示掩码状态",
            "src/stock_desk/security/secrets.py",
            'self._fernet.encrypt(value.encode("utf-8"))',
        ),
        (
            "原生应用只监听随机的 `127.0.0.1` 端口",
            "src/stock_desk/desktop.py",
            "api_socket.bind((_LOOPBACK_HOST, 0))",
        ),
        (
            "自动日志脱敏只替换已登记的明文秘密，分享前仍须人工检查路径、研究内容和受许可数据",
            "src/stock_desk/security/redaction.py",
            "_replace_known_strings(normalized_text, secrets, markers.redacted)",
        ),
    ),
    "Credentials-Logs-and-Local-Security-en.md": (
        (
            "Provider secrets are encrypted locally with Fernet and the UI exposes only masked status",
            "src/stock_desk/security/secrets.py",
            'self._fernet.encrypt(value.encode("utf-8"))',
        ),
        (
            "The native app listens only on a random `127.0.0.1` port",
            "src/stock_desk/desktop.py",
            "api_socket.bind((_LOOPBACK_HOST, 0))",
        ),
        (
            "Automatic log redaction replaces registered plaintext secrets only; paths, research content, and licensed data still need manual review before sharing",
            "src/stock_desk/security/redaction.py",
            "_replace_known_strings(normalized_text, secrets, markers.redacted)",
        ),
    ),
    "Backup-Restore-Upgrade-and-Uninstall.md": (
        (
            "无源码 Windows 和 macOS 安装包都不内置备份/恢复命令行工具",
            "docs/backup-and-restore.md",
            "The source-free Windows and macOS installers do not bundle this operator CLI.",
        ),
        (
            "便携归档永不包含主密钥",
            "docs/backup-and-restore.md",
            "still never includes the master key",
        ),
        (
            "非空目标恢复必须在全部进程停止后使用 `--offline`",
            "docs/backup-and-restore.md",
            "`--offline` is mandatory for a non-empty destination",
        ),
        (
            "不能用 Alembic downgrade 作为运行回滚",
            "docs/backup-and-restore.md",
            "Never use Alembic downgrade as an operational rollback.",
        ),
    ),
    "Backup-Restore-Upgrade-and-Uninstall-en.md": (
        (
            "Source-free Windows and macOS installers do not bundle the backup/restore operator CLI",
            "docs/backup-and-restore.md",
            "The source-free Windows and macOS installers do not bundle this operator CLI.",
        ),
        (
            "A portable archive never contains the master key",
            "docs/backup-and-restore.md",
            "still never includes the master key",
        ),
        (
            "Restoring a non-empty destination requires every process to be stopped and `--offline`",
            "docs/backup-and-restore.md",
            "`--offline` is mandatory for a non-empty destination",
        ),
        (
            "Alembic downgrade is not an operational rollback mechanism",
            "docs/backup-and-restore.md",
            "Never use Alembic downgrade as an operational rollback.",
        ),
    ),
    "Troubleshooting.md": (
        (
            "原生安装版使用随机本机端口，不要按源码/容器的固定 8000 端口排查",
            "src/stock_desk/desktop.py",
            "api_socket.bind((_LOOPBACK_HOST, 0))",
        ),
        (
            "任务排查使用安全任务与安全事件视图，不等同于原始运行日志",
            "web/src/features/tasks/taskApi.ts",
            "`/tasks/${id}/events?view=safe&limit=100`",
        ),
        (
            "行情图表只读本地不可变缓存，不会因缺失区间静默联网抓取",
            "docs/data-sources.md",
            "Chart GET requests never invoke providers",
        ),
        (
            "恢复日志与暂存目录必须保留，不能手工编辑或删除",
            "docs/backup-and-restore.md",
            "Do not delete or edit a journal",
        ),
    ),
    "Troubleshooting-en.md": (
        (
            "The native installer uses a random local port; do not troubleshoot it as source/Compose port 8000",
            "src/stock_desk/desktop.py",
            "api_socket.bind((_LOOPBACK_HOST, 0))",
        ),
        (
            "Task diagnosis uses safe task and safe event views, not raw runtime logs",
            "web/src/features/tasks/taskApi.ts",
            "`/tasks/${id}/events?view=safe&limit=100`",
        ),
        (
            "Market charts read the local immutable cache and never silently fetch a missing range",
            "docs/data-sources.md",
            "Chart GET requests never invoke providers",
        ),
        (
            "Preserve recovery journals and staging directories; never edit or delete them manually",
            "docs/backup-and-restore.md",
            "Do not delete or edit a journal",
        ),
    ),
}


EXPECTED_WIKI_ANALYSIS_PLATFORM_CONTENT = {
    "Model-Provider-Setup.md": (
        (
            "模型连接错误码只包括 `timeout`、`authentication`、`rate_limit`、`server`、`transport`、`dns`、`unsafe_endpoint`、`invalid_response` 和 `storage`",
            "模型列表中找不到配置模型时也会折叠为 `invalid_response`",
            "[Ollama 官方快速开始](https://docs.ollama.com/quickstart)",
        ),
        ("model_not_found", "not_found 错误码"),
    ),
    "Model-Provider-Setup-en.md": (
        (
            "Model connection error codes are limited to `timeout`, `authentication`, `rate_limit`, `server`, `transport`, `dns`, `unsafe_endpoint`, `invalid_response`, and `storage`",
            "A configured model missing from the provider's model list is also folded into `invalid_response`",
            "[official Ollama quickstart](https://docs.ollama.com/quickstart)",
        ),
        ("model_not_found", "a `not_found` error code"),
    ),
    "Responsive-Navigation-and-Accessibility.md": (
        (
            "分析流程和证据抽屉没有 Escape 快捷关闭",
            "点击“关闭分析流程”或“关闭证据”",
            "关闭分析流程后焦点返回“查看分析流程”",
            "关闭证据后焦点返回原判断；若由工具栏打开，则返回“查看证据”",
        ),
        ("按 Escape 关闭分析抽屉", "Escape 会关闭分析抽屉"),
    ),
    "Responsive-Navigation-and-Accessibility-en.md": (
        (
            "The analysis process and evidence drawers do not have an Escape shortcut",
            "use the visible Close analysis process or Close evidence button",
            "Closing the process drawer returns focus to View analysis process",
            "Closing evidence returns focus to the originating claim, or to View evidence when opened from the toolbar",
        ),
        ("Escape closes an analysis drawer", "close analysis drawers with Escape"),
    ),
    "Backup-Restore-Upgrade-and-Uninstall.md": (
        (
            "便携归档永不包含主密钥",
            "原生整目录离线副本包含 `config/master.key`",
            "整目录副本必须整体加密并限制访问",
            "用户必须在副本旁另存产品版本、安装包文件名和 SHA-256 记录",
        ),
        ("副本位置的版本记录", "整目录副本不包含主密钥"),
    ),
    "Backup-Restore-Upgrade-and-Uninstall-en.md": (
        (
            "A portable archive never contains the master key",
            "A full native offline directory copy contains `config/master.key`",
            "The full-directory copy must be encrypted as a whole and access controlled",
            "The user must store product version, installer filename, and SHA-256 beside the copy as a separate record",
        ),
        (
            "the version record in the copy",
            "A full-directory copy never contains the master key",
        ),
    ),
    "Credentials-Logs-and-Local-Security.md": (
        (
            "[私密安全报告渠道](https://github.com/CongBao/stock-desk/blob/main/SECURITY.md)",
        ),
        ("`SECURITY.md` 指定的",),
    ),
    "Credentials-Logs-and-Local-Security-en.md": (
        (
            "[private security reporting channel](https://github.com/CongBao/stock-desk/blob/main/SECURITY.md)",
        ),
        ("in `SECURITY.md`",),
    ),
    "Troubleshooting.md": (
        (
            "启动条件是完整证券代码、已验证模型、0–5 的最大重试次数，以及与当前代码和模型匹配的预检",
            "数据覆盖不足仍允许运行，但报告不会给出评级",
            "[公开支持指南](https://github.com/CongBao/stock-desk/blob/main/SUPPORT.md)",
            "[私密安全报告渠道](https://github.com/CongBao/stock-desk/blob/main/SECURITY.md)",
        ),
        ("数据覆盖不足会阻止启动", "预检不允许启动"),
    ),
    "Troubleshooting-en.md": (
        (
            "Start requires a complete symbol, a verified model, maximum retries from 0 to 5, and preflight that still matches the current symbol and model",
            "Insufficient data coverage still allows a run, but the report emits no rating",
            "[public support guide](https://github.com/CongBao/stock-desk/blob/main/SUPPORT.md)",
            "[private security reporting channel](https://github.com/CongBao/stock-desk/blob/main/SECURITY.md)",
        ),
        ("insufficient coverage blocks start", "preflight blocks start"),
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
    routes = root / "web/src/app/route-paths.json"
    routes.parent.mkdir(parents=True, exist_ok=True)
    routes.write_text(
        (PROJECT_ROOT / "web/src/app/route-paths.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_readme_screenshot_manifest(root)
    _initialize_fixture_git(root)


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
            chinese_source_contract = (
                EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS.get(f"{stem}.md")
                or EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS.get(f"{stem}.md")
                or EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS.get(f"{stem}.md")
            )
            if chinese_source_contract is not None:
                chinese += (
                    "\n"
                    + "；".join(claim[0] for claim in chinese_source_contract)
                    + "\n"
                )
            english_source_contract = (
                EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS.get(f"{stem}-en.md")
                or EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS.get(f"{stem}-en.md")
                or EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS.get(
                    f"{stem}-en.md"
                )
            )
            if english_source_contract is not None:
                english += (
                    "\n"
                    + "; ".join(claim[0] for claim in english_source_contract)
                    + "\n"
                )
            chinese_platform_contract = EXPECTED_WIKI_ANALYSIS_PLATFORM_CONTENT.get(
                f"{stem}.md"
            )
            if chinese_platform_contract is not None:
                chinese += "\n" + "；".join(chinese_platform_contract[0]) + "\n"
            english_platform_contract = EXPECTED_WIKI_ANALYSIS_PLATFORM_CONTENT.get(
                f"{stem}-en.md"
            )
            if english_platform_contract is not None:
                english += "\n" + "; ".join(english_platform_contract[0]) + "\n"
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


@lru_cache(maxsize=128)
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


def _write_readme_screenshot_manifest(root: Path) -> None:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    definitions: tuple[tuple[str, str, str, bool, str, str], ...] = (
        (
            "market-data-and-charts",
            "/market",
            "real_chart",
            True,
            "600519.SH",
            "贵州茅台",
        ),
        (
            "formula-studio",
            "/formulas",
            "real_formula_preview",
            True,
            "300750.SZ",
            "宁德时代",
        ),
        (
            "backtesting",
            "/backtests",
            "blocked_real_backtest_preflight",
            True,
            "000001.SZ",
            "平安银行",
        ),
        (
            "multi-agent-research",
            "/analysis",
            "analysis_readiness",
            False,
            "600036.SH",
            "招商银行",
        ),
    )
    dataset_versions = {
        "market-data-and-charts": "sha256:aa8112c9eda7ed05ed8d92d21afe9dae45fafb295a0fa5ba278c1805a7533236",
        "formula-studio": "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
        "backtesting": "sha256:5a3d9256e58f5bafbad48a7d1fb4ec690d032552aee4c6ae4df7b9940356ec24",
    }
    image_sections = {
        "README.md": """
![带来源证据的 A 股行情图](docs/images/market-data-and-charts.png)

贵州茅台 `600519.SH`，BaoStock 日线/前复权，数据截至 `2026-07-08T07:00:00Z`。仅作功能演示，不构成投资建议。

| 真实公式预览 | 被阻断的真实回测预检 | 分析准备状态 |
| --- | --- | --- |
| ![宁德时代 MACD BUY/SELL 公式预览](docs/images/formula-studio.png)<br>宁德时代 `300750.SZ`；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`；显示 MACD BUY/SELL。仅作功能演示，不构成投资建议。 | ![平安银行 MACD 回测严格预检被阻断](docs/images/backtesting.png)<br>平安银行 `000001.SZ` 的真实 MACD 配置；BaoStock，1d/qfq；截至 `2026-07-08T07:00:00Z`。因没有合法的 Tushare execution-status 快照，严格预检被阻断；未创建任务或报告，不代表回测成功、结果或胜率。仅作功能演示，不构成投资建议。 | ![招商银行模型与证据准备状态](docs/images/multi-agent-research.png)<br>招商银行 `600036.SH` 的模型/证据准备状态：无已验证模型，未发起模型调用，也未生成报告。 |
""",
        "README.en.md": """
![A-share market chart with provenance](docs/images/market-data-and-charts.png)

Kweichow Moutai `600519.SH`; BaoStock daily/qfq data; cutoff `2026-07-08T07:00:00Z`. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。）

| Real formula preview | Blocked real backtest preflight | Analysis readiness |
| --- | --- | --- |
| ![CATL MACD BUY/SELL formula preview](docs/images/formula-studio.png)<br>CATL `300750.SZ`; BaoStock, 1d/qfq; cutoff `2026-07-08T07:00:00Z`; MACD BUY/SELL are visible. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。） | ![Ping An Bank MACD strict preflight blocked](docs/images/backtesting.png)<br>Real MACD configuration for Ping An Bank `000001.SZ`; BaoStock, 1d/qfq; cutoff `2026-07-08T07:00:00Z`. Strict preflight is blocked because no authorized Tushare execution-status snapshot exists. No task or report was created; this is not a successful backtest, result, or win rate. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。） | ![China Merchants Bank model and evidence readiness](docs/images/multi-agent-research.png)<br>Model/evidence readiness for China Merchants Bank `600036.SH`: no verified model, no model call started, and no report generated. |
""",
    }
    for readme_name, image_section in image_sections.items():
        readme = root / readme_name
        readme.write_text(
            readme.read_text(encoding="utf-8") + image_section,
            encoding="utf-8",
        )

    entries: list[dict[str, Any]] = []
    for ordinal, (
        screenshot_id,
        route,
        state,
        contains_market_data,
        symbol,
        name,
    ) in enumerate(definitions, start=1):
        relative_path = f"docs/images/{screenshot_id}.png"
        payload = _png_bytes(1440, 1000, varied=True, seed=ordinal)
        image = root / relative_path
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(payload)
        market_data = None
        if contains_market_data:
            market_data = {
                "symbol": symbol,
                "name": name,
                "period": "1d",
                "adjustment": "qfq",
                "start": "2021-01-01",
                "end": "2026-07-08",
                "source": "baostock",
                "data_cutoff": "2026-07-08T07:00:00Z",
                "dataset_version": dataset_versions[screenshot_id],
            }
        entries.append(
            {
                "screenshot_id": screenshot_id,
                "path": relative_path,
                "state": state,
                "route": route,
                "viewport": {
                    "width": 1440,
                    "height": 1000,
                    "device_scale_factor": 1,
                },
                "product": {"version": "1.0.0", "git_commit": commit},
                "captured_at": "2026-07-09T00:00:00Z",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "contains_market_data": contains_market_data,
                "market_data": market_data,
                "capture": "in-app-browser",
                "editing": "none",
                "redaction": "passed",
                "disclaimer": "仅作功能演示，不构成投资建议",
            }
        )
    (root / "docs/images/manifest.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "stock-desk-documentation-screenshots-v1",
                "screenshots": entries,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
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


def _readme_manifest(root: Path) -> tuple[Path, dict[str, Any]]:
    path = root / "docs/images/manifest.yml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return path, loaded


def test_repository_requires_readme_screenshot_manifest(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/images/manifest.yml").unlink()

    failures = verify_repository(tmp_path)

    assert any("README screenshot manifest is missing" in item for item in failures)


def test_repository_rejects_symlinked_readme_manifest(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    manifest = tmp_path / "docs/images/manifest.yml"
    target = tmp_path / "manifest-target.yml"
    manifest.replace(target)
    manifest.symlink_to(target)

    failures = verify_repository(tmp_path)

    assert any("manifest" in item.casefold() and "symlink" in item for item in failures)


def test_repository_rejects_symlink_in_readme_image_parent_path(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    images = tmp_path / "docs/images"
    target = tmp_path / "images-target"
    images.replace(target)
    images.symlink_to(target, target_is_directory=True)

    failures = verify_repository(tmp_path)

    assert any("docs/images" in item and "symlink" in item for item in failures)


def test_repository_rejects_symlinked_readme_image(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    image = tmp_path / "docs/images/market-data-and-charts.png"
    target = tmp_path / "outside-market.png"
    image.replace(target)
    image.symlink_to(target)

    failures = verify_repository(tmp_path)

    assert any(
        "market-data-and-charts.png" in item and "symlink" in item for item in failures
    )


def test_repository_rejects_nul_image_path_without_raising(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["path"] = "docs/images/bad\0name.png"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("invalid docs/images path" in item for item in failures)


def test_repository_reports_unreadable_image_hash_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repository(tmp_path)
    target = (tmp_path / "docs/images/market-data-and-charts.png").resolve()
    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path.resolve() == target:
            raise PermissionError("portable unreadable image simulation")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    failures = verify_repository(tmp_path)

    assert any("image is unreadable" in item for item in failures)


def test_repository_reports_pillow_reopen_error_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repository(tmp_path)
    target = (tmp_path / "docs/images/market-data-and-charts.png").resolve()
    original = verify_docs_module.Image.open
    target_calls = 0

    def fail_third_open(path: object, *args: object, **kwargs: object) -> object:
        nonlocal target_calls
        if Path(path).resolve() == target:
            target_calls += 1
            if target_calls == 3:
                raise OSError("portable Pillow reopen simulation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(verify_docs_module.Image, "open", fail_third_open)

    failures = verify_repository(tmp_path)

    assert any("image metadata is unreadable" in item for item in failures)


def test_repository_reports_corrupt_manifest_image_without_raising(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    image_path = tmp_path / "docs/images/market-data-and-charts.png"
    image_path.write_bytes(b"not a raster image")
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["sha256"] = hashlib.sha256(
        image_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("image decode failed" in item for item in failures)


def test_repository_readme_manifest_binds_stable_id_to_path_state_and_route(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["screenshot_id"] = "renamed-market-shot"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("stable identity binding" in item for item in failures)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("symbol", "600000.SH"),
        ("name", "浦发银行"),
        ("period", "1w"),
        ("adjustment", "hfq"),
        ("start", "2020-01-01"),
        ("end", "2026-07-07"),
        ("source", "tushare"),
        ("data_cutoff", "2026-07-08T06:00:00Z"),
        ("dataset_version", "sha256:" + "b" * 64),
    ),
)
def test_repository_readme_manifest_binds_complete_market_data_identity(
    tmp_path: Path, field: str, replacement: str
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["market_data"][field] = replacement
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("stable market-data identity" in item for item in failures)


def test_repository_readme_manifest_rejects_extra_market_data_key(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["market_data"]["is_real"] = False
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("market_data keys must match exactly" in item for item in failures)


def test_market_fake_markers_ignore_hashes_and_dates() -> None:
    assert not verify_docs_module._market_provenance_has_forbidden_marker(
        {
            "name": "贵州茅台",
            "source": "baostock",
            "start": "demo-date",
            "dataset_version": "sha256:cc0demo",
        }
    )


def test_repository_uses_routes_from_verified_root(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    routes = tmp_path / "web/src/app/route-paths.json"
    routes.parent.mkdir(parents=True, exist_ok=True)
    routes.write_text("{}\n", encoding="utf-8")

    failures = verify_repository(tmp_path)

    assert any(
        "Unable to load canonical application routes" in item for item in failures
    )


def test_repository_requires_commit_reachable_from_verified_root(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    git = tmp_path / ".git"
    if git.is_dir():
        for path in sorted(git.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        git.rmdir()

    failures = verify_repository(tmp_path)

    assert any("reachable repository commit" in item for item in failures)


def test_wiki_uses_routes_from_explicit_repository_root(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_complete_final_wiki(wiki)
    repository = tmp_path / "repository"
    repository.mkdir()
    _write_repository(repository)
    routes = repository / "web/src/app/route-paths.json"
    routes.parent.mkdir(parents=True, exist_ok=True)
    routes.write_text("{}\n", encoding="utf-8")

    failures = verify_wiki(wiki, final=True, repo_root=repository)

    assert any(
        "Unable to load canonical application routes" in item for item in failures
    )


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "extra"))
def test_repository_readme_images_and_manifest_paths_match_exactly_once(
    tmp_path: Path, mutation: str
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    entries = manifest["screenshots"]
    assert isinstance(entries, list)
    if mutation == "missing":
        entries.pop()
    elif mutation == "duplicate":
        entries[-1]["path"] = entries[0]["path"]
    else:
        extra = dict(entries[-1])
        extra["screenshot_id"] = "unreferenced-extra"
        extra["path"] = "docs/images/unreferenced-extra.png"
        (tmp_path / extra["path"]).write_bytes(
            _png_bytes(1440, 1000, varied=True, seed=99)
        )
        extra["sha256"] = hashlib.sha256(
            (tmp_path / extra["path"]).read_bytes()
        ).hexdigest()
        entries.append(extra)
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("exactly once" in item for item in failures)


@pytest.mark.parametrize(
    ("field", "invalid", "expected"),
    (
        ("sha256", "0" * 64, "SHA-256 does not match"),
        ("path", "../outside.png", "escapes docs/images"),
        (
            "viewport",
            {"width": 0, "height": 1000, "device_scale_factor": 1},
            "1440x1000",
        ),
        (
            "product",
            {
                "version": "0.9.9",
                "git_commit": "17912f5fa8cb43c1df7c41315b8cd60199b9d403",
            },
            "version 1.0.0",
        ),
        (
            "product",
            {"version": "1.0.0", "git_commit": "f" * 40},
            "reachable repository commit",
        ),
        ("captured_at", "2026-07-09T08:00:00+08:00", "aware UTC captured_at"),
    ),
)
def test_repository_readme_manifest_rejects_invalid_capture_metadata(
    tmp_path: Path, field: str, invalid: object, expected: str
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0][field] = invalid
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(expected in item for item in failures)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("fake_source", "real market provenance"),
        ("market_null", "requires real market provenance"),
        ("readiness_market", "market_data must be null"),
    ),
)
def test_repository_readme_manifest_enforces_truthful_market_provenance(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    entries = manifest["screenshots"]
    if mutation == "fake_source":
        entries[0]["market_data"]["source"] = "synthetic fixture"
    elif mutation == "market_null":
        entries[0]["market_data"] = None
    else:
        entries[-1]["market_data"] = dict(entries[0]["market_data"])
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(expected in item for item in failures)


@pytest.mark.parametrize("forbidden_marker", ("DEMO dataset", "cC0 licensed data"))
def test_repository_readme_manifest_rejects_each_demo_or_cc0_marker(
    tmp_path: Path, forbidden_marker: str
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    manifest["screenshots"][0]["market_data"]["name"] = forbidden_marker
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("requires real market provenance" in item for item in failures)


def test_repository_readme_manifest_rejects_legacy_cutoff_field(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    manifest_path, manifest = _readme_manifest(tmp_path)
    market_data = manifest["screenshots"][0]["market_data"]
    market_data["cutoff"] = market_data.pop("data_cutoff")
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("aware UTC data_cutoff" in item for item in failures)


@pytest.mark.parametrize(
    ("readme_name", "truthful", "misleading", "image_name"),
    (
        (
            "README.md",
            "不代表回测成功、结果或胜率",
            "回测成功，已有结果和胜率",
            "backtesting.png",
        ),
        (
            "README.en.md",
            "no verified model, no model call started, and no report generated",
            "verified model, model call completed, and report generated",
            "multi-agent-research.png",
        ),
        (
            "README.md",
            "显示 MACD BUY/SELL。仅作功能演示，不构成投资建议。",
            "显示 MACD BUY/SELL。",
            "formula-studio.png",
        ),
    ),
)
def test_repository_readme_manifest_requires_local_truthful_image_context(
    tmp_path: Path,
    readme_name: str,
    truthful: str,
    misleading: str,
    image_name: str,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / readme_name
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(truthful, misleading, 1),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(
        readme_name in item and image_name in item and "local truthful caption" in item
        for item in failures
    )


@pytest.mark.parametrize(
    ("readme_name", "truthful", "contradiction"),
    (
        (
            "README.en.md",
            "this is not a successful backtest, result, or win rate",
            "this is not a successful backtest, result, or win rate; "
            "however, this is a successful backtest result with a 99% win rate",
        ),
        (
            "README.md",
            "不代表回测成功、结果或胜率",
            "不代表回测成功、结果或胜率；但回测成功，胜率 99%",
        ),
        (
            "README.en.md",
            "this is not a successful backtest, result, or win rate",
            "this is not a successful backtest, result, or win rate; "
            "the backtest succeeded and achieved a 99% win rate",
        ),
        (
            "README.md",
            "不代表回测成功、结果或胜率",
            "不代表回测成功、结果或胜率；该回测已经成功，胜率为 99%",
        ),
    ),
)
def test_repository_readme_manifest_rejects_local_contradictory_backtest_claim(
    tmp_path: Path,
    readme_name: str,
    truthful: str,
    contradiction: str,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / readme_name
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(truthful, contradiction, 1),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(
        readme_name in item
        and "backtesting.png" in item
        and ("contradictory claim" in item or "exact local caption contract" in item)
        for item in failures
    )


@pytest.mark.parametrize("addition", (r" escaped\|pipe", " `inline|code`"))
def test_repository_table_context_conservatively_rejects_pipe_alterations(
    tmp_path: Path, addition: str
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.en.md"
    truthful = "not a successful backtest, result, or win rate"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(truthful, truthful + addition, 1),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("exact local caption contract" in item for item in failures)


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


def test_ui_label_parser_preserves_parentheses_inside_visible_chinese_label() -> None:
    document = """# Formula versions

## Chinese UI labels

1. `Read-only historical versions（历史版本（只读））` — select a version.
2. `Save draft（保存草稿）` — save work.

## Steps
"""

    assert verify_docs_module._wiki_ui_label_mappings(document) == (
        (
            ("Read-only historical versions", "历史版本（只读）"),
            ("Save draft", "保存草稿"),
        ),
        True,
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
    assert (
        getattr(
            verify_docs_module,
            "REQUIRED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE",
            None,
        )
        == EXPECTED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE
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


def test_formula_ui_evidence_rejects_aria_and_hidden_text_as_visible_labels() -> None:
    checker = getattr(verify_docs_module, "_source_contains_visible_ui_label", None)
    assert callable(checker)
    assert not checker(
        '<input aria-label="搜索函数或模板" />',
        "搜索函数或模板",
        "placeholder",
    )
    assert not checker(
        '<span className="visually-hidden">搜索函数或模板</span>',
        "搜索函数或模板",
        "jsx_text",
    )
    assert checker(
        '<input placeholder="函数、字段或说明" />',
        "函数、字段或说明",
        "placeholder",
    )
    assert checker("<span>打开公式</span>", "打开公式", "jsx_text")
    assert not checker(
        '<button aria-label="运行预览"></button>',
        "运行预览",
        "button_expression",
    )
    assert checker(
        "<button>{isLoading ? '计算中…' : '运行预览'}</button>",
        "运行预览",
        "button_expression",
    )


def test_formula_and_backtest_ui_visible_source_contract_covers_every_mapped_label() -> (
    None
):
    exact_source_stems = {
        "Formula-Studio-Quickstart",
        "Formula-Compatibility-and-Errors",
        "Formula-Versions-and-Safety",
        "MACD-Backtest-Tutorial",
        "A-Share-Execution-and-Costs",
        "Backtest-Metrics-and-Reliability",
        "Backtest-Replay-Export-and-Failures",
    }
    for stem in exact_source_stems:
        mapped = {chinese for _english, chinese in EXPECTED_WIKI_APP_UI_LABELS[stem]}
        assert set(EXPECTED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE[stem]) == mapped


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


def test_formula_guide_claims_are_backed_by_tracked_product_source() -> None:
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS", None)
        == EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS
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
    for claims in EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS.values():
        for _wiki_marker, relative_path, source_marker in claims:
            assert relative_path in tracked
            assert source_marker in (repo / relative_path).read_text(encoding="utf-8")


def test_formula_guide_pages_require_source_backed_claims(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    for filename, claims in EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                claims[0][0], "removed source-backed formula claim"
            ),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in EXPECTED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS:
        assert any(
            filename in failure and "source-backed formula-guide contract" in failure
            for failure in failures
        )


def test_backtest_guide_claims_are_backed_by_tracked_product_source() -> None:
    assert (
        getattr(verify_docs_module, "REQUIRED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS", None)
        == EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS
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
    for claims in EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS.values():
        for _wiki_marker, relative_path, source_marker in claims:
            assert relative_path in tracked
            assert source_marker in (repo / relative_path).read_text(encoding="utf-8")


def test_backtest_guide_pages_require_source_backed_claims(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    for filename, claims in EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                claims[0][0], "removed source-backed backtest claim"
            ),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in EXPECTED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS:
        assert any(
            filename in failure and "source-backed backtest-guide contract" in failure
            for failure in failures
        )


def test_analysis_platform_guide_claims_are_backed_by_tracked_product_source() -> None:
    assert (
        getattr(
            verify_docs_module,
            "REQUIRED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS",
            None,
        )
        == EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS
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
    for claims in EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS.values():
        for _wiki_marker, relative_path, source_marker in claims:
            assert relative_path in tracked
            assert source_marker in (repo / relative_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("filename", "wiki_marker"),
    tuple(
        (filename, claim[0])
        for filename, claims in EXPECTED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS.items()
        for claim in claims
    ),
)
def test_each_analysis_platform_source_backed_claim_is_required(
    tmp_path: Path,
    filename: str,
    wiki_marker: str,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / filename
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            wiki_marker, "removed source-backed analysis/platform claim"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        filename in failure
        and "source-backed analysis/platform-guide contract" in failure
        for failure in failures
    )


@pytest.mark.parametrize(
    ("filename", "required_marker", "forbidden_marker"),
    tuple(
        (filename, required_marker, forbidden[0] if forbidden else "false capability")
        for filename, (
            required,
            forbidden,
        ) in EXPECTED_WIKI_ANALYSIS_PLATFORM_CONTENT.items()
        for required_marker in required
    ),
)
def test_each_analysis_platform_fact_is_required_and_rejects_false_capability(
    tmp_path: Path,
    filename: str,
    required_marker: str,
    forbidden_marker: str,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / filename
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            required_marker, "removed required analysis/platform fact"
        )
        + f"\n{forbidden_marker}\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        filename in failure and "analysis/platform content contract" in failure
        for failure in failures
    )


@pytest.mark.parametrize(
    ("filename", "forbidden_marker"),
    tuple(
        (filename, forbidden_marker)
        for filename, (
            _required,
            forbidden,
        ) in EXPECTED_WIKI_ANALYSIS_PLATFORM_CONTENT.items()
        for forbidden_marker in forbidden
    ),
)
def test_each_false_analysis_platform_capability_is_rejected(
    tmp_path: Path,
    filename: str,
    forbidden_marker: str,
) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / filename
    page.write_text(
        page.read_text(encoding="utf-8") + f"\n{forbidden_marker}\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        filename in failure
        and "analysis/platform content contract" in failure
        and forbidden_marker in failure
        for failure in failures
    )


def test_analysis_start_eligibility_rejects_each_control_drift() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "web/src/features/analysis/AnalysisRunPanel.tsx").read_text(
        encoding="utf-8"
    )
    assert verify_docs_module._analysis_start_eligibility_contract(source)
    mutations = (
        source.replace("preflight === null ||", "false ||", 1),
        source.replace("preflight.symbol !== symbol ||", "false ||", 1),
        source.replace("!selectedModelIsVerified ||", "false ||", 1),
        source.replace("!maxRetriesIsValid", "false", 1),
        source.replace(
            "!maxRetriesIsValid\n          }",
            "!maxRetriesIsValid || !preflight.ratingEligible\n          }",
            1,
        ),
    )
    for mutated in mutations:
        assert not verify_docs_module._analysis_start_eligibility_contract(mutated)


def test_analysis_drawer_contract_rejects_escape_or_focus_drift() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "web/src/features/analysis/AnalysisPage.tsx").read_text(
        encoding="utf-8"
    )
    assert verify_docs_module._analysis_drawer_focus_contract(source)
    mutations = (
        source.replace("processButtonRef.current?.focus();", "return;", 1),
        source.replace("claimTriggerRef.current.focus();", "return;", 1),
        source.replace("evidenceButtonRef.current?.focus();", "return;", 1),
        source.replace("关闭分析流程", "关闭流程", 1),
        source.replace("关闭证据", "关闭详情", 1),
        source.replace(
            "function closeDrawer() {",
            "function closeDrawer() {\n    if (event.key === 'Escape') return;",
            1,
        ),
    )
    for mutated in mutations:
        assert not verify_docs_module._analysis_drawer_focus_contract(mutated)


def test_model_provider_and_connection_error_enums_are_exact() -> None:
    repo = Path(__file__).resolve().parents[2]
    model_source = (repo / "src/stock_desk/analysis/model_config.py").read_text(
        encoding="utf-8"
    )
    error_source = (repo / "src/stock_desk/analysis/providers/base.py").read_text(
        encoding="utf-8"
    )
    provider_members = {
        "DEEPSEEK": "deepseek",
        "OPENAI_COMPATIBLE": "openai_compatible",
        "OLLAMA": "ollama",
    }
    error_members = {
        "TIMEOUT": "timeout",
        "AUTHENTICATION": "authentication",
        "RATE_LIMIT": "rate_limit",
        "SERVER": "server",
        "TRANSPORT": "transport",
        "DNS": "dns",
        "UNSAFE_ENDPOINT": "unsafe_endpoint",
        "INVALID_RESPONSE": "invalid_response",
        "STORAGE": "storage",
    }
    assert (
        verify_docs_module._python_str_enum_members(model_source, "ModelProviderKind")
        == provider_members
    )
    assert (
        verify_docs_module._python_str_enum_members(error_source, "ModelErrorCode")
        == error_members
    )
    for name, value in provider_members.items():
        mutated = model_source.replace(
            f'{name} = "{value}"', f'{name} = "mutated_{value}"', 1
        )
        assert (
            verify_docs_module._python_str_enum_members(mutated, "ModelProviderKind")
            != provider_members
        )
    for name, value in error_members.items():
        mutated = error_source.replace(
            f'{name} = "{value}"', f'{name} = "mutated_{value}"', 1
        )
        assert (
            verify_docs_module._python_str_enum_members(mutated, "ModelErrorCode")
            != error_members
        )
    extra = error_source.replace(
        '    STORAGE = "storage"',
        '    STORAGE = "storage"\n    NOT_FOUND = "not_found"',
        1,
    )
    assert (
        verify_docs_module._python_str_enum_members(extra, "ModelErrorCode")
        != error_members
    )


def test_missing_provider_model_structurally_maps_to_invalid_response() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (
        repo / "src/stock_desk/analysis/providers/openai_compatible.py"
    ).read_text(encoding="utf-8")
    assert verify_docs_module._model_missing_maps_to_invalid_response(source)
    mutations = (
        source.replace(
            "if not any(entry.id == self.model for entry in models.data):",
            "if False:",
            1,
        ),
        source.replace(
            "else ModelInvalidResponseError()",
            "else ModelTransportError()",
            1,
        ),
    )
    for mutated in mutations:
        assert not verify_docs_module._model_missing_maps_to_invalid_response(mutated)


def test_rating_record_requires_all_five_exact_levels() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "web/src/features/analysis/ConclusionPanel.tsx").read_text(
        encoding="utf-8"
    )
    expected = {
        "strong_bullish": "强烈看多",
        "bullish": "看多",
        "neutral": "中性",
        "bearish": "看空",
        "strong_bearish": "强烈看空",
    }
    assert (
        verify_docs_module._typescript_literal_record(source, "ratingLabels")
        == expected
    )
    for key, value in expected.items():
        mutated = source.replace(f"{key}: '{value}'", f"{key}: '错误值'", 1)
        assert (
            verify_docs_module._typescript_literal_record(mutated, "ratingLabels")
            != expected
        )
    extra = source.replace(
        "  strong_bearish: '强烈看空',",
        "  strong_bearish: '强烈看空',\n  speculative: '投机',",
        1,
    )
    assert (
        verify_docs_module._typescript_literal_record(extra, "ratingLabels") != expected
    )


def test_report_state_invariants_reject_each_policy_mutation() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/stock_desk/analysis/report.py").read_text(encoding="utf-8")
    assert verify_docs_module._analysis_report_state_invariants(source)
    mutations = (
        ("self.rating is None", "False"),
        ("self.rating is not None", "False"),
        ("self.confidence != 0.0", "False"),
        ("not self.missing_modules", "False"),
        ("not self.retry_actions", "False"),
        ("elif self.retry_actions:", "elif False:"),
    )
    for before, after in mutations:
        mutated = source.replace(before, after, 1)
        assert not verify_docs_module._analysis_report_state_invariants(mutated)


def test_evidence_contract_rejects_each_missing_field_or_display_binding() -> None:
    repo = Path(__file__).resolve().parents[2]
    domain_source = (repo / "src/stock_desk/analysis/evidence.py").read_text(
        encoding="utf-8"
    )
    component_source = (repo / "web/src/features/analysis/EvidencePanel.tsx").read_text(
        encoding="utf-8"
    )
    assert verify_docs_module._evidence_display_contract(
        domain_source, component_source
    )
    fields = (
        "canonical_source",
        "source_record",
        "published_at",
        "data_cutoff",
        "fetched_at",
        "dataset_version",
        "quality_flags",
        "route",
    )
    for field in fields:
        mutated = domain_source.replace(f"    {field}:", f"    removed_{field}:", 1)
        assert not verify_docs_module._evidence_display_contract(
            mutated, component_source
        )
    bindings = (
        "item.canonicalSource",
        "item.sourceRecord",
        "item.publishedAt",
        "item.dataCutoff",
        "item.fetchedAt",
        "item.datasetVersion",
        "item.qualityFlags",
        "item.route",
    )
    for binding in bindings:
        mutated = component_source.replace(binding, "item.removed", 1)
        assert not verify_docs_module._evidence_display_contract(domain_source, mutated)


def test_prompt_trust_boundary_rejects_each_structural_mutation() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/stock_desk/analysis/content_policy.py").read_text(
        encoding="utf-8"
    )
    assert verify_docs_module._prompt_trust_boundary_contract(source)
    mutations = (
        (
            'UNTRUSTED_DATA_LABEL: Final = "untrusted-data"',
            'UNTRUSTED_DATA_LABEL: Final = "trusted-control"',
        ),
        (
            'TRUSTED_CONTROL_LABEL: Final = "trusted-control"',
            'TRUSTED_CONTROL_LABEL: Final = "untrusted-data"',
        ),
        ('"trust_label": UNTRUSTED_DATA_LABEL', '"trust_label": TRUSTED_CONTROL_LABEL'),
        ("if label == UNTRUSTED_DATA_LABEL:", "if False:"),
        ("elif label == TRUSTED_CONTROL_LABEL:", "elif False:"),
    )
    for before, after in mutations:
        mutated = source.replace(before, after, 1)
        assert not verify_docs_module._prompt_trust_boundary_contract(mutated)


def test_responsive_matrix_rejects_each_route_viewport_and_gate_mutation() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "web/e2e/responsive.spec.ts").read_text(encoding="utf-8")
    assert verify_docs_module._responsive_e2e_contract(source)
    mutations = [
        source.replace(f"  '{route}',", "  '/removed',", 1)
        for route in (
            "/market",
            "/formulas",
            "/backtests",
            "/analysis",
            "/tasks",
            "/settings",
        )
    ]
    mutations.extend(
        source.replace(before, after, 1)
        for before, after in (
            (
                "width: 1600, height: 900, collapsed: false",
                "width: 1599, height: 900, collapsed: false",
            ),
            (
                "width: 1100, height: 700, collapsed: true",
                "width: 1101, height: 700, collapsed: true",
            ),
            (
                "width: 1024, height: 768, collapsed: true",
                "width: 1024, height: 767, collapsed: true",
            ),
            (
                "width: 768, height: 1024, collapsed: true",
                "width: 767, height: 1024, collapsed: true",
            ),
            (
                "width: 390, height: 844, collapsed: true",
                "width: 391, height: 844, collapsed: true",
            ),
            ("width: 640,\n    height: 450,", "width: 641,\n    height: 450,"),
            ("width: 640,\n    height: 360,", "width: 641,\n    height: 360,"),
            ("for (const route of routes)", "for (const route of ['/market'])"),
            ("await expectNoShellOverlap(page);", "await Promise.resolve();"),
            (
                "await expectNoInteractiveControlOverlap(page);",
                "await Promise.resolve();",
            ),
            ("await expectNavigationIsOperable(page);", "await Promise.resolve();"),
        )
    )
    for mutated in mutations:
        assert not verify_docs_module._responsive_e2e_contract(mutated)


def test_backtest_docs_match_current_no_one_click_rerun_or_failed_only_retry_ui() -> (
    None
):
    repo = Path(__file__).resolve().parents[2]
    workspace = (
        repo / "web/src/features/backtests/BacktestWorkspacePage.tsx"
    ).read_text(encoding="utf-8")
    run_page = (repo / "web/src/features/backtests/BacktestRunPage.tsx").read_text(
        encoding="utf-8"
    )
    browser_api = (repo / "web/src/features/backtests/backtestApi.ts").read_text(
        encoding="utf-8"
    )

    for unsupported_label in ("一键重跑", "重跑回测", "只重试失败证券"):
        assert unsupported_label not in workspace
        assert unsupported_label not in run_page
    assert "/copy" not in browser_api


def test_backtest_guides_reject_runtime_status_and_copy_mode_conflation(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    replacements = {
        "A-Share-Execution-and-Costs.md": (
            "冻结状态引用存在但逐点证据不完整时，当前版本把该证券记为普通失败 `symbol_execution_failed`",
            "逐点证据不完整也记为数据不足",
        ),
        "A-Share-Execution-and-Costs-en.md": (
            "When a frozen status reference exists but per-point evidence is incomplete, the current release records an ordinary `symbol_execution_failed` failure",
            "incomplete per-point evidence is data insufficient",
        ),
        "Backtest-Replay-Export-and-Failures.md": (
            '请求体 `{"mode":"exact"}` 复用原 `snapshot_id` 和全部冻结输入',
            "exact 重建最新快照",
        ),
        "Backtest-Replay-Export-and-Failures-en.md": (
            'Body `{"mode":"exact"}` reuses the original `snapshot_id` and every frozen input',
            "exact rebuilds the latest snapshot",
        ),
    }
    for filename, (truth, false_claim) in replacements.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(truth, false_claim),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in replacements:
        assert any(
            filename in failure and "workflow content contract" in failure
            for failure in failures
        )


def test_backtest_guides_reject_gap_samples_for_zero_runnable_scopes(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    replacements = {
        "A-Share-Execution-and-Costs.md": (
            "单股无执行状态覆盖或股票池无任何可运行证券时，预检整体失败",
            "单股无覆盖会显示 missing_execution_status 缺口样例",
        ),
        "A-Share-Execution-and-Costs-en.md": (
            "A single symbol without status coverage, or a pool with no runnable symbol, fails preflight as a whole",
            "a single symbol without coverage shows a missing_execution_status gap sample",
        ),
    }
    for filename, (truth, false_claim) in replacements.items():
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8").replace(truth, false_claim),
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=False)

    for filename in replacements:
        assert any(
            filename in failure and "workflow content contract" in failure
            for failure in failures
        )


def test_formula_lifecycle_docs_match_current_no_delete_or_toggle_api() -> None:
    repo = Path(__file__).resolve().parents[2]
    router = (repo / "src/stock_desk/api/formulas.py").read_text(encoding="utf-8")
    api = (repo / "web/src/features/formulas/formulaApi.ts").read_text(encoding="utf-8")
    studio = (repo / "web/src/features/formulas/FormulaStudioPage.tsx").read_text(
        encoding="utf-8"
    )

    assert "@router.delete" not in router
    assert '/disable"' not in router
    assert '/enable"' not in router
    assert "deleteFormula" not in api
    assert "disableFormula" not in api
    assert "enableFormula" not in api
    for fictional_label in ("删除公式", "停用公式", "启用公式"):
        assert fictional_label not in studio


def test_formula_docs_lock_current_tdx_v1_time_behavior_boundary() -> None:
    assert tuple(
        (function.name, function.future_behavior)
        for function in V1_REGISTRY.functions()
    ) == (
        ("ABS", "current_only"),
        ("BARSLAST", "past_only"),
        ("COUNT", "past_only"),
        ("CROSS", "past_only"),
        ("EMA", "past_only"),
        ("FILTER", "past_only"),
        ("HHV", "past_only"),
        ("IF", "current_only"),
        ("LLV", "past_only"),
        ("LONGCROSS", "past_only"),
        ("MA", "past_only"),
        ("MAX", "current_only"),
        ("MIN", "current_only"),
        ("REF", "past_only"),
        ("SMA", "past_only"),
        ("STD", "past_only"),
        ("SUM", "past_only"),
    )
    with pytest.raises(FormulaCompileError) as captured:
        compile_formula("BUY:BACKSET(C>0,1);SELL:C<0;")
    assert captured.value.code == "unsupported_function"


def test_formula_pages_keep_bilingual_fixed_capture_metadata(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    expected = {
        "Formula-Studio-Quickstart.md": (
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "2026-07-08",
            "54 个买点",
            "55 个卖点",
            "待截图元数据",
            "不是已捕获声明",
        ),
        "Formula-Studio-Quickstart-en.md": (
            "300750.SZ",
            "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
            "2026-07-08",
            "54 BUY signals",
            "55 SELL signals",
            "future-screenshot metadata",
            "not a capture-complete claim",
        ),
    }
    for filename, markers in expected.items():
        page = tmp_path / filename
        document = page.read_text(encoding="utf-8")
        page.write_text(document.replace(markers[0], "600000.SH", 1), encoding="utf-8")

    failures = verify_wiki(tmp_path, final=False)

    for filename in expected:
        assert any(
            filename in failure and "workflow content contract" in failure
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
            "Only a task with a `backtest_run` target currently exposes a report deep link",
            "only completed tasks show a backtest report",
        )
        .replace(
            "Market updates and analysis currently have no Task Center result deep link",
            "every task has a result deep link",
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Task-Center-en.md" in failure
        and "workflow content contract" in failure
        and "only completed tasks show a backtest report" in failure
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
    assert verify_docs_module._canonical_app_routes(repo) == frozenset(
        contract.values()
    )
    for key in contract:
        assert source.count(f"routePaths.{key}") == 1
    assert "/comment-only-route" not in verify_docs_module._canonical_app_routes(repo)


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
            verify_docs_module._canonical_app_routes(
                Path(__file__).resolve().parents[2]
            ),
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
