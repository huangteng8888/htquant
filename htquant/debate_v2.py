"""
htquant 辩论引擎 v3

核心改进：一对多广播模式
- 每轮每个项目同时看到所有其他项目的信号和证据
- 重新评估基于全局信息，而非单一对手
- 收敛检测基于全局信号差值，而非成对差值

设计原则：
1. 辩论是"带有全局上下文的重新评估"
2. 每轮所有项目同时重新评估（非成对）
3. 收敛条件：所有项目信号差值<=1档（第1轮），或<=2档（第2轮）
4. 最多3轮
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
    how_other_evidence_changed_mind: str  # 对方证据如何改变了我自己的判断


@dataclass
class DebateRound:
    round_num: int
    messages: List[DebateMessage] = field(default_factory=list)


@dataclass
class DebateResult:
    stock_code: str
    horizon: str
    initial_signals: Dict[str, str] = field(default_factory=dict)  # 项目 -> 初始信号
    initial_reasons: Dict[str, str] = field(default_factory=dict)  # 项目 -> 初始理由
    rounds: List[DebateRound] = field(default_factory=list)

    # 最终结果
    final_signals: Dict[str, str] = field(default_factory=dict)
    converged: bool = False
    converged_round: int = 0  # 在第几轮收敛
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

    def get_signal_evolution(self) -> Dict[str, List[str]]:
        """获取每个项目的信号演进路径"""
        evolution = {}
        for r in self.rounds:
            for m in r.messages:
                if m.project not in evolution:
                    evolution[m.project] = [m.original_signal]
                evolution[m.project].append(m.revised_signal)
        return evolution


class DebateEngineV3:
    """
    辩论引擎 v3：一对多广播模式

    每轮流程：
    1. 所有项目同时看到当前所有其他项目的信号和理由
    2. 每个项目独立重新评估（基于全局上下文）
    3. 全局收敛检测：所有项目信号差值<=1档

    关键方法：
    - re_evaluate_with_all(): 让某项目在看所有其他项目证据后重新评估
    """

    MAX_ROUNDS = 3

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.aggregator = Aggregator()

    def debate(
        self,
        stock_code: str,
        horizon: str,
        all_results: Dict[str, ProjectResult],
        initial_signals: Dict[str, str],
        initial_reasons: Dict[str, str],
    ) -> DebateResult:
        """
        对一只股票的所有项目进行辩论

        Args:
            stock_code: 股票代码
            horizon: 时间周期
            all_results: 所有项目的完整结果
            initial_signals: 项目 -> 初始信号
            initial_reasons: 项目 -> 初始理由
        """
        result = DebateResult(
            stock_code=stock_code,
            horizon=horizon,
            initial_signals=initial_signals.copy(),
            initial_reasons=initial_reasons.copy(),
        )

        projects = list(all_results.keys())
        logger.info(
            f"[htquant/debate_v3] 开始辩论: {stock_code} "
            f"项目={projects} 信号={initial_signals}"
        )

        # 当前各方状态（每轮可能更新）
        current_signals = initial_signals.copy()
        current_reasons = initial_reasons.copy()

        for round_num in range(1, self.MAX_ROUNDS + 1):
            round_obj = DebateRound(round_num=round_num)

            # 每个项目重新评估（基于当前全局状态）
            for proj in projects:
                # 收集其他所有项目的信息
                opponents_info = {
                    other: {
                        "signal": current_signals[other],
                        "reason": current_reasons[other],
                        "result": all_results.get(other),
                    }
                    for other in projects
                    if other != proj
                }

                msg = self._re_evaluate_with_all(
                    project=proj,
                    my_signal=current_signals[proj],
                    my_reason=current_reasons[proj],
                    my_result=all_results.get(proj),
                    opponents_info=opponents_info,
                    round_num=round_num,
                )
                round_obj.messages.append(msg)
                current_signals[proj] = msg.revised_signal
                current_reasons[proj] = msg.revised_reason or msg.how_other_evidence_changed_mind

            result.rounds.append(round_obj)

            # 全局收敛检测
            converged, max_diff = self._check_global_convergence(current_signals)
            if converged:
                result.converged = True
                result.converged_round = round_num
                logger.info(
                    f"[htquant/debate_v3] {stock_code} 第{round_num}轮收敛: "
                    f"{current_signals} (最大档差={max_diff})"
                )
                break

        result.final_signals = current_signals.copy()

        # 确定最终信号：取更接近"持有"的保守信号
        priorities = Aggregator.SIGNAL_PRIORITY
        final_sig = min(
            current_signals.values(),
            key=lambda s: abs(priorities.get(s, 2) - 2.5)
        )
        result.final_signal = final_sig

        # 置信度
        if result.converged:
            result.final_confidence = 0.90 if result.converged_round == 1 else 0.85
        else:
            result.final_confidence = 0.65

        return result

    def _check_global_convergence(self, signals: Dict[str, str]) -> Tuple[bool, int]:
        """
        检测全局收敛：所有项目信号两两差值<=1档
        Returns: (是否收敛, 最大档差)
        """
        priorities = Aggregator.SIGNAL_PRIORITY
        signal_list = list(signals.values())
        if len(signal_list) < 2:
            return True, 0

        max_diff = 0
        for i in range(len(signal_list)):
            for j in range(i + 1, len(signal_list)):
                diff = abs(
                    priorities.get(signal_list[i], 2) - priorities.get(signal_list[j], 2)
                )
                max_diff = max(max_diff, diff)

        return max_diff <= 1, max_diff

    def _re_evaluate_with_all(
        self,
        project: str,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponents_info: Dict[str, Dict],
        round_num: int,
    ) -> DebateMessage:
        """
        核心方法：让某一方在看所有其他项目证据后重新评估

        每种项目有自己的"重新评估策略"：
        - qlib（均值回归）：RSI超买超卖是核心，但被多个项目反对时会谨慎
        - momentum（动量）：趋势强度是核心，但看到多个RSI极端会谨慎
        - backtrader（趋势跟踪）：MA叉是核心，但策略跑输时承认失效
        """

        if project == "qlib":
            revised_signal, revised_reason, change_note = self._qlib_re_evaluate(
                my_signal, my_reason, my_result, opponents_info
            )
        elif project == "momentum":
            revised_signal, revised_reason, change_note = self._momentum_re_evaluate(
                my_signal, my_reason, my_result, opponents_info
            )
        elif project == "backtrader":
            revised_signal, revised_reason, change_note = self._backtrader_re_evaluate(
                my_signal, my_reason, my_result, opponents_info
            )
        else:
            # 未知项目：少数服从多数
            revised_signal, revised_reason, change_note = self._default_re_evaluate(
                my_signal, my_reason, opponents_info
            )

        if revised_signal != my_signal:
            opponent_sigs = {k: v["signal"] for k, v in opponents_info.items()}
            logger.info(
                f"[htquant/debate_v3]   {project}: {my_signal} -> {revised_signal} "
                f"(看全局 {opponent_sigs})"
            )

        return DebateMessage(
            round_num=round_num,
            project=project,
            original_signal=my_signal,
            revised_signal=revised_signal,
            original_reason=my_reason,
            revised_reason=revised_reason or "",
            how_other_evidence_changed_mind=change_note,
        )

    def _qlib_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponents_info: Dict[str, Dict],
    ) -> Tuple[str, str, str]:
        """qlib重新评估策略（均值回归派）"""
        import re

        # 提取我的RSI
        rsi_match = re.search(r'RSI[=:]?(\d+\.?\d*)', my_reason)
        rsi = float(rsi_match.group(1)) if rsi_match else 50.0

        # 提取涨跌
        pct_match = re.search(r'涨跌[=:]?([+-]?\d+\.?\d*)%', my_reason)
        pct_change = float(pct_match.group(1)) if pct_match else 0.0

        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)

        # 统计其他项目的信号
        opponent_signals = [info["signal"] for info in opponents_info.values()]
        opponent_priorities = [priorities.get(s, 2) for s in opponent_signals]

        # 多头/空头计数
        bullish = sum(1 for p in opponent_priorities if p >= 4)  # 增持+
        bearish = sum(1 for p in opponent_priorities if p <= 1)  # 减持-
        neutral = len(opponent_priorities) - bullish - bearish

        # 检查是否有momentum的动量评分
        momentum_score = 0.0
        for proj, info in opponents_info.items():
            if proj == "momentum":
                m = re.search(r'动量评分[=:]?(\d+\.?\d*)', info["reason"])
                momentum_score = float(m.group(1)) if m else 0.0

        # ===== 我的立场是减持/清仓 =====
        if my_signal in ["减持", "清仓"]:
            # Case 1: 多个项目反对（≥2个多头）→ 更谨慎
            if bullish >= 2:
                if rsi >= 85:
                    revised = "持有"
                    change = (
                        f"RSI={rsi:.0f}极端，但{bullish}个项目看多，"
                        f"均值回归需等待，修正为持有"
                    )
                elif rsi >= 75:
                    revised = "观望"
                    change = f"RSI={rsi:.0f}偏高但非极端，{bullish}个多头，修正为观望"
                else:
                    revised = "增持"
                    change = f"RSI={rsi:.0f}未达严重超买，多头占优({bullish}v{bearish})，修正为增持"
            elif bullish == 1:
                # 只有一个多头
                if rsi >= 85:
                    revised = "持有"
                    change = f"RSI={rsi:.0f}极端，坚持持有"
                elif rsi >= 75:
                    revised = "观望"
                    change = f"RSI={rsi:.0f}偏高，1个多头，修正为观望"
                else:
                    revised = "持有"
                    change = f"RSI={rsi:.0f}可接受，让步给1个多头，修正为持有"
            else:
                # 没有多头
                revised = my_signal
                change = f"没有项目支持，维持{my_signal}"

        # ===== 我的立场是观望 =====
        elif my_signal == "观望":
            if bullish >= 2:
                revised = "增持"
                change = f"{bullish}个多头强劲信号，修正为增持"
            elif bullish == 1:
                if momentum_score > 60:
                    revised = "增持"
                    change = f"1个多头+momentum强({momentum_score:.0f})，修正为增持"
                else:
                    revised = "持有"
                    change = f"1个多头但momentum一般，修正为持有"
            else:
                revised = "观望"
                change = "没有足够多头信号，维持观望"

        # ===== 我的立场是增持/买入 =====
        elif my_signal in ["增持", "买入"]:
            if bearish >= 2:
                revised = "持有"
                change = f"{bearish}个空头，修正为持有"
            elif momentum_score > 80:
                # momentum极强，可以忽略部分空头
                revised = my_signal
                change = f"momentum极强({momentum_score:.0f})，维持{my_signal}"
            else:
                revised = my_signal
                change = f"没有强空头信号，维持{my_signal}"

        # ===== 我的立场是持有 =====
        else:  # 持有
            if bullish >= 2:
                revised = "增持"
                change = f"{bullish}个多头，修正为增持"
            elif bearish >= 2:
                revised = "观望"
                change = f"{bearish}个空头，修正为观望"
            else:
                revised = "持有"
                change = "维持持有"

        return revised, "", change

    def _momentum_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponents_info: Dict[str, Dict],
    ) -> Tuple[str, str, str]:
        """momentum重新评估策略（动量派）"""
        import re

        # 提取我的动量评分
        m = re.search(r'动量评分[=:]?(\d+\.?\d*)', my_reason)
        my_mom_score = float(m.group(1)) if m else 0.0

        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)

        # 收集其他项目的RSI和超额收益
        rsi_values = []
        excess_values = []
        for proj, info in opponents_info.items():
            rsi_m = re.search(r'RSI[=:]?(\d+\.?\d*)', info["reason"])
            if rsi_m:
                rsi_values.append((proj, float(rsi_m.group(1))))
            excess_m = re.search(r'(-?\d+\.?\d*)%', info["reason"])
            if excess_m:
                excess_values.append((proj, float(excess_m.group(1))))

        # 统计
        max_rsi = max((r for _, r in rsi_values), default=0)
        min_excess = min((e for _, e in excess_values), default=0)

        # RSI极端数
        extreme_rsi_count = sum(1 for _, r in rsi_values if r >= 80)
        severe_rsi_count = sum(1 for _, r in rsi_values if r >= 85)

        # ===== 我的立场是增持/买入 =====
        if my_signal in ["增持", "买入"]:
            if severe_rsi_count >= 2:
                revised = "观望"
                change = f"2个以上项目RSI≥85极端，风险极高，修正为观望"
            elif severe_rsi_count == 1:
                if my_mom_score > 70:
                    revised = "持有"
                    change = f"1个RSI极端({max_rsi:.0f})但动量极强({my_mom_score:.0f})，修正为持有"
                else:
                    revised = "观望"
                    change = f"1个RSI极端({max_rsi:.0f})，动量不足，修正为观望"
            elif extreme_rsi_count >= 2:
                revised = "观望"
                change = f"2个以上项目RSI≥80，修正为观望"
            elif min_excess < -60:
                # 趋势策略严重跑输，市场可能风格切换
                if my_mom_score > 70:
                    revised = "持有"
                    change = f"趋势策略跑输{min_excess:.0f}%但动量仍强，修正为持有"
                else:
                    revised = "观望"
                    change = f"趋势策略跑输{min_excess:.0f}%，市场风格可能切换，修正为观望"
            else:
                revised = my_signal
                change = f"无极端信号，维持{my_signal}"

        # ===== 我的立场是观望 =====
        elif my_signal == "观望":
            if severe_rsi_count == 0 and extreme_rsi_count == 0 and min_excess > -50:
                if my_mom_score > 60:
                    revised = "增持"
                    change = f"无RSI极端+趋势策略有效，momentum({my_mom_score:.0f})可参与，修正为增持"
                else:
                    revised = "观望"
                    change = "维持观望"
            else:
                revised = "观望"
                change = "仍有风险信号，维持观望"

        # ===== 我的立场是减持/清仓 =====
        elif my_signal in ["减持", "清仓"]:
            # momentum不应该主动让步
            revised = my_signal
            change = f"维持{my_signal}，等待趋势反转"

        # ===== 持有 =====
        else:
            if severe_rsi_count >= 1 or extreme_rsi_count >= 2:
                revised = "观望"
                change = f"RSI信号偏极端，修正为观望"
            else:
                revised = "持有"
                change = "维持持有"

        return revised, "", change

    def _backtrader_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        my_result: Optional[ProjectResult],
        opponents_info: Dict[str, Dict],
    ) -> Tuple[str, str, str]:
        """backtrader重新评估策略（趋势跟踪派）"""
        import re

        # 提取我的超额收益
        m = re.search(r'(-?\d+\.?\d*)%', my_reason)
        my_excess = float(m.group(1)) if m else 0.0

        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)

        # 收集其他项目信号
        opponent_priorities = [priorities.get(info["signal"], 2) for info in opponents_info.values()]
        bullish = sum(1 for p in opponent_priorities if p >= 4)
        bearish = sum(1 for p in opponent_priorities if p <= 1)

        # ===== 我的策略严重跑输（>-50%） =====
        if my_excess < -50:
            if bullish >= 2:
                revised = "持有"
                change = (
                    f"策略跑输{my_excess:.0f}%但{bullish}个多头，"
                    f"市场趋势型，修正为持有"
                )
            elif bullish == 1:
                revised = "观望"
                change = f"策略跑输{my_excess:.0f}%，1个多头，修正为观望"
            else:
                revised = "持有"
                change = f"策略跑输{my_excess:.0f}%，无强支撑，修正为持有"

        # ===== 我的策略跑输（-20%~-50%） =====
        elif my_excess < -20:
            if bullish >= 2:
                revised = "持有"
                change = f"策略跑输{my_excess:.0f}%，{bullish}个多头，修正为持有"
            else:
                revised = my_signal
                change = f"策略跑输{my_excess:.0f}%，但无强多头，维持{my_signal}"

        # ===== 策略正常 =====
        else:
            if bearish >= 2:
                revised = "持有"
                change = f"{bearish}个空头但策略有效，修正为持有"
            else:
                revised = my_signal
                change = f"MA策略有效({my_excess:+.1f}%)，维持{my_signal}"

        return revised, "", change

    def _default_re_evaluate(
        self,
        my_signal: str,
        my_reason: str,
        opponents_info: Dict[str, Dict],
    ) -> Tuple[str, str, str]:
        """默认重新评估：少数服从多数"""
        priorities = Aggregator.SIGNAL_PRIORITY
        my_priority = priorities.get(my_signal, 2)

        opponent_priorities = [priorities.get(info["signal"], 2) for info in opponents_info.values()]

        avg_opponent = sum(opponent_priorities) / len(opponent_priorities) if opponent_priorities else 2.5

        diff = abs(my_priority - avg_opponent)
        if diff >= 2:
            # 走向中间
            mid = (my_priority + avg_opponent) / 2
            candidates = [(abs(priorities.get(s, 2) - mid), s) for s in priorities]
            candidates.sort()
            revised = candidates[0][1]
            change = f"与多数意见分歧，修正为{revised}"
        else:
            revised = my_signal
            change = "维持原判断"

        return revised, "", change


# ===== 冲突检测 =====
def detect_conflicts(
    project_signals: Dict[str, str],
    project_reasons: Dict[str, str],
    stock_code: str,
    horizon: str = "medium",
) -> List[Conflict]:
    """检测项目间的信号冲突"""
    priorities = Aggregator.SIGNAL_PRIORITY
    projects = list(project_signals.keys())
    conflicts = []

    for i in range(len(projects)):
        for j in range(i + 1, len(projects)):
            proj_a, proj_b = projects[i], projects[j]
            sig_a, sig_b = project_signals[proj_a], project_signals[proj_b]
            reason_a = project_reasons.get(proj_a, "")
            reason_b = project_reasons.get(proj_b, "")

            diff = abs(priorities.get(sig_a, 2) - priorities.get(sig_b, 2))
            if diff >= 2:
                conflicts.append(Conflict(
                    stock_code=stock_code,
                    horizon=horizon,
                    project_a=proj_a,
                    project_b=proj_b,
                    signal_a=sig_a,
                    signal_b=sig_b,
                    reason_a=reason_a,
                    reason_b=reason_b,
                    severity=diff,
                ))

    return conflicts
