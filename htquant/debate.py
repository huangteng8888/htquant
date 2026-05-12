"""
htquant 辩论引擎 v2

核心思路：分歧不是投票权不够，而是信息差。
让分歧各方看到对方的证据，在自己的体系内重新评估，
分歧逐步消亡。

设计原则：
1. 辩论是"带有上下文的重新评估"，不是独立裁判
2. 每轮辩论后，各方信号应趋向收敛（因为看到了对方的证据）
3. 收敛条件：各方信号差值<=1档，或达到最大轮数
4. 最终信号 = 收敛后的信号，或最后一轮的加权平均（保守）
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
import numpy as np

from .dispatcher import ProjectResult, Query
from .aggregator import Conflict, Aggregator

logger = logging.getLogger(__name__)


@dataclass
class DebateMessage:
    round_num: int
    project: str
    original_signal: str
    revised_signal: str
    original_reason: str
    revised_reason: str
    how_other_evidence_changed_mind: str  # 关键：对方证据如何改变了我自己的判断


@dataclass
class DebateRound:
    round_num: int
    messages: List[DebateMessage] = field(default_factory=list)


@dataclass  
class DebateResult:
    stock_code: str
    horizon: str
    initial_conflicts: List[Conflict]
    rounds: List[DebateRound] = field(default_factory=list)
    
    # 最终结果
    final_signals: Dict[str, str] = field(default_factory=dict)  # project -> final signal
    converged: bool = False
    final_signal: str = "观望"
    final_confidence: float = 0.0
    
    def to_log(self) -> str:
        lines = [f"=== 辩论日志: {self.stock_code} ==="]
        for r in self.rounds:
            lines.append(f"--- 第 {r.round_num} 轮 ---")
            for m in r.messages:
                if m.revised_signal != m.original_signal:
                    lines.append(f"  [{m.project}] {m.original_signal} -> {m.revised_signal}")
                    lines.append(f"    理由进化: {m.how_other_evidence_changed_mind}")
                else:
                    lines.append(f"  [{m.project}] 维持: {m.original_signal}")
                    lines.append(f"    理由: {m.how_other_evidence_changed_mind}")
        if self.converged:
            lines.append(f"收敛！最终信号: {self.final_signal}")
        else:
            lines.append(f"未收敛，最终信号: {self.final_signal}")
        return "\n".join(lines)


class DebateEngine:
    """
    辩论引擎 v2
    
    每轮流程：
    1. 识别冲突对 (project_a, project_b)
    2. 让a看到b的证据后重新评估
    3. 让b看到a的证据后重新评估
    4. 检测是否收敛
    
    关键方法：
    - re_evaluate_with_opponent(): 让某项目在看对方证据后重新评估
    - 不同项目有不同的"重新评估策略"
    """
    
    # 辩论最大轮数
    MAX_ROUNDS = 3
    
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.aggregator = Aggregator()
    
    def debate(self, conflict: Conflict, all_results: Dict[str, ProjectResult]) -> DebateResult:
        """
        对单个冲突进行辩论
        
        all_results: 该股票所有项目的当前结果（含本轮可能已修正的结果）
        """
        result = DebateResult(
            stock_code=conflict.stock_code,
            horizon=conflict.horizon,
            initial_conflicts=[conflict],
        )
        
        logger.info(
            f"[htquant/debate] 开始辩论: {conflict.stock_code} "
            f"{conflict.project_a}({conflict.signal_a}) vs "
            f"{conflict.project_b}({conflict.signal_b})"
        )
        
        # 当前各方信号状态（每轮可能更新）
        current_signals = {
            conflict.project_a: conflict.signal_a,
            conflict.project_b: conflict.signal_b,
        }
        current_reasons = {
            conflict.project_a: conflict.reason_a,
            conflict.project_b: conflict.reason_b,
        }
        current_results = {
            conflict.project_a: all_results.get(conflict.project_a),
            conflict.project_b: all_results.get(conflict.project_b),
        }
        
        for round_num in range(1, self.MAX_ROUNDS + 1):
            round_obj = DebateRound(round_num=round_num)
            
            # A看B的证据后重新评估
            msg_a = self._re_evaluate_one_side(
                project=conflict.project_a,
                my_signal=current_signals[conflict.project_a],
                my_reason=current_reasons[conflict.project_a],
                my_result=current_results[conflict.project_a],
                opponent_project=conflict.project_b,
                opponent_signal=current_signals[conflict.project_b],
                opponent_reason=current_reasons[conflict.project_b],
                opponent_result=current_results[conflict.project_b],
                round_num=round_num,
            )
            round_obj.messages.append(msg_a)
            current_signals[conflict.project_a] = msg_a.revised_signal
            current_reasons[conflict.project_a] = msg_a.revised_reason
            
            # B看A的证据后重新评估
            msg_b = self._re_evaluate_one_side(
                project=conflict.project_b,
                my_signal=current_signals[conflict.project_b],
                my_reason=current_reasons[conflict.project_b],
                my_result=current_results[conflict.project_b],
                opponent_project=conflict.project_a,
                opponent_signal=current_signals[conflict.project_a],
                opponent_reason=current_reasons[conflict.project_a],
                opponent_result=current_results[conflict.project_a],
                round_num=round_num,
            )
            round_obj.messages.append(msg_b)
            current_signals[conflict.project_b] = msg_b.revised_signal
            current_reasons[conflict.project_b] = msg_b.revised_reason
            
            result.rounds.append(round_obj)
            
            # 检测收敛
            diff = abs(
                Aggregator.SIGNAL_PRIORITY.get(current_signals[conflict.project_a], 2) -
                Aggregator.SIGNAL_PRIORITY.get(current_signals[conflict.project_b], 2)
            )
            if diff <= 1:
                result.converged = True
                logger.info(
                    f"[htquant/debate] {conflict.stock_code} 第{round_num}轮收敛: "
                    f"{current_signals[conflict.project_a]} vs {current_signals[conflict.project_b]}"
                )
                break
        
        result.final_signals = current_signals
        
        # 确定最终信号：取更接近"持有"的保守信号
        priorities = Aggregator.SIGNAL_PRIORITY
        final_sig = min(
            current_signals.values(),
            key=lambda s: abs(priorities.get(s, 2) - 2.5)
        )
        result.final_signal = final_sig
        
        # 置信度：如果收敛则高，否则中等
        result.final_confidence = 0.85 if result.converged else 0.65
        
        return result
    
    def debate_all(
        self,
        conflicts: List[Conflict],
        all_results: Dict[str, ProjectResult]
    ) -> List[DebateResult]:
        """对所有冲突进行辩论"""
        # 按股票分组处理
        by_stock = {}
        for c in conflicts:
            if c.stock_code not in by_stock:
                by_stock[c.stock_code] = []
            by_stock[c.stock_code].append(c)
        
        results = []
        for stock_code, stock_conflicts in by_stock.items():
            stock_results = all_results.get(stock_code, {})
            for conflict in stock_conflicts:
                dr = self.debate(conflict, stock_results)
                results.append(dr)
        
        return results
    
    def _re_evaluate_one_side(
        self,
        project: str,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponent_project: str,
        opponent_signal: str,
        opponent_reason: str,
        opponent_result: Optional[ProjectResult],
        round_num: int,
    ) -> DebateMessage:
        """
        核心方法：让某一方在看对方证据后重新评估自己的判断
        
        每种项目有自己的"重新评估策略"：
        - qlib（均值回归）：RSI超买超卖是核心信号，但看到动量强会修正
        - momentum（动量）：趋势强度是核心，但看到RSI极端高会谨慎
        - backtrader（趋势跟踪）：MA叉是核心，但看到策略跑输会承认失效
        """
        
        if project == "qlib":
            revised_signal, revised_reason, change_note = self._qlib_re_evaluate(
                my_signal, my_reason, my_result,
                opponent_project,
                opponent_signal, opponent_reason, opponent_result
            )
        elif project == "momentum":
            revised_signal, revised_reason, change_note = self._momentum_re_evaluate(
                my_signal, my_reason, my_result,
                opponent_project,
                opponent_signal, opponent_reason, opponent_result
            )
        elif project == "backtrader":
            revised_signal, revised_reason, change_note = self._backtrader_re_evaluate(
                my_signal, my_reason, my_result,
                opponent_project,
                opponent_signal, opponent_reason, opponent_result
            )
        else:
            # 未知项目：简单中间化
            priorities = Aggregator.SIGNAL_PRIORITY
            diff = abs(
                priorities.get(my_signal, 2) - priorities.get(opponent_signal, 2)
            )
            if diff >= 2:
                revised_signal = "持有"
                revised_reason = f"与对方({opponent_signal})分歧大，取中间"
            else:
                revised_signal = my_signal
            change_note = revised_reason
        
        if revised_signal != my_signal:
            logger.info(
                f"[htquant/debate]   {project}: {my_signal} -> {revised_signal} "
                f"(看{opponent_project}的{opponent_signal}后)"
            )
        
        return DebateMessage(
            round_num=round_num,
            project=project,
            original_signal=my_signal,
            revised_signal=revised_signal,
            original_reason=my_reason,
            revised_reason=revised_reason or change_note,
            how_other_evidence_changed_mind=change_note,
        )
    
    def _qlib_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponent_project: str,
        opponent_signal: str,
        opponent_reason: str,
        opponent_result: Optional[ProjectResult],
    ) -> Tuple[str, str, str]:
        """
        qlib重新评估策略（均值回归派）
        
        qlib的核心信号：RSI超买(>70)→减持，RSI超卖(<30)→增持
        但如果动量/趋势信号强，会修正自己的判断
        """
        import re
        
        # 提取RSI
        rsi_match = re.search(r'RSI[=:]?(\d+\.?\d*)', my_reason)
        rsi = float(rsi_match.group(1)) if rsi_match else 50.0
        
        # 提取涨跌（月）
        pct_match = re.search(r'涨跌[=:]?([+-]?\d+\.?\d*)%', my_reason)
        pct_change = float(pct_match.group(1)) if pct_match else 0.0
        
        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)
        opp_priority = priorities.get(opponent_signal, 2)
        
        # qlib看momentum（动量派）的证据
        if opponent_project == "momentum" or opponent_signal in ["增持", "买入"]:
            # momentum说增持/买入 → 趋势可能很强
            # 检查momentum的动量强度
            mom_score_match = re.search(r'动量评分[=:]?(\d+\.?\d*)', opponent_reason)
            mom_score = float(mom_score_match.group(1)) if mom_score_match else 0.0
            
            if "增持" in opponent_signal or "买入" in opponent_signal:
                if my_signal in ["减持", "清仓"]:
                    # qlib看空，但momentum看多
                    # 检查RSI严重程度
                    if rsi >= 85:
                        # RSI非常极端，坚持减持但调整为"持有"
                        revised = "持有"
                        change = (
                            f"RSI={rsi:.0f}极端偏高，但momentum评分{mom_score:.1f}显示趋势强，"
                            f"均值回归需等待，修正为持有观察"
                        )
                    elif rsi >= 75:
                        revised = "持有"
                        change = (
                            f"RSI={rsi:.0f}偏高但非极端，momentum({opponent_signal})显示趋势强，"
                            f"均值回归可能迟到，修正为持有"
                        )
                    else:
                        # RSI只是偏高，让步给动量
                        revised = "增持"
                        change = (
                            f"RSI={rsi:.0f}未达严重超买，momentum({opponent_signal})确认趋势，"
                            f"修正为增持"
                        )
                elif my_signal == "观望":
                    if opponent_signal in ["增持", "买入"]:
                        if pct_change > 20:
                            revised = "增持"
                            change = f"月涨{pct_change:.1f}%+动量强，修正为增持"
                        else:
                            revised = my_signal
                            change = "维持观望，等待更多证据"
                    else:
                        revised = my_signal
                        change = "维持观望"
                else:
                    revised = my_signal
                    change = "维持原判断"
            else:
                revised = my_signal
                change = f"momentum({opponent_signal})信号偏弱，维持原判断"
        
        # qlib看backtrader（趋势派）的证据
        elif opponent_project == "backtrader":
            if my_signal in ["减持", "清仓"]:
                # 检查backtrader的超额收益
                excess_match = re.search(r'(-?\d+\.?\d*)%', opponent_reason)
                if excess_match:
                    excess = float(excess_match.group(1))
                    if excess < -50:
                        # backtrader策略严重跑输 → 趋势市场，RSI失效风险高
                        revised = "持有"
                        change = (
                            f"backtrader策略跑输{excess:.0f}%，趋势市场中RSI信号可能失效，"
                            f"修正为持有观察"
                        )
                    elif excess < -20:
                        revised = "持有"
                        change = f"backtrader策略跑输{excess:.0f}%，谨慎起见修正为持有"
                    else:
                        revised = my_signal
                        change = "维持减持"
                else:
                    revised = my_signal
                    change = "维持原判断"
            else:
                revised = my_signal
                change = "维持原判断"
        
        else:
            # 默认策略
            diff = abs(my_priority - opp_priority)
            if diff >= 2:
                revised = "持有"
                change = f"与对方({opponent_signal})分歧大，取中间"
            else:
                revised = my_signal
                change = "维持原判断"
        
        return revised, "", change
    
    def _momentum_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponent_project: str,
        opponent_signal: str,
        opponent_reason: str,
        opponent_result: Optional[ProjectResult],
    ) -> Tuple[str, str, str]:
        """
        momentum重新评估策略（动量派）
        
        momentum的核心信号：1月/3月/6月涨幅排名，趋势强则增持
        但如果RSI极端高或策略严重跑输，会谨慎
        """
        import re
        
        # 提取momentum评分
        mom_score_match = re.search(r'动量评分[=:]?(\d+\.?\d*)', my_reason)
        mom_score = float(mom_score_match.group(1)) if mom_score_match else 0.0
        
        # 提取RSI（来自qlib）
        rsi_match = re.search(r'RSI[=:]?(\d+\.?\d*)', opponent_reason)
        rsi = float(rsi_match.group(1)) if rsi_match else None
        
        # 提取超额收益（来自backtrader）
        excess_match = re.search(r'(-?\d+\.?\d*)%', opponent_reason)
        excess = float(excess_match.group(1)) if excess_match else None
        
        priorities = Aggregator.SIGNAL_PRIORITY
        
        # momentum看qlib（均值回归派）的证据
        if opponent_project == "qlib":
            if my_signal in ["增持", "买入"]:
                if rsi is not None:
                    if rsi >= 85:
                        # RSI极端高，谨慎
                        if mom_score > 50:
                            revised = "持有"
                            change = (
                                f"RSI={rsi:.0f}极端偏高，但动量评分{mom_score:.1f}显示极强趋势，"
                                f"修正为持有观察"
                            )
                        else:
                            revised = "观望"
                            change = f"RSI={rsi:.0f}极端，动量不足，修正为观望"
                    elif rsi >= 75:
                        revised = "持有"
                        change = f"RSI={rsi:.0f}偏高但非极端，修正为持有"
                    else:
                        revised = my_signal
                        change = f"RSI={rsi:.0f}可接受，维持{my_signal}"
                else:
                    revised = my_signal
                    change = "无RSI数据，维持原判断"
            else:
                revised = my_signal
                change = "维持原判断"
        
        # momentum看backtrader（趋势跟踪）的证据
        elif opponent_project == "backtrader":
            if my_signal in ["增持", "买入"]:
                if excess is not None and excess < -50:
                    # backtrader策略在当前市场严重跑输
                    # 说明市场风格可能已经改变
                    if mom_score > 60:
                        revised = "持有"
                        change = (
                            f"backtrader策略跑输{excess:.0f}%，市场风格可能改变，"
                            f"但动量评分{mom_score:.1f}仍强，修正为持有"
                        )
                    else:
                        revised = "观望"
                        change = (
                            f"backtrader策略跑输{excess:.0f}%，市场风格改变，"
                            f"动量减弱，修正为观望"
                        )
                else:
                    revised = my_signal
                    change = "backtrader信号不反对，维持原判断"
            else:
                revised = my_signal
                change = "维持原判断"
        
        else:
            revised = my_signal
            change = "维持原判断"
        
        return revised, "", change
    
    def _backtrader_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponent_project: str,
        opponent_signal: str,
        opponent_reason: str,
        opponent_result: Optional[ProjectResult],
    ) -> Tuple[str, str, str]:
        """
        backtrader重新评估策略（趋势跟踪派）
        
        backtrader的核心信号：MA金叉死叉，超额收益是策略有效性的验证
        如果策略严重跑输，说明不适应当前市场，应该承认失效
        """
        import re
        
        # 提取超额收益
        excess_match = re.search(r'(-?\d+\.?\d*)%', my_reason)
        excess = float(excess_match.group(1)) if excess_match else None
        
        priorities = Aggregator.SIGNAL_PRIORITY
        
        # backtrader的核心问题：策略是否适应当前市场
        if excess is not None and excess < -50:
            # 策略严重跑输
            if opponent_signal in ["增持", "买入", "持有"]:
                # 其他项目看多/中性 → 市场可能是趋势型的，MA策略不适应当前
                revised = "持有"
                change = (
                    f"策略跑输{excess:.0f}%，不适应当前市场风格，"
                    f"其他项目({opponent_signal})确认趋势，修正为持有"
                )
            else:
                revised = my_signal
                change = "维持减持，其他项目也看空"
        elif excess is not None and excess < -20:
            if opponent_signal in ["增持", "买入"]:
                revised = "持有"
                change = f"策略跑输{excess:.0f}%，其他项目偏多，修正为持有"
            else:
                revised = my_signal
                change = "维持原判断"
        else:
            # 策略表现尚可
            revised = my_signal
            change = "MA策略有效，维持原判断"
        
        return revised, "", change


_debate_engine: Optional[DebateEngine] = None

def get_debate_engine(dispatcher=None) -> DebateEngine:
    global _debate_engine
    if _debate_engine is None:
        from .dispatcher import get_dispatcher
        _debate_engine = DebateEngine(get_dispatcher())
    return _debate_engine
