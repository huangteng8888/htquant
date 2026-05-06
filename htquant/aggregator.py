"""
htquant 结果聚合器
将多个量化项目的分析结果聚合成单一综合结论
"""
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from collections import Counter
import numpy as np

from .dispatcher import ProjectResult, QueryType
from .config import SIGNALS, DEBATE_CONFIG

logger = logging.getLogger(__name__)


@dataclass
class Conflict:
    """分歧项"""
    stock_code: str
    horizon: str
    project_a: str
    project_b: str
    signal_a: str
    signal_b: str
    reason_a: str
    reason_b: str
    severity: float = 1.0   # 0~1, 分歧严重程度


@dataclass
class AggregatedResult:
    """聚合后的结果"""
    stock_code: str
    stock_name: str = ""
    
    # 各周期信号
    signal_short: str = "观望"
    signal_medium: str = "观望"
    signal_long: str = "观望"
    
    # 建议仓位
    position_weight: float = 0.0   # 0~1
    
    # 综合置信度
    confidence: float = 0.0
    
    # 各项目信号汇总
    project_signals: Dict[str, str] = field(default_factory=dict)
    project_confidences: Dict[str, float] = field(default_factory=dict)
    
    # 分歧列表（如果有）
    conflicts: List[Conflict] = field(default_factory=list)
    
    # 综合理由
    reasons: List[str] = field(default_factory=list)
    
    # 证据强度
    evidence_strength: float = 0.0   # 0~1


class Aggregator:
    """
    结果聚合器
    
    聚合策略：
    1. 简单投票：多数信号获胜
    2. 加权投票：根据项目置信度加权
    3. 冲突检测：发现分歧时标记并触发辩论
    """
    
    # 信号优先级（数值越高越积极）
    SIGNAL_PRIORITY = {
        "买入": 5,
        "增持": 4,
        "持有": 3,
        "观望": 2,
        "减持": 1,
        "清仓": 0,
    }
    
    # 仓位映射
    SIGNAL_WEIGHT = {
        "买入": 0.20,
        "增持": 0.15,
        "持有": 0.10,
        "观望": 0.05,
        "减持": 0.03,
        "清仓": 0.0,
    }
    
    def __init__(self):
        self.conflicts = []
    
    def aggregate(
        self,
        results: Dict[str, ProjectResult],
        stock_code: str,
        horizon: str = "medium"
    ) -> AggregatedResult:
        """
        聚合单个股票的结果
        
        Args:
            results: {项目名: ProjectResult}
            stock_code: 股票代码
            horizon: short/medium/long
        """
        from .config import STOCK_CODE_MAPPING
        
        stock_name = STOCK_CODE_MAPPING.get(stock_code, (None, stock_code))[1]
        
        # 收集有效结果
        valid_results = {k: v for k, v in results.items() if v.success}
        
        if not valid_results:
            return AggregatedResult(
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.0,
            )
        
        # 提取信号和置信度
        signals = [r.signal for r in valid_results.values()]
        confidences = [r.confidence for r in valid_results.values()]
        
        # 加权投票
        final_signal = self._weighted_vote(signals, confidences)
        
        # 计算综合置信度
        avg_confidence = np.mean(confidences) if confidences else 0.0
        
        # 检测冲突
        conflicts = self._detect_conflicts(valid_results, stock_code, horizon)
        
        # 计算仓位
        weight = self.SIGNAL_WEIGHT.get(final_signal, 0.05)
        
        # 收集理由
        reasons = []
        for project, result in valid_results.items():
            if result.reason:
                reasons.append(f"[{project}] {result.reason}")
        
        # 计算证据强度（基于有多少项目支持同一结论）
        evidence_strength = self._calc_evidence_strength(signals)
        
        return AggregatedResult(
            stock_code=stock_code,
            stock_name=stock_name,
            signal_medium=final_signal,  # 默认存中期
            position_weight=weight,
            confidence=avg_confidence,
            project_signals={k: v.signal for k, v in valid_results.items()},
            project_confidences={k: v.confidence for k, v in valid_results.items()},
            conflicts=conflicts,
            reasons=reasons,
            evidence_strength=evidence_strength,
        )
    
    def aggregate_all(
        self,
        all_results: Dict[str, Dict[str, ProjectResult]],
        horizons: List[str] = None
    ) -> List[AggregatedResult]:
        """
        聚合所有股票的结果
        
        Args:
            all_results: {股票代码: {项目名: ProjectResult}}
            horizons: ["short", "medium", "long"]
        """
        if horizons is None:
            horizons = ["short", "medium", "long"]
        
        aggregated = []
        
        for stock_code, results in all_results.items():
            # 简化：只取第一个horizon的聚合
            agg_result = self.aggregate(results, stock_code)
            
            # 设置各周期信号（这里简化处理）
            for horizon in horizons:
                signal_method = f"signal_{horizon}"
                if hasattr(agg_result, signal_method):
                    # 各周期用不同权重重新计算
                    pass
            
            aggregated.append(agg_result)
        
        return aggregated
    
    def _weighted_vote(self, signals: List[str], confidences: List[float]) -> str:
        """
        加权投票决定最终信号
        
        每个项目的投票权重 = 置信度
        """
        if not signals:
            return "观望"
        
        # 计算加权得分
        scores = Counter()
        for signal, conf in zip(signals, confidences):
            priority = self.SIGNAL_PRIORITY.get(signal, 2)
            scores[signal] += conf * priority
        
        if not scores:
            return "观望"
        
        # 返回得分最高的信号
        return scores.most_common(1)[0][0]
    
    def _detect_conflicts(
        self,
        results: Dict[str, ProjectResult],
        stock_code: str,
        horizon: str
    ) -> List[Conflict]:
        """
        检测项目间的分析冲突
        
        冲突定义：两个项目给出相反信号（买入vs清仓，或差值超过2档）
        """
        conflicts = []
        project_names = list(results.keys())
        
        for i in range(len(project_names)):
            for j in range(i + 1, len(project_names)):
                pA = project_names[i]
                pB = project_names[j]
                rA = results[pA]
                rB = results[pB]
                
                priA = self.SIGNAL_PRIORITY.get(rA.signal, 2)
                priB = self.SIGNAL_PRIORITY.get(rB.signal, 2)
                
                # 信号差值超过2档视为冲突
                if abs(priA - priB) >= 2:
                    severity = min(abs(priA - priB) / 4, 1.0)  # 归一化到0~1
                    
                    conflict = Conflict(
                        stock_code=stock_code,
                        horizon=horizon,
                        project_a=pA,
                        project_b=pB,
                        signal_a=rA.signal,
                        signal_b=rB.signal,
                        reason_a=rA.reason,
                        reason_b=rB.reason,
                        severity=severity,
                    )
                    conflicts.append(conflict)
                    logger.info(
                        f"[htquant] 检测到冲突: {stock_code} "
                        f"{pA}({rA.signal}) vs {pB}({rB.signal})"
                    )
        
        return conflicts
    
    def _calc_evidence_strength(self, signals: List[str]) -> float:
        """
        计算证据强度
        
        基于信号一致性：所有项目一致则强度最高，散则降低
        """
        if not signals:
            return 0.0
        
        counter = Counter(signals)
        most_common_count = counter.most_common(1)[0][1]
        
        # 一致性 = 最常见信号的比例
        consistency = most_common_count / len(signals)
        
        # 考虑信号强度：如果都是买入/增持等强信号，强度更高
        avg_priority = np.mean([self.SIGNAL_PRIORITY.get(s, 2) for s in signals])
        intensity = avg_priority / 5.0  # 归一化
        
        return consistency * 0.6 + intensity * 0.4
    
    def need_debate(self, aggregated: AggregatedResult) -> bool:
        """
        判断是否需要触发辩论
        
        条件：
        1. 存在高严重性冲突 (severity > 0.5)
        2. 置信度低于阈值
        3. 证据强度不足
        """
        if not aggregated.conflicts:
            return False
        
        # 有高严重性冲突
        has_severe = any(c.severity > 0.5 for c in aggregated.conflicts)
        
        # 置信度不足
        low_confidence = aggregated.confidence < DEBATE_CONFIG["confidence_threshold"]
        
        # 证据散乱
        weak_evidence = aggregated.evidence_strength < 0.4
        
        return has_severe or low_confidence or weak_evidence


# 全局聚合器
_aggregator: Optional[Aggregator] = None


def get_aggregator() -> Aggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = Aggregator()
    return _aggregator
