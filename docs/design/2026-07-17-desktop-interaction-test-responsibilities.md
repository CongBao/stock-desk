# 桌面交互测试职责设计

## 背景

Stock Desk v1.1 只发布 Windows 安装包，但开发机是 macOS。测试需要同时满足两个目标：

1. 在本地快速验证真实桌面应用的物理点击与窗口状态变化。
2. 在 Windows 环境验证 NSIS 安装、普通用户权限、WebView2、原生窗口和卸载行为。

GitHub Hosted Windows Runner 不能提供可信物理鼠标输入。实测中，`SendInput` 返回成功，但系统光标始终固定在 `(512,384)`；即使归一化目标已经校准到 `(65535,0)`，光标仍不移动。因此 Hosted Runner 的结果不得标记为“物理点击”或用来替代本地快速反馈。

## 决策

采用三层职责划分：

| 层级 | 环境 | 交互方式 | 证明范围 | 发布作用 |
| --- | --- | --- | --- | --- |
| 本地快速门禁 | macOS 上的真实 Tauri `.app` | Codex Computer Use 系统级物理点击 | 原生窗口关闭、退出确认框、取消与确认、进程退出 | PR 合并前必须通过 |
| Windows Hosted 验证 | GitHub Actions Windows Runner 中安装后的程序 | UI Automation 调用原生控件；CDP/Playwright 操作 WebView 控件 | NSIS、当前用户安装、权限边界、窗口事件、WebView2 交互、卸载与清理 | PR 和 main CI 必须通过，但不宣称物理点击 |
| Windows 实机验收 | 普通用户 Win10 机器 | 用户真实鼠标和键盘 | 安装、首次向导、选股、日常启动、退出、卸载 | 替换 v1.1.0 Release 前必须通过 |

不得用 Windows Hosted 自动化结果替代 macOS 本地物理点击，也不得用 macOS 结果替代 Windows 实机安装与权限验收。

## 本地 macOS 物理点击门禁

`scripts/macos_tauri_smoke.py` 构建精确提交对应的临时 `Stock Desk.app`，启动真实 Tauri/WKWebView 进程，并等待 Codex Computer Use 完成以下系统级动作：

1. 点击原生标题栏关闭按钮。
2. 点击“取消”。
3. 再次点击原生标题栏关闭按钮。
4. 点击“退出应用”。

独立辅助功能观察器验证每次点击后的窗口状态和最终进程退出。操作证据必须绑定源 SHA、源 tree、会话 nonce、PID、动作顺序和输入方式。脚本结束后必须清理临时 Cargo target、应用注册和残留进程。

该门禁证明本地真实桌面反馈环路，不使用浏览器页面、DOM 事件注入或脚本直接调用退出命令。

## Windows Hosted 自动化交互

Windows Runner 继续安装并启动精确安装包，但交互证据改为诚实标注的 Hosted 自动化：

- 原生标题栏关闭按钮：通过 Windows UI Automation 精确定位唯一按钮，校验 PID、HWND、标题栏祖先和控件状态后调用 `InvokePattern`。
- WebView 退出框按钮：通过已绑定到隔离 WebView2 实例的 CDP 会话，按唯一角色和名称定位并调用 Playwright 点击。
- 每一步由另一侧观察状态变化：原生关闭后 CDP 必须观察到对话框；WebView 点击后宿主进程必须观察到继续运行或正常退出。

证据文件使用 `stock-desk-windows-hosted-automation-v1` schema，并明确记录：

- `input_method = windows-uia-and-cdp-automation`
- `physical_mouse_click = false`
- 源 SHA、源 tree、候选安装包 SHA-256、已安装宿主 SHA-256
- 宿主 PID、主 HWND、WebView2 隔离目录和 CDP 端口归属
- 四步动作顺序及每一步的目标身份、调用方式和观察结果
- Hosted Runner 不是 Win10/Win11 桌面 SKU、不是普通用户实机、不能证明 UAC 安全桌面行为

任何自动化定位歧义、跨进程目标、状态未变化、非零退出码或证据身份不一致都必须失败。不得回退到被错误标记为物理点击的路径。

## 数据流与证据边界

1. CI 从 PR 精确 SHA 构建一次 Windows 安装包。
2. 安装包以当前用户方式静默安装到测试隔离目录。
3. 宿主启动后，PowerShell 验证安装路径、权限、快捷方式、进程树、无可见 PowerShell 窗口和 WebView2 归属。
4. Node/CDP 观察 WebView 状态；PowerShell/UIA 驱动原生控件，两侧通过带 nonce 的原子文件握手。
5. 自动化交互、截图、后台状态、卸载和清理结果写入绑定精确 SHA/tree 的证据清单。
6. PR CI 通过后、合并前仍需本地 macOS 物理门禁；Release 替换前仍需真实 Win10 普通用户验收。

## 失败处理与清理

- 所有等待都有明确超时；失败信息指出具体目标和阶段。
- 失败时上传 Node 日志、WebView2 归属、安装包身份和最近完成的交互步骤，但不上传用户数据或源码。
- 无论成功失败，都终止测试宿主、sidecar、隔离 WebView2 和观察器进程，卸载测试安装，并删除临时安装数据、Cargo target、下载包和解压目录。
- 若本地 Mac 锁屏，物理门禁必须拒绝执行并提示解锁；不得绕过登录或锁屏。

## 验收标准

实现完成必须同时满足：

1. Windows CI 不再包含 `windows-real-click`、`real_os_mouse_click` 或 Hosted Runner 物理点击成功声明。
2. Windows Hosted 自动化证据完整记录四步动作，并通过安装、普通用户数据目录、首次启动、后台无可见终端、退出和卸载验证。
3. 同一源 SHA 的本地 `Stock Desk.app` 完成四次系统级物理点击，独立观察器确认状态序列与进程退出。
4. PR 合并后 main 全量 CI 通过。
5. 真实 Win10 普通用户完成安装、首次向导、默认上证指数与普通 A 股选择、正常启动、退出和卸载验收。
6. 通过验证的新安装包原位替换现有不可用的 v1.1.0 Release；Release 名称不再强调 `unsigned`。

## 非目标

- 不把 GitHub Hosted Runner 改造成物理桌面实验室。
- 不为 v1.1 增加 macOS 发布包。
- 不以降低安装、权限、进程清理或证据身份校验为代价换取 CI 通过。
- 不用浏览器网页测试替代真实 Tauri `.app` 的本地物理点击。
