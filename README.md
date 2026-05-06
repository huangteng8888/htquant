# htquant - 量化研究聚合引擎

Multi-Agent Debate Aggregation for Quantitative Research

将同一问题分发给多个量化项目（qlib/backtrader/FinRL/freqtrade/vnpy），收集各自结论，发现分歧时触发多轮辩论，最终给出综合策略。

## 核心特性

- **多项目聚合**：集成qlib、backtrader、FinRL、freqtrade、vnpy
- **辩论机制**：当项目间结论冲突时，自动触发多轮辩论
- **交叉验证**：多策略互相验证，提高信号可靠性
- **短中长期**：支持短线(<1周)、中线(1周~1月)、长线(>1月)分析

## 架构

```
htquant/
├── htquant/
│   ├── config.py          # 配置
│   ├── dispatcher.py      # 查询分发器
│   ├── aggregator.py     # 结果聚合器
│   ├── debate.py          # 辩论引擎
│   ├── scoring.py         # 评分系统
│   ├── main.py           # 主入口
│   └── projects/
│       ├── qlib_adapter.py       # qlib技术分析
│       ├── backtrader_adapter.py  # 策略回测
│       ├── finrl_adapter.py       # 强化学习(stub)
│       ├── freqtrade_adapter.py   # 加密货币(stub)
│       └── vnpy_adapter.py        # 交易执行(stub)
└── examples/
    └── analyze_7stocks.py  # 示例：7只股票分析
```

## 安装

```bash
cd ~/github/htquant
pip install -e .
```

## 使用

```bash
# 分析7只默认股票
python -m htquant.main

# 分析指定股票
python -m htquant.main --stocks 301071 000901

# 强制触发辩论
python -m htquant.main --stocks 600422 688089 --debate

# 列出可用项目
python -m htquant.main --list-projects
```

## 辩论机制

当不同项目给出相反信号时：

1. **第一轮**：双方各自陈述论据
2. **第二轮**：考虑对方观点后重新评估
3. **收敛检测**：连续2轮信号接近则收敛
4. **最终判决**：取更保守的信号

示例冲突：
- qlib均值回归(RSI超卖) → 买入
- backtrader趋势跟踪(MA死叉) → 清仓
- → 触发辩论 → 综合为"持有/观望"

## 信号定义

| 信号 | 含义 | 建议仓位 |
|------|------|---------|
| 买入 | 强烈买入信号 | 20-25% |
| 增持 | 建议增加持仓 | 15-20% |
| 持有 | 可继续持有 | 10-15% |
| 观望 | 谨慎观望 | 5-10% |
| 减持 | 建议减少持仓 | 0-5% |
| 清仓 | 建议卖出 | 0% |

## 评分维度

- **技术面 (40%)**：MA排列、RSI、量价配合
- **动量 (30%)**：1月/3月/1年涨跌
- **策略 (30%)**：回测超额收益、多项目一致性

## 依赖项目

请确保以下项目已克隆到 ~/github/：

- qlib - 微软量化研究平台
- backtrader - Python回测引擎
- FinRL - 强化学习量化交易
- freqtrade - 加密货币交易机器人
- vnpy - 国产量化交易框架

## 状态

当前版本：0.1.0 (开发中)

已实现：
- ✅ qlib适配器（技术分析、因子分析）
- ✅ backtrader适配器（MA双叉策略回测）
- ✅ 辩论引擎
- ✅ 聚合评分系统

待实现：
- ⏳ FinRL适配器
- ⏳ vnpy交易执行
- ⏳ Web界面
- ⏳ 微信/飞书推送
