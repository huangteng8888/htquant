# 量化 Adapter 核心策略文档

> 每个 adapter 代表一种量化范式，辩论引擎将各范式的信号进行真理裁决。

---

## 1. Qlib Adapter — 均值回归 · Alpha因子

**项目**: microsoft/qlib | **路径**: `htquant/projects/qlib_adapter.py`

### 核心优势

**RSI均值回归 + 多因子Alpha** — qlib 是微软开源的AI量化研究平台，提供了丰富的因子库（Alpha158、Alpha360）和机器学习预测能力。其核心优势在于：

- **因子挖掘**: 基于OLSL窗口的Alpha因子族，可捕捉短期定价偏差
- **机器学习预测**: LightGBM、MLP等模型学习因子与收益的非线性关系
- **高精度数据**: 预处理的A股日线/分钟线数据，涵盖2000+只股票

### 核心策略逻辑

```
信号判断（RSI均值回归）:
  RSI < 30 → 市场超卖 → 信号: 增持/买入（均值回归做多）
  RSI > 70 → 市场超买 → 信号: 减持/清仓（均值回归做空）
  30 < RSI < 70 → 中性 → 持有

极值事件修正（基于回测数据）:
  W20/W50 触低 + RSI<50 → 做多翻转做空（短期趋势延续）
  W20/W50 触高        → 强制短空（市场继续下跌）
  W252 触低/高        → 均值回归方向强化
```

### 输入特征

- `$close`, `$high`, `$low`, `$volume`
- RSI(14), RSI(28)
- MA5/MA20 均线系统
- 20日/252日区间位置

### 信号输出

- 5档信号: 买入 / 增持 / 持有 / 减持 / 清仓
- 置信度: 0.60 ~ 0.85

### 适用场景

- **均值回归交易**: 超买超卖反转
- **因子Alpha**: 多因子组合轮动
- **宽基扫描**: 全市场机会筛选

---

## 2. Momentum Adapter — 动量趋势 · 多周期确认

**项目**: 内部实现 | **路径**: `htquant/projects/momentum_adapter.py`

### 核心优势

**趋势跟踪 · 相对强弱分解** — 与qlib的均值回归互补，Momentum策略捕捉趋势的延续性：

- **多周期动量**: 5日/20日/60日价格动量，避免被短期噪声误导
- **MACD趋势确认**: 快线/慢线交叉过滤假突破
- **相对强弱**: 股票与指数的相对表现，过滤大盘噪音

### 核心策略逻辑

```
动量信号（Momentum Trend Following）:
  短周期动量 > 长周期动量 → 趋势向上 → 做多
  短周期动量 < 长周期动量 → 趋势向下 → 做空

RSI 趋势信号:
  RSI(14) > 55 AND MACD > 0 → 增持/买入
  RSI(14) < 45 AND MACD < 0 → 减持/清仓

极值事件修正:
  W20/W50 触低 + RSI<50 → 趋势跟踪应做空（不逆势抄底）
  W20/W50 触高         → 强化短空（不逆势做多）
  W252 触低/高         → 均值回归信号可与动量共振
```

### 与qlib的互补关系

| 场景 | qlib信号 | Momentum信号 | 预期最优 |
|------|---------|-------------|---------|
| 超卖反弹 | 做多 ↑ | 可能做空 ↓ | 分歧→辩论裁决 |
| 趋势延续 | 可能中性 | 做多 ↑ | 跟随趋势 |
| 触低继续跌 | 做多 ↓ | 做空 ↑ | Momentum正确 |
| 触高继续涨 | 做空 ↓ | 可能做多 ↑ | 分歧 |

---

## 3. Backtrader Adapter — CTA策略 · MA双叉

**项目**: backtrader/backtrader | **路径**: `htquant/projects/backtrader_adapter.py`

### 核心优势

**MA双叉趋势系统** — backtrader是Python最成熟的回测框架之一，其MA双叉策略简单有效：

- **双重确认**: 短周期MA上穿/下穿长周期MA，减少假信号
- **参数稳健**: MA5/MA20参数在A股有较好普适性
- **统计属性**: 可计算夏普比、最大回撤等核心指标

### 核心策略逻辑

```
MA双叉策略:
  MA5 上穿 MA20 → 金叉 → 买入信号（增持）
  MA5 下穿 MA20 → 死叉 → 卖出信号（减持）
  无交叉       → 持有

超额收益评估:
  策略收益 - 买入持有收益 = 超额收益
  超额 > 20% → 买入, > 10% → 增持, > 0 → 持有
```

### 信号输出

- 基于策略回测结果的超额收益评估
- 置信度: 0.65 ~ 0.80

---

## 4. FinRL Adapter — 深度强化学习

**项目**: AI4Finance-Foundation/FinRL | **路径**: `htquant/projects/finrl_adapter.py`

### 核心优势

**深度强化学习策略** — FinRL使用PPO/DQN/SAC等强化学习算法直接从市场数据学习最优交易策略：

- **自适应市场**: RL代理能从数据中学习复杂、非线性的市场模式
- **多目标优化**: 同时优化收益与风险（Sharpe比率）
- **端到端学习**: 从原始OHLCV数据直接输出交易动作

### 核心策略逻辑

```
RL代理状态特征（PPO-like策略）:
  - 5日/20日收益率
  - 20日年化波动率
  - 收盘价相对MA20位置
  - RSI(14)
  - 动量加速度
  - 成交量变化率

PPO策略输出:
  - 动量强势向上 → 增持/买入（confidence 0.72-0.82）
  - 动量弱势/反转 → 减持/清仓（confidence 0.72-0.82）
  - 中性市场     → 持有（confidence 0.55）
```

### RL vs 传统策略的本质区别

| 维度 | 传统策略(MA/RSI) | FinRL(RL) |
|------|---------------|----------|
| 策略形式 | 固定规则 |  Learned Policy |
| 市场适应 | 滞后调整 | 自动适应 |
| 计算成本 | 低 | 高 |
| 可解释性 | 高 | 低 |
| 数据效率 | 低 | 高 |

---

## 5. Freqtrade Adapter — 加密货币网格/趋势

**项目**: freqtrade/freqtrade | **路径**: `htquant/projects/freqtrade_adapter.py`

### 核心优势

**加密货币专用的MACD+RSI+成交量趋势系统**：

- **加密适配**: 专门针对高波动加密货币市场的指标参数
- **量价共振**: 成交量放大时确认趋势信号
- **极端RSI**: 币圈RSI极端值(20/80)比A股(30/70)更有效

### 适用场景

⚠️ **注意**: 此adapter仅适用于加密货币，A股量化回测中标记为"不适用"。

```
MACD + RSI 信号:
  MACD>0 AND RSI<80 → 增持
  MACD<0 AND RSI>20 → 减持
  RSI>80 → 清仓（极度超买）
  RSI<20 → 买入（极度超卖）
  成交量放大>2x → 趋势确认
```

---

## 6. Vnpy Adapter — CTA·R-Breaker/DualThrust

**项目**: vnpy/vnpy | **路径**: `htquant/projects/vnpy_adapter.py`

### 核心优势

**R-Breaker日内轴心系统** — vnpy是国产量化交易框架，R-Breaker是其最经典的CTA策略：

- **日内交易**: 基于昨日OHLC计算当日枢轴点，捕捉日内波动
- **支撑阻力量化**: 6个关键价位(s1/s2/s3/r1/r2/r3)精准定义买卖点
- **国内优化**: 参数针对A股/期货市场优化

### 核心策略逻辑

```
R-Breaker 轴心系统（基于昨日数据）:
  pivot = (High_y + Low_y + Close_y) / 3
  s1 = 2*pivot - High_y      # 支撑1
  s2 = pivot - (High_y - Low_y)
  s3 = Low_y - 2*(High_y - pivot)
  r1 = 2*pivot - Low_y       # 阻力1
  r2 = pivot + (High_y - Low_y)
  r3 = High_y + 2*(pivot - Low_y)

今日信号:
  价格上穿 r3 → 清仓（强势做空）
  价格上穿 r1/r2 → 减持
  价格下穿 s1/s2 → 增持
  价格下穿 s3 → 买入（强势做多）
  其他 → 持有

DualThrust 备用（N日区间突破）:
  range = max(HH-LC, HC-LL)  # N日区间
  upper = open + K*range
  lower = open - K*range
  close > upper → 增持
  close < lower → 减持
```

### 与其他adapter的互补

| vnpy信号 | qlib信号 | Momentum信号 | 含义 |
|---------|---------|------------|------|
| 触高做空 | 超买做空 | 趋势向下 | 三空共振→清仓 |
| 触低做多 | 超卖做多 | 趋势向上 | 三多共振→买入 |

---

## 7. Fincept Adapter — 市场宽度·资金流向

**项目**: FinceptTerminal | **路径**: `htquant/projects/fincept_adapter.py`

### 核心优势

**Bloomberg式多数据源聚合** — 模拟Bloomberg终端的市场宽度和资金流分析能力：

- **市场宽度**: 全市场涨跌家数比，捕捉市场整体情绪
- **北向/主力资金**: 资金流向大数据，机构行为先于价格
- **板块轮动**: 行业板块资金轮动顺序

### 核心策略逻辑

```
市场宽度分析:
  breadth = (上涨家数 - 下跌家数) / 总家数
  breadth > +20% → 增持
  breadth > +5%  → 持有
  breadth < -20% → 清仓

资金流向分析:
  主力净流入占比 > 5% → 增持
  主力净流出占比 > 5% → 减持
```

### 信号定位

Fincept不产生个股方向信号，而是提供**市场整体情绪**信号，用于调整其他adapter的置信度。

---

## 8. GsQuant Adapter — 波动率风险·IV Rank

**项目**: goldmansachs/gs-quant | **路径**: `htquant/projects/gsquant_adapter.py`

### 核心优势

**波动率风险分析** — gs_quant是Goldman Sachs的量化工具，擅长衍生品定价和波动率分析：

- **HV比率**: 短期历史波动率 vs 长期波动率，捕捉波动率均值回归
- **IV Rank**: 期权隐含波动率分位数，A股可通过HV模拟
- **风险因子**: 宏观因子（利率、信用）暴露分析

### 核心策略逻辑

```
波动率信号:
  HV_ratio = HV_20d / HV_252d

  HV_ratio > 1.5 → 波动率急剧放大 → 减持（风险警示）
  HV_ratio > 1.2 → 波动率偏高 → 持有
  HV_ratio < 0.6 → 波动率低位蓄势 → 增持（机会）
  正常区间     → 持有
```

### 与其他adapter的互补

GsQuant提供**风险维度**信号，与方向信号形成互补：
- 高波动率 + 其他adapter做多 → 降低置信度
- 低波动率 + 其他adapter做多 → 提高置信度

---

## 9. TradingAgents Adapter — LLM多Agent

**项目**: tradingagents/TradingAgents | **路径**: `htquant/projects/tradingagents_adapter.py`

### 核心优势

**LLM多Agent辩论系统** — TauricResearch的TradingAgents使用多Agent协作（Research Manager → Trader → Portfolio Manager），通过LLM的推理能力生成信号：

- **深度研究**: Research Manager分析财务、宏观、行业数据
- **交易决策**: Trader基于研究结果做具体买卖决策
- **组合优化**: Portfolio Manager给出仓位和风险建议

### 核心策略逻辑

```
LLM Agent 流程:
  1. Research Manager: 分析 {股票, 日期} 的基本面和技术面
  2. Trader: 生成买入/卖出/持有建议（含置信度）
  3. Portfolio Manager: 综合给出 PortfolioRating

Rating映射:
  Buy        → 增持 (0.82)
  Overweight → 买入 (0.72)
  Hold       → 持有 (0.55)
  Underweight → 减持 (0.72)
  Sell       → 清仓 (0.82)
```

### 使用注意

⚠️ LLM调用每次约10-30秒，全量回测需预计算：
```python
TradingAgentsAdapter.batch_precompute(
    stock_codes=['000001'], start_date='2023-01-01',
    end_date='2025-06-01', cache_path='/tmp/ta_signals.db')
```

---

## 10. Lean Adapter — QuantConnect Lean 多Alpha融合

**项目**: quantconnect/Lean | **路径**: `htquant/projects/lean_adapter.py`

### 核心优势

**多Alpha模型投票机制** — QuantConnect Lean是专业级算法交易引擎，包含丰富的Alpha模型库。此Adapter实现了4种经典Lean Alpha模型的融合：

- **事件驱动**: Insight生成机制，信号明确
- **多模型融合**: 4种模型多数投票，减少单一模型偏差
- **200+技术指标**: C#优化指标库，pandas实现无依赖

### 4种Alpha模型

```
1. RsiAlphaModel (Lean源码):
   RSI < 25 → 增持    RSI > 75 → 减持    RSI 25-75 → 中性

2. MacdAlphaModel (Lean源码):
   MACD - Signal > 1% → 增持    MACD - Signal < -1% → 减持

3. EmaCrossAlphaModel (Lean源码):
   EMA10 上穿 EMA20 → 增持    EMA10 下穿 EMA20 → 减持

4. DualThrustAlpha (Lean VIXDualThrust):
   close > upper_bound → 增持    close < lower_bound → 减持
```

### 信号融合

4个模型投票 → 一致模型≥3个 → 置信度0.78-0.88
分歧时 → 中性信号

---

## Adapter 信号对比矩阵

| Adapter | 范式 | 核心指标 | 极值敏感性 | 回测速度 |
|--------|------|---------|-----------|---------|
| Qlib | 均值回归 | RSI, Alpha因子 | ★★★ 高 | ★★★★★ 快 |
| Momentum | 趋势跟踪 | MACD, 动量 | ★★☆ 中 | ★★★★★ 快 |
| Backtrader | CTA/MA叉 | MA5/20叉 | ★★☆ 中 | ★★★★ 快 |
| FinRL | 深度RL | Learned Policy | ★★★ 高 | ★☆☆☆ 慢 |
| Vnpy | CTA/R-Breaker | Pivot轴心 | ★★☆ 中 | ★★★★ 快 |
| Freqtrade | 加密趋势 | MACD+RSI | ★★★ 高 | ★★★★ 快 |
| Fincept | 市场宽度 | 涨跌家数 | ★☆☆ 低 | ★★☆ 中 |
| GsQuant | 波动率 | HV比率 | ★☆☆ 低 | ★★★★★ 快 |
| TradingAgents | LLM推理 | 深度研究 | ★★★ 高 | ★☆☆☆ 极慢 |
| Lean | 多Alpha融合 | RSI+MACD+EMA+DualThrust | ★★★ 高 | ★★★★ 快 |

---

## 回测结论回顾

| 事件 | qlib胜率 | Momentum胜率 | 辩论胜率 |
|------|---------|-------------|---------|
| W20触低 | 60.6% | 60.5% | 60.5% |
| W50触低 | 60.4% | 60.1% | 60.1% |
| W20触高 | 65.1% | 62.6% | 63.9% |
| W50触高 | 71.3% | 75.0% | 71.6% |
| W100触低 | 44.9% | 44.9% | 44.9% |
| W100触高 | 79.5% | 79.5% | 74.5% |
| W252触低 | 57.0% | 56.1% | 56.1% |
| W252触高 | 62.5% | 64.7% | 64.7% |

**整体胜率: 59.8%（z=10.67, p<0.0001）**
