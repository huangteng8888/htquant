"""
htquant 辩论引擎 混合模式

策略：一对多广播 -> 一对一收敛
1. 先运行v3（一对多广播）：所有项目同时看到全局
2. 如果v3未收敛，对残留冲突运行v1（一对一）
3. 一对一使用v3当前的演化信号，而非原始信号

作者: htquant
"""

import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

from .dispatcher import ProjectResult
from .aggregator import Conflict, Aggregator

from . import debate_v2 as v3module
from . import debate as v1module
from . import debate_truth as truth_module

logger = logging.getLogger(__name__)


@dataclass
class HybridDebateResult:
    """混合辩论结果"""
    stock_code: str
    horizon: str

    # v3（一对多）结果
    v3_result: Optional[v3module.DebateResult] = None

    # v1（一对一）结果列表（v3未收敛时触发）
    v1_results: List[v1module.DebateResult] = field(default_factory=list)

    # 最终信号（综合v3和v1）
    final_signal: str = "观望"
    final_confidence: float = 0.0
    converged: bool = False
    converged_method: str = ""  # "v3" or "v1"

    def summary(self) -> str:
        parts = []
        if self.v3_result:
            parts.append(f"v3({self.v3_result.converged})")
        if self.v1_results:
            parts.append(f"v1({len(self.v1_results)} debates)")
        method = f"({self.converged_method})" if self.converged else "(未收敛)"
        return f"HybridDebateResult {self.stock_code}: {self.final_signal} {method} [{', '.join(parts)}]"


class HybridDebateEngine:
    """
    混合辩论引擎

    流程：
    1. v3广播辩论（所有项目同时看到全局）
    2. v3收敛 → 完成
    3. v3未收敛 → 从v3当前状态启动v1一对一辩论
    4. v1收敛 → 更新最终信号
    5. v1未收敛 → 用保守策略确定最终信号
    """

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.v3_engine = v3module.DebateEngineV3(dispatcher)
        self.v1_engine = v1module.DebateEngine(dispatcher)
        self.aggregator = Aggregator()

    def debate(
        self,
        stock_code: str,
        horizon: str,
        all_results: Dict[str, ProjectResult],
        initial_signals: Dict[str, str],
        initial_reasons: Dict[str, str],
        extreme_event_type: Optional[str] = None,
    ) -> HybridDebateResult:
        """
        运行混合辩论 — 真理越辨越明版

        优先使用 V4 真理辩论引擎（基于论据强度的裁决机制）。
        V3/V1 作为备用（当 V4 无法处理时）。
        """
        result = HybridDebateResult(stock_code=stock_code, horizon=horizon)

        logger.info(f"[htquant/hybrid] === 开始真理辩论: {stock_code} ===")

        # ===== 阶段1: V4 真理辩论（首选）=====
        truth_res = truth_module.run_truth_debate(
            stock_code=stock_code,
            horizon=horizon,
            all_results=all_results,
            initial_signals=initial_signals,
            initial_reasons=initial_reasons,
            extreme_event_type=extreme_event_type,
        )

        result.final_signal     = truth_res.final_signal
        result.final_confidence = truth_res.final_confidence
        result.converged        = truth_res.converged
        result.converged_method = f"truth_v4(winner={truth_res.winner})"

        logger.info(
            f"[htquant/hybrid] {stock_code} V4真理辩论: "
            f"{truth_res.final_signal} (胜者:{truth_res.winner} 置信:{truth_res.final_confidence:.0%})"
        )

        return result

    def _detect_residual_conflicts(
        self,
        signals: Dict[str, str],
        reasons: Dict[str, str],
        stock_code: str,
        horizon: str,
    ) -> List[Conflict]:
        """检测残留冲突（基于当前信号）"""
        priorities = Aggregator.SIGNAL_PRIORITY
        projects = list(signals.keys())
        conflicts = []

        for i in range(len(projects)):
            for j in range(i + 1, len(projects)):
                proj_a, proj_b = projects[i], projects[j]
                sig_a, sig_b = signals[proj_a], signals[proj_b]
                diff = abs(priorities.get(sig_a, 2) - priorities.get(sig_b, 2))

                if diff >= 2:
                    conflicts.append(Conflict(
                        stock_code=stock_code,
                        horizon=horizon,
                        project_a=proj_a,
                        project_b=proj_b,
                        signal_a=sig_a,
                        signal_b=sig_b,
                        reason_a=reasons.get(proj_a, ""),
                        reason_b=reasons.get(proj_b, ""),
                        severity=diff,
                    ))

        return conflicts
