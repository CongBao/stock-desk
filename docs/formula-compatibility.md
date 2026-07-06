# 通达信公式兼容性

<!-- 此文件由 stock_desk.formula.compatibility 生成，请勿手工修改表格。 -->

当前兼容版本：`tdx-v1`。此清单由 API、Monaco 编辑器和公开文档共同使用。
官方名称与基础公式参考：[通达信公式系统函数列表](https://help.tdx.com.cn/gspt/docs/markdown/redword/functionlist.html)。未由官方明确规定的细节会标注为 stock-desk 固化语义。

## 支持的行情字段

| 名称 | 来源/缩放 | 单位 | 类型 | 说明 |
| --- | --- | --- | --- | --- |
| `C` | `CLOSE` × 1/1 | `price` | `number_series` | 收盘价 CLOSE 的通达信别名。 |
| `CLOSE` | `CLOSE` × 1/1 | `price` | `number_series` | 当前周期的收盘价。 |
| `H` | `HIGH` × 1/1 | `price` | `number_series` | 最高价 HIGH 的通达信别名。 |
| `HIGH` | `HIGH` × 1/1 | `price` | `number_series` | 当前周期的最高价。 |
| `L` | `LOW` × 1/1 | `price` | `number_series` | 最低价 LOW 的通达信别名。 |
| `LOW` | `LOW` × 1/1 | `price` | `number_series` | 当前周期的最低价。 |
| `O` | `OPEN` × 1/1 | `price` | `number_series` | 开盘价 OPEN 的通达信别名。 |
| `OPEN` | `OPEN` × 1/1 | `price` | `number_series` | 当前周期的开盘价。 |
| `V` | `VOLUME` × 1/100 | `hands` | `number_series` | VOL 的通达信短别名，A股按 100 股/手换算。 |
| `VOL` | `VOLUME` × 1/100 | `hands` | `number_series` | 通达信成交量，A股按 100 股/手换算。 |
| `VOLUME` | `VOLUME` × 1/1 | `shares` | `number_series` | stock-desk 扩展成交量，单位为股。 |

## 支持的函数

| 函数 | 参数约束 | 结果/派发 | 时间行为 | 精确语义 |
| --- | --- | --- | --- | --- |
| `ABS(X)` | X: scalar/number_series | `number_series` / `math.abs` | `current_only` | 逐项返回 X 的绝对值；null 传播。 |
| `BARSLAST(X)` | X: scalar/boolean_series/number_series | `number_series` / `signal.barslast` | `past_only` | 当前周期条件成立返回 0，之后逐周期递增；从未成立则为 null。条件 null 视为未命中，已有状态时距离仍按 bar 递增。未命中语义为 stock-desk tdx-v1 固化语义。 |
| `COUNT(X, N)` | X: scalar/boolean_series/number_series; N: integer_scalar，非负整数；N=0 表示从首个有效值累计。 | `number_series` / `series.count` | `past_only` | 统计最近 N 个 bar 内忽略 null 后非零/true 的次数，至少一个有效值才输出；N=0 从首个有效值累计。 |
| `CROSS(X, Y)` | X: scalar/number_series; Y: scalar/number_series | `boolean_series` / `signal.cross` | `past_only` | 仅当 X[t]>Y[t] 且 X[t-1]<=Y[t-1] 时为 true；首周期或任一比较值为 null 时为 false。边界/null 规则为 stock-desk tdx-v1 固化语义。 |
| `EMA(X, N)` | X: scalar/number_series; N: integer_scalar，整数且 N>=1。 | `number_series` / `series.ema` | `past_only` | 递推 Y=2*X/(N+1)+(N-1)*Y_PREV/(N+1)；首个有效值以 X 初始化，输入 null 时输出 null 且不更新状态。初始化/null 规则为 stock-desk tdx-v1 固化语义。 |
| `FILTER(X, N)` | X: scalar/boolean_series/number_series; N: integer_scalar，常量，常量整数且 N>=1。 | `boolean_series` / `signal.filter` | `past_only` | 当前命中保留为 true，并将后续 N 个周期内再次出现的命中抑制为 false；条件 null 视为未命中且抑制期仍按 bar 推进；N 为常量正整数。 |
| `HHV(X, N)` | X: scalar/number_series; N: integer_scalar，非负整数；N=0 表示从首个有效值累计。 | `number_series` / `series.hhv` | `past_only` | 返回最近 N 个 bar 内忽略 null 后的最大值，至少一个有效值才输出；N=0 从首个有效值累计。 |
| `IF(CONDITION, A, B)` | CONDITION: scalar/boolean_series/number_series; A: scalar/number_series; B: scalar/number_series | `number_series` / `logic.if` | `current_only` | 条件非零/true 时逐项返回 A，否则返回 B；条件为 null 时结果为 null。 |
| `LLV(X, N)` | X: scalar/number_series; N: integer_scalar，非负整数；N=0 表示从首个有效值累计。 | `number_series` / `series.llv` | `past_only` | 返回最近 N 个 bar 内忽略 null 后的最小值，至少一个有效值才输出；N=0 从首个有效值累计。 |
| `LONGCROSS(X, Y, N)` | X: scalar/number_series; Y: scalar/number_series; N: integer_scalar，常量，常量整数且 N>=1。 | `boolean_series` / `signal.longcross` | `past_only` | 当此前连续 N 个完整周期 X<Y，且当前周期 X>Y 时为 true；窗口不足或含 null 时为 false。N 固定为常量是 stock-desk tdx-v1 约束。 |
| `MA(X, N)` | X: scalar/number_series; N: integer_scalar，整数且 N>=1。 | `number_series` / `series.ma` | `past_only` | 仅当最近 N 个 bar 位置全部有效时返回算术平均，否则为 null；预热期为 null。这是 stock-desk tdx-v1 固化语义。 |
| `MAX(X, Y)` | X: scalar/number_series; Y: scalar/number_series | `number_series` / `math.max` | `current_only` | 逐项返回 X、Y 较大值；任一输入为 null 时结果为 null。 |
| `MIN(X, Y)` | X: scalar/number_series; Y: scalar/number_series | `number_series` / `math.min` | `current_only` | 逐项返回 X、Y 较小值；任一输入为 null 时结果为 null。 |
| `REF(X, N)` | X: scalar/number_series; N: integer_scalar，非负整数，可为参数。 | `number_series` / `series.ref` | `past_only` | 返回 X[t-N]；N 为非负整数，历史不足返回 null。 |
| `SMA(X, N, M)` | X: scalar/number_series; N: integer_scalar，整数且 N>=1。; M: integer_scalar，整数且 1<=M<=N。 | `number_series` / `series.sma` | `past_only` | 递推 Y=(M*X+(N-M)*Y_PREV)/N，且 1<=M<=N；首个有效值以 X 初始化，输入 null 时输出 null 且不更新状态。初始化/null 规则为 stock-desk tdx-v1 固化语义。 |
| 关系约束 | `M <= N` | 结构化约束 | - | Task 3 必须通用执行此关系。 |
| `STD(X, N)` | X: scalar/number_series; N: integer_scalar，整数且 N>=2。 | `number_series` / `statistics.std` | `past_only` | 仅当最近 N 个 bar 位置全部有效时返回样本标准差（分母 N-1），否则为 null；预热期为 null。样本及预热规则为 stock-desk tdx-v1 固化语义。 |
| `SUM(X, N)` | X: scalar/number_series; N: integer_scalar，非负整数；N=0 表示从首个有效值累计。 | `number_series` / `series.sum` | `past_only` | 返回最近 N 个 bar 内忽略 null 后的和，至少一个有效值才输出；N=0 从首个有效值累计。 |

## 语法和限制

- `:=` 声明隐藏中间量，`:` 声明公开输出；标识符不区分大小写。
- 仅支持受控表达式、静态函数调用和 `//` 注释；不执行 Python 代码，也不提供文件或网络访问。
- 未列出的函数会返回 `unsupported_function`；参数数量不符会返回 `invalid_argument_count`。
- 本版本只登记稳定的当前值或历史依赖语义。未来数据和信号漂移分析由保存/回测校验阶段处理。
- 首版不支持条件选股、五彩 K 线或 AI 生成、解释、修复公式。
- `VOLUME` 是 stock-desk 扩展，单位为股；A股 `VOL`/`V` 按 100 股/手从 `VOLUME` 缩放为手。`AMOUNT` 尚未支持。
- 所有数值按 float64 计算；除零、溢出、NaN 和 Infinity 转为 null，JSON 只输出有限数字或 null。
- 除函数条目明确说明外，必要输入为 null 时结果为 null。初始化、预热及浮点规范均为 stock-desk tdx-v1 固化语义。

## 解析上限

- 源码：64000 UTF-8 字节
- 语句：128 条
- AST 节点：256 个
- 括号嵌套：64 层
- 数字字面量：128 个字符
- 指数绝对值：10000

## 校验与重新生成

```bash
uv run --frozen python -m stock_desk.formula.compatibility --check docs/formula-compatibility.md
uv run --frozen python -m stock_desk.formula.compatibility --write docs/formula-compatibility.md
uv run --frozen python -m stock_desk.formula.compatibility --json
```
