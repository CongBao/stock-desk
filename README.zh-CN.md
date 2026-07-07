[English](README.md)

# Stock Desk

Stock Desk `v0.5.0` 是一个本地优先的 A 股行情、公式、回测与证据研究工作台。Stage 1 提供可配置数据源和日线、周线、60 分钟交互图；Stage 2 提供受控的通达信兼容公式；Stage 3 提供可复现的 A 股回测；Stage 4 提供基于 DeepSeek、OpenAI 兼容接口或本地 Ollama 的按需多智能体研究。

报告将判断绑定到固定来源证据；关键证据不足时不输出评级。它仅用于辅助研究，不构成投资建议，详见[路线图](ROADMAP.md)。

## 快速启动

原生环境需要 Python `>=3.12,<3.13`、[uv](https://docs.astral.sh/uv/)、Node.js 22 或 24 LTS 与 pnpm 11：

```bash
make bootstrap
make dev
```

打开 [http://localhost:5173/market](http://localhost:5173/market)。`make dev` 会监管 API、行情 worker 和 Vite；按 `Ctrl-C` 停止。

Docker Compose 会安装相同的锁定数据源依赖，并默认只绑定本机回环地址：

```bash
docker compose up --build --wait
# 打开 http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

API 健康检查位于 [http://localhost:8000/api/health](http://localhost:8000/api/health)，交互文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。原生与容器的持久数据都在 `data/`；API 和 worker 必须使用同一个数据库与行情湖路径。

Stage 0 基础能力继续保留：`/market`、`/formulas`、`/backtests`、`/analysis`、`/tasks` 与 `/settings` 共用同一工作台外壳，`demo.double` 持久化任务仍可用于 worker 诊断。Stage 1–4 分别完成行情数据、公式、回测和智能分析。

共享外壳兼容宽屏桌面、窄屏桌面和平板比例。窄屏下左侧导航会自动收起为带辅助名称的 SVG 图标栏，用户仍可通过指针或键盘展开/收回；核心控件会重新排布，不会相互遮挡。

## 配置数据源

保存 Tushare token 前，先复制环境文件并生成 Fernet 主密钥：

```bash
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

把输出写入 `.env` 的 `STOCK_DESK_MASTER_KEY`，然后打开 `/settings`：

- Tushare token 只写、本地加密；浏览器只能看到脱敏状态。
- TDX 路径必须是包含 `vipdoc` 的通达信安装绝对路径。本地 TDX 只提供支持的日线文件，不提供证券目录或交易日历。使用 Compose 时，把宿主机目录写入 `STOCK_DESK_TDX_HOST_PATH`，再在设置页填写 `/app/tdx`；API 与 worker 会共享同一个只读挂载。
- 日线、周线、60 分钟、证券目录和交易日历分别配置优先级；缺少凭据或 SDK 会如实记录为类型化路由失败。
- Eastmoney 仅是保留配置项，Stage 1 没有运行时适配器，不会伪装成可用。

Tushare、AKShare 和 BaoStock 受上游服务、权限、网络和许可条款约束。请自行核对各数据源条款；未经许可不要再分发数据。

## 使用行情工作区

1. 新安装先在 `/market` 点击“更新证券目录”。目录成功后会发布全 A；当前主要指数与数据源发现的行业成分通过 AKShare 独立刷新，部分失败时保留上次有效快照。
2. 搜索证券或打开预设/自定义股票池。自定义池支持低代码搜索、添加、排序、重命名、移除和删除。
3. 选择周期、复权、日期范围，以及单只证券或冻结的股票池范围，再明确启动更新。进度、取消和逐证券成功/失败/取消结果都会持久化。
4. 可配置唯一的 Asia/Shanghai 每日计划。计划保存证券列表快照，后续修改股票池不会静默改变范围。
5. 在图表旁检查数据源路由、截止时间、回退尝试和来源证明。

浏览图表只读取本地缓存。缓存缺失会显示引导，不会静默访问外部数据源。同一个请求序列只选择一个数据源，不会拼接多个来源。

## 使用公式工作台

1. 打开 `/formulas`，选择内置 MACD 模板、粘贴兼容公式，或从可搜索函数库插入字段和函数。
2. 通过表单编辑命名参数，使用 Monaco 补全与函数说明，再执行校验。不支持函数、未来数据或重绘能力会定位到行列，并阻止保存与预览。
3. 先保存不可变版本，再显式运行预览。右栏始终以 K 线为主图，并按声明在主图叠加或独立副图绘制公式输出与 BUY/SELL 买卖点。
4. 后续修改保存为新版本；历史版本只读，也可以复制为独立公式。行情图表和直接预览共用同一引擎与固定来源快照。

支持范围与运行语义见[公式兼容清单](docs/formula-compatibility.md)。首版明确不提供条件选股公式、五彩 K 线公式，以及 AI 生成、解释或修复公式。

## 运行历史回测

1. 保存有效的交易系统公式后，打开 `/backtests`，或从行情工作区点击“回测当前股票”。五步向导会选择精确公式版本、单股或固定股票池、日线/周线/60 分钟、半开日期、复权、固定手数、佣金、税费与滑点。
2. 提交前检查数据覆盖和“收盘确认、下一周期首次可成交开盘”规则。股票池任务异步运行，持续保存进度与日志，支持取消并保留部分结果。
3. 结论优先报告展示已实现胜率及分母、净收益统计、可靠性、分布、分组样本、未平仓、失败和逐项毛收益到净收益成本。股票池结果是逐股票独立交易样本，不是组合收益。
4. 单笔回放只读取本次运行固定的行情/状态 manifest 与 SignalSeries；K 线是主图，公式是副图，周线成交另行披露精确日线执行证据。JSON/CSV 导出保留可复现元数据。

成交语义、指标定义和限制见[回测语义](docs/backtesting-semantics.md)。Stock Desk 不下单，也不连接券商。

## 运行智能分析

1. 打开 `/analysis`，创建 DeepSeek、OpenAI 兼容或 Ollama 模型配置并通过连接测试。API Key 仅在本地加密保存，浏览器只能看到掩码。
2. 输入一只 A 股并运行四类数据预检。行情只读缓存；基本面、公告和新闻会展示实际路由、回退、权限缺口与截止时间。
3. 启动九阶段异步研究。技术与基本面/新闻先执行，多方与空方随后评审，风险决策仅在关键证据充分时输出五档评级。
4. 选择判断可查看来源与时间。部分报告保留成功内容；重试失败阶段会创建关联子运行，不覆盖历史。

## 当前范围与安全边界

Stage 4 包含行情工作区、技术指标/交易系统公式、可复现历史回测和证据关联的 LLM 研究。不包含实时行情、动态选股器、画线工具、共享资金组合模拟、券商连接、实盘/自动交易、目标价、仓位比例或个性化建议。

当前是可信单用户本地服务，没有认证、授权或 TLS。请只在回环地址使用；不要提交 `.env`、token、主密钥、本地 TDX 路径、数据库或下载的数据，也不要把它们粘贴到 issue。参阅[数据源说明](docs/data-sources.md)、[安全说明](SECURITY.md)和[架构](docs/architecture.md)。

## 质量门禁

```bash
make test
make acceptance
make acceptance-formula
make acceptance-backtest
make benchmark
make benchmark-formula
make benchmark-backtest
make lint
make typecheck
make build
make public-tree
make security
```

先用 `pnpm exec playwright install chromium` 安装 Chromium，再运行 `make e2e-market`、`make e2e-formula`、`make e2e-backtest` 与 `make e2e-analysis`，即可验证真实 Stage 1–4 浏览器流程。`make security` 需要网络访问：它通过 OSV 检查 Python 依赖，并通过 npm registry 检查 JavaScript 生产依赖；执行审计前还会确认清单与锁文件一致。Docker 运行时，`make release-check` 会执行全部浏览器、安全和隔离的容器 smoke 门禁，并自行启动、清理 Compose。项目采用 Apache-2.0 许可证。Stock Desk 是研究软件，不构成投资建议；请独立核验数据和决策。
