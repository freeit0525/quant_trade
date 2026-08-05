# quant_trade

A 股量化交易工具集：MACD 指标计算与可视化分析 + 多策略回测引擎。

## 功能概览

### 1. MACD 计算工具（macd_calculator.py）

基于历史收盘价计算 MACD 指标，支持自定义参数与价格区间扫描。

- `calc_macd()` — 计算 DIF / DEA / MACD 柱
- `predict_macd()` — 输入预测价格计算明日 MACD
- `predict_macd_range()` — 扫描价格区间内 MACD 走势

### 2. MACD 可视化分析（macd.html）

浏览器端 MACD 分析工具，支持 A 股实时数据拉取与交互式图表。

- 股票数据拉取（代码 / 名称搜索，支持近期与自定义时间段）
- MACD 参数自定义（含 A 股专用预设：短线 3/8/3、长线 10/20/7）
- 单点 MACD 预测与价格区间扫描
- 涨跌停价格实时显示
- 交易信号判定（基于 MACD 一阶差分变化速率）

### 3. 策略回测引擎（backtest.html）

A 股回测工具，内置 14 种预设策略。

**预设策略**

- MACD 系列：金叉买入、死叉卖出、零轴过滤、强度确认
- RSI 系列：双线策略、底背离
- KDJ 系列：J 值策略、超卖反转
- 组合策略：RSI+KDJ 底部共振、KDJ+RSI 超卖、RSI+KDJ 双确认
- 经典策略：BOLL 突破、BIAS 乖离

**增强规则**

- T+1 交易限制
- A 股标准手续费（佣金 0.025% 最低 5 元 / 印花税 0.05% / 过户费 0.001%）
- 止损止盈、分批卖出
- 横盘过滤、快速卖出

## 使用方式

- Python 工具：`python macd_calculator.py`
- HTML 工具：浏览器直接打开 `macd.html` 或 `backtest.html`

## 技术栈

- Python + pandas（MACD 计算）
- 原生 HTML / JS + Chart.js（可视化）
- A 股数据通过东方财富接口拉取

## License

MIT
