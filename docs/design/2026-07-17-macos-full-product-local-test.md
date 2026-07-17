# macOS 本地完整产品测试设计

## 目标

在开发 Mac 上临时构建并运行真实 Stock Desk Tauri `.app` 与原生 Python
sidecar，使用隔离数据完成首次向导、真实行情、公式和回测的完整产品旅程，
让大部分产品问题在提交 Windows CI 前获得快速反馈。

该能力只用于开发测试。它不增加 macOS 正式发布、DMG、签名、公证、自动更新或
用户支持承诺，也不替代 Windows NSIS、WebView2、权限、卸载和真实 Win10 普通
用户验收。

## 方案

采用“原生 sidecar + 临时 Tauri 桌面包 + 隔离状态 + 可验证旅程”的方案：

1. 将现有 PyInstaller sidecar 规格参数化，以当前 Rust host target triple 生成
   `stock-desk-sidecar-aarch64-apple-darwin` 等原生测试二进制；Windows 产物名称和
   发布链保持不变。
2. 新增显式传入的 `tauri.macos-test.conf.json`，只为本地测试声明 macOS
   `externalBin` 和 `.app` bundle；基础配置与 Windows 发布配置不增加 macOS 发布
   target。
3. macOS debug 测试宿主通过受限测试环境变量使用临时数据根。Windows release
   build 忽略该覆盖，测试不得读取或修改日常 Stock Desk 用户数据。
4. 扩展现有 `scripts/macos_tauri_smoke.py` 的职责边界：保留原生窗口与物理点击
   证明，新增独立的完整产品旅程驱动和证据，避免把一个脚本变成同时负责构建、
   产品断言和证据解析的单体。
5. 每个被点击访问的核心页面都同时执行视觉验收。自动化检查浅色/深色主题、
   常规与窄屏布局的文字和控件对比度、裁切、遮挡和横向溢出；本机真实 Tauri
   旅程为向导、行情、公式、回测、智能分析、任务中心、设置及退出对话框保留
   脱敏截图，页面可操作但视觉不可读时仍判定门禁失败。
6. 无论成功或失败，测试都优雅停止 sidecar，并以 PID/进程树检查作为异常清理
   兜底；删除临时 PyInstaller、Cargo、应用注册、测试数据和截图中间物，只保留
   忽略目录下的最终脱敏证据。

## 开发机执行边界

在已安装项目依赖、Rust 与 Xcode 命令行工具的开发机上运行
`pnpm desktop:test:macos:full`。门禁构建临时原生应用后暂停，由 Codex Computer Use
在真实窗口内执行真实物理点击；不得用 DOM 调用、合成事件或伪造证据替代。

最终脱敏报告和截图仅写入 Git 忽略的 `test-results/macos-full-product`。成功或失败后
都要删除临时 sidecar、应用、测试数据和构建工作目录，并确认宿主、sidecar 与后代
进程已经退出。该命令只服务源码开发测试，不提供终端用户安装路径，
不发布 macOS 安装包，且不能替代任何 Windows 验收。

## 完整旅程

本地门禁必须在真实 Tauri/WKWebView 窗口内验证：

1. 全新隔离数据根启动并进入四步首次向导。
2. 初始化免 Token 真实数据源，接受默认 `上证指数 (000001.SS)`，显示可追溯
   的真实日线 K 线。
3. 搜索并选择一只普通 A 股，确认证券身份、来源和 K 线更新。
4. 打开 Formula Studio，执行并保存通达信兼容 MACD 公式，验证副图或信号结果。
5. 使用 MACD 金叉买入、死叉卖出运行一次历史回测，确认产生非空、可追溯报告。
   免 Token 路径使用 BaoStock 明确交易日历与 `tradestatus` 的基础成交状态，仍校验
   停牌、T+1 和下一周期开盘，但不伪造历史涨跌停证据；预检、报告和回放必须持续
   显示这一限制。
6. 依次打开智能分析、任务中心和设置，验证页面在浅色/深色以及常规/窄屏下
   可读、无重叠和无裁切，并保存逐页视觉证据。
7. 关闭窗口、取消退出、再次关闭并确认退出，确认对话框视觉状态、sidecar 和
   所有后代进程退出。

真实免费数据源不可用时门禁必须失败并保留可操作的脱敏诊断，不得回退到演示
数据后宣称完整产品旅程通过。

## 测试结构

- Python 单元测试验证 target triple、sidecar 文件名、测试配置合并、数据根限制、
  旅程证据 schema 和清理边界。
- Rust 测试验证测试数据根只在非 Windows debug 测试宿主生效，正式 Windows
  路径不受影响。
- macOS 构建冒烟验证原生 sidecar 能启动并通过版本、来源修订、存储和健康握手。
- 真实窗口旅程由 Codex Computer Use 执行系统级点击；可稳定观测的 WebView
  状态由辅助观察器验证，证据明确记录 macOS/WKWebView，不能标记为 Windows
  验收。
- Playwright 页面矩阵对向导与六个核心工作区执行浅色/深色、常规/窄屏视觉
  契约，检查语义表面、文字和图标对比度、全局溢出、控件遮挡与关键区域可达；
  每个矩阵状态输出截图供人工复核，但截图差异不能取代上述确定性断言。

## 完成标准

- 一条本地命令可以从干净提交构建临时原生 sidecar 和 `.app`，完成完整旅程并
  输出绑定 source SHA/tree 的证据。
- 默认指数、普通 A 股、真实 K 线、MACD 公式、带基础成交状态披露的 MACD 回测和
  优雅退出全部成功。
- 向导、行情、公式、回测、智能分析、任务中心、设置和退出对话框均有逐页视觉
  证据；浅色/深色与常规/窄屏状态均无低对比度、重叠、裁切或横向丢失。
- 测试结束后无 Stock Desk host、sidecar、Worker、临时 Cargo/PyInstaller 和测试
  数据残留。
- 最终脱敏证据仅保留在被 Git 忽略的 `test-results/macos-full-product`。
- Windows sidecar 名称、NSIS 配置、CI/release 资产与发布范围不发生变化。
