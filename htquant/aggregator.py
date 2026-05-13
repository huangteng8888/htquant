"""
htquant 结果聚合器
将多个量化项目的分析结果聚合成单一综合结论

改进：
- 三项目加权投票（momentum权重提升）
- 加权多数裁定：只有当最高权重信号>50%总权重且≥1.5x次高时
  才跳过加权投票，否则加权投票决定最终信号
- 辩论只在加权投票后、置信度低或有严重冲突时触发
"""
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter
import numpy as np

from .dispatcher import ProjectResult, QueryType
from .config import SIGNALS, DEBATE_CONFIG

logger = logging.getLogger(__name__)


@dataclass
class Conflict:
    stock_code: str
    horizon: str
    project_a: str
    project_b: str
    signal_a: str
    signal_b: str
    reason_a: str
    reason_b: str
    severity: float = 1.0


@dataclass
class AggregatedResult:
    stock_code: str
    stock_name: str = ""
    
    signal_short: str = "观望"
    signal_medium: str = "观望"
    signal_long: str = "观望"
    
    position_weight: float = 0.0
    
    confidence: float = 0.0
    
    project_signals: Dict[str, str] = field(default_factory=dict)
    project_confidences: Dict[str, float] = field(default_factory=dict)
    
    conflicts: List[Conflict] = field(default_factory=list)
    
    reasons: List[str] = field(default_factory=list)
    
    evidence_strength: float = 0.0
    
    majority_signal: Optional[str] = None
    majority_count: int = 0


class Aggregator:
    """
    结果聚合器
    """
    
    SIGNAL_PRIORITY = {
        "买入": 5,
        "增持": 4,
        "持有": 3,
        "观望": 2,
        "减持": 1,
        "清仓": 0,
    }
    
    SIGNAL_WEIGHT = {
        "买入": 0.20,
        "增持": 0.15,
        "持有": 0.10,
        "观望": 0.05,
        "减持": 0.03,
        "清仓": 0.0,
    }
    
    # 项目权重（总和约 1.50，按 A股量化范式分配）
    # 原则：机器学习/回测 > 单一技术指标 > 辅助数据源
    # - qlib/backtrader: 基于真实历史数据回测，最可靠
    # - momentum: 趋势指标，但仅单一因子，权重适中
    # - lean/gs_quant: 量化引擎，有模型支撑
    # - finrl: DRL强化学习，实验性强
    # - vnpy: CTA策略，对个股信号有参考价值
    # - yanbao_reports/fincept/tradingagents: 辅助信息源
    # - freqtrade: 仅适用加密货币，A股最低权重
    PROJECT_WEIGHTS = {
        "qlib":                 0.22,
        "backtrader":           0.22,
        "momentum":             0.18,
        "lean":                 0.15,
        "gs_quant":             0.12,
        "finrl":                0.10,
        "vnpy":                 0.08,
        "yanbao_reports":       0.10,
        "financial_services":   0.08,
        "tradingagents":        0.08,
        "fincept":              0.10,
        "freqtrade":            0.05,
    }

    # 各 adapter 的典型最大置信度（用于 cross-adapter 归一化）
    # 格式: adapter → (typical_min, typical_max)
    # 归一化公式: norm = (raw - min) / (max - min)，结果clamp到[0.5, 1.0]
    ADAPTER_CONF_RANGES = {
        "qlib":                 (0.50, 0.80),
        "backtrader":           (0.55, 0.75),
        "momentum":             (0.60, 0.80),
        "finrl":                (0.55, 0.80),
        "freqtrade":            (0.50, 0.70),
        "vnpy":                 (0.50, 0.82),
        "yanbao_reports":       (0.55, 0.75),
        "financial_services":    (0.40, 0.70),
        "lean":                 (0.60, 0.88),
        "gs_quant":             (0.30, 0.70),
        "tradingagents":        (0.30, 0.80),
        "fincept":              (0.50, 0.80),
    }

    def _normalize_confidence(self, raw_conf: float, adapter: str) -> float:
        """将各 adapter 原始置信度归一化到 [0.5, 1.0] 统一标尺"""
        conf_min, conf_max = self.ADAPTER_CONF_RANGES.get(adapter, (0.50, 0.70))
        span = conf_max - conf_min
        if span <= 0:
            return 0.70
        # Min-max normalize to [0.5, 1.0]
        normalized = 0.5 + 0.5 * (raw_conf - conf_min) / span
        return max(0.55, min(1.0, normalized))  # 全局最低 0.55，避免 conf=0 导致完全无权重
    
    def __init__(self):
        self.conflicts = []
    
    def aggregate(
        self,
        results: Dict[str, ProjectResult],
        stock_code: str,
        horizon: str = "medium"
    ) -> AggregatedResult:
        """聚合单个股票的结果"""
        from .config import STOCK_CODE_MAPPING
        
        stock_name = STOCK_CODE_MAPPING.get(stock_code, (None, stock_code))[1]
        
        valid_results = {k: v for k, v in results.items() if v.success}
        
        if not valid_results:
            return AggregatedResult(stock_code=stock_code, stock_name=stock_name, confidence=0.0)
        
        signals = [r.signal for r in valid_results.values()]
        confidences = [r.confidence for r in valid_results.values()]
        
        # 检测加权多数裁定
        top_signal, top_weight, second_weight = self._check_weighted_majority(
            signals, confidences, valid_results
        )
        
        # 加权多数裁定条件：
        # 1. 最高权重信号 > 50% 总权重
        # 2. 最高权重 >= 1.5x 第二权重
        total_weight = sum(self.PROJECT_WEIGHTS.get(p, 0.2) for p in valid_results.keys())
        top_weight_ratio = top_weight / total_weight if total_weight > 0 else 0
        weight_ratio = top_weight / second_weight if second_weight > 0 else float('inf')
        
        use_majority = top_weight_ratio > 0.5 and weight_ratio >= 1.5
        
        if use_majority:
            final_signal = top_signal
            logger.info(
                f"[htquant/agg] {stock_code} 加权多数裁定: {top_signal} "
                f"(权重{top_weight_ratio*100:.0f}%, 第2名{weight_ratio:.2f}x)"
            )
        else:
            final_signal = self._weighted_vote(signals, confidences, valid_results)
            logger.info(
                f"[htquant/agg] {stock_code} 加权投票: {final_signal} "
                f"(最高{top_signal}仅{top_weight_ratio*100:.0f}%<50%或<1.5x)"
            )
        
        # 使用归一化置信度计算平均置信度
        norm_confs = [self._normalize_confidence(c, p) for p, c in zip(valid_results.keys(), confidences)]
        avg_confidence = np.mean(norm_confs) if norm_confs else 0.0
        
        # 检测冲突（用于辩论）
        conflicts = self._detect_conflicts(valid_results, stock_code, horizon)
        
        weight = self.SIGNAL_WEIGHT.get(final_signal, 0.05)
        
        reasons = []
        for project, result in valid_results.items():
            if result.reason:
                reasons.append(f"[{project}] {result.reason}")
        
        evidence_strength = self._calc_evidence_strength(signals)
        
        return AggregatedResult(
            stock_code=stock_code,
            stock_name=stock_name,
            signal_medium=final_signal,
            position_weight=weight,
            confidence=avg_confidence,
            project_signals={k: v.signal for k, v in valid_results.items()},
            project_confidences={k: self._normalize_confidence(v.confidence, k) for k, v in valid_results.items()},
            conflicts=conflicts,
            reasons=reasons,
            evidence_strength=evidence_strength,
            majority_signal=top_signal,
            majority_count=int(top_weight_ratio * len(valid_results)),
        )
    
    def _check_weighted_majority(
        self,
        signals: List[str],
        confidences: List[float],
        valid_results: Dict[str, ProjectResult]
    ) -> Tuple[str, float, float]:
        """
        检测加权多数裁定
        
        Returns:
            (最高信号, 最高权重, 第二高权重)
        """
        if not signals:
            return "观望", 0.0, 0.0
        
        # 按项目聚合权重（使用归一化置信度）
        signal_weights = {}
        for (project, result) in valid_results.items():
            sig = result.signal
            proj_weight = self.PROJECT_WEIGHTS.get(project, 0.2)
            raw_conf = result.confidence
            norm_conf = self._normalize_confidence(raw_conf, project)
            w = proj_weight * norm_conf
            signal_weights[sig] = signal_weights.get(sig, 0) + w
        
        if not signal_weights:
            return "观望", 0.0, 0.0
        
        # 排序
        sorted_signals = sorted(signal_weights.items(), key=lambda x: x[1], reverse=True)
        top_signal, top_weight = sorted_signals[0]
        second_weight = sorted_signals[1][1] if len(sorted_signals) > 1 else 0.0
        
        return top_signal, top_weight, second_weight
    
    def _weighted_vote(
        self,
        signals: List[str],
        confidences: List[float],
        valid_results: Dict[str, ProjectResult]
    ) -> str:
        """
        加权投票决定最终信号
        权重 = 项目权重 × 置信度 × 信号优先级
        """
        if not signals:
            return "观望"
        
        scores = Counter()
        for signal, raw_conf, (project, result) in zip(signals, confidences, valid_results.items()):
            project_weight = self.PROJECT_WEIGHTS.get(project, 0.2)
            norm_conf = self._normalize_confidence(raw_conf, project)
            # 优先级系数：增持×1.5, 观望×1.25, 减持/清仓×1.0
            # 轻微偏向做多，避免多空 adapter 数量不均时出现系统性偏差
            priority_map = {'增持': 1.5, '买入': 1.5, '持有': 1.25, '观望': 1.25, '减持': 1.0, '清仓': 1.0}
            priority = priority_map.get(signal, 1.25)
            scores[signal] += project_weight * norm_conf * priority
        
        if not scores:
            return "观望"
        
        return scores.most_common(1)[0][0]
    
    def _detect_conflicts(
        self,
        results: Dict[str, ProjectResult],
        stock_code: str,
        horizon: str
    ) -> List[Conflict]:
        """检测项目间的分析冲突"""
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
                
                if abs(priA - priB) >= 2:
                    severity = min(abs(priA - priB) / 4, 1.0)
                    
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
        """计算证据强度"""
        if not signals:
            return 0.0
        
        counter = Counter(signals)
        most_common_count = counter.most_common(1)[0][1]
        
        consistency = most_common_count / len(signals)
        
        avg_priority = np.mean([self.SIGNAL_PRIORITY.get(s, 2) for s in signals])
        intensity = avg_priority / 5.0
        
        return consistency * 0.6 + intensity * 0.4
    
    def need_debate(self, aggregated: AggregatedResult) -> bool:
        """
        判断是否需要触发辩论
        """
        # 无冲突则不辩论
        if not aggregated.conflicts:
            return False
        
        # 高严重性冲突
        has_severe = any(c.severity > 0.5 for c in aggregated.conflicts)
        
        # 置信度不足
        low_confidence = aggregated.confidence < DEBATE_CONFIG["confidence_threshold"]
        
        # 证据散乱
        weak_evidence = aggregated.evidence_strength < 0.4
        
        return has_severe or low_confidence or weak_evidence


_aggregator: Optional[Aggregator] = None

def get_aggregator() -> Aggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = Aggregator()
    return _aggregator
