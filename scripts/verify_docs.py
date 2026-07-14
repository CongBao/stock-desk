from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Literal
from urllib.parse import unquote, urlsplit
import warnings

from markdown_it import MarkdownIt
from markdown_it.token import Token
from PIL import Image, UnidentifiedImageError
import yaml  # type: ignore[import-untyped]

from stock_desk.market.types import BAR_SOURCE_PROVIDER_IDS


REQUIRED_PUBLIC_DOCUMENTS = (
    "README.md",
    "README.en.md",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "docs/architecture.md",
    "docs/backup-and-restore.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
    "docs/disclaimer.md",
)

REQUIRED_SECTIONS = {
    "README.md": (
        "产品定位",
        "核心功能",
        "下载安装",
        "使用文档",
        "安全与范围",
    ),
    "README.en.md": (
        "Product positioning",
        "Core features",
        "Download and install",
        "Documentation",
        "Safety and scope",
    ),
    "CONTRIBUTING.md": ("Development setup", "Quality gates", "Pull requests"),
    "SUPPORT.md": ("Questions", "Bug reports", "Security"),
    "CHANGELOG.md": ("Unreleased",),
    "ROADMAP.md": ("Released", "Planned"),
    "docs/architecture.md": (
        "Deployment model",
        "Modules and boundaries",
        "Data and storage",
        "Trust and security",
    ),
    "docs/backup-and-restore.md": (
        "Deployment support",
        "Upgrade and rollback procedure",
    ),
    "docs/configuration.md": (
        "Native installers",
        "Source development",
        "Container deployment",
        "Application settings",
        "Container settings",
        "Provider credentials",
    ),
    "docs/troubleshooting.md": (
        "Startup and health",
        "Data and charts",
        "Tasks and workers",
        "Model providers",
        "Backup and restore",
    ),
    "docs/disclaimer.md": (
        "Research use only",
        "Data limitations",
        "Model limitations",
        "User responsibility",
    ),
}

REQUIRED_WIKI_PAGE_STEMS = (
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

REQUIRED_WIKI_APP_UI_LABELS = {
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

REQUIRED_WIKI_EXTERNAL_UI_LABELS = {
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

WIKI_EXTERNAL_UI_LABEL_ALLOWLIST = {
    "github": frozenset(
        {
            ("Pull Requests", "拉取请求"),
            ("Actions", "自动化"),
            ("Releases", "发行版"),
        }
    ),
    "windows": frozenset(
        {
            ("Start menu", "“开始”菜单"),
            ("Installed apps", "已安装的应用"),
        }
    ),
    "macos": frozenset(
        {
            ("About This Mac", "关于本机"),
            ("Applications", "“应用程序”"),
            ("Gatekeeper", "安全性检查"),
        }
    ),
}

REQUIRED_WIKI_APP_UI_SOURCE_FILES = {
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

type VisibleUiEvidenceKind = Literal[
    "button_expression", "jsx_text", "placeholder", "route_label"
]

REQUIRED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE: dict[
    str, dict[str, tuple[str, VisibleUiEvidenceKind]]
] = {
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

REQUIRED_WIKI_FEATURE_BINDINGS = {
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
        "github-actions:release-run-29114342142",
    ),
    "R-080": (
        "Project-Governance-and-Release-Evidence#发布验证",
        "Project-Governance-and-Release-Evidence-en#release-verification",
        "发布验证 / Release verification",
        "cross-platform-release-assets",
        "github-actions:release-run-29114342142",
    ),
    "R-081": (
        "Project-Governance-and-Release-Evidence#发布验证",
        "Project-Governance-and-Release-Evidence-en#release-verification",
        "发布验证 / Release verification",
        "cross-platform-release-assets",
        "github-actions:release-run-29114342142",
    ),
    "R-082": (
        "Project-Governance-and-Release-Evidence#发布验证",
        "Project-Governance-and-Release-Evidence-en#release-verification",
        "发布验证 / Release verification",
        "cross-platform-release-assets",
        "github-actions:release-run-29114342142",
    ),
}

REQUIRED_WIKI_DOCUMENTATION_ENTRY_MARKERS = {
    "Project-Governance-and-Release-Evidence.md": (
        "README 提供精简的中英双语入口",
        "详细的中英双语 Wiki",
    ),
    "Project-Governance-and-Release-Evidence-en.md": (
        "README provides a concise bilingual entry point",
        "detailed bilingual Wiki",
    ),
}

REQUIRED_WIKI_WORKFLOW_CONTENT = {
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
            "预览只使用已保存、已校验的不可变版本。产品运行结果会绑定数据集版本、数据截止时间、公式版本和公式摘要；本页截图清单只记录图片与行情来源元数据，不把截图当作完整运行结果的替代品。",
            "截图可见已保存的 v1、通过校验的 MACD 源码、宁德时代 K 线主图、公式副图和 BUY/SELL 标记。当前可见区域没有展示完整信号计数或公式摘要，因此本页不据此声称精确的信号总数，也不把图片用作回测结果。",
            "不公开原始行",
            "保存草稿可以保留尚未通过校验的文本",
            "不会生成可预览或可回测版本",
            "复制公式",
            "技术指标主要用于绘图输出",
            "即使技术指标保存了 BUY/SELL",
            "也不会出现在回测向导",
        ),
        (
            "编辑草稿时会自动运行预览",
            "直接预览未保存草稿",
            "54 个买点",
            "55 个卖点",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "待截图元数据",
            "不是已捕获声明",
        ),
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
            "Preview uses only a saved, validated, immutable version. The product runtime binds dataset version, data cutoff, formula version, and formula checksum; this page's screenshot manifest records only image and market-source metadata and does not replace the complete runtime result.",
            "The image visibly shows saved v1, the validated MACD source, CATL's K-line main chart, the formula subchart, and BUY/SELL markers. The visible crop does not show a complete signal count or formula checksum, so this page makes no exact signal-total claim from the image and does not present it as a backtest result.",
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
            "54 BUY signals",
            "55 SELL signals",
            "sha256:47d4a02851407ae0d2730497f7b93bd2b249f02c3f03a84b8e42a1e20c2530a0",
            "future-screenshot metadata",
            "not a capture-complete claim",
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
            "公式版本 `627365c8-8ac3-4fc4-adb6-4c1e05a055b5`",
            "价格与信号来自真实 BaoStock 数据",
            "执行状态明确使用 `stock_desk_demo`",
            "不是含权威停牌/涨跌停证据的生产回测",
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
            "38 笔已实现交易",
            "胜率 36.84%",
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
            "formula version `627365c8-8ac3-4fc4-adb6-4c1e05a055b5`",
            "Prices and signals in this documentation run are real BaoStock data",
            "execution status explicitly uses the `stock_desk_demo` assumption",
            "not a production run with authoritative suspension and price-limit evidence",
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
            "38 realized trades",
            "36.84% win rate",
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

REQUIRED_WIKI_LOW_CODE_SECTION_FORBIDDEN = {
    "Stock-Pools.md": (
        ("操作步骤", "预期结果"),
        ("`code", "`issues"),
    ),
    "Stock-Pools-en.md": (
        ("Steps", "Expected result"),
        ("`code", "`issues"),
    ),
}

REQUIRED_WIKI_LOW_CODE_SECTION_REQUIRED = {
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

REQUIRED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS = {
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

REQUIRED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS = {
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
            "预览只使用已保存、已校验的不可变版本。产品运行结果会绑定数据集版本、数据截止时间、公式版本和公式摘要；本页截图清单只记录图片与行情来源元数据，不把截图当作完整运行结果的替代品。",
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
            "Preview uses only a saved, validated, immutable version. The product runtime binds dataset version, data cutoff, formula version, and formula checksum; this page's screenshot manifest records only image and market-source metadata and does not replace the complete runtime result.",
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


REQUIRED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS = {
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


REQUIRED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS = {
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


REQUIRED_WIKI_ANALYSIS_PLATFORM_CONTENT = {
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


REQUIRED_WIKI_ENTRY_FILES = (
    "Home.md",
    "Home-en.md",
    "_Sidebar.md",
    "_Sidebar-en.md",
    "Feature-Index.md",
    "Feature-Index-en.md",
    "SCREENSHOT-MANIFEST.yml",
)

REPLACED_WIKI_PAGE_FILENAMES = frozenset(
    {
        "Installation.md",
        "Market-Data-and-Charts.md",
        "Formula-Studio.md",
        "Backtesting.md",
        "Multi-Agent-Research.md",
        "Backup-and-Restore.md",
        "Configuration-and-Security.md",
    }
)

FORBIDDEN_PUBLIC_REFERENCES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "SCREENSHOT_PLACEHOLDER",
    "/Users/",
)

FORBIDDEN_TRACKED_PREFIXES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "outputs/",
    "work/",
)

SOURCE_FREE_WINDOWS_INSTALLER_PATTERNS = (
    "stock-desk-1.1.0-beta.2-unsigned-x64-setup.exe",
)

REQUIRED_PUBLIC_SNIPPETS = {
    "README.md": ("https://github.com/CongBao/stock-desk/releases/latest",),
    "README.en.md": ("https://github.com/CongBao/stock-desk/releases/latest",),
    "docs/architecture.md": (
        "v1.1 Windows desktop topology",
        "Source development topology",
        "Container topology",
        "Tauri v2 host",
        "127.0.0.1",
        "random",
        r"%LOCALAPPDATA%\Stock Desk\v1.1",
        "safe checkpoint",
    ),
    "docs/backup-and-restore.md": (
        "Compose image digest",
        "immutable source commit",
        "exact macOS installer artifact",
    ),
    "docs/configuration.md": (
        "Native installers",
        "Source development",
        "Container deployment",
        "v1.1 Windows desktop",
        r"%LOCALAPPDATA%\Stock Desk\v1.1",
        r"%LOCALAPPDATA%\stock-desk",
        "~/Library/Application Support/stock-desk",
        "external browser",
        "config/master.key",
    ),
}

WIKI_FORBIDDEN_REFERENCES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "/Users/",
    "C:\\Users\\",
    "file://",
    "~/.ssh/",
    "id_ed25519",
    "BEGIN OPENSSH PRIVATE KEY",
)

WIKI_PLACEHOLDER_PATTERNS = (
    "screenshot_placeholder",
    "screenshot placeholder",
    "replace after integrated release-candidate capture",
)

APPROVED_RASTER_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".webp"})
PUBLISHABLE_SUFFIXES = frozenset({".md", *APPROVED_RASTER_SUFFIXES})
ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
MIN_SCREENSHOT_WIDTH = 320
MIN_SCREENSHOT_HEIGHT = 180
SCREENSHOT_MANIFEST_SCHEMA = "stock-desk-documentation-screenshots-v1"
SCREENSHOT_DISCLAIMER = "\u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"
README_ENGLISH_SCREENSHOT_DISCLAIMER = (
    "For feature demonstration only; not investment advice."
)
README_SCREENSHOT_MANIFEST = "docs/images/manifest.yml"
README_SCREENSHOT_STATES = {
    "real_chart": ("/market", True),
    "real_formula_preview": ("/formulas", True),
    "blocked_real_backtest_preflight": ("/backtests", True),
    "analysis_readiness": ("/analysis", False),
}


@dataclass(frozen=True)
class ReadmeScreenshotBinding:
    path: str
    state: str
    route: str
    contains_market_data: bool


@dataclass(frozen=True)
class ReadmeMarketDataIdentity:
    symbol: str
    name: str
    period: str
    adjustment: str
    start: str
    end: str
    source: str
    data_cutoff: str
    dataset_version: str


README_MARKET_DATA_KEYS = frozenset(ReadmeMarketDataIdentity.__dataclass_fields__)
README_SCREENSHOT_BINDINGS = {
    "market-data-and-charts": ReadmeScreenshotBinding(
        "docs/images/market-data-and-charts.png",
        "real_chart",
        "/market",
        True,
    ),
    "formula-studio": ReadmeScreenshotBinding(
        "docs/images/formula-studio.png",
        "real_formula_preview",
        "/formulas",
        True,
    ),
    "backtesting": ReadmeScreenshotBinding(
        "docs/images/backtesting.png",
        "blocked_real_backtest_preflight",
        "/backtests",
        True,
    ),
    "multi-agent-research": ReadmeScreenshotBinding(
        "docs/images/multi-agent-research.png",
        "analysis_readiness",
        "/analysis",
        False,
    ),
}
README_SCREENSHOT_MARKET_IDENTITIES = {
    "market-data-and-charts": ReadmeMarketDataIdentity(
        "600519.SH",
        "贵州茅台",
        "1d",
        "qfq",
        "2021-01-01",
        "2026-07-08",
        "baostock",
        "2026-07-08T07:00:00Z",
        "sha256:aa8112c9eda7ed05ed8d92d21afe9dae45fafb295a0fa5ba278c1805a7533236",
    ),
    "formula-studio": ReadmeMarketDataIdentity(
        "300750.SZ",
        "宁德时代",
        "1d",
        "qfq",
        "2021-01-01",
        "2026-07-08",
        "baostock",
        "2026-07-08T07:00:00Z",
        "sha256:7e7fbcce7ee0c7a0bd58b9ebd7d7e06c0755b4195ee3a32c49dfab269147f2fe",
    ),
    "backtesting": ReadmeMarketDataIdentity(
        "000001.SZ",
        "平安银行",
        "1d",
        "qfq",
        "2021-01-01",
        "2026-07-08",
        "baostock",
        "2026-07-08T07:00:00Z",
        "sha256:5a3d9256e58f5bafbad48a7d1fb4ec690d032552aee4c6ae4df7b9940356ec24",
    ),
    "multi-agent-research": None,
}
README_SCREENSHOT_CAPTION_MARKERS = {
    "README.md": {
        "docs/images/market-data-and-charts.png": (
            "贵州茅台",
            "600519.SH",
            "BaoStock",
            SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/formula-studio.png": (
            "宁德时代",
            "300750.SZ",
            "BaoStock",
            "MACD BUY/SELL",
            SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/backtesting.png": (
            "平安银行",
            "000001.SZ",
            "BaoStock",
            "严格预检被阻断",
            "没有合法的 Tushare execution-status 快照",
            "未创建任务或报告",
            "不代表回测成功、结果或胜率",
            SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/multi-agent-research.png": (
            "招商银行",
            "600036.SH",
            "无已验证模型",
            "未发起模型调用",
            "未生成报告",
        ),
    },
    "README.en.md": {
        "docs/images/market-data-and-charts.png": (
            "Kweichow Moutai",
            "600519.SH",
            "BaoStock",
            SCREENSHOT_DISCLAIMER,
            README_ENGLISH_SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/formula-studio.png": (
            "CATL",
            "300750.SZ",
            "BaoStock",
            "MACD BUY/SELL",
            SCREENSHOT_DISCLAIMER,
            README_ENGLISH_SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/backtesting.png": (
            "Ping An Bank",
            "000001.SZ",
            "BaoStock",
            "Strict preflight is blocked",
            "no authorized Tushare execution-status snapshot",
            "No task or report was created",
            "not a successful backtest, result, or win rate",
            SCREENSHOT_DISCLAIMER,
            README_ENGLISH_SCREENSHOT_DISCLAIMER,
        ),
        "docs/images/multi-agent-research.png": (
            "China Merchants Bank",
            "600036.SH",
            "no verified model",
            "no model call started",
            "no report generated",
        ),
    },
}
README_SCREENSHOT_LOCAL_CONTEXTS = {
    "README.md": {
        "docs/images/market-data-and-charts.png": (
            "![带来源证据的 A 股行情图](docs/images/market-data-and-charts.png) "
            "贵州茅台 `600519.SH`，BaoStock 日线/前复权，数据截至 "
            "`2026-07-08T07:00:00Z`。仅作功能演示，不构成投资建议。"
        ),
        "docs/images/formula-studio.png": (
            "![宁德时代 MACD BUY/SELL 公式预览](docs/images/formula-studio.png)"
            "<br>宁德时代 `300750.SZ`；BaoStock，1d/qfq；截至 "
            "`2026-07-08T07:00:00Z`；显示 MACD BUY/SELL。"
            "仅作功能演示，不构成投资建议。"
        ),
        "docs/images/backtesting.png": (
            "![平安银行 MACD 回测严格预检被阻断](docs/images/backtesting.png)"
            "<br>平安银行 `000001.SZ` 的真实 MACD 配置；BaoStock，1d/qfq；截至 "
            "`2026-07-08T07:00:00Z`。因没有合法的 Tushare execution-status "
            "快照，严格预检被阻断；未创建任务或报告，不代表回测成功、结果或胜率。"
            "仅作功能演示，不构成投资建议。"
        ),
        "docs/images/multi-agent-research.png": (
            "![招商银行模型与证据准备状态](docs/images/multi-agent-research.png)"
            "<br>招商银行 `600036.SH` 的模型/证据准备状态：无已验证模型，"
            "未发起模型调用，也未生成报告。"
        ),
    },
    "README.en.md": {
        "docs/images/market-data-and-charts.png": (
            "![A-share market chart with provenance]"
            "(docs/images/market-data-and-charts.png) Kweichow Moutai `600519.SH`; "
            "BaoStock daily/qfq data; cutoff `2026-07-08T07:00:00Z`. "
            "For feature demonstration only; not investment advice. "
            "（仅作功能演示，不构成投资建议。）"
        ),
        "docs/images/formula-studio.png": (
            "![CATL MACD BUY/SELL formula preview](docs/images/formula-studio.png)"
            "<br>CATL `300750.SZ`; BaoStock, 1d/qfq; cutoff "
            "`2026-07-08T07:00:00Z`; MACD BUY/SELL are visible. "
            "For feature demonstration only; not investment advice. "
            "（仅作功能演示，不构成投资建议。）"
        ),
        "docs/images/backtesting.png": (
            "![Ping An Bank MACD strict preflight blocked](docs/images/backtesting.png)"
            "<br>Real MACD configuration for Ping An Bank `000001.SZ`; BaoStock, "
            "1d/qfq; cutoff `2026-07-08T07:00:00Z`. Strict preflight is blocked "
            "because no authorized Tushare execution-status snapshot exists. No task "
            "or report was created; this is not a successful backtest, result, or win "
            "rate. For feature demonstration only; not investment advice. "
            "（仅作功能演示，不构成投资建议。）"
        ),
        "docs/images/multi-agent-research.png": (
            "![China Merchants Bank model and evidence readiness]"
            "(docs/images/multi-agent-research.png)<br>Model/evidence readiness for "
            "China Merchants Bank `600036.SH`: no verified model, no model call "
            "started, and no report generated."
        ),
    },
}
README_SCREENSHOT_CONTRADICTORY_MARKERS = {
    "README.md": {
        "docs/images/backtesting.png": ("回测成功，胜率",),
    },
    "README.en.md": {
        "docs/images/backtesting.png": ("successful backtest result with a",),
    },
}
FORBIDDEN_MARKET_PROVENANCE_MARKERS = ("synthetic", "fixture", "demo", "cc0")
ACTIVE_REQUIREMENT_IDS = frozenset(f"R-{number:03d}" for number in range(1, 83))
MARKET_SCREENSHOT_PAGE_PREFIXES = (
    "Market-",
    "Data-",
    "Stock-Pools",
    "Formula-",
    "MACD-",
    "A-Share-",
    "Backtest-",
)
EVIDENCE_SURFACE_TYPES = frozenset(
    {
        "app-route",
        "wiki-page",
        "windows-installer",
        "macos-installer",
        "github-release",
        "github-actions",
        "repository-audit",
    }
)
REPOSITORY_AUDIT_LOCATORS = frozenset(
    {
        "requirements-boundary",
        "repository-name",
        "remote",
        "git-identity",
        "local-layout",
        "branch-policy",
        "public-boundary",
        "stage-delivery",
        "open-source-governance",
        "release-verification",
        "documentation-entry",
        "private-spec-boundary",
        "ssh-identity-policy",
    }
)

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_FENCED_SHELL = re.compile(
    r"^```(?:bash|sh|shell)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)

_MARKDOWN = MarkdownIt("gfm-like", {"html": True})

_FEATURE_INDEX_ROW = re.compile(
    r"^\|\s*(R-\d{3}(?:\s*[\u2013\u2014-]\s*R?-?\d{3})?)\s*\|"
    r"\s*\[[^]]+\]\(([^)]+)\)\s*\|"
    r"\s*\[[^]]+\]\(([^)]+)\)\s*\|"
    r"\s*([^|]+?)\s*\|\s*`?([a-z0-9][a-z0-9-]*)`?\s*\|"
    r"\s*`?([^|`]+)`?\s*\|\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    kind: Literal["link", "image"]
    target: str


@dataclass(frozen=True, slots=True)
class ReadmeCommandEvidence:
    gate: str
    test_selectors: tuple[str, ...]


class _RenderedHTMLTargets(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[RenderedTarget] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.casefold(): value for name, value in attrs}
        normalized_tag = tag.casefold()
        if normalized_tag == "a" and attributes.get("href"):
            self.targets.append(RenderedTarget("link", attributes["href"] or ""))
        elif normalized_tag == "img" and attributes.get("src"):
            self.targets.append(RenderedTarget("image", attributes["src"] or ""))


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)", re.MULTILINE)
_MAKE_COMMAND = re.compile(r"(?:^|[;&|]\s*|\s)make\s+([A-Za-z0-9_.-]+)")
_SCRIPT_COMMAND = re.compile(
    r"uv\s+run(?:\s+--frozen)?\s+python\s+(scripts/[A-Za-z0-9_./-]+\.py)"
)

_ATTESTATION_BASE = (
    "gh",
    "attestation",
    "verify",
    "INSTALLER_PATH",
    "--repo",
    "CongBao/stock-desk",
    "--signer-workflow",
    "CongBao/stock-desk/.github/workflows/release.yml",
)
_NATIVE_ATTESTATION_TESTS = (
    "tests/acceptance/test_release_artifacts.py::"
    "test_native_manifest_checksum_sbom_and_attestation_chain_is_revision_bound",
    "tests/acceptance/test_installed_distribution.py::"
    "test_release_workflow_generates_checksums_sbom_and_provenance",
)
_CONTAINER_SMOKE_TESTS = (
    "tests/acceptance/test_container_smoke.py::"
    "test_compose_worker_completes_demo_task_through_shared_sqlite",
)

README_COMMAND_EVIDENCE: dict[tuple[str, ...], ReadmeCommandEvidence] = {
    _ATTESTATION_BASE: ReadmeCommandEvidence(
        gate="clean-install:native-attestation",
        test_selectors=_NATIVE_ATTESTATION_TESTS,
    ),
    (*_ATTESTATION_BASE, "--predicate-type", "https://spdx.dev/Document/v2.3"): (
        ReadmeCommandEvidence(
            gate="clean-install:native-sbom-attestation",
            test_selectors=_NATIVE_ATTESTATION_TESTS,
        )
    ),
    ("docker", "compose", "up", "--build", "--wait"): ReadmeCommandEvidence(
        gate="smoke:release-container",
        test_selectors=_CONTAINER_SMOKE_TESTS,
    ),
    (
        "docker",
        "compose",
        "down",
        "--volumes",
        "--remove-orphans",
    ): ReadmeCommandEvidence(
        gate="smoke:release-container",
        test_selectors=_CONTAINER_SMOKE_TESTS,
    ),
    (
        "uv",
        "run",
        "--frozen",
        "python",
        "scripts/verify_docs.py",
    ): ReadmeCommandEvidence(
        gate="candidate:verify-docs",
        test_selectors=(
            "tests/acceptance/test_release_docs.py::"
            "test_bilingual_readme_baseline_contains_verified_installation_and_use",
        ),
    ),
}

for _target, _selector in {
    "acceptance": "tests/acceptance/test_market_flow.py",
    "acceptance-formula": "tests/acceptance/test_formula_consistency.py",
    "acceptance-backtest": "tests/acceptance/test_backtest_semantics.py",
    "e2e-market": "web/e2e/market.spec.ts",
    "e2e-formula": "web/e2e/formula-studio.spec.ts",
    "e2e-backtest": "web/e2e/backtest.spec.ts",
    "e2e-analysis": "web/e2e/analysis.spec.ts",
    "e2e-task-center": "web/e2e/task-center.spec.ts",
    "security": "tests/security",
}.items():
    README_COMMAND_EVIDENCE[("make", _target)] = ReadmeCommandEvidence(
        gate=f"candidate:make-{_target}",
        test_selectors=(_selector,),
    )

for _target, _selector in {
    "benchmark": "tests/performance/test_chart_query.py",
    "benchmark-formula": "tests/performance/test_formula_preview.py",
    "benchmark-backtest": "tests/performance/test_single_backtest.py",
}.items():
    README_COMMAND_EVIDENCE[("make", _target)] = ReadmeCommandEvidence(
        gate="candidate:make-performance-regressions",
        test_selectors=(_selector,),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _headings(document: str) -> set[str]:
    headings: set[str] = set()
    for raw_heading in _HEADING.findall(document):
        heading = raw_heading.strip().rstrip("#").strip()
        if heading.startswith("[") and "]" in heading:
            heading = heading[1 : heading.index("]")]
        headings.add(heading)
    return headings


def _heading_sequence(document: str) -> tuple[str, ...]:
    headings: list[str] = []
    for raw_heading in _HEADING.findall(document):
        heading = raw_heading.strip().rstrip("#").strip()
        if heading.startswith("[") and "]" in heading:
            heading = heading[1 : heading.index("]")]
        headings.append(heading)
    return tuple(headings)


def _level_two_section(document: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        document,
        re.MULTILINE | re.DOTALL,
    )
    return "" if match is None else match.group("body")


def _wiki_screenshot_evidence_ids(document: str) -> tuple[str, ...]:
    identifiers: list[str] = []
    for line in document.splitlines():
        if not re.match(
            r"^\s*(?:截图证据 ID[：:]|Screenshot evidence ID:)\s*",
            line,
            re.IGNORECASE,
        ):
            continue
        identifiers.extend(re.findall(r"`([a-z0-9][a-z0-9-]*)`", line, re.IGNORECASE))
    return tuple(identifiers)


def _wiki_ui_label_mappings(
    document: str,
) -> tuple[tuple[tuple[str, str], ...], bool]:
    marker = "## Chinese UI labels\n"
    start = document.find(marker)
    if start < 0:
        return (), False
    section = document[start + len(marker) :]
    next_heading = section.find("\n## ")
    if next_heading >= 0:
        section = section[:next_heading]
    mappings: list[tuple[str, str]] = []
    ordinals: list[int] = []
    for line in section.splitlines():
        match = re.fullmatch(
            r"(\d+)\.\s+`([^`]+)`\s+[—-]\s+\S.*",
            line,
        )
        if match is None:
            continue
        label = match.group(2)
        english, separator, chinese_with_close = label.partition("（")
        if (
            not separator
            or not english
            or not chinese_with_close.endswith("）")
            or len(chinese_with_close) == 1
        ):
            continue
        ordinals.append(int(match.group(1)))
        mappings.append((english, chinese_with_close[:-1]))
    return tuple(mappings), ordinals == list(range(1, len(ordinals) + 1))


@lru_cache(maxsize=1)
def _tracked_web_source_paths() -> frozenset[str]:
    repo = Path(__file__).resolve().parent.parent
    try:
        return frozenset(
            subprocess.run(
                ("git", "ls-files", "web/src"),
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.splitlines()
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return frozenset()


@lru_cache(maxsize=1)
def _tracked_repository_paths() -> frozenset[str]:
    repo = Path(__file__).resolve().parent.parent
    try:
        return frozenset(
            subprocess.run(
                ("git", "ls-files"),
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.splitlines()
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return frozenset()


@lru_cache(maxsize=None)
def _tracked_source_text(relative_path: str) -> str:
    if relative_path not in _tracked_repository_paths():
        return ""
    repo = Path(__file__).resolve().parent.parent
    try:
        return (repo / relative_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _python_str_enum_members(source: str, class_name: str) -> dict[str, str] | None:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    enum_class = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if enum_class is None or not any(
        (isinstance(base, ast.Name) and base.id == "StrEnum")
        or (isinstance(base, ast.Attribute) and base.attr == "StrEnum")
        for base in enum_class.bases
    ):
        return None
    members: dict[str, str] = {}
    for statement in enum_class.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id.isupper()
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            members[statement.targets[0].id] = statement.value.value
    return members


def _python_class_method(
    source: str, class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    class_node = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if class_node is None:
        return None
    return next(
        (
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ),
        None,
    )


def _expression_dump(expression: str) -> str:
    return ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)


def _node_is_expression(node: ast.AST, expression: str) -> bool:
    return ast.dump(node, include_attributes=False) == _expression_dump(expression)


def _tree_contains_expression(node: ast.AST, expression: str) -> bool:
    expected = _expression_dump(expression)
    return any(
        ast.dump(candidate, include_attributes=False) == expected
        for candidate in ast.walk(node)
    )


def _model_missing_maps_to_invalid_response(source: str) -> bool:
    method = _python_class_method(source, "OpenAICompatibleProvider", "test_connection")
    if method is None:
        return False
    missing_check = next(
        (
            node
            for node in ast.walk(method)
            if isinstance(node, ast.If)
            and _node_is_expression(
                node.test,
                "not any(entry.id == self.model for entry in models.data)",
            )
            and any(
                isinstance(child, ast.Raise)
                and (
                    (isinstance(child.exc, ast.Name) and child.exc.id == "ValueError")
                    or (
                        isinstance(child.exc, ast.Call)
                        and isinstance(child.exc.func, ast.Name)
                        and child.exc.func.id == "ValueError"
                    )
                )
                for child in node.body
            )
        ),
        None,
    )
    invalid_response_fold = any(
        isinstance(node, ast.IfExp)
        and _node_is_expression(node.test, "isinstance(error, ModelProviderError)")
        and isinstance(node.orelse, ast.Call)
        and isinstance(node.orelse.func, ast.Name)
        and node.orelse.func.id == "ModelInvalidResponseError"
        for node in ast.walk(method)
    )
    return missing_check is not None and invalid_response_fold


def _typescript_literal_record(source: str, name: str) -> dict[str, str] | None:
    match = re.search(
        rf"\bconst\s+{re.escape(name)}\s*:[^=]+?=\s*\{{(?P<body>.*?)^\}};",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return None
    members: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(
            r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'\s*,?\s*",
            line,
        )
        if item is None or item.group(1) in members:
            return None
        members[item.group(1)] = item.group(2)
    return members


def _analysis_report_state_invariants(source: str) -> bool:
    method = _python_class_method(source, "ResearchReport", "validate_report")
    if method is None:
        return False
    complete = next(
        (
            node
            for node in method.body
            if isinstance(node, ast.If)
            and _node_is_expression(node.test, "self.status is ReportStatus.COMPLETE")
        ),
        None,
    )
    if complete is None or not complete.orelse:
        return False
    partial = complete.orelse[0]
    if not (
        isinstance(partial, ast.If)
        and _node_is_expression(partial.test, "self.status is ReportStatus.PARTIAL")
        and complete.body
        and isinstance(complete.body[0], ast.If)
        and partial.body
        and isinstance(partial.body[0], ast.If)
        and partial.orelse
        and isinstance(partial.orelse[0], ast.If)
    ):
        return False
    complete_guard = complete.body[0].test
    partial_guard = partial.body[0].test
    insufficient = partial.orelse[0]
    if not _node_is_expression(
        insufficient.test,
        "self.rating is not None or self.confidence != 0.0",
    ):
        return False
    retry_guard = insufficient.orelse[0] if insufficient.orelse else None
    return (
        _tree_contains_expression(complete_guard, "self.rating is None")
        and _tree_contains_expression(partial_guard, "self.rating is not None")
        and _tree_contains_expression(partial_guard, "self.confidence != 0.0")
        and _tree_contains_expression(partial_guard, "not self.missing_modules")
        and _tree_contains_expression(partial_guard, "not self.retry_actions")
        and isinstance(retry_guard, ast.If)
        and _node_is_expression(retry_guard.test, "self.retry_actions")
    )


def _python_class_annotated_fields(source: str, class_name: str) -> frozenset[str]:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return frozenset()
    class_node = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if class_node is None:
        return frozenset()
    return frozenset(
        statement.target.id
        for statement in class_node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    )


def _evidence_display_contract(domain_source: str, component_source: str) -> bool:
    required_fields = frozenset(
        {
            "canonical_source",
            "source_record",
            "published_at",
            "data_cutoff",
            "fetched_at",
            "dataset_version",
            "quality_flags",
            "route",
        }
    )
    if not required_fields <= _python_class_annotated_fields(
        domain_source, "EvidenceItem"
    ):
        return False
    expected_binding_counts = {
        "item.canonicalSource": 1,
        "item.sourceRecord": 1,
        "item.publishedAt": 2,
        "item.dataCutoff": 1,
        "item.fetchedAt": 1,
        "item.datasetVersion": 1,
        "item.qualityFlags": 2,
        "item.route": 3,
    }
    if any(
        component_source.count(binding) != expected_count
        for binding, expected_count in expected_binding_counts.items()
    ):
        return False
    return all(
        marker in component_source
        for marker in (
            "<dt>记录</dt>",
            "<dt>数据版本</dt>",
            "<dt>发布时间：</dt>",
            "<dt>数据截止</dt>",
            "<dt>采集时间</dt>",
            "<dt>质量标记：</dt>",
            "<dt>来源路由：</dt>",
        )
    )


def _module_string_constant(source: str, name: str) -> str | None:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    for statement in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value = statement.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _branch_calls(branch: ast.If, function_name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
        for statement in branch.body
        for node in ast.walk(statement)
    )


def _prompt_trust_boundary_contract(source: str) -> bool:
    if (
        _module_string_constant(source, "UNTRUSTED_DATA_LABEL") != "untrusted-data"
        or _module_string_constant(source, "TRUSTED_CONTROL_LABEL") != "trusted-control"
    ):
        return False
    try:
        module = ast.parse(source)
    except SyntaxError:
        return False
    make_block = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "make_untrusted_data_block"
        ),
        None,
    )
    validate_blocks = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_prompt_blocks"
        ),
        None,
    )
    if make_block is None or validate_blocks is None:
        return False
    envelope_is_untrusted = any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "trust_label"
            and isinstance(value, ast.Name)
            and value.id == "UNTRUSTED_DATA_LABEL"
            for key, value in zip(node.keys, node.values, strict=True)
        )
        for node in ast.walk(make_block)
    )
    untrusted_branch = next(
        (
            node
            for node in ast.walk(validate_blocks)
            if isinstance(node, ast.If)
            and _node_is_expression(node.test, "label == UNTRUSTED_DATA_LABEL")
        ),
        None,
    )
    trusted_branch = next(
        (
            node
            for node in ast.walk(validate_blocks)
            if isinstance(node, ast.If)
            and _node_is_expression(node.test, "label == TRUSTED_CONTROL_LABEL")
        ),
        None,
    )
    return (
        envelope_is_untrusted
        and untrusted_branch is not None
        and _branch_calls(untrusted_branch, "_validate_untrusted_block")
        and trusted_branch is not None
        and _branch_calls(trusted_branch, "_validate_trusted_control_block")
    )


def _responsive_e2e_contract(source: str) -> bool:
    routes_match = re.search(
        r"\bconst\s+routes\s*=\s*\[(?P<body>.*?)\]\s*as\s+const;",
        source,
        re.DOTALL,
    )
    viewports_match = re.search(
        r"\bconst\s+viewports\s*=\s*\[(?P<body>.*?)\]\s*as\s+const;",
        source,
        re.DOTALL,
    )
    if routes_match is None or viewports_match is None:
        return False
    routes = tuple(re.findall(r"'(/[^']+)'", routes_match.group("body")))
    viewport_values: list[tuple[str, int, int, bool]] = []
    for item in re.findall(r"\{(?P<body>[^{}]+)\}", viewports_match.group("body")):
        name = re.search(r"name:\s*'([^']+)'", item)
        width = re.search(r"width:\s*(\d+)", item)
        height = re.search(r"height:\s*(\d+)", item)
        collapsed = re.search(r"collapsed:\s*(true|false)", item)
        if None in {name, width, height, collapsed}:
            return False
        assert name is not None and width is not None and height is not None
        assert collapsed is not None
        viewport_values.append(
            (
                name.group(1),
                int(width.group(1)),
                int(height.group(1)),
                collapsed.group(1) == "true",
            )
        )
    expected_routes = (
        "/market",
        "/formulas",
        "/backtests",
        "/analysis",
        "/tasks",
        "/settings",
    )
    expected_viewports = (
        ("wide desktop", 1600, 900, False),
        ("narrow desktop", 1100, 700, True),
        ("tablet landscape", 1024, 768, True),
        ("tablet portrait", 768, 1024, True),
        ("mobile portrait", 390, 844, True),
        ("200 percent effective viewport", 640, 450, True),
        ("short landscape effective viewport", 640, 360, True),
    )
    required_matrix_code = (
        "for (const viewport of viewports)",
        "for (const route of routes)",
        "await expectNoShellOverlap(page);",
        "await expectNoInteractiveControlOverlap(page);",
        "await expectNavigationIsOperable(page);",
        "overflow.scrollWidth",
        "overflow.clientWidth + 1",
        "navigation auto-collapses only when crossing the narrow breakpoint",
        "collapsed navigation renders icons without textual abbreviations",
        "link.locator('.nav-icon svg')",
        "link.locator('.nav-label')",
    )
    return (
        routes == expected_routes
        and tuple(viewport_values) == expected_viewports
        and all(marker in source for marker in required_matrix_code)
    )


def _analysis_start_eligibility_contract(source: str) -> bool:
    start_index = source.find("async function start()")
    start_end = source.find("\n  return (", start_index)
    label_index = source.find("'启动智能分析'")
    button_start = source.rfind("<button", 0, label_index)
    button_end = source.find("</button>", label_index)
    if min(start_index, start_end, label_index, button_start, button_end) < 0:
        return False
    start_section = source[start_index:start_end]
    button_section = source[button_start : button_end + len("</button>")]
    start_required = (
        "if (!maxRetriesIsValid)",
        "preflight === null",
        "preflight.symbol !== symbol",
        "!verifiedModels.some((model) => model.id === modelId)",
    )
    button_required = (
        "preflight === null",
        "preflight.symbol !== symbol",
        "!selectedModelIsVerified",
        "!maxRetriesIsValid",
    )
    return (
        "const maxRetriesIsValid = /^[0-5]$/u.test(maxRetries);" in source
        and all(marker in start_section for marker in start_required)
        and all(marker in button_section for marker in button_required)
        and "ratingEligible" not in start_section
        and "ratingEligible" not in button_section
    )


def _analysis_drawer_focus_contract(source: str) -> bool:
    start = source.find("function closeDrawer()")
    end = source.find("\n  const cancellable", start)
    if start < 0 or end < 0:
        return False
    close_section = source[start:end]
    required_focus = (
        "processButtonRef.current?.focus();",
        "claimTriggerRef.current.focus();",
        "evidenceButtonRef.current?.focus();",
        "setDrawer(null);",
    )
    return (
        all(marker in close_section for marker in required_focus)
        and "event.key === 'Escape'" not in source
        and source.count("onClick={closeDrawer}") == 2
        and source.count("关闭分析流程") == 1
        and source.count("关闭证据") == 1
    )


def _analysis_platform_source_invariant_failures() -> list[str]:
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
    rating_members = {
        "strong_bullish": "强烈看多",
        "bullish": "看多",
        "neutral": "中性",
        "bearish": "看空",
        "strong_bearish": "强烈看空",
    }
    checks = (
        (
            "Model-Provider-Setup: ModelProviderKind exact enum",
            _python_str_enum_members(
                _tracked_source_text("src/stock_desk/analysis/model_config.py"),
                "ModelProviderKind",
            )
            == provider_members,
        ),
        (
            "Model-Provider-Setup: ModelErrorCode exact enum",
            _python_str_enum_members(
                _tracked_source_text("src/stock_desk/analysis/providers/base.py"),
                "ModelErrorCode",
            )
            == error_members,
        ),
        (
            "Model-Provider-Setup: missing model folds to invalid_response",
            _model_missing_maps_to_invalid_response(
                _tracked_source_text(
                    "src/stock_desk/analysis/providers/openai_compatible.py"
                )
            ),
        ),
        (
            "Research-Reports-and-Evidence: exact five-level rating record",
            _typescript_literal_record(
                _tracked_source_text("web/src/features/analysis/ConclusionPanel.tsx"),
                "ratingLabels",
            )
            == rating_members,
        ),
        (
            "Research-Reports-and-Evidence: report state invariants",
            _analysis_report_state_invariants(
                _tracked_source_text("src/stock_desk/analysis/report.py")
            ),
        ),
        (
            "Research-Reports-and-Evidence: evidence fields and UI bindings",
            _evidence_display_contract(
                _tracked_source_text("src/stock_desk/analysis/evidence.py"),
                _tracked_source_text("web/src/features/analysis/EvidencePanel.tsx"),
            ),
        ),
        (
            "Research-Failures-Retries-and-Safety: prompt trust boundary",
            _prompt_trust_boundary_contract(
                _tracked_source_text("src/stock_desk/analysis/content_policy.py")
            ),
        ),
        (
            "Responsive-Navigation-and-Accessibility: executable route matrix",
            _responsive_e2e_contract(
                _tracked_source_text("web/e2e/responsive.spec.ts")
            ),
        ),
        (
            "Research-Reports-and-Evidence: run eligibility excludes rating coverage",
            _analysis_start_eligibility_contract(
                _tracked_source_text("web/src/features/analysis/AnalysisRunPanel.tsx")
            ),
        ),
        (
            "Responsive-Navigation-and-Accessibility: drawer close and focus behavior",
            _analysis_drawer_focus_contract(
                _tracked_source_text("web/src/features/analysis/AnalysisPage.tsx")
            ),
        ),
    )
    return [
        f"source invariant failed: {description}"
        for description, valid in checks
        if not valid
    ]


@lru_cache(maxsize=None)
def _page_ui_source_text(stem: str) -> str:
    repo = Path(__file__).resolve().parent.parent
    tracked = _tracked_web_source_paths()
    documents: list[str] = []
    for relative_path in REQUIRED_WIKI_APP_UI_SOURCE_FILES.get(stem, ()):
        if relative_path not in tracked:
            return ""
        try:
            documents.append((repo / relative_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return ""
    return "\n".join(documents)


def _source_contains_visible_ui_label(
    source: str,
    label: str,
    evidence_kind: VisibleUiEvidenceKind,
) -> bool:
    escaped = re.escape(label)
    if evidence_kind == "placeholder":
        return re.search(rf'\bplaceholder=["\']{escaped}["\']', source) is not None
    if evidence_kind == "route_label":
        return re.search(rf"\blabel:\s*['\"]{escaped}['\"]", source) is not None
    if evidence_kind == "jsx_text":
        for match in re.finditer(rf">\s*{escaped}\s*<", source):
            tag_start = source.rfind("<", 0, match.start() + 1)
            attributes = source[tag_start : match.start() + 1]
            if (
                tag_start >= 0
                and "visually-hidden" not in attributes
                and not re.search(r"\baria-hidden=['\"]?true\b", attributes)
                and not re.search(r"(?:^|\s)hidden(?:\s|=|$)", attributes)
            ):
                return True
        return False
    cursor = 0
    while (button_start := source.find("<button", cursor)) >= 0:
        brace_depth = 0
        quote: str | None = None
        escaped_character = False
        opening_end = -1
        for index in range(button_start + len("<button"), len(source)):
            character = source[index]
            if quote is not None:
                if escaped_character:
                    escaped_character = False
                elif character == "\\":
                    escaped_character = True
                elif character == quote:
                    quote = None
                continue
            if character in {'"', "'", "`"}:
                quote = character
            elif character == "{":
                brace_depth += 1
            elif character == "}" and brace_depth > 0:
                brace_depth -= 1
            elif character == ">" and brace_depth == 0:
                opening_end = index
                break
        if opening_end < 0:
            return False
        closing_start = source.find("</button>", opening_end + 1)
        if closing_start < 0:
            return False
        if label in source[opening_end + 1 : closing_start]:
            return True
        cursor = closing_start + len("</button>")
    return False


def _app_ui_label_in_page_source(stem: str, chinese_label: str) -> bool:
    visible_contract = REQUIRED_WIKI_VISIBLE_APP_UI_SOURCE_EVIDENCE.get(stem)
    if visible_contract is not None:
        evidence = visible_contract.get(chinese_label)
        if evidence is None:
            return False
        relative_path, evidence_kind = evidence
        if relative_path not in _tracked_web_source_paths():
            return False
        source = _tracked_source_text(relative_path)
        return _source_contains_visible_ui_label(source, chinese_label, evidence_kind)
    source = _page_ui_source_text(stem)
    if chinese_label in source:
        return True
    dynamic_connection = re.fullmatch(r"测试 (.+) 连接", chinese_label)
    return (
        dynamic_connection is not None
        and "测试 ${source.name} 连接" in source
        and dynamic_connection.group(1) in source
    )


def _wiki_steps_ui_references(document: str) -> tuple[str, ...]:
    marker = "## Steps\n"
    start = document.find(marker)
    if start < 0:
        return ()
    section = document[start + len(marker) :]
    next_heading = section.find("\n## ")
    if next_heading >= 0:
        section = section[:next_heading]
    return tuple(re.findall(r"\*\*([^*\n]+)\*\*", section))


def _normalized_wiki_navigation(
    targets: tuple[RenderedTarget, ...],
) -> tuple[str, ...]:
    normalized: list[str] = []
    for path in _wiki_navigation_paths(targets):
        if path.endswith("-en"):
            path = path[:-3]
        normalized.append(path)
    return tuple(normalized)


def _wiki_navigation_paths(
    targets: tuple[RenderedTarget, ...],
) -> tuple[str, ...]:
    paths: list[str] = []
    for rendered in targets:
        if rendered.kind != "link":
            continue
        parsed = urlsplit(rendered.target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        paths.append(unquote(parsed.path).removesuffix(".md"))
    return tuple(paths)


def _rendered_targets(document: str) -> tuple[RenderedTarget, ...]:
    rendered: list[RenderedTarget] = []

    def visit(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type == "link_open":
                target = token.attrGet("href")
                if isinstance(target, str) and target:
                    rendered.append(RenderedTarget("link", target))
            elif token.type == "image":
                target = token.attrGet("src")
                if isinstance(target, str) and target:
                    rendered.append(RenderedTarget("image", target))
            elif token.type in {"html_block", "html_inline"}:
                parser = _RenderedHTMLTargets()
                parser.feed(token.content)
                parser.close()
                rendered.extend(parser.targets)
            if token.children:
                visit(token.children)

    visit(_MARKDOWN.parse(document))
    return tuple(rendered)


def _markdown_visible_text(document: str) -> str:
    visible: list[str] = []

    def visit(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type in {"text", "code_inline", "code_block", "fence"}:
                visible.append(token.content)
            elif token.type in {"softbreak", "hardbreak"}:
                visible.append("\n")
            if token.children:
                visit(token.children)

    visit(_MARKDOWN.parse(document))
    return " ".join(visible)


def _local_destination(root: Path, source: Path, target: str) -> Path | None:
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or target.startswith("#"):
        return None
    decoded_path = unquote(parts.path)
    if not decoded_path:
        return None
    return (source.parent / decoded_path).resolve()


def _rendered_target_failures(
    root: Path,
    relative_path: str,
    targets: tuple[RenderedTarget, ...],
    *,
    allowed_files: frozenset[Path] | None = None,
    allow_extensionless_markdown: bool = False,
) -> list[str]:
    failures: list[str] = []
    source = root / relative_path
    resolved_root = root.resolve()
    for rendered in targets:
        target = rendered.target
        parts = urlsplit(target)
        if parts.scheme or parts.netloc:
            if rendered.kind == "image":
                failures.append(
                    f"{relative_path}: external image cannot be verified: {target}"
                )
            elif parts.scheme.casefold() not in ALLOWED_LINK_SCHEMES:
                failures.append(
                    f"{relative_path}: unsupported rendered link scheme: {target}"
                )
            continue
        if target.startswith("#"):
            continue
        destination = _local_destination(root, source, target)
        if destination is None:
            continue
        try:
            destination.relative_to(resolved_root)
        except ValueError:
            failures.append(
                f"{relative_path}: rendered {rendered.kind} escapes the publication root: {target}"
            )
            continue
        if (
            allow_extensionless_markdown
            and rendered.kind == "link"
            and destination.with_name(f"{destination.name}.md") in (allowed_files or ())
        ):
            destination = destination.with_name(f"{destination.name}.md")
        if allowed_files is not None and destination not in allowed_files:
            failures.append(
                f"{relative_path}: rendered {rendered.kind} target is not a scanned publication file: {target}"
            )
            continue
        if rendered.kind == "image":
            if not destination.is_file():
                failures.append(
                    f"{relative_path}: image is not a regular image file: {target}"
                )
            elif destination.suffix.casefold() not in APPROVED_RASTER_SUFFIXES:
                failures.append(
                    f"{relative_path}: unsupported rendered image type: {target}"
                )
        elif not destination.exists():
            failures.append(f"{relative_path}: broken rendered link: {target}")
    return failures


def _make_targets(repo_root: Path) -> set[str]:
    makefile = repo_root / "Makefile"
    if not makefile.is_file():
        return set()
    return set(_MAKE_TARGET.findall(_read(makefile)))


def _command_failures(repo_root: Path, relative_path: str, document: str) -> list[str]:
    failures: list[str] = []
    make_targets = _make_targets(repo_root)
    for block in _FENCED_SHELL.findall(document):
        if relative_path in {"README.md", "README.en.md"}:
            failures.extend(_readme_command_failures(repo_root, relative_path, block))
        for target in _MAKE_COMMAND.findall(block):
            if target not in make_targets:
                failures.append(
                    f"{relative_path}: unsupported Make target in command example: {target}"
                )
        for script in _SCRIPT_COMMAND.findall(block):
            if not (repo_root / script).is_file():
                failures.append(
                    f"{relative_path}: command references missing script: {script}"
                )
    return failures


def _logical_shell_commands(block: str) -> tuple[str, ...]:
    commands: list[str] = []
    pending = ""
    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        commands.append(pending)
        pending = ""
    if pending:
        commands.append(pending)
    return tuple(commands)


def _readme_command_failures(
    repo_root: Path, relative_path: str, block: str
) -> list[str]:
    del repo_root
    failures: list[str] = []
    for command in _logical_shell_commands(block):
        if any(token in command for token in ("|", ";", "`", "$(", ">", "<")):
            failures.append(f"{relative_path}: README command is not allowlisted")
            continue
        try:
            arguments = shlex.split(command, posix=True)
        except ValueError:
            failures.append(f"{relative_path}: README command is not allowlisted")
            continue
        if tuple(arguments) not in README_COMMAND_EVIDENCE:
            failures.append(f"{relative_path}: README command is not allowlisted")
    return failures


def _tracked_boundary_failures(repo_root: Path) -> list[str]:
    if not (repo_root / ".git").exists():
        return []
    try:
        output = subprocess.check_output(
            ["git", "-C", os.fspath(repo_root), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ["Unable to inspect tracked paths for the public-boundary contract"]
    tracked_paths = (os.fsdecode(value) for value in output.split(b"\0") if value)
    return [
        f"Internal path is tracked: {path}"
        for path in sorted(tracked_paths)
        if path.startswith(FORBIDDEN_TRACKED_PREFIXES)
    ]


def _public_markdown_paths(repo_root: Path) -> list[Path]:
    """Return tracked or publishable Markdown while excluding ignored private work."""
    candidates = sorted(repo_root.glob("*.md")) + sorted(
        (repo_root / "docs").rglob("*.md")
    )
    if not (repo_root / ".git").exists() or not candidates:
        return candidates
    relative = [path.relative_to(repo_root).as_posix() for path in candidates]
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo_root), "check-ignore", "-z", "--stdin"],
            input="\0".join(relative).encode() + b"\0",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return candidates
    if result.returncode not in {0, 1}:
        return candidates
    ignored = {os.fsdecode(value) for value in result.stdout.split(b"\0") if value}
    return [
        path
        for path, relative_path in zip(candidates, relative, strict=True)
        if relative_path not in ignored
    ]


def _required_settings(repo_root: Path) -> set[str]:
    settings = {"STOCK_DESK_WEB_DIST_DIR"}
    environment = repo_root / ".env.example"
    if not environment.is_file():
        return settings
    for line in _read(environment).splitlines():
        match = re.match(r"^(STOCK_DESK_[A-Z0-9_]+)=", line.strip())
        if match:
            settings.add(match.group(1))
    return settings


def _raster_failure(path: Path) -> str | None:
    expected_formats = {
        ".jpeg": "JPEG",
        ".jpg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }
    expected_format = expected_formats.get(path.suffix.casefold())
    if expected_format is None:
        return "unsupported raster type"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as candidate:
                if candidate.format != expected_format:
                    return "decoded format does not match the filename"
                candidate.verify()
            with Image.open(path) as decoded:
                decoded.load()
                width, height = decoded.size
                if width < MIN_SCREENSHOT_WIDTH or height < MIN_SCREENSHOT_HEIGHT:
                    return (
                        "screenshot dimensions are too small "
                        f"({width}x{height}; minimum "
                        f"{MIN_SCREENSHOT_WIDTH}x{MIN_SCREENSHOT_HEIGHT})"
                    )
                sample = decoded.convert("RGB").resize((64, 36))
                colors = sample.getcolors(maxcolors=(64 * 36) + 1)
                if colors is not None and len(colors) < 4:
                    return "screenshot content is visually trivial"
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        RuntimeError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        return f"image decode failed: {type(error).__name__}"
    return None


def _wiki_publishable_paths(
    root: Path, *, final: bool
) -> tuple[list[Path], list[Path], list[str]]:
    markdown: list[Path] = []
    images: list[Path] = []
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        relative_text = relative.as_posix()
        relative_casefolded = relative_text.casefold()
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked.casefold() in relative_casefolded:
                failures.append(
                    f"{relative_text}: forbidden public-boundary path: {blocked}"
                )
        if final and any(
            placeholder in relative_casefolded
            for placeholder in WIKI_PLACEHOLDER_PATTERNS
        ):
            failures.append(
                f"{relative_text}: placeholder path blocks final Wiki publication"
            )
        if path.is_symlink():
            failures.append(f"{relative_text}: symlink is not publishable")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        try:
            payload = path.read_bytes()
        except OSError:
            failures.append(f"{relative_text}: publication file is unreadable")
            continue
        payload_casefolded = payload.lower()
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked.casefold().encode("utf-8") in payload_casefolded:
                failures.append(
                    f"{relative_text}: forbidden public-boundary content: {blocked}"
                )
        if final:
            for placeholder in WIKI_PLACEHOLDER_PATTERNS:
                if placeholder.encode("utf-8") in payload_casefolded:
                    failures.append(
                        f"{relative_text}: placeholder content blocks final Wiki publication: {placeholder}"
                    )
            if (
                suffix not in PUBLISHABLE_SUFFIXES
                and relative_text != "SCREENSHOT-MANIFEST.yml"
            ):
                failures.append(
                    f"{relative_text}: unsupported Wiki publication file type"
                )
                continue
        if suffix == ".md":
            markdown.append(path)
        elif suffix in APPROVED_RASTER_SUFFIXES:
            images.append(path)
    return markdown, images, failures


def verify_repository(repo_root: Path) -> list[str]:
    """Return public-documentation contract failures without changing the tree."""

    root = repo_root.resolve()
    failures: list[str] = []
    documents: dict[str, str] = {}
    for relative_path in REQUIRED_PUBLIC_DOCUMENTS:
        path = root / relative_path
        if not path.is_file():
            failures.append(f"Missing required public document: {relative_path}")
            continue
        document = _read(path)
        documents[relative_path] = document
        headings = _headings(document)
        for required_heading in REQUIRED_SECTIONS[relative_path]:
            if required_heading not in headings:
                failures.append(
                    f"{relative_path}: missing required heading: {required_heading}"
                )
        if relative_path in {"README.md", "README.en.md"}:
            section_positions = [
                document.find(f"## {heading}")
                for heading in REQUIRED_SECTIONS[relative_path]
            ]
            if all(position >= 0 for position in section_positions) and (
                section_positions != sorted(section_positions)
            ):
                failures.append(f"{relative_path}: required sections are out of order")
            if len(document.splitlines()) > 100:
                failures.append(f"{relative_path}: must not exceed 100 lines")
        for snippet in REQUIRED_PUBLIC_SNIPPETS.get(relative_path, ()):
            if snippet not in document:
                failures.append(
                    f"{relative_path}: missing required guidance: {snippet}"
                )

    public_paths = _public_markdown_paths(root)
    for path in public_paths:
        relative_path = path.relative_to(root).as_posix()
        document = documents.get(relative_path, _read(path))
        failures.extend(
            _rendered_target_failures(root, relative_path, _rendered_targets(document))
        )
        failures.extend(_command_failures(root, relative_path, document))
        for blocked in FORBIDDEN_PUBLIC_REFERENCES:
            if blocked in document:
                failures.append(
                    f"{relative_path}: forbidden public-boundary reference: {blocked}"
                )

    chinese = documents.get("README.md", "")
    if not chinese.splitlines() or chinese.splitlines()[0] != "[English](README.en.md)":
        failures.append("README.md must start with a link to README.en.md")
    english = documents.get("README.en.md", "")
    if not english.splitlines() or english.splitlines()[0] != "[简体中文](README.md)":
        failures.append("README.en.md must start with a link to README.md")

    for relative_path, document in (
        ("README.md", chinese),
        ("README.en.md", english),
    ):
        positions = [
            document.find(pattern) for pattern in SOURCE_FREE_WINDOWS_INSTALLER_PATTERNS
        ]
        source_setup = document.find("make bootstrap")
        if any(position < 0 for position in positions):
            failures.append(
                f"{relative_path}: source-free Windows installer artifact name is missing"
            )
        elif source_setup >= 0 and max(positions) > source_setup:
            failures.append(
                f"{relative_path}: source-free installers must precede source setup"
            )

    failures.extend(_readme_screenshot_manifest_failures(root, documents))

    configuration = documents.get("docs/configuration.md", "")
    for setting in sorted(_required_settings(root)):
        if setting not in configuration:
            failures.append(f"docs/configuration.md: missing setting: {setting}")

    failures.extend(_tracked_boundary_failures(root))
    return sorted(set(failures))


def _readme_image_paths(
    root: Path, documents: dict[str, str]
) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    targets: dict[str, tuple[str, ...]] = {}
    failures: list[str] = []
    for readme_name in ("README.md", "README.en.md"):
        readme_path = root / readme_name
        image_paths: list[str] = []
        for rendered in _rendered_targets(documents.get(readme_name, "")):
            if rendered.kind != "image":
                continue
            destination = _local_destination(root, readme_path, rendered.target)
            if destination is None:
                continue
            try:
                relative_path = destination.relative_to(root).as_posix()
            except ValueError:
                failures.append(
                    f"{readme_name}: local image escapes repository: {rendered.target}"
                )
                continue
            image_paths.append(relative_path)
        targets[readme_name] = tuple(image_paths)
    return targets, failures


def _readme_local_image_context(document: str, target: str) -> str:
    image_pattern = re.compile(rf"!\[[^\]]*\]\({re.escape(target)}\)")
    lines = document.splitlines()
    for position, line in enumerate(lines):
        if image_pattern.search(line) is None:
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            for cell in stripped.strip("|").split("|"):
                if image_pattern.search(cell) is not None:
                    return cell.strip()
        context = [line]
        for following in lines[position + 1 :]:
            if following.strip():
                context.append(following)
                break
        return "\n".join(context)
    return ""


def _readme_truthful_caption_failures(documents: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for readme_name, contracts in README_SCREENSHOT_CAPTION_MARKERS.items():
        document = documents.get(readme_name, "")
        for image_path, markers in contracts.items():
            context = _readme_local_image_context(document, image_path)
            normalized_context = re.sub(r"\s+", " ", context).strip()
            expected_context = README_SCREENSHOT_LOCAL_CONTEXTS[readme_name][image_path]
            normalized_expected = re.sub(r"\s+", " ", expected_context).strip()
            if normalized_context != normalized_expected:
                failures.append(
                    f"{readme_name}: {image_path} violates its exact local caption "
                    "contract"
                )
            context_casefolded = context.casefold()
            missing = [
                marker
                for marker in markers
                if marker.casefold() not in context_casefolded
            ]
            if missing:
                failures.append(
                    f"{readme_name}: {image_path} local truthful caption is missing "
                    f"required markers: {missing!r}"
                )
            contradictory = [
                marker
                for marker in README_SCREENSHOT_CONTRADICTORY_MARKERS.get(
                    readme_name, {}
                ).get(image_path, ())
                if marker.casefold() in context_casefolded
            ]
            if contradictory:
                failures.append(
                    f"{readme_name}: {image_path} local truthful caption contains a "
                    f"contradictory claim: {contradictory!r}"
                )
    return failures


def _market_provenance_has_forbidden_marker(market_data: dict[object, object]) -> bool:
    serialized = " ".join(
        str(market_data.get(key, "")) for key in ("name", "source")
    ).casefold()
    return any(
        forbidden in serialized for forbidden in FORBIDDEN_MARKET_PROVENANCE_MARKERS
    )


def _normalized_manifest_utc_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return str(value)
    if candidate.tzinfo is None or candidate.utcoffset() != timezone.utc.utcoffset(
        candidate
    ):
        return str(value)
    return candidate.isoformat(timespec="seconds").replace("+00:00", "Z")


def _readme_market_data_identity(
    market_data: object,
) -> ReadmeMarketDataIdentity | tuple[()] | None:
    if market_data is None:
        return None
    if not isinstance(market_data, dict):
        return ()
    return ReadmeMarketDataIdentity(
        str(market_data.get("symbol", "")),
        str(market_data.get("name", "")),
        str(market_data.get("period", "")),
        str(market_data.get("adjustment", "")),
        str(market_data.get("start", "")),
        str(market_data.get("end", "")),
        str(market_data.get("source", "")),
        _normalized_manifest_utc_timestamp(market_data.get("data_cutoff")),
        str(market_data.get("dataset_version", "")),
    )


def _relative_symlink(root: Path, relative_path: Path) -> Path | None:
    """Return the first symlink in a repository-relative path, if any."""

    current = root
    for part in relative_path.parts:
        current /= part
        try:
            if current.is_symlink():
                return current
        except (OSError, ValueError, RuntimeError):
            return current
    return None


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def _readme_screenshot_manifest_failures(
    root: Path, documents: dict[str, str]
) -> list[str]:
    """Validate provenance for every local image rendered by both main READMEs."""

    manifest_path = root / README_SCREENSHOT_MANIFEST
    manifest_relative = Path(README_SCREENSHOT_MANIFEST)
    manifest_symlink = _relative_symlink(root, manifest_relative)
    if manifest_symlink is not None:
        return [
            "README screenshot manifest path must not contain a symlink: "
            f"{manifest_symlink.relative_to(root).as_posix()}"
        ]
    if not _safe_is_file(manifest_path):
        return [f"README screenshot manifest is missing: {README_SCREENSHOT_MANIFEST}"]
    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, ValueError, yaml.YAMLError) as error:
        return [f"README screenshot manifest is unreadable: {type(error).__name__}"]
    if not isinstance(loaded, dict):
        return ["README screenshot manifest root must be a mapping"]

    failures: list[str] = []
    if loaded.get("schema_version") != SCREENSHOT_MANIFEST_SCHEMA:
        failures.append(
            "README screenshot manifest has an unsupported schema_version: "
            f"{loaded.get('schema_version')!r}"
        )
    screenshots = loaded.get("screenshots")
    if not isinstance(screenshots, list):
        return [
            *failures,
            "README screenshot manifest screenshots must be a list",
        ]

    readme_targets, target_failures = _readme_image_paths(root, documents)
    failures.extend(target_failures)
    failures.extend(_readme_truthful_caption_failures(documents))
    for readme_name, targets in readme_targets.items():
        duplicates = sorted(
            target for target in set(targets) if targets.count(target) != 1
        )
        if duplicates:
            failures.append(
                f"{readme_name}: every local image must appear exactly once; "
                f"duplicates={duplicates!r}"
            )
    if set(readme_targets.get("README.md", ())) != set(
        readme_targets.get("README.en.md", ())
    ):
        failures.append(
            "README.md and README.en.md must reference the same local images exactly once"
        )

    images_relative = Path("docs/images")
    images_symlink = _relative_symlink(root, images_relative)
    if images_symlink is not None:
        failures.append(
            "README screenshot image path must not contain a symlink: "
            f"{images_symlink.relative_to(root).as_posix()}"
        )
    try:
        images_root = (root / images_relative).resolve()
    except (OSError, ValueError, RuntimeError) as error:
        failures.append(
            f"README screenshot image directory is unreadable: {type(error).__name__}"
        )
        images_root = root / images_relative
    manifest_paths: list[str] = []
    screenshot_ids: set[str] = set()
    states: list[str] = []
    canonical_routes = _canonical_app_routes(root)
    if not canonical_routes:
        failures.append("Unable to load canonical application routes")
    for position, raw_entry in enumerate(screenshots, start=1):
        label = f"README screenshot manifest entry {position}"
        if not isinstance(raw_entry, dict):
            failures.append(f"{label} must be a mapping")
            continue
        entry = {str(key): value for key, value in raw_entry.items()}
        screenshot_id = entry.get("screenshot_id")
        if not isinstance(screenshot_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", screenshot_id
        ):
            failures.append(f"{label} has an invalid screenshot_id")
        elif screenshot_id in screenshot_ids:
            failures.append(f"{label} duplicates screenshot_id: {screenshot_id}")
        else:
            screenshot_ids.add(screenshot_id)
            label = f"README screenshot manifest {screenshot_id}"

        relative_path = entry.get("path")
        image_path: Path | None = None
        validated_relative_path: str | None = None
        if isinstance(relative_path, str):
            manifest_paths.append(relative_path)
            if "\0" not in relative_path and ".." in Path(relative_path).parts:
                failures.append(f"{label} path escapes docs/images: {relative_path}")
            if (
                "\0" not in relative_path
                and re.fullmatch(
                    r"docs/images/[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:png|jpe?g|webp)",
                    relative_path,
                    re.IGNORECASE,
                )
                is not None
                and not Path(relative_path).is_absolute()
                and ".." not in Path(relative_path).parts
            ):
                validated_relative_path = relative_path
        if validated_relative_path is not None:
            relative_image_path = Path(validated_relative_path)
            image_symlink = _relative_symlink(root, relative_image_path)
            if image_symlink is not None:
                failures.append(
                    f"{label} image path must not contain a symlink: "
                    f"{image_symlink.relative_to(root).as_posix()}"
                )
            else:
                candidate = root / relative_image_path
                try:
                    image_path = candidate.resolve()
                    image_path.relative_to(images_root)
                except (OSError, ValueError, RuntimeError):
                    image_path = None
                    failures.append(
                        f"{label} path escapes docs/images: {relative_path}"
                    )
        if validated_relative_path is None:
            failures.append(f"{label} has an invalid docs/images path")

        state = entry.get("state")
        expected_binding = README_SCREENSHOT_BINDINGS.get(str(screenshot_id))
        actual_binding = ReadmeScreenshotBinding(
            str(relative_path),
            str(state),
            str(entry.get("route")),
            entry.get("contains_market_data") is True,
        )
        if expected_binding is None or actual_binding != expected_binding:
            failures.append(f"{label} violates its stable identity binding")
        expected_route_and_market = README_SCREENSHOT_STATES.get(str(state))
        if expected_route_and_market is None:
            failures.append(f"{label} has an unsupported truthful state: {state!r}")
        else:
            states.append(str(state))
            expected_route, expected_market = expected_route_and_market
            if entry.get("route") != expected_route:
                failures.append(
                    f"{label} route does not match state: {entry.get('route')!r}"
                )
            if entry.get("contains_market_data") is not expected_market:
                failures.append(
                    f"{label} contains_market_data does not match state {state}"
                )
        route = entry.get("route")
        if not isinstance(route, str) or route not in canonical_routes:
            failures.append(f"{label} route is not a canonical application route")

        viewport = entry.get("viewport")
        if not isinstance(viewport, dict) or any(
            type(viewport.get(key)) is not int or viewport[key] <= 0
            for key in ("width", "height", "device_scale_factor")
        ):
            failures.append(
                f"{label} viewport must use positive integers and be 1440x1000 at dsf 1"
            )
        elif viewport != {
            "width": 1440,
            "height": 1000,
            "device_scale_factor": 1,
        }:
            failures.append(f"{label} viewport must be 1440x1000 at dsf 1")

        product = entry.get("product")
        if not isinstance(product, dict):
            failures.append(f"{label} requires product provenance")
        else:
            version = product.get("version")
            commit = product.get("git_commit")
            version_match = (
                re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
                if isinstance(version, str)
                else None
            )
            if version_match is None or tuple(
                int(part) for part in version_match.groups()
            ) < (1, 0, 0):
                failures.append(f"{label} requires product version 1.0.0 or later")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                failures.append(f"{label} requires a 40-character git commit")
            elif not _repository_commit_is_reachable(root, commit):
                failures.append(
                    f"{label} git_commit is not a reachable repository commit"
                )

        if not _manifest_timestamp_is_utc(entry.get("captured_at")):
            failures.append(f"{label} requires an aware UTC captured_at")
        if entry.get("capture") not in {"in-app-browser", "playwright"}:
            failures.append(f"{label} has an unsupported capture method")
        if entry.get("editing") not in {"none", "crop-only"}:
            failures.append(f"{label} has unsupported editing metadata")
        if entry.get("redaction") != "passed":
            failures.append(f"{label} requires redaction: passed")
        if entry.get("disclaimer") != SCREENSHOT_DISCLAIMER:
            failures.append(f"{label} has an invalid disclaimer")

        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"{label} requires a lowercase SHA-256")
        elif image_path is None or not _safe_is_file(image_path):
            failures.append(f"{label} image does not exist: {relative_path}")
        else:
            try:
                actual_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            except (OSError, ValueError, RuntimeError) as error:
                failures.append(
                    f"{label} image is unreadable for SHA-256: {type(error).__name__}"
                )
            else:
                if actual_digest != digest:
                    failures.append(f"{label} SHA-256 does not match: {relative_path}")
        if image_path is not None and _safe_is_file(image_path):
            raster_failure = _raster_failure(image_path)
            if raster_failure is not None:
                failures.append(f"{label} {raster_failure}")
            else:
                try:
                    with Image.open(image_path) as image:
                        if image.size != (1440, 1000):
                            failures.append(f"{label} raster must be 1440x1000")
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    UnidentifiedImageError,
                ) as error:
                    failures.append(
                        f"{label} image metadata is unreadable: {type(error).__name__}"
                    )

        contains_market_data = entry.get("contains_market_data")
        market_data = entry.get("market_data")
        if type(contains_market_data) is not bool:
            failures.append(f"{label} requires boolean contains_market_data")
        if contains_market_data is False:
            if market_data is not None:
                failures.append(
                    f"{label} market_data must be null when contains_market_data is false"
                )
        elif contains_market_data is True:
            if not isinstance(market_data, dict):
                failures.append(f"{label} requires real market provenance")
            else:
                market_keys = frozenset(str(key) for key in market_data)
                if market_keys != README_MARKET_DATA_KEYS:
                    failures.append(
                        f"{label} market_data keys must match exactly: "
                        f"{sorted(README_MARKET_DATA_KEYS)!r}"
                    )
                if _market_provenance_has_forbidden_marker(market_data):
                    failures.append(f"{label} requires real market provenance")
                symbol = market_data.get("symbol")
                if not isinstance(symbol, str) or not re.fullmatch(
                    r"(?:[036]\d{5})\.(?:SH|SZ)", symbol
                ):
                    failures.append(f"{label} has an invalid canonical A-share symbol")
                name = market_data.get("name")
                if not isinstance(name, str) or not name.strip():
                    failures.append(f"{label} requires a market instrument name")
                if market_data.get("period") not in {"1d", "1w", "60m"}:
                    failures.append(f"{label} has an invalid market period")
                if market_data.get("adjustment") not in {"none", "qfq", "hfq"}:
                    failures.append(f"{label} has an invalid adjustment")
                source = market_data.get("source")
                if (
                    not isinstance(source, str)
                    or source not in _real_market_source_ids()
                ):
                    failures.append(f"{label} requires real market provenance")
                start = str(market_data.get("start", ""))
                end = str(market_data.get("end", ""))
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", end
                ):
                    failures.append(f"{label} requires market start and end dates")
                elif start > end:
                    failures.append(f"{label} market date range is reversed")
                if "cutoff" in market_data:
                    failures.append(f"{label} does not allow legacy market cutoff")
                if not _manifest_timestamp_is_utc(market_data.get("data_cutoff")):
                    failures.append(f"{label} requires an aware UTC data_cutoff")
                dataset_version = market_data.get("dataset_version")
                if not isinstance(dataset_version, str) or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", dataset_version
                ):
                    failures.append(f"{label} requires a dataset version")
                elif isinstance(digest, str) and dataset_version == f"sha256:{digest}":
                    failures.append(
                        f"{label} dataset_version must differ from screenshot SHA-256"
                    )
        expected_market_identity = README_SCREENSHOT_MARKET_IDENTITIES.get(
            str(screenshot_id)
        )
        if (
            str(screenshot_id) not in README_SCREENSHOT_MARKET_IDENTITIES
            or _readme_market_data_identity(market_data) != expected_market_identity
        ):
            failures.append(f"{label} violates its stable market-data identity")

    duplicate_manifest_paths = sorted(
        path for path in set(manifest_paths) if manifest_paths.count(path) != 1
    )
    expected_targets = set(readme_targets.get("README.md", ()))
    if duplicate_manifest_paths or set(manifest_paths) != expected_targets:
        failures.append(
            "README local images and manifest paths must match exactly once; "
            f"duplicates={duplicate_manifest_paths!r}, "
            f"missing={sorted(expected_targets - set(manifest_paths))!r}, "
            f"extra={sorted(set(manifest_paths) - expected_targets)!r}"
        )
    duplicate_states = sorted(
        state for state in set(states) if states.count(state) != 1
    )
    if duplicate_states or set(states) != set(README_SCREENSHOT_STATES):
        failures.append(
            "README screenshot truthful states must each appear exactly once; "
            f"duplicates={duplicate_states!r}"
        )
    return failures


def _manifest_timestamp_is_utc(value: object) -> bool:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    else:
        return False
    return (
        candidate.tzinfo is not None
        and candidate.utcoffset() == timezone.utc.utcoffset(candidate)
    )


def _manifest_market_page(page_pairs: object) -> bool:
    if not isinstance(page_pairs, list):
        return False
    return any(
        isinstance(page, str)
        and page.removesuffix("-en.md")
        .removesuffix(".md")
        .startswith(MARKET_SCREENSHOT_PAGE_PREFIXES)
        for page in page_pairs
    )


def _canonical_app_routes(repo_root: Path) -> frozenset[str]:
    routes_path = repo_root / "web/src/app/route-paths.json"
    try:
        loaded = json.loads(routes_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(loaded, dict) or not all(
        isinstance(key, str)
        and key
        and isinstance(value, str)
        and re.fullmatch(r"/[a-z][a-z0-9-]*", value)
        for key, value in loaded.items()
    ):
        return frozenset()
    routes = frozenset(loaded.values())
    return routes if len(routes) == len(loaded) else frozenset()


def _real_market_source_ids() -> frozenset[str]:
    return frozenset(provider.value for provider in BAR_SOURCE_PROVIDER_IDS)


def _repository_commit_is_reachable(repo_root: Path, commit: str) -> bool:
    audit_root = os.environ.get("STOCK_DESK_DOC_PROVENANCE_GIT_DIR")
    audit_tip = os.environ.get("STOCK_DESK_DOC_PROVENANCE_TIP")
    if bool(audit_root) != bool(audit_tip):
        return False
    try:
        root_key = os.fspath(
            Path(audit_root).resolve() if audit_root else repo_root.resolve()
        )
    except (OSError, ValueError, RuntimeError):
        return False
    return _repository_commit_is_reachable_cached(root_key, commit, audit_tip or "HEAD")


@lru_cache(maxsize=128)
def _repository_commit_is_reachable_cached(
    root_key: str, commit: str, audit_tip: str
) -> bool:
    try:
        completed = subprocess.run(
            (
                "git",
                "merge-base",
                "--is-ancestor",
                f"{commit}^{{commit}}",
                f"{audit_tip}^{{commit}}",
            ),
            cwd=root_key,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _surface_tuple(value: object) -> tuple[str, str] | None:
    if isinstance(value, str):
        surface_type, separator, locator = value.partition(":")
        if separator and surface_type and locator:
            return surface_type, locator
        return None
    if not isinstance(value, dict):
        return None
    mapped_type = value.get("type")
    mapped_locator = value.get("locator")
    if not isinstance(mapped_type, str) or not isinstance(mapped_locator, str):
        return None
    return mapped_type, mapped_locator


def _surface_failure(
    surface: tuple[str, str] | None,
    canonical_routes: frozenset[str],
) -> str | None:
    if surface is None:
        return "requires a typed evidence surface"
    surface_type, locator = surface
    if surface_type not in EVIDENCE_SURFACE_TYPES:
        return f"has an unsupported evidence surface type: {surface_type}"
    if surface_type == "app-route":
        if locator not in canonical_routes:
            return f"is not a canonical application route: {locator}"
    elif surface_type == "wiki-page":
        if locator not in REQUIRED_WIKI_PAGE_STEMS:
            return f"has an unknown Wiki page surface: {locator}"
    elif surface_type == "windows-installer":
        if not re.fullmatch(r"stock-desk-<version>-windows-x86_64\.exe", locator):
            return f"has an invalid Windows installer surface: {locator}"
    elif surface_type == "macos-installer":
        if not re.fullmatch(
            r"stock-desk-<version>-macos-(?:x86_64|arm64)\.dmg", locator
        ):
            return f"has an invalid macOS installer surface: {locator}"
    elif surface_type == "github-release":
        if locator != "latest":
            return f"has an invalid GitHub Release surface: {locator}"
    elif surface_type == "github-actions":
        if re.fullmatch(r"release-run-[1-9][0-9]*", locator) is None:
            return f"has an invalid GitHub Actions surface: {locator}"
    elif locator not in REPOSITORY_AUDIT_LOCATORS:
        return f"has an invalid repository audit surface: {locator}"
    return None


def _screenshot_manifest(
    root: Path,
    *,
    repository_root: Path,
    final: bool,
    publication_files: frozenset[Path],
    documents: dict[str, str],
    rendered_targets: dict[str, tuple[RenderedTarget, ...]],
    canonical_routes: frozenset[str],
) -> tuple[dict[str, dict[str, object]], dict[Path, dict[str, object]], list[str]]:
    path = root / "SCREENSHOT-MANIFEST.yml"
    if not path.is_file():
        return {}, {}, ["Screenshot manifest is missing: SCREENSHOT-MANIFEST.yml"]
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return {}, {}, [f"Screenshot manifest is unreadable: {type(error).__name__}"]
    if not isinstance(loaded, dict):
        return {}, {}, ["Screenshot manifest root must be a mapping"]
    failures: list[str] = []
    if loaded.get("schema_version") != SCREENSHOT_MANIFEST_SCHEMA:
        failures.append(
            "Screenshot manifest has an unsupported schema_version: "
            f"{loaded.get('schema_version')!r}"
        )
    screenshots = loaded.get("screenshots")
    if not isinstance(screenshots, list):
        return {}, {}, [*failures, "Screenshot manifest screenshots must be a list"]

    by_id: dict[str, dict[str, object]] = {}
    valid_captured_images: dict[Path, dict[str, object]] = {}
    paths: set[str] = set()
    captured_digests: dict[str, str] = {}
    images_root = (root / "images").resolve()
    for position, raw_entry in enumerate(screenshots, start=1):
        entry_failure_start = len(failures)
        label = f"Screenshot manifest entry {position}"
        if not isinstance(raw_entry, dict):
            failures.append(f"{label} must be a mapping")
            continue
        entry = {str(key): value for key, value in raw_entry.items()}
        screenshot_id = entry.get("screenshot_id")
        if not isinstance(screenshot_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", screenshot_id
        ):
            failures.append(f"{label} has an invalid screenshot_id")
            continue
        label = f"Screenshot manifest {screenshot_id}"
        if screenshot_id in by_id:
            failures.append(f"{label} duplicates screenshot_id")
            continue
        by_id[screenshot_id] = entry

        relative_path = entry.get("path")
        resolved_image: Path | None = None
        if isinstance(relative_path, str):
            candidate = root / relative_path
            resolved_image = candidate.resolve()
            if ".." in Path(relative_path).parts:
                failures.append(f"{label} path escapes Wiki images: {relative_path}")
            else:
                try:
                    resolved_image.relative_to(images_root)
                except ValueError:
                    failures.append(
                        f"{label} path escapes Wiki images: {relative_path}"
                    )
            if candidate.is_symlink():
                failures.append(f"{label} image path must not be a symlink")
        if not isinstance(relative_path, str) or not re.fullmatch(
            r"images/[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:png|jpe?g|webp)",
            relative_path,
            re.IGNORECASE,
        ):
            failures.append(f"{label} has an invalid Wiki-relative image path")
        elif relative_path in paths:
            failures.append(f"{label} duplicates image path: {relative_path}")
        else:
            paths.add(relative_path)

        page_pairs = entry.get("page_pairs")
        if (
            not isinstance(page_pairs, list)
            or len(page_pairs) != 2
            or not all(
                isinstance(page, str) and page.endswith(".md") for page in page_pairs
            )
        ):
            failures.append(f"{label} page_pairs must contain two Markdown pages")
        elif not (page_pairs[1] == page_pairs[0].removesuffix(".md") + "-en.md"):
            failures.append(f"{label} page_pairs must be a Chinese/English pair")

        captions = entry.get("caption_locales")
        if not isinstance(captions, dict) or not all(
            isinstance(captions.get(locale), str) and captions[locale].strip()
            for locale in ("zh-CN", "en")
        ):
            failures.append(f"{label} requires zh-CN and en caption_locales")
        features = entry.get("features")
        if not isinstance(features, list) or not all(
            isinstance(feature, str) and feature in ACTIVE_REQUIREMENT_IDS
            for feature in features
        ):
            failures.append(f"{label} has invalid features")
        surface = _surface_tuple(entry.get("surface"))
        surface_failure = _surface_failure(surface, canonical_routes)
        if surface_failure is not None:
            failures.append(f"{label} {surface_failure}")
        contains_market_data = entry.get("contains_market_data")
        if type(contains_market_data) is not bool:
            failures.append(f"{label} requires boolean contains_market_data")
        market_surface = (
            surface is not None
            and surface[0] == "app-route"
            and (surface[1] in {"/market", "/formulas", "/backtests"})
        )
        market_page = _manifest_market_page(page_pairs)
        if (market_surface or market_page) and contains_market_data is not True:
            failures.append(
                f"{label} contains_market_data must be true for this surface or page"
            )
        if contains_market_data is False and entry.get("market_data") is not None:
            failures.append(
                f"{label} market_data must be null when contains_market_data is false"
            )
        if entry.get("disclaimer") != SCREENSHOT_DISCLAIMER:
            failures.append(f"{label} has an invalid disclaimer")

        state = entry.get("state")
        if final or state == "captured":
            if isinstance(page_pairs, list):
                for page_name in page_pairs:
                    if not isinstance(page_name, str):
                        continue
                    page_path = root / page_name
                    if (
                        not page_path.is_file()
                        or page_path.resolve() not in publication_files
                    ):
                        failures.append(
                            f"{label} page_pairs page does not exist in the Wiki publication: "
                            f"{page_name}"
                        )
        if state == "pending":
            for field in (
                "viewport",
                "product",
                "captured_at",
                "sha256",
                "market_data",
                "capture",
                "editing",
            ):
                if entry.get(field) is not None:
                    failures.append(f"{label} pending entry must leave {field} null")
            if entry.get("redaction") != "pending":
                failures.append(f"{label} pending entry requires redaction: pending")
            if final:
                failures.append(f"{label} is pending and blocks final publication")
            continue
        if state != "captured":
            failures.append(f"{label} state must be pending or captured")
            continue

        if not isinstance(relative_path, str):
            continue
        image_path = root / relative_path
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"{label} requires a lowercase SHA-256")
        else:
            digest_owner = captured_digests.get(digest)
            if digest_owner is not None:
                failures.append(
                    f"{label} captured screenshot SHA-256 is reused by {digest_owner}"
                )
            else:
                captured_digests[digest] = screenshot_id
            if not image_path.is_file():
                failures.append(f"{label} image does not exist: {relative_path}")
            elif hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
                failures.append(f"{label} SHA-256 does not match: {relative_path}")
        if resolved_image not in publication_files:
            failures.append(
                f"{label} image is not a scanned Wiki publication file: {relative_path}"
            )
        elif image_path.is_file():
            raster_failure = _raster_failure(image_path)
            if raster_failure is not None:
                failures.append(f"{label} {raster_failure}")

        viewport = entry.get("viewport")
        if not isinstance(viewport, dict) or not all(
            isinstance(viewport.get(key), int) and viewport[key] > 0
            for key in ("width", "height", "device_scale_factor")
        ):
            failures.append(f"{label} requires a positive viewport")
        product = entry.get("product")
        if not isinstance(product, dict):
            failures.append(f"{label} requires product provenance")
        else:
            version = product.get("version")
            commit = product.get("git_commit")
            if not isinstance(version, str) or not re.fullmatch(
                r"(?:[1-9]\d*|0)\.(?:[0-9]+)\.(?:[0-9]+)", version
            ):
                failures.append(f"{label} has an invalid product version")
            elif int(version.split(".")[0]) < 1:
                failures.append(f"{label} requires product version 1.0.0 or later")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                failures.append(f"{label} requires a 40-character git commit")
            elif not _repository_commit_is_reachable(repository_root, commit):
                failures.append(
                    f"{label} git_commit is not a reachable repository commit"
                )
        if not _manifest_timestamp_is_utc(entry.get("captured_at")):
            failures.append(f"{label} requires an aware UTC captured_at")
        if entry.get("capture") not in {"playwright", "in-app-browser"}:
            failures.append(f"{label} has an unsupported capture method")
        if entry.get("editing") not in {"none", "crop-only"}:
            failures.append(f"{label} has unsupported editing metadata")
        if entry.get("redaction") != "passed":
            failures.append(f"{label} requires redaction: passed")

        market_data = entry.get("market_data")
        if contains_market_data is True:
            if not isinstance(market_data, dict):
                failures.append(f"{label} requires real market provenance")
            else:
                if _market_provenance_has_forbidden_marker(market_data):
                    failures.append(f"{label} requires real market provenance")
                if not re.fullmatch(
                    r"(?:[036]\d{5})\.(?:SH|SZ)", str(market_data.get("symbol", ""))
                ):
                    failures.append(f"{label} has an invalid A-share symbol")
                if market_data.get("period") not in {"1d", "1w", "60m"}:
                    failures.append(f"{label} has an invalid market period")
                if market_data.get("adjustment") not in {"none", "qfq", "hfq"}:
                    failures.append(f"{label} has an invalid adjustment")
                source = market_data.get("source")
                if not isinstance(source, str) or not source.strip():
                    failures.append(f"{label} requires a market source")
                elif source not in _real_market_source_ids():
                    failures.append(
                        f"{label} market source is not a product ProviderId: {source}"
                    )
                name = market_data.get("name")
                if not isinstance(name, str) or not name.strip():
                    failures.append(f"{label} requires a market instrument name")
                start = str(market_data.get("start", ""))
                end = str(market_data.get("end", ""))
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", end
                ):
                    failures.append(f"{label} requires market start and end dates")
                elif start > end:
                    failures.append(f"{label} market date range is reversed")
                if not _manifest_timestamp_is_utc(market_data.get("cutoff")):
                    failures.append(f"{label} requires an aware UTC market cutoff")
                if not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(market_data.get("dataset_version", "")),
                ):
                    failures.append(f"{label} requires a dataset version")
                elif market_data.get("dataset_version") == f"sha256:{digest}":
                    failures.append(
                        f"{label} dataset_version must be distinct from screenshot "
                        "SHA-256"
                    )
        if isinstance(page_pairs, list):
            for page_name in page_pairs:
                if not isinstance(page_name, str) or page_name not in documents:
                    continue
                page_path = root / page_name
                expected_image = image_path.resolve()
                referenced = any(
                    rendered.kind == "image"
                    and _local_destination(root, page_path, rendered.target)
                    == expected_image
                    for rendered in rendered_targets.get(page_name, ())
                )
                if not referenced:
                    failures.append(
                        f"{label} article {page_name} must reference {relative_path}"
                    )
        if (
            state == "captured"
            and resolved_image is not None
            and len(failures) == entry_failure_start
        ):
            valid_captured_images[resolved_image] = entry
    return by_id, valid_captured_images, failures


def _github_heading_anchor(heading: str) -> str:
    anchor = heading.casefold().strip()
    anchor = re.sub(r"[^\w\s-]", "", anchor, flags=re.UNICODE)
    anchor = re.sub(r"\s+", "-", anchor)
    return re.sub(r"-+", "-", anchor).strip("-")


def _feature_requirement_ids(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", value).replace("\u2013", "-").replace("\u2014", "-")
    match = re.fullmatch(r"R-(\d{3})(?:-R?-?(\d{3}))?", normalized)
    if match is None:
        return ()
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start:
        return ()
    return tuple(f"R-{number:03d}" for number in range(start, end + 1))


def _feature_index_rows(
    document: str,
) -> tuple[list[tuple[tuple[str, ...], str, str, str, str, str]], list[str]]:
    rows: list[tuple[tuple[str, ...], str, str, str, str, str]] = []
    failures: list[str] = []
    lines = document.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|")
            and ("Screenshot ID" in line or "\u622a\u56fe ID" in line)
            and ("Feature/requirement" in line or "\u529f\u80fd/\u9700\u6c42" in line)
        ),
        None,
    )
    if header_index is None or header_index + 1 >= len(lines):
        return [], ["missing feature-index table header"]
    separator = [cell.strip() for cell in lines[header_index + 1].strip("|").split("|")]
    if len(separator) != 6 or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        return [], ["invalid feature-index table separator"]
    table_closed = False
    for line_number, line in enumerate(
        lines[header_index + 2 :], start=header_index + 3
    ):
        if not line.startswith("|"):
            table_closed = True
            if re.search(r"\bR-\d{3}\b", line):
                failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        if table_closed:
            failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        match = _FEATURE_INDEX_ROW.fullmatch(line)
        if match is None:
            failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        identifiers = _feature_requirement_ids(match.group(1))
        rows.append(
            (
                identifiers,
                match.group(2).strip(),
                match.group(3).strip(),
                match.group(4).strip(),
                match.group(5).strip(),
                match.group(6).strip(),
            )
        )
    return rows, failures


def _feature_index_failures(
    root: Path,
    documents: dict[str, str],
    screenshot_entries: dict[str, dict[str, object]],
    canonical_routes: frozenset[str],
) -> list[str]:
    failures: list[str] = []
    parsed: dict[str, list[tuple[tuple[str, ...], str, str, str, str, str]]] = {}
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        rows, row_failures = _feature_index_rows(documents.get(filename, ""))
        parsed[filename] = rows
        failures.extend(
            f"Feature index {filename}: {failure}" for failure in row_failures
        )
        if not rows:
            failures.append(f"Feature index {filename}: no machine-readable rows")
            continue
        seen: list[str] = [identifier for row in rows for identifier in row[0]]
        for identifier in sorted(ACTIVE_REQUIREMENT_IDS - set(seen)):
            failures.append(
                f"Feature index {filename}: missing requirement ID: {identifier}"
            )
        for identifier in sorted(set(seen) - ACTIVE_REQUIREMENT_IDS):
            failures.append(
                f"Feature index {filename}: unknown requirement ID: {identifier}"
            )
        for identifier in sorted({item for item in seen if seen.count(item) > 1}):
            failures.append(
                f"Feature index {filename}: duplicate requirement ID: {identifier}"
            )
        for (
            identifiers,
            chinese_target,
            english_target,
            section_text,
            screenshot_id,
            surface_text,
        ) in rows:
            row_label = identifiers[0] if identifiers else "invalid row"
            chinese_section, separator, english_section = section_text.partition(" / ")
            if not separator or not chinese_section or not english_section:
                failures.append(
                    f"Feature index {filename} {row_label}: section must be bilingual: "
                    f"{section_text}"
                )
            else:
                for target, section in (
                    (chinese_target, chinese_section),
                    (english_target, english_section),
                ):
                    target_anchor = unquote(urlsplit(target).fragment).casefold()
                    expected_anchor = _github_heading_anchor(section)
                    if target_anchor != expected_anchor:
                        failures.append(
                            f"Feature index {filename} {row_label}: section {section} "
                            f"does not match target anchor: {target}"
                        )
            surface = _surface_tuple(surface_text)
            surface_failure = _surface_failure(surface, canonical_routes)
            if surface_failure is not None:
                failures.append(
                    f"Feature index {filename} {row_label}: {surface_failure}"
                )
            if screenshot_id not in screenshot_entries:
                failures.append(
                    f"Feature index {filename} {row_label}: missing screenshot reference: "
                    f"{screenshot_id}"
                )
            else:
                screenshot_entry = screenshot_entries[screenshot_id]
                manifest_features = screenshot_entry.get("features")
                if not isinstance(manifest_features, list) or not set(
                    identifiers
                ).issubset(manifest_features):
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} does not cover mapped requirement"
                    )
                manifest_surface = _surface_tuple(screenshot_entry.get("surface"))
                if manifest_surface != surface:
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} surface does not match manifest: "
                        f"{surface} != {manifest_surface}"
                    )
                page_pairs = screenshot_entry.get("page_pairs")
                expected_page_pairs = [
                    (
                        unquote(urlsplit(target).path)
                        if unquote(urlsplit(target).path).endswith(".md")
                        else f"{unquote(urlsplit(target).path)}.md"
                    )
                    for target in (chinese_target, english_target)
                ]
                if page_pairs != expected_page_pairs:
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} page_pairs do not match feature targets"
                    )
            for target in (chinese_target, english_target):
                split = urlsplit(target)
                target_path = unquote(split.path)
                page_name = (
                    target_path if target_path.endswith(".md") else f"{target_path}.md"
                )
                page = root / page_name
                if not page.is_file():
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced page does not exist: "
                        f"{page_name}"
                    )
                    continue
                if not split.fragment:
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced page lacks a section anchor: "
                        f"{target}"
                    )
                    continue
                anchors = {
                    _github_heading_anchor(heading)
                    for heading in _headings(documents.get(page_name, _read(page)))
                }
                if unquote(split.fragment).casefold() not in anchors:
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced section does not exist: "
                        f"{target}"
                    )

    chinese_rows = parsed.get("Feature-Index.md", [])
    english_rows = parsed.get("Feature-Index-en.md", [])
    if chinese_rows and english_rows and chinese_rows != english_rows:
        failures.append(
            "Feature index language pages must contain the same requirement mappings"
        )
    rows_by_requirement = {
        identifier: row[1:] for row in chinese_rows for identifier in row[0]
    }
    for requirement_id, expected_binding in REQUIRED_WIKI_FEATURE_BINDINGS.items():
        if rows_by_requirement.get(requirement_id) != expected_binding:
            failures.append(
                f"Feature index {requirement_id}: semantic binding must be "
                f"{expected_binding!r}"
            )
    indexed_features: dict[str, set[str]] = {}
    for (
        identifiers,
        _chinese,
        _english,
        _section,
        screenshot_id,
        _surface,
    ) in chinese_rows:
        indexed_features.setdefault(screenshot_id, set()).update(identifiers)
    for screenshot_id, entry in screenshot_entries.items():
        manifest_features = entry.get("features")
        if isinstance(manifest_features, list) and set(manifest_features) != (
            indexed_features.get(screenshot_id, set())
        ):
            failures.append(
                f"Screenshot manifest {screenshot_id} features do not exactly match "
                "Feature index mappings"
            )
    referenced_ids = {
        row[4]
        for rows in parsed.values()
        for row in rows
        if row[4] in screenshot_entries
    }
    for screenshot_id in sorted(set(screenshot_entries) - referenced_ids):
        features = screenshot_entries[screenshot_id].get("features")
        if isinstance(features, list) and not features:
            continue
        failures.append(
            f"Feature index has an unreferenced screenshot manifest entry: {screenshot_id}"
        )
    return failures


def verify_wiki(
    wiki_root: Path, *, final: bool, repo_root: Path | None = None
) -> list[str]:
    """Verify bilingual external Wiki staging or its final publication boundary."""

    if wiki_root.is_symlink():
        return ["Wiki root must not be a symlink"]
    root = wiki_root.absolute()
    if not root.is_dir():
        return [f"Wiki root is not a directory: {root}"]
    failures: list[str] = []
    markdown_paths, image_paths, path_failures = _wiki_publishable_paths(
        root, final=final
    )
    failures.extend(path_failures)
    publication_files = frozenset(
        path.resolve()
        for path in (
            *markdown_paths,
            *image_paths,
            *(
                (root / "SCREENSHOT-MANIFEST.yml",)
                if (root / "SCREENSHOT-MANIFEST.yml").is_file()
                else ()
            ),
        )
    )
    images_root = (root / "images").resolve()
    documents: dict[str, str] = {}
    rendered_targets: dict[str, tuple[RenderedTarget, ...]] = {}
    for path in markdown_paths:
        relative_path = path.relative_to(root).as_posix()
        try:
            document = _read(path)
        except (OSError, UnicodeError):
            failures.append(f"{relative_path}: Markdown is unreadable")
            continue
        documents[relative_path] = document
        if final and path.name.endswith(".zh-CN.md"):
            failures.append(
                f"{relative_path}: legacy .zh-CN Wiki alias is not publishable"
            )
        if final and relative_path in REPLACED_WIKI_PAGE_FILENAMES:
            failures.append(
                f"{relative_path}: replaced Wiki page name is not publishable"
            )
        targets = _rendered_targets(document)
        rendered_targets[relative_path] = targets
        failures.extend(
            _rendered_target_failures(
                root,
                relative_path,
                targets,
                allowed_files=publication_files,
                allow_extensionless_markdown=True,
            )
        )
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked in document:
                failures.append(
                    f"{relative_path}: forbidden public-boundary reference: {blocked}"
                )
        legacy_typed = sorted(set(re.findall(r"`((?:code|path):[^`]+)`", document)))
        if legacy_typed:
            failures.append(
                f"{relative_path}: legacy typed prefix is not publishable: "
                f"{legacy_typed!r}"
            )
        if final:
            casefolded = document.casefold()
            for placeholder in WIKI_PLACEHOLDER_PATTERNS:
                if placeholder in casefolded:
                    failures.append(
                        f"{relative_path}: placeholder blocks final Wiki publication: {placeholder}"
                    )

    for path in image_paths:
        relative_path = path.relative_to(root).as_posix()
        if final:
            image_failure = _raster_failure(path)
            if image_failure is not None:
                failures.append(f"{relative_path}: {image_failure}")

    repository_root = (
        repo_root if repo_root is not None else Path(__file__).resolve().parent.parent
    )
    canonical_routes = _canonical_app_routes(repository_root)
    if not canonical_routes:
        failures.append("Unable to load canonical application routes")
    screenshot_entries, valid_captured_images, manifest_failures = _screenshot_manifest(
        root,
        repository_root=repository_root,
        final=final,
        publication_files=publication_files,
        documents=documents,
        rendered_targets=rendered_targets,
        canonical_routes=canonical_routes,
    )
    failures.extend(manifest_failures)
    failures.extend(
        _feature_index_failures(root, documents, screenshot_entries, canonical_routes)
    )
    evidence_owners: dict[str, set[str]] = {}
    for page_name, document in documents.items():
        for screenshot_id in _wiki_screenshot_evidence_ids(document):
            evidence_owners.setdefault(screenshot_id, set()).add(page_name)
    for screenshot_id, supplemental_entry in screenshot_entries.items():
        features = supplemental_entry.get("features")
        page_pairs = supplemental_entry.get("page_pairs")
        if not isinstance(features, list) or features:
            continue
        expected_owners = (
            set(page_pairs)
            if isinstance(page_pairs, list)
            and all(isinstance(page, str) for page in page_pairs)
            else set()
        )
        actual_owners = evidence_owners.get(screenshot_id, set())
        if actual_owners != expected_owners:
            missing = sorted(expected_owners - actual_owners)
            unexpected = sorted(actual_owners - expected_owners)
            failures.append(
                f"Screenshot manifest {screenshot_id} with features: [] must be "
                "declared by both manifest page_pairs and no other page; "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
    if final:
        for image_path in image_paths:
            relative_path = image_path.relative_to(root).as_posix()
            if not relative_path.startswith("images/"):
                failures.append(
                    f"{relative_path}: publication raster is outside Wiki images/"
                )
            if image_path.resolve() not in valid_captured_images:
                failures.append(
                    f"{relative_path}: must have exactly one valid captured manifest entry"
                )
        for relative_path, targets in rendered_targets.items():
            source = root / relative_path
            for rendered in targets:
                if rendered.kind != "image":
                    continue
                destination = _local_destination(root, source, rendered.target)
                if (
                    destination is not None
                    and destination.suffix.casefold() in APPROVED_RASTER_SUFFIXES
                ):
                    manifest_entry = valid_captured_images.get(destination)
                    if manifest_entry is None:
                        failures.append(
                            f"{relative_path}: local raster {rendered.target} is not "
                            "backed by a valid captured manifest entry"
                        )
                    else:
                        page_pairs = manifest_entry.get("page_pairs")
                        if (
                            not isinstance(page_pairs, list)
                            or relative_path not in page_pairs
                        ):
                            failures.append(
                                f"{relative_path}: local raster {rendered.target} is "
                                "not listed in manifest page_pairs"
                            )

    checklist = documents.get("PUBLISHING-CHECKLIST.md")
    if final and checklist is not None:
        if "Status: final" not in checklist or re.search(
            r"^- \[ \]", checklist, re.MULTILINE
        ):
            failures.append(
                "PUBLISHING-CHECKLIST.md must be deleted or finalized before publication"
            )

    for filename in REQUIRED_WIKI_ENTRY_FILES:
        path = root / filename
        if not path.is_file():
            failures.append(f"Missing required Wiki entry file: {filename}")

    for filename, markers in REQUIRED_WIKI_DOCUMENTATION_ENTRY_MARKERS.items():
        document = documents.get(filename, "")
        missing = [marker for marker in markers if marker not in document]
        if missing:
            failures.append(
                f"{filename}: R-073 documentation entry proof requires: {missing!r}"
            )

    for filename, (required, forbidden) in REQUIRED_WIKI_WORKFLOW_CONTENT.items():
        document = documents.get(filename, "")
        missing = [marker for marker in required if marker not in document]
        present_forbidden = [marker for marker in forbidden if marker in document]
        if missing or present_forbidden:
            failures.append(
                f"{filename}: workflow content contract mismatch; "
                f"missing={missing!r}, forbidden={present_forbidden!r}"
            )

    for (
        filename,
        (required, forbidden),
    ) in REQUIRED_WIKI_ANALYSIS_PLATFORM_CONTENT.items():
        document = documents.get(filename, "")
        missing = [marker for marker in required if marker not in document]
        present_forbidden = [marker for marker in forbidden if marker in document]
        if missing or present_forbidden:
            failures.append(
                f"{filename}: analysis/platform content contract mismatch; "
                f"missing={missing!r}, forbidden={present_forbidden!r}"
            )

    for filename, (
        section_headings,
        forbidden,
    ) in REQUIRED_WIKI_LOW_CODE_SECTION_FORBIDDEN.items():
        document = documents.get(filename, "")
        exposed = {
            heading: [
                marker
                for marker in forbidden
                if marker in _level_two_section(document, heading)
            ]
            for heading in section_headings
        }
        exposed = {heading: markers for heading, markers in exposed.items() if markers}
        if exposed:
            failures.append(
                f"{filename}: low-code section exposes advanced API fields: {exposed!r}"
            )

    for filename, sections in REQUIRED_WIKI_LOW_CODE_SECTION_REQUIRED.items():
        document = documents.get(filename, "")
        missing_by_heading = {
            heading: [
                marker
                for marker in required
                if marker not in _level_two_section(document, heading)
            ]
            for heading, required in sections.items()
        }
        missing_by_heading = {
            heading: markers
            for heading, markers in missing_by_heading.items()
            if markers
        }
        if missing_by_heading:
            failures.append(
                f"{filename}: low-code section is missing required UI guidance: "
                f"{missing_by_heading!r}"
            )

    for filename, claims in REQUIRED_WIKI_MARKET_GUIDE_SOURCE_CLAIMS.items():
        document = documents.get(filename, "")
        missing_claims = [
            wiki_marker
            for wiki_marker, _relative_path, _source_marker in claims
            if wiki_marker not in document
        ]
        invalid_sources = [
            f"{relative_path}:{source_marker}"
            for _wiki_marker, relative_path, source_marker in claims
            if source_marker not in _tracked_source_text(relative_path)
        ]
        if missing_claims or invalid_sources:
            failures.append(
                f"{filename}: source-backed market-guide contract mismatch; "
                f"missing={missing_claims!r}, invalid_sources={invalid_sources!r}"
            )

    for filename, claims in REQUIRED_WIKI_FORMULA_GUIDE_SOURCE_CLAIMS.items():
        document = documents.get(filename, "")
        missing_claims = [
            wiki_marker
            for wiki_marker, _relative_path, _source_marker in claims
            if wiki_marker not in document
        ]
        invalid_sources = [
            f"{relative_path}:{source_marker}"
            for _wiki_marker, relative_path, source_marker in claims
            if source_marker not in _tracked_source_text(relative_path)
        ]
        if missing_claims or invalid_sources:
            failures.append(
                f"{filename}: source-backed formula-guide contract mismatch; "
                f"missing={missing_claims!r}, invalid_sources={invalid_sources!r}"
            )

    for filename, claims in REQUIRED_WIKI_BACKTEST_GUIDE_SOURCE_CLAIMS.items():
        document = documents.get(filename, "")
        missing_claims = [
            wiki_marker
            for wiki_marker, _relative_path, _source_marker in claims
            if wiki_marker not in document
        ]
        invalid_sources = [
            f"{relative_path}:{source_marker}"
            for _wiki_marker, relative_path, source_marker in claims
            if source_marker not in _tracked_source_text(relative_path)
        ]
        if missing_claims or invalid_sources:
            failures.append(
                f"{filename}: source-backed backtest-guide contract mismatch; "
                f"missing={missing_claims!r}, invalid_sources={invalid_sources!r}"
            )

    for (
        filename,
        claims,
    ) in REQUIRED_WIKI_ANALYSIS_PLATFORM_GUIDE_SOURCE_CLAIMS.items():
        document = documents.get(filename, "")
        missing_claims = [
            wiki_marker
            for wiki_marker, _relative_path, _source_marker in claims
            if wiki_marker not in document
        ]
        invalid_sources = [
            f"{relative_path}:{source_marker}"
            for _wiki_marker, relative_path, source_marker in claims
            if source_marker not in _tracked_source_text(relative_path)
        ]
        if missing_claims or invalid_sources:
            failures.append(
                f"{filename}: source-backed analysis/platform-guide contract "
                f"mismatch; missing={missing_claims!r}, "
                f"invalid_sources={invalid_sources!r}"
            )

    failures.extend(_analysis_platform_source_invariant_failures())

    for filename, required_link in (
        ("_Sidebar.md", "[English](Home-en)"),
        ("_Sidebar-en.md", "[简体中文](Home)"),
    ):
        sidebar = documents.get(filename, "")
        if sidebar and required_link not in sidebar:
            failures.append(f"{filename}: missing language entry link: {required_link}")

    if final:
        sidebar_targets: dict[str, set[str]] = {}
        for filename in ("_Sidebar.md", "_Sidebar-en.md"):
            sidebar_targets[filename] = {
                unquote(urlsplit(rendered.target).path)
                for rendered in rendered_targets.get(filename, ())
                if rendered.kind == "link"
                and not urlsplit(rendered.target).scheme
                and not urlsplit(rendered.target).netloc
            }
        chinese_targets = sidebar_targets["_Sidebar.md"]
        english_targets = sidebar_targets["_Sidebar-en.md"]
        for stem in REQUIRED_WIKI_PAGE_STEMS:
            if stem not in chinese_targets:
                failures.append(
                    f"_Sidebar.md: missing authoritative Chinese target: {stem}"
                )
            english_target = f"{stem}-en"
            if english_target not in english_targets:
                failures.append(
                    "_Sidebar-en.md: missing authoritative English target: "
                    f"{english_target}"
                )
        for wrong_target in sorted(
            {f"{stem}-en" for stem in REQUIRED_WIKI_PAGE_STEMS if stem != "Home"}
            & chinese_targets
        ):
            failures.append(
                f"_Sidebar.md: cross-language navigation target: {wrong_target}"
            )
        for wrong_target in sorted(
            (set(REQUIRED_WIKI_PAGE_STEMS) - {"Home"}) & english_targets
        ):
            failures.append(
                f"_Sidebar-en.md: cross-language navigation target: {wrong_target}"
            )

    for stem in REQUIRED_WIKI_PAGE_STEMS:
        chinese_path = root / f"{stem}.md"
        english_path = root / f"{stem}-en.md"
        for path in (chinese_path, english_path):
            if not path.is_file():
                failures.append(f"Missing required Wiki page: {path.name}")
        if not english_path.is_file() or not chinese_path.is_file():
            continue
        english = documents.get(english_path.name, "")
        chinese = documents.get(chinese_path.name, "")
        if f"[简体中文]({stem})" not in english:
            failures.append(f"{english_path.name}: missing counterpart link to {stem}")
        if f"[English]({stem}-en)" not in chinese:
            failures.append(
                f"{chinese_path.name}: missing counterpart link to {stem}-en"
            )
        if stem in {"Home", "Feature-Index"}:
            continue
        for path, document, required_headings, required_navigation in (
            (
                english_path,
                english,
                (
                    "When to use this",
                    "Before you start",
                    "Chinese UI labels",
                    "Steps",
                    "Expected result",
                    "Screenshot",
                    "Common problems",
                    "Recovery",
                ),
                (
                    "[Feature index](Feature-Index-en)",
                    "[Home](Home-en)",
                    "[Previous](",
                    "[Next](",
                ),
            ),
            (
                chinese_path,
                chinese,
                (
                    "适用场景",
                    "使用前",
                    "操作步骤",
                    "预期结果",
                    "截图",
                    "常见问题",
                    "恢复方法",
                ),
                (
                    "[功能索引](Feature-Index)",
                    "[首页](Home)",
                    "[上一页](",
                    "[下一页](",
                ),
            ),
        ):
            heading_sequence = _heading_sequence(document)
            article_headings = set(heading_sequence)
            for heading in required_headings:
                if heading not in article_headings:
                    failures.append(f"{path.name}: missing required heading: {heading}")
            if all(heading in article_headings for heading in required_headings):
                positions = tuple(
                    heading_sequence.index(heading) for heading in required_headings
                )
                if positions != tuple(sorted(positions)):
                    failures.append(
                        f"{path.name}: required shared-template headings are out of order"
                    )
            for navigation in required_navigation:
                if navigation not in document:
                    failures.append(
                        f"{path.name}: missing required navigation: "
                        f"{navigation.removesuffix('(')}"
                    )
            if not re.search(r"^1\.\s+\S", document, re.MULTILINE):
                failures.append(f"{path.name}: missing ordered workflow steps")
            marker_present = "screenshot_placeholder" in document.casefold()
            if final and marker_present:
                failures.append(
                    f"{path.name}: SCREENSHOT_PLACEHOLDER blocks final Wiki publication"
                )
            if final:
                has_real_screenshot = False
                for rendered in rendered_targets.get(path.name, ()):
                    if rendered.kind != "image":
                        continue
                    destination = _local_destination(root, path, rendered.target)
                    if destination is None:
                        continue
                    try:
                        destination.relative_to(images_root)
                    except ValueError:
                        continue
                    if destination not in publication_files:
                        continue
                    manifest_entry = valid_captured_images.get(destination)
                    page_pairs = (
                        manifest_entry.get("page_pairs")
                        if manifest_entry is not None
                        else None
                    )
                    if isinstance(page_pairs, list) and path.name in page_pairs:
                        has_real_screenshot = True
                        break
                if not has_real_screenshot:
                    failures.append(
                        f"{path.name}: final page is missing a real screenshot backed by "
                        "captured manifest evidence"
                    )

        chinese_evidence = _wiki_screenshot_evidence_ids(chinese)
        english_evidence = _wiki_screenshot_evidence_ids(english)
        if not chinese_evidence:
            failures.append(
                f"{chinese_path.name}: missing ordered screenshot evidence IDs"
            )
        if not english_evidence:
            failures.append(
                f"{english_path.name}: missing ordered screenshot evidence IDs"
            )
        if chinese_evidence != english_evidence:
            failures.append(
                f"{stem}: Chinese/English screenshot evidence order differs"
            )
        for path, evidence_ids in (
            (chinese_path, chinese_evidence),
            (english_path, english_evidence),
        ):
            for screenshot_id in evidence_ids:
                entry = screenshot_entries.get(screenshot_id)
                if entry is None:
                    failures.append(
                        f"{stem}: screenshot evidence ID is absent from the manifest: "
                        f"{screenshot_id}"
                    )
                    continue
                page_pairs = entry.get("page_pairs")
                if not isinstance(page_pairs, list) or path.name not in page_pairs:
                    failures.append(
                        f"{path.name}: screenshot evidence ID {screenshot_id} manifest "
                        f"page_pairs does not include {path.name}"
                    )

        app_ui_labels = REQUIRED_WIKI_APP_UI_LABELS.get(stem)
        external_ui_labels = REQUIRED_WIKI_EXTERNAL_UI_LABELS.get(stem)
        if (app_ui_labels is None) == (external_ui_labels is None):
            failures.append(
                f"{english_path.name}: requires exactly one typed UI-label contract"
            )
        else:
            expected_labels = (
                app_ui_labels
                if app_ui_labels is not None
                else tuple(
                    (english_label, chinese_label)
                    for _kind, english_label, chinese_label in external_ui_labels or ()
                )
            )
            actual_labels, sequential = _wiki_ui_label_mappings(english)
            if actual_labels != expected_labels or not sequential:
                failures.append(
                    f"{english_path.name}: Chinese UI labels must be the numbered "
                    f"controlled mappings: {expected_labels!r}"
                )
            if app_ui_labels is not None:
                for _english_label, chinese_label in actual_labels:
                    if not _app_ui_label_in_page_source(stem, chinese_label):
                        failures.append(
                            f"{english_path.name}: application UI label is absent from "
                            f"page-specific production source: {chinese_label}"
                        )
            else:
                for kind, english_label, chinese_label in external_ui_labels or ():
                    if (english_label, chinese_label) not in (
                        WIKI_EXTERNAL_UI_LABEL_ALLOWLIST.get(kind, frozenset())
                    ):
                        failures.append(
                            f"{english_path.name}: external UI label is not in the "
                            f"typed {kind} allowlist: {english_label}（{chinese_label}）"
                        )
            mapped_tokens = tuple(
                f"{english_label}（{chinese_label}）"
                for english_label, chinese_label in actual_labels
            )
            step_references = _wiki_steps_ui_references(english)
            for reference in step_references:
                if reference not in mapped_tokens:
                    failures.append(
                        f"{english_path.name}: Steps UI reference is missing from UI "
                        f"label map: {reference}"
                    )
            for mapped_token in mapped_tokens:
                if mapped_token not in step_references:
                    failures.append(
                        f"{english_path.name}: UI label map item is unused in Steps: "
                        f"{mapped_token}"
                    )
            visible_english = _markdown_visible_text(english)
            for english_label, chinese_label in expected_labels:
                bilingual_label = f"{english_label}（{chinese_label}）"
                first_label = re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(english_label)}(?![A-Za-z0-9])",
                    visible_english,
                )
                bilingual_position = visible_english.find(bilingual_label)
                if (
                    first_label is None
                    or bilingual_position < 0
                    or first_label.start() != bilingual_position
                ):
                    failures.append(
                        f"{english_path.name}: first occurrence of the controlled UI "
                        f"label must be bilingual: {bilingual_label}"
                    )

        chinese_paths = _wiki_navigation_paths(
            rendered_targets.get(chinese_path.name, ())
        )
        english_paths = _wiki_navigation_paths(
            rendered_targets.get(english_path.name, ())
        )
        for target in chinese_paths:
            if target.endswith("-en") and target != f"{stem}-en":
                failures.append(
                    f"{chinese_path.name}: cross-language navigation target: {target}"
                )
        for target in english_paths:
            if (
                target in REQUIRED_WIKI_PAGE_STEMS
                and target != stem
                and not target.endswith("-en")
            ):
                failures.append(
                    f"{english_path.name}: cross-language navigation target: {target}"
                )

        chinese_navigation = _normalized_wiki_navigation(
            rendered_targets.get(chinese_path.name, ())
        )
        english_navigation = _normalized_wiki_navigation(
            rendered_targets.get(english_path.name, ())
        )
        if chinese_navigation != english_navigation:
            failures.append(f"{stem}: Chinese/English normalized navigation differs")

    for relative_path, document in documents.items():
        if (
            "uv run python scripts/backup.py" in document
            or "uv run python scripts/restore.py" in document
        ):
            required_scope = (
                "仅适用于源码或容器 POSIX"
                if not relative_path.endswith("-en.md")
                else "source/container POSIX only"
            )
            if required_scope not in document:
                failures.append(
                    f"{relative_path}: backup commands require {required_scope} scope"
                )
    return sorted(set(failures))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Stock Desk public documentation"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="application repository root",
    )
    parser.add_argument(
        "--wiki-root",
        type=Path,
        help="optional external bilingual Wiki root",
    )
    parser.add_argument(
        "--final-wiki",
        action="store_true",
        help="reject placeholders and require real Wiki screenshots",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.final_wiki and arguments.wiki_root is None:
        parser.error("--final-wiki requires --wiki-root")
    failures = verify_repository(arguments.repo_root)
    if arguments.wiki_root is not None:
        failures.extend(
            verify_wiki(
                arguments.wiki_root,
                final=arguments.final_wiki,
                repo_root=arguments.repo_root,
            )
        )
    if failures:
        print("Documentation verification failed:", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"- {failure}", file=sys.stderr)
        return 1
    mode = "final" if arguments.final_wiki else "staging"
    suffix = f" and {mode} Wiki" if arguments.wiki_root is not None else ""
    print(f"Public documentation{suffix} verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
