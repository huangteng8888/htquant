"""
htquant 辩论引擎
多项目结论冲突时，触发多轮辩论机制
"""
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from collections import namedtuple
import json

from .dispatcher import Dispatcher, ProjectResult, Query, QueryType
from .aggregator import Aggregator, AggregatedResult, Conflict
from .config import DEBATE_CONFIG

logger = logging.getLogger(__name__)

# 辩论消息
DebateMessage = namedtuple("DebateMessage", ["round", "project", "content", "verdict"])


@dataclass
class DebateRound:
    """单轮辩论"""
    round_num: int
    initiator: str                          # 发起方
    opponent: str                           # 回应方
    conflict: Conflict                      # 争议点
    messages: List[DebateMessage] = field(default_factory=list)
    consensus_reached: bool = False
    final_verdict: Optional[str] = None


@dataclass
class DebateResult:
    """辩论结果"""
    stock_code: str
    horizon: str
    conflict: Conflict
    
    rounds: List[DebateRound] = field(default_factory=list)
    
    # 辩论后的最终信号
    final_signal: str = "观望"
    final_confidence: float = 0.0
    
    # 是否达成共识
    consensus: bool = False
    
    # 详细日志
    debate_log: str = ""


class DebateEngine:
    """
    多轮辩论引擎
    
    辩论流程：
    1. 接收冲突信息
    2. 让双方各自陈述论据
    3. 将一方论点传给另一方让其重新评估
    4. 多轮迭代直到收敛或达到最大轮数
    5. 输出综合结论
    """
    
    def __init__(self, dispatcher: Dispatcher, aggregator: Aggregator):
        self.dispatcher = dispatcher
        self.aggregator = aggregator
        self.max_rounds = DEBATE_CONFIG["max_rounds"]
        self.confidence_threshold = DEBATE_CONFIG["confidence_threshold"]
        self.convergence_window = DEBATE_CONFIG["convergence_window"]
    
    def debate(self, conflict: Conflict) -> DebateResult:
        """
        对单个冲突进行多轮辩论
        
        Args:
            conflict: Conflict对象
            
        Returns:
            DebateResult
        """
        result = DebateResult(
            stock_code=conflict.stock_code,
            horizon=conflict.horizon,
            conflict=conflict,
        )
        
        logger.info(
            f"[htquant/debate] 开始辩论: {conflict.stock_code} "
            f"{conflict.project_a}({conflict.signal_a}) vs "
            f"{conflict.project_b}({conflict.signal_b})"
        )
        
        # 轮次历史（用于检测收敛）
        signal_history_a = []
        signal_history_b = []
        
        for round_num in range(1, self.max_rounds + 1):
            logger.info(f"[htquant/debate] 第 {round_num} 轮")
            
            debate_round = DebateRound(
                round_num=round_num,
                initiator=conflict.project_a,
                opponent=conflict.project_b,
                conflict=conflict,
            )
            
            # 第一轮：双方各自陈述
            if round_num == 1:
                # A方陈述
                msg_a = self._get_statement(
                    project_name=conflict.project_a,
                    signal=conflict.signal_a,
                    reason=conflict.reason_a,
                    opponent_signal=conflict.signal_b,
                    opponent_reason=conflict.reason_b,
                    round_num=round_num,
                )
                debate_round.messages.append(msg_a)
                
                # B方陈述
                msg_b = self._get_statement(
                    project_name=conflict.project_b,
                    signal=conflict.signal_b,
                    reason=conflict.reason_b,
                    opponent_signal=conflict.signal_a,
                    opponent_reason=conflict.reason_a,
                    round_num=round_num,
                )
                debate_round.messages.append(msg_b)
            
            # 后续轮次：考虑对方论点后重新评估
            else:
                # A方重新评估（考虑B方的论点）
                msg_a = self._rebuttal(
                    project_name=conflict.project_a,
                    my_signal=signal_history_a[-1] if signal_history_a else conflict.signal_a,
                    my_reason=conflict.reason_a,
                    opponent_signal=signal_history_b[-1] if signal_history_b else conflict.signal_b,
                    opponent_reason=conflict.reason_b,
                    round_num=round_num,
                )
                debate_round.messages.append(msg_a)
                
                # B方重新评估
                msg_b = self._rebuttal(
                    project_name=conflict.project_b,
                    my_signal=signal_history_b[-1] if signal_history_b else conflict.signal_b,
                    my_reason=conflict.reason_b,
                    opponent_signal=signal_history_a[-1] if signal_history_a else conflict.signal_a,
                    opponent_reason=conflict.reason_a,
                    round_num=round_num,
                )
                debate_round.messages.append(msg_b)
            
            # 记录本轮信号
            signal_history_a.append(msg_a.verdict)
            signal_history_b.append(msg_b.verdict)
            
            # 检查是否收敛
            if round_num >= 2:
                if self._check_convergence(signal_history_a, signal_history_b):
                    debate_round.consensus_reached = True
                    debate_round.final_verdict = signal_history_a[-1]
                    logger.info(
                        f"[htquant/debate] {conflict.stock_code} 第{round_num}轮收敛: "
                        f"{signal_history_a[-1]}"
                    )
                    break
            
            result.rounds.append(debate_round)
        
        # 最终判决：取最后一轮的多数或加权结果
        if signal_history_a and signal_history_b:
            final_signals = [signal_history_a[-1], signal_history_b[-1]]
            
            # 简单：取更保守的信号
            priorities = Aggregator.SIGNAL_PRIORITY
            final_signal = min(
                final_signals,
                key=lambda s: abs(priorities.get(s, 2) - 2.5)  # 靠近"持有"
            )
            
            result.final_signal = final_signal
            result.final_confidence = 0.7  # 辩论后置信度提高
            result.consensus = (
                debate_round.consensus_reached or
                abs(priorities.get(signal_history_a[-1], 2) - priorities.get(signal_history_b[-1], 2)) <= 1
            )
        
        # 生成辩论日志
        result.debate_log = self._generate_log(result)
        
        return result
    
    def debate_all(
        self,
        conflicts: List[Conflict],
        existing_results: Dict[str, ProjectResult] = None
    ) -> List[DebateResult]:
        """对所有冲突进行辩论"""
        results = []
        
        for conflict in conflicts:
            debate_result = self.debate(conflict)
            results.append(debate_result)
        
        return results
    
    def _get_statement(
        self,
        project_name: str,
        signal: str,
        reason: str,
        opponent_signal: str,
        opponent_reason: str,
        round_num: int,
    ) -> DebateMessage:
        """生成陈述"""
        template = (
            f"[{project_name}] 我认为应 '{signal}'，理由：{reason}。"
            f"对方({opponent_signal})的理由是：{opponent_reason}。"
        )
        
        return DebateMessage(
            round=round_num,
            project=project_name,
            content=template,
            verdict=signal,  # 第一轮保持原判断
        )
    
    def _rebuttal(
        self,
        project_name: str,
        my_signal: str,
        my_reason: str,
        opponent_signal: str,
        opponent_reason: str,
        round_num: int,
    ) -> DebateMessage:
        """
        生成反驳/重新评估
        
        策略：
        - 如果对方信号更乐观，且有合理依据，则考虑调整
        - 如果对方信号过于乐观/悲观，则坚持己见
        """
        from .aggregator import Aggregator
        
        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)
        opp_priority = priorities.get(opponent_signal, 2)
        
        # 评估对方论点的合理性
        opp_diff = abs(opp_priority - 2.5)  # 偏离"持有"的程度
        
        # 如果对方信号更极端，且本方信号适中，则可能调整
        if opp_diff > my_priority - 2.5:
            # 对方过于极端，不采纳
            new_signal = my_signal
            adjustment_note = "对方信号过于极端，不采纳。"
        else:
            # 双方接近，考虑折中
            new_signal = "持有"
            adjustment_note = f"综合考虑，调整为 '持有'。"
        
        content = (
            f"[{project_name}] 第{round_num}轮重新评估："
            f"我的原判断 '{my_signal}'，理由：{my_reason}。"
            f"对方第{round_num-1}轮主张 '{opponent_signal}'，理由：{opponent_reason}。"
            f"{adjustment_note}"
        )
        
        return DebateMessage(
            round=round_num,
            project=project_name,
            content=content,
            verdict=new_signal,
        )
    
    def _check_convergence(
        self,
        history_a: List[str],
        history_b: List[str],
    ) -> bool:
        """
        检查是否收敛
        
        收敛条件：连续convergence_window轮信号一致
        """
        if len(history_a) < self.convergence_window:
            return False
        
        window = self.convergence_window
        recent_a = history_a[-window:]
        recent_b = history_b[-window:]
        
        # 双方最后一轮信号接近（差值<=1档）
        priorities = Aggregator.SIGNAL_PRIORITY
        diff = abs(
            priorities.get(recent_a[-1], 2) - priorities.get(recent_b[-1], 2)
        )
        
        return diff <= 1
    
    def _generate_log(self, result: DebateResult) -> str:
        """生成辩论日志"""
        lines = [
            f"=== 辩论日志: {result.stock_code} ({result.horizon}) ===",
            f"冲突: {result.conflict.project_a}({result.conflict.signal_a}) "
            f"vs {result.conflict.project_b}({result.conflict.signal_b})",
            f"共识: {'是' if result.consensus else '否'}",
            f"最终信号: {result.final_signal}",
            "",
        ]
        
        for round_obj in result.rounds:
            lines.append(f"--- 第 {round_obj.round_num} 轮 ---")
            for msg in round_obj.messages:
                lines.append(f"  [{msg.project}] {msg.content}")
                lines.append(f"    -> 判决: {msg.verdict}")
            if round_obj.consensus_reached:
                lines.append(f"  [收敛] 达成共识: {round_obj.final_verdict}")
            lines.append("")
        
        return "\n".join(lines)


# 全局辩论引擎
_debate_engine: Optional[DebateEngine] = None


def get_debate_engine() -> DebateEngine:
    global _debate_engine
    if _debate_engine is None:
        from .dispatcher import get_dispatcher
        from .aggregator import get_aggregator
        _debate_engine = DebateEngine(get_dispatcher(), get_aggregator())
    return _debate_engine
