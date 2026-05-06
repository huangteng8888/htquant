"""
htquant 评分系统
综合多项目结论，给出最终评分和排序
"""
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
import numpy as np

from .aggregator import AggregatedResult
from .dispatcher import ProjectResult

logger = logging.getLogger(__name__)


@dataclass
class StockScore:
    """个股评分"""
    stock_code: str
    stock_name: str
    
    # 综合得分 (0~100)
    composite_score: float = 0.0
    
    # 各维度得分
    technical_score: float = 0.0     # 技术面
    momentum_score: float = 0.0      # 动量
    strategy_score: float = 0.0      # 策略得分
    confidence_score: float = 0.0    # 置信度
    
    # 信号
    signal: str = "观望"
    
    # 建议仓位
    position_weight: float = 0.0
    
    # 详细评分理由
    factors: Dict[str, float] = field(default_factory=dict)
    
    # 综合结论
    summary: str = ""


class ScoringEngine:
    """
    评分引擎
    
    评分维度：
    1. 技术面得分 (40%)
       - MA多头排列 +10
       - RSI适中 +10
       - 量价配合 +10
       - 趋势强度 +10
    
    2. 动量得分 (30%)
       - 短期动量 (1月涨跌)
       - 中期动量 (3月涨跌)
       - 长期动量 (1年涨跌)
    
    3. 策略得分 (30%)
       - backtrader回测超额收益
       - 多项目一致性
    """
    
    # 评分权重
    WEIGHTS = {
        "technical": 0.40,
        "momentum": 0.30,
        "strategy": 0.30,
    }
    
    def __init__(self):
        pass
    
    def score(
        self,
        aggregated: AggregatedResult,
        technical_data: Dict[str, Any] = None,
        backtest_data: Dict[str, Any] = None,
    ) -> StockScore:
        """
        对个股进行综合评分
        
        Args:
            aggregated: 聚合结果
            technical_data: 技术分析原始数据
            backtest_data: 回测数据
        """
        score_obj = StockScore(
            stock_code=aggregated.stock_code,
            stock_name=aggregated.stock_name,
            signal=aggregated.signal_medium,
            position_weight=aggregated.position_weight,
        )
        
        # 1. 技术面得分
        tech_score = self._calc_technical_score(technical_data)
        score_obj.technical_score = tech_score
        score_obj.factors["technical"] = tech_score
        
        # 2. 动量得分
        momentum_score = self._calc_momentum_score(technical_data)
        score_obj.momentum_score = momentum_score
        score_obj.factors["momentum"] = momentum_score
        
        # 3. 策略得分（基于回测和项目一致性）
        strat_score = self._calc_strategy_score(aggregated, backtest_data)
        score_obj.strategy_score = strat_score
        score_obj.factors["strategy"] = strat_score
        
        # 4. 置信度得分
        confidence_score = aggregated.confidence * 100
        score_obj.confidence_score = confidence_score
        score_obj.factors["confidence"] = confidence_score
        
        # 综合得分
        composite = (
            tech_score * self.WEIGHTS["technical"] +
            momentum_score * self.WEIGHTS["momentum"] +
            strat_score * self.WEIGHTS["strategy"]
        )
        
        # 根据置信度调整
        composite *= (0.5 + 0.5 * aggregated.confidence)
        
        score_obj.composite_score = min(100, max(0, composite))
        
        # 生成总结
        score_obj.summary = self._generate_summary(score_obj)
        
        return score_obj
    
    def _calc_technical_score(self, data: Dict[str, Any]) -> float:
        """计算技术面得分"""
        if not data:
            return 50.0  # 无数据返回中性
        
        score = 50.0
        
        # MA多头排列
        if data.get("ma_bullish", False):
            score += 10
        
        # RSI
        rsi = data.get("rsi", 50)
        if 40 <= rsi <= 60:
            score += 10  # 适中
        elif rsi < 30:
            score += 5   # 超卖，可能反弹
        elif rsi > 70:
            score -= 5   # 超买
        
        # 量价配合
        vol_ratio = data.get("volume_ratio", 1.0)
        if vol_ratio > 1.2:
            score += 10
        elif vol_ratio < 0.5:
            score -= 5
        
        # 趋势
        trend = data.get("trend", "neutral")
        if trend == "up":
            score += 10
        elif trend == "down":
            score -= 10
        
        return min(100, max(0, score))
    
    def _calc_momentum_score(self, data: Dict[str, Any]) -> float:
        """计算动量得分"""
        if not data:
            return 50.0
        
        # 各周期涨跌
        p_1m = data.get("pct_1month", 0)
        p_3m = data.get("pct_3month", 0)
        p_1y = data.get("pct_1year", 0)
        
        # 动量评分：使用相对强度
        # 短期权重更高
        momentum = p_1m * 0.5 + p_3m * 0.3 + p_1y * 0.2
        
        # 转换为0~100分
        # 假设 +20% = 80分, 0% = 50分, -20% = 20分
        score = 50 + momentum * 1.5
        
        return min(100, max(0, score))
    
    def _calc_strategy_score(
        self,
        aggregated: AggregatedResult,
        backtest_data: Dict[str, Any]
    ) -> float:
        """计算策略得分"""
        if not aggregated.project_signals:
            return 50.0
        
        score = 50.0
        
        # 1. 回测超额收益
        if backtest_data:
            excess = backtest_data.get("excess_return", 0)
            # +20%超额 -> 80分, 0% -> 50分, -20% -> 20分
            score = 50 + excess * 1.5
        
        # 2. 项目一致性
        signals = list(aggregated.project_signals.values())
        if len(signals) >= 2:
            # 一致性因子
            from .aggregator import Aggregator
            priorities = [Aggregator.SIGNAL_PRIORITY.get(s, 2) for s in signals]
            std = np.std(priorities)
            
            # 标准差越小，一致性越高
            consistency = max(0, 1 - std / 2)
            score = score * 0.7 + consistency * 30
        
        # 3. 证据强度
        evidence = aggregated.evidence_strength
        score = score * 0.8 + evidence * 20
        
        return min(100, max(0, score))
    
    def _generate_summary(self, score: StockScore) -> str:
        """生成评分总结"""
        signal_cn = {
            "买入": "强烈推荐",
            "增持": "建议增持",
            "持有": "可继续持有",
            "观望": "建议观望",
            "减持": "建议减持",
            "清仓": "建议清仓",
        }
        
        action = signal_cn.get(score.signal, "待观察")
        
        lines = [
            f"{score.stock_name}({score.stock_code}): {action}",
            f"  综合评分: {score.composite_score:.1f}/100",
            f"  技术面: {score.technical_score:.1f} | 动量: {score.momentum_score:.1f} | 策略: {score.strategy_score:.1f}",
            f"  置信度: {score.confidence_score:.1f}%",
            f"  建议仓位: {score.position_weight*100:.1f}%",
        ]
        
        return "\n".join(lines)
    
    def rank(self, scores: List[StockScore]) -> List[StockScore]:
        """按综合得分排序"""
        return sorted(scores, key=lambda s: s.composite_score, reverse=True)


# 全局评分引擎
_scoring_engine: Optional[ScoringEngine] = None


def get_scoring_engine() -> ScoringEngine:
    global _scoring_engine
    if _scoring_engine is None:
        _scoring_engine = ScoringEngine()
    return _scoring_engine
