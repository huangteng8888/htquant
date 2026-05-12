"""
htquant 辩论引擎 v4 — 真理越辨越明版

核心原则：辩论是"证据的较量"，而非"信号的妥协"
- 信号一致 → 放大，增强置信度
- 信号分歧 → 评判论据强度，弱者大步退让（2-3档），强者维持
- 收敛 → 弱者彻底认输（<=1档差），而非停留在中间

与v1/v2的核心区别：
- 不再"各退一步"（diff>=2时各退1步 → 导致停留在"持有"）
- 而是"弱者大步撤退"（让弱者移动2-3档，强者维持）
- 信号优先级：RSI极端 > 252日极值 > 趋势强度 > 普通信号
"""

import re
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from .dispatcher import ProjectResult
from .aggregator import Conflict, Aggregator

logger = logging.getLogger(__name__)

# ── 信号优先级定义 ─────────────────────────────────────────────────────────
# 优先级越高：在极端事件中越可靠
SIGNAL_ORDER = ['清仓', '减持', '观望', '持有', '增持', '买入']
SIGNAL_SCORE = {s: i for i, s in enumerate(SIGNAL_ORDER)}
SIGNAL_POLARITY = {  # +1=多头方向, -1=空头方向, 0=中性
    '清仓': -2, '减持': -1, '观望': 0,
    '持有': 0, '增持': +1, '买入': +2,
}

# 论据强度基准分
_BASE_SCORE = 50.0

# 【核心修正：基于实际市场数据重新计算先验】
# 争议时qlib方向的真实市场胜率（从CSV统计得出）
HISTORICAL_WINRATES: Dict[str, Dict[str, float]] = {
    # 短期触低：qlib做多胜率仅39%，正确方向是趋势向下(做空)
    'W20_LOW':  {'bullish': 0.390, 'bearish': 0.610},
    'W50_LOW':  {'bullish': 0.387, 'bearish': 0.613},
    # 中长期触低：qlib做多胜率随期限延长而提高
    'W100_LOW': {'bullish': 0.419, 'bearish': 0.581},
    'W252_LOW': {'bullish': 0.570, 'bearish': 0.430},
    # 触高：qlib做空胜率始终较高（均值回归在高位成立）
    'W20_HIGH': {'bullish': 0.391, 'bearish': 0.609},
    'W50_HIGH': {'bullish': 0.364, 'bearish': 0.636},
    'W100_HIGH':{'bullish': 0.278, 'bearish': 0.722},
    'W252_HIGH':{'bullish': 0.333, 'bearish': 0.667},
}

# 事件方向基准：来自实际市场数据
# 短期(W20/W50)：触低→趋势向下(做空)，触高→趋势向上(做多)
# 中长期(W100/W252)：触低→均值回归向上，触高→均值回归向下
EVENT_BIAS: Dict[str, str] = {
    # 短期：趋势延续
    'W20_LOW': 'bearish',   # 触低→短期继续跌→做空
    'W50_LOW': 'bearish',   # 触低→短期继续跌→做空
    'W20_HIGH': 'bullish',  # 触高→短期继续涨→做多
    'W50_HIGH': 'bullish',  # 触高→短期继续涨→做多
    # 中长期：均值回归
    'W100_LOW': 'bullish',  # 触低→均值回归向上
    'W252_LOW': 'bullish',  # 触低→均值回归向上
    'W100_HIGH': 'bearish', # 触高→均值回归向下
    'W252_HIGH': 'bearish', # 触高→均值回归向下
}


def _get_event_winrate(event_type: Optional[str], signal: str) -> float:
    """获取某事件类型+信号方向的历史胜率（0~1）"""
    if not event_type:
        return 0.50
    # 提取基础类型（去掉W20_前缀）
    key = event_type.replace('W20_', 'W20_').replace('W50_', 'W50_').replace('W100_', 'W100_').replace('W252_', 'W252_')

    bias = EVENT_BIAS.get(key, 'neutral')
    winrates = HISTORICAL_WINRATES.get(key, {})

    if signal in ['买入', '增持']:
        return winrates.get('bullish', 0.50)
    elif signal in ['减持', '清仓']:
        return winrates.get('bearish', 0.50)
    else:
        return 0.50


@dataclass
class ArgumentEvidence:
    """从信号理由中提取的关键证据"""
    rsi: Optional[float] = None          # RSI值
    rsi_extreme: bool = False            # RSI是否极端（>75超买 或 <35超卖）
    rsi_severe: bool = False             # RSI严重极端（>85超买 或 <25超卖）
    momentum_score: Optional[float] = None  # 动量评分
    momentum_strong: bool = False        # 动量强（>60）
    momentum_weak: bool = False          # 动量弱（<0）
    excess_return: Optional[float] = None # 超额收益（来自backtrader）
    price_vs_252w_high: Optional[float] = None  # 价格相对252日高的位置（0~1）
    price_vs_252w_low: Optional[float] = None  # 价格相对252日低的位置（0~1）
    # 极值事件类型（外部传入）
    extreme_event_type: Optional[str] = None  # 如 'HIGH_TOUCH', 'LOW_TOUCH', 'NEW_HIGH' 等


@dataclass
class ConvictionScore:
    """辩论中某方信号的"论据强度"评分"""
    signal: str
    score: float             # 总分
    rsi_component: float      # RSI贡献
    momentum_component: float  # 动量贡献
    reason: str               # 解释为何强/弱


@dataclass
class DebateResultV4:
    """V4辩论结果"""
    stock_code: str
    horizon: str
    # 初始信号
    initial_signals: Dict[str, str] = field(default_factory=dict)
    initial_reasons: Dict[str, str] = field(default_factory=dict)
    # 演化
    round_history: List[Dict[str, str]] = field(default_factory=list)  # 每轮的信号状态
    # 最终
    final_signal: str = "观望"
    final_confidence: float = 0.0
    converged: bool = False
    winner: str = ""               # 胜出的项目名
    loser: str = ""                # 认输的项目名
    debate_log: List[str] = field(default_factory=list)

    def log(self, msg: str):
        self.debate_log.append(msg)
        logger.info(f"[debate_truth] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数：从reason文本中提取证据
# ─────────────────────────────────────────────────────────────────────────────

def extract_evidence(signal: str, reason: str) -> ArgumentEvidence:
    """从信号理由中提取关键证据"""
    ev = ArgumentEvidence()

    rsi_m = re.search(r'RSI[=:\s](\d+\.?\d*)', reason)
    if rsi_m:
        ev.rsi = float(rsi_m.group(1))
        ev.rsi_extreme = ev.rsi > 75 or ev.rsi < 35
        ev.rsi_severe  = ev.rsi > 85 or ev.rsi < 25

    mom_m = re.search(r'动量评分[=:]?([-+]?\d+\.?\d*)', reason)
    if mom_m:
        ev.momentum_score = float(mom_m.group(1))
        ev.momentum_strong = ev.momentum_score > 60
        ev.momentum_weak   = ev.momentum_score < 0

    excess_m = re.search(r'(-?\d+\.?\d*)%', reason)
    if excess_m:
        ev.excess_return = float(excess_m.group(1))

    return ev


def calc_conviction(project: str, signal: str, ev: ArgumentEvidence,
                    opponents: Dict[str, Tuple[str, ArgumentEvidence]]) -> ConvictionScore:
    """
    计算某方信号的"论据强度"（0~100）

    核心逻辑（真理越辨越明）：
    1. 信号质量加成（RSI极端性 / 动量强度）→ 核心分
    2. 对手反驳 → 减少
    3. 历史先验 → 仅在双方质量分接近时(差距<15)做裁决
    """
    # ── Step 1: 信号质量加成（核心）───────────────────────────────────
    quality_score = 0.0
    quality_reasons = []

    if project == 'qlib':
        quality_score, quality_reasons = _qlib_signal_quality(signal, ev)
    elif project == 'momentum':
        quality_score, quality_reasons = _momentum_signal_quality(signal, ev)
    elif project == 'backtrader':
        quality_score, quality_reasons = _backtrader_signal_quality(signal, ev)
    elif project == 'yanbao_reports':
        # 研报信号质量：基于置信度和研报覆盖量
        conf = ev.excess_return or 0.5  # excess_return 复用为研报置信度
        quality_score = conf * 30  # 0~30 分
        quality_reasons = [f"研报置信度 {conf*100:.0f}%"]
    else:
        quality_score, quality_reasons = 0.0, ["未知项目"]

    # ── Step 2: 对手反驳力 ───────────────────────────────────────────
    rebuttal = 0.0
    rebuttal_reasons = []
    if project == 'qlib':
        rebuttal, rebuttal_reasons = _qlib_facing_rebuttal(signal, ev, opponents)
    elif project == 'momentum':
        rebuttal, rebuttal_reasons = _momentum_facing_rebuttal(signal, ev, opponents)

    # ── Step 3: 基础分 = 50 + 质量加成 - 反驳 ──────────────────────
    base_total = 50.0 + quality_score - rebuttal

    # ── Step 4: 先验裁决（仅在双方质量分差距<20时介入）────────────────
    prior_bonus = 0.0
    prior_reason = ""
    event_type = ev.extreme_event_type

    if event_type and len(opponents) > 0:
        opp_quality_scores = []
        for proj, (opp_sig, opp_ev) in opponents.items():
            if proj == 'qlib':
                oscore, _ = _qlib_signal_quality(opp_sig, opp_ev)
            elif proj == 'momentum':
                oscore, _ = _momentum_signal_quality(opp_sig, opp_ev)
            else:
                oscore = 25.0
            opp_quality_scores.append(oscore)

        quality_gap = abs(quality_score - max(opp_quality_scores)) if opp_quality_scores else 99

        if quality_gap < 20:
            # 质量差距小，先验介入裁决
            prior_wr = _get_event_winrate(event_type, signal)
            direction_aligned = (signal in ['买入', '增持'] and EVENT_BIAS.get(event_type, '') == 'bullish') or \
                               (signal in ['减持', '清仓'] and EVENT_BIAS.get(event_type, '') == 'bearish')
            # 先验权重 × 方向一致性：先验胜率偏离50%越多，裁决分越多
            # prior_wr=0.61 → +8.25分；prior_wr=0.72 → +11分；prior_wr=0.39 → -5.5分
            prior_bonus = (prior_wr - 0.50) * 30.0  # 最多±15分
            if not direction_aligned:
                prior_bonus *= 1.5  # 方向相反时加倍惩罚
            prior_reason = f"先验({prior_wr:.1%}方向{'一致' if direction_aligned else '相反'},{prior_bonus:+.0f}分)"

    total = base_total + prior_bonus
    total = max(5.0, min(100.0, total))

    return ConvictionScore(
        signal=signal,
        score=total,
        rsi_component=quality_score,
        momentum_component=-rebuttal,
        reason=f"质量{quality_score:.0f} | 反驳-{rebuttal:.0f} | 基底={base_total:.0f} | " +
               prior_reason + " | " + " | ".join(quality_reasons + rebuttal_reasons)
    )


def _qlib_signal_quality(signal: str, ev: ArgumentEvidence) -> Tuple[float, List[str]]:
    """qlib信号的证据质量（0~30分）"""
    bonus = 0.0
    reasons = []

    if ev.rsi_severe and signal in ['清仓', '减持', '买入', '增持']:
        bonus = 30.0
        reasons.append(f"RSI极端({ev.rsi:.0f})")
    elif ev.rsi_extreme and signal in ['清仓', '减持', '买入', '增持']:
        bonus = 20.0
        reasons.append(f"RSI偏高/低({ev.rsi:.0f})")
    elif ev.rsi is not None and signal in ['清仓', '减持']:
        bonus = 10.0
        reasons.append(f"RSI正常偏高({ev.rsi:.0f})")
    elif ev.rsi is not None and signal in ['买入', '增持']:
        bonus = 8.0
        reasons.append(f"RSI正常偏低({ev.rsi:.0f})")

    return bonus, reasons


def _momentum_signal_quality(signal: str, ev: ArgumentEvidence) -> Tuple[float, List[str]]:
    """momentum信号的证据质量（0~30分）"""
    bonus = 0.0
    reasons = []

    if ev.momentum_score is not None:
        if ev.momentum_strong:
            bonus = 25.0
            reasons.append(f"动量强势({ev.momentum_score:.0f})")
        elif ev.momentum_weak:
            bonus = 5.0
            reasons.append(f"动量弱势({ev.momentum_score:.0f})")
        else:
            bonus = 15.0
            reasons.append(f"动量中性({ev.momentum_score:.0f})")

    return bonus, reasons


def _backtrader_signal_quality(signal: str, ev: ArgumentEvidence) -> Tuple[float, List[str]]:
    """backtrader信号的证据质量"""
    bonus = 0.0
    reasons = []
    if ev.excess_return is not None:
        bonus = min(abs(ev.excess_return) * 0.5, 25.0)
        reasons.append(f"策略超额{ev.excess_return:+.0f}%")
    return bonus, reasons


def _qlib_facing_rebuttal(signal: str, ev: ArgumentEvidence,
                           opponents: Dict[str, Tuple[str, ArgumentEvidence]]) -> Tuple[float, List[str]]:
    """qlib面对momentum反驳时的承压（0~20分）"""
    penalty = 0.0
    reasons = []

    for proj, (opp_sig, opp_ev) in opponents.items():
        if proj == 'momentum':
            opp_dir = SIGNAL_POLARITY.get(opp_sig, 0)
            ev_dir  = SIGNAL_POLARITY.get(signal, 0)

            # momentum方向与qlib相反，且momentum很强 → qlib承压
            if opp_dir * ev_dir < 0:  # 方向相反
                if opp_ev.momentum_strong:
                    penalty = 18.0
                    reasons.append(f"momentum强({opp_ev.momentum_score:.0f})反驳")
                elif opp_ev.momentum_score is not None and opp_ev.momentum_score > 20:
                    penalty = 10.0
                    reasons.append(f"momentum偏强({opp_ev.momentum_score:.0f})")

    return penalty, reasons


def _momentum_facing_rebuttal(signal: str, ev: ArgumentEvidence,
                               opponents: Dict[str, Tuple[str, ArgumentEvidence]]) -> Tuple[float, List[str]]:
    """momentum面对qlib反驳时的承压（0~20分）"""
    penalty = 0.0
    reasons = []

    for proj, (opp_sig, opp_ev) in opponents.items():
        if proj == 'qlib':
            opp_dir = SIGNAL_POLARITY.get(opp_sig, 0)
            ev_dir  = SIGNAL_POLARITY.get(signal, 0)

            # qlib方向与momentum相反，且RSI极端 → momentum承压
            if opp_dir * ev_dir < 0:  # 方向相反
                if opp_ev.rsi_severe:
                    penalty = 18.0
                    reasons.append(f"qlib RSI极端({opp_ev.rsi:.0f})反驳")
                elif opp_ev.rsi_extreme:
                    penalty = 12.0
                    reasons.append(f"qlib RSI极端({opp_ev.rsi:.0f})")
                elif opp_ev.rsi is not None:
                    penalty = 6.0
                    reasons.append(f"qlib RSI({opp_ev.rsi:.0f})")

    return penalty, reasons


# ─────────────────────────────────────────────────────────────────────────────
# 核心辩论逻辑
# ─────────────────────────────────────────────────────────────────────────────

def run_truth_debate(
    stock_code: str,
    horizon: str,
    all_results: Dict[str, ProjectResult],
    initial_signals: Dict[str, str],
    initial_reasons: Dict[str, str],
    extreme_event_type: Optional[str] = None,
) -> DebateResultV4:
    """
    真理越辨越明版辩论

    核心原则：
    1. 触低/触高事件有方向性基准偏向（均值回归假设）
    2. 辩论应该让证据更清晰的信号赢，而非各退一步
    3. 弱事件（W20/W50）：胜率<50%时，辩论应保守，倾向观望
    4. 强事件（W100/W252）：胜率>50%，辩论可激进，给出明确信号
    """
    result = DebateResultV4(
        stock_code=stock_code,
        horizon=horizon,
        initial_signals=initial_signals.copy(),
        initial_reasons=initial_reasons.copy(),
    )

    projects = list(all_results.keys())
    if len(projects) < 2:
        result.final_signal = initial_signals.get(projects[0], '观望')
        result.final_confidence = 0.8
        result.converged = True
        result.winner = projects[0] if projects else ''
        return result

    # ── 事件级别的先验偏向 ───────────────────────────────────────────
    event_bias = EVENT_BIAS.get(extreme_event_type, 'neutral') if extreme_event_type else 'neutral'
    prior_wr = _get_event_winrate(extreme_event_type, 'bullish') if extreme_event_type else 0.50
    # W20/W50事件：胜率<50%，辩论应保守
    is_weak_event = extreme_event_type and extreme_event_type.startswith(('W20_', 'W50_'))
    # W252事件：胜率>50%，辩论可激进
    is_strong_event = extreme_event_type and extreme_event_type.startswith('W252_')

    result.log(f"=== 真理辩论开始: {stock_code} 事件={extreme_event_type} "
               f"偏向={event_bias} 先验胜率={prior_wr:.1%} "
               f"{'[弱事件-保守]' if is_weak_event else '[强事件-激进]' if is_strong_event else ''} ===")
    result.log(f"初始信号: {initial_signals}")

    current = dict(initial_signals)
    MAX_ROUNDS = 4

    for round_num in range(1, MAX_ROUNDS + 1):
        result.round_history.append(dict(current))

        # ── Step 1: 提取各方证据 ──────────────────────────────────────
        evidence = {}
        for proj in projects:
            reason = initial_reasons.get(proj, '') or (all_results[proj].reason if all_results[proj].reason else '')
            ev = extract_evidence(current[proj], reason)
            ev.extreme_event_type = extreme_event_type
            evidence[proj] = ev

        # ── Step 2: 计算论据强度 ──────────────────────────────────────
        convictions = {}
        for proj in projects:
            opp = {p: (current[p], evidence[p]) for p in projects if p != proj}
            convictions[proj] = calc_conviction(proj, current[proj], evidence[proj], opp)

        result.log(f"  第{round_num}轮论据强度: " +
                   " | ".join(f"{p}:{convictions[p].score:.0f}分({convictions[p].reason[:40]})"
                              for p in projects))

        # ── Step 3: 检测收敛 ──────────────────────────────────────────
        scores = [SIGNAL_SCORE[current[p]] for p in projects]
        max_diff = max(scores) - min(scores)

        if max_diff <= 1:
            result.converged = True
            result.log(f"  → 第{round_num}轮收敛！档差={max_diff}")
            break

        # ── Step 4: 找最强方和最弱方 ─────────────────────────────────
        strongest = max(projects, key=lambda p: convictions[p].score)
        weakest   = min(projects, key=lambda p: convictions[p].score)
        diff_conviction = convictions[strongest].score - convictions[weakest].score

        if diff_conviction < 10:
            # 论据差不多强：双方各退一步
            result.log(f"  → 论据强度相近({diff_conviction:.0f}分差)，双方各退一步")
            for proj in projects:
                old = current[proj]
                sc = SIGNAL_SCORE[old]
                if sc >= 3:
                    current[proj] = SIGNAL_ORDER[max(0, sc - 1)]
                else:
                    current[proj] = SIGNAL_ORDER[min(5, sc + 1)]
                if current[proj] != old:
                    result.log(f"    {proj}: {old} -> {current[proj]}")
        else:
            # ── Step 5: 弱者大步退让 ─────────────────────────────────
            winner_sig = current[strongest]
            loser_sig  = current[weakest]
            winner_sc  = SIGNAL_SCORE[winner_sig]
            loser_sc   = SIGNAL_SCORE[loser_sig]

            if diff_conviction >= 40:
                retreat = 3
            elif diff_conviction >= 20:
                retreat = 2
            else:
                retreat = 2

            if winner_sc > loser_sc:
                new_loser_sc = max(0, loser_sc - retreat)
            else:
                new_loser_sc = min(5, loser_sc + retreat)

            weakest_proj = weakest
            old_loser = current[weakest_proj]
            current[weakest_proj] = SIGNAL_ORDER[new_loser_sc]
            result.log(f"  → 裁决: {strongest}({winner_sig},{convictions[strongest].score:.0f}分) "
                       f"胜出，{weakest_proj}({old_loser})退让 {retreat} 档 -> {current[weakest_proj]}")
            result.log(f"    原因: 强者论据={convictions[strongest].reason[:50]}")

            if abs(SIGNAL_SCORE[current[weakest_proj]] - winner_sc) <= 1:
                result.converged = True
                result.log(f"  → 辩论收敛（档差<=1）")

    # ── 最终信号判定 ─────────────────────────────────────────────────────
    final_scores = [SIGNAL_SCORE[current[p]] for p in projects]
    max_sc = max(final_scores)
    min_sc = min(final_scores)
    winner_proj = max(projects, key=lambda p: convictions[p].score)
    winner_final_sig = current[winner_proj]

    # ── 强制方向检查（核心修复）────────────────────────────────────────
    # 当辩论结果与事件历史方向严重不符时，直接翻转
    # 适用于 agreement case（qlib和momentum都同意但方向错误）
    if extreme_event_type:
        bias = EVENT_BIAS.get(extreme_event_type, 'neutral')
        prior_wr = _get_event_winrate(extreme_event_type, 'bullish')
        
        # 判断辩论结果是否与bias方向一致
        result_dir = 'bullish' if winner_final_sig in ['买入', '增持'] else 'bearish' if winner_final_sig in ['减持', '清仓'] else 'neutral'
        is_wrong_bias = (bias == 'bearish' and winner_final_sig in ['买入', '增持']) or \
                       (bias == 'bullish' and winner_final_sig in ['减持', '清仓'])
        
        # 判断qlib和momentum是否都同意（agreement case）
        qlib_bull = any(initial_signals.get(p, '') in ['买入', '增持'] for p in projects)
        mom_bull  = any(initial_signals.get(p, '') in ['买入', '增持'] for p in projects if 'mom' in p.lower())
        
        # 检查qlib项目key（可能是 'qlib' 或其他映射）
        qlib_proj = next((p for p in projects if 'qlib' in p.lower()), None)
        if qlib_proj:
            qlib_sig = initial_signals.get(qlib_proj, '')
            qlib_bull = qlib_sig in ['买入', '增持']
        
        # momentum项目key
        mom_proj = next((p for p in projects if 'mom' in p.lower()), None)
        if mom_proj:
            mom_sig = initial_signals.get(mom_proj, '')
            mom_bull = mom_sig in ['买入', '增持']
        
        # momentum项目key（可能是 'momentum'）
        mom_proj = next((p for p in projects if 'momentum' in p.lower() or '动量' in p.lower()), None)
        if mom_proj:
            mom_sig = initial_signals.get(mom_proj, '')
            mom_bull = mom_sig in ['买入', '增持']
        else:
            mom_bull = False
        
        is_agreement_bull = qlib_bull and mom_bull  # qlib+momentum都看多
        is_agreement_bear = (not qlib_bull and not mom_bull)  # qlib+momentum都看空
        
        # 【核心】Agreement case 且方向与bias相反 → 强制翻转
        # 例如：qlib+momentum都看多(买入) + W20_LOW(bias=做空正确) → 翻转为做空
        if is_wrong_bias and (is_agreement_bull or is_agreement_bear):
            old_sig = winner_final_sig
            if winner_final_sig in ['买入']:
                winner_final_sig = '减持'
            elif winner_final_sig in ['增持']:
                winner_final_sig = '减持'
            elif winner_final_sig in ['减持']:
                winner_final_sig = '增持'
            elif winner_final_sig in ['清仓']:
                winner_final_sig = '增持'
            result.log(f"  → [强制翻转] qlib+momentum一致({old_sig})但方向与bias({bias})相反 → 翻转为{winner_final_sig}")

    # ── 特殊规则：弱事件（W20/W50）——方向翻转 ──────────────────────────
    if is_weak_event:
        # 【核心】弱事件辩论结果的方向翻转
        # W20/W50触低时辩论输出买入/增持 → 实际应该做空（趋势向下）
        # W20/W50触高时辩论输出增持/减持 → 实际应该做多（趋势向上）
        bias = EVENT_BIAS.get(extreme_event_type, 'neutral')
        
        if winner_final_sig in ['增持', '减持']:
            # 中间信号：向bias方向移动一档
            sc = SIGNAL_SCORE[winner_final_sig]
            if bias == 'bullish':
                winner_final_sig = SIGNAL_ORDER[min(5, sc + 1)]  # 升级
            else:
                winner_final_sig = SIGNAL_ORDER[max(0, sc - 1)]  # 降级
            result.log(f"  → [弱事件规则] 中间信号向bias({bias})调整: -> {winner_final_sig}")
        elif winner_final_sig in ['买入'] and bias == 'bearish':
            # 触低时辩论输出买入 → 应该做空
            winner_final_sig = '减持'
            result.log(f"  → [弱事件规则] 触低买入翻转为做空: -> {winner_final_sig}")
        elif winner_final_sig in ['清仓'] and bias == 'bullish':
            # 触高时辩论输出清仓 → 应该做多（但至少是增持）
            winner_final_sig = '增持'
            result.log(f"  → [弱事件规则] 触高清仓降为增持: -> {winner_final_sig}")
        elif winner_final_sig in ['持有']:
            # 持有信号：根据bias给出方向
            if bias == 'bearish':
                winner_final_sig = '减持'
            else:
                winner_final_sig = '增持'
            result.log(f"  → [弱事件规则] 持有→bias方向: -> {winner_final_sig}")
        
        result.final_signal = winner_final_sig
        result.final_confidence = 0.72
        result.winner = winner_proj

    elif is_strong_event:
        # W252事件：胜率>50%，辩论可激进
        # 快速收敛（<=2轮）时保持极端信号
        if winner_final_sig in ['清仓', '买入'] and len(result.round_history) <= 2:
            result.final_signal = winner_final_sig
            result.final_confidence = 0.92
        elif winner_final_sig in ['清仓', '买入']:
            sc = SIGNAL_SCORE[winner_final_sig]
            result.final_signal = winner_final_sig  # 保持
            result.final_confidence = 0.82
        else:
            result.final_signal = winner_final_sig
            result.final_confidence = 0.82
        result.final_confidence = max(result.final_confidence, 0.85)
        result.winner = winner_proj

    elif result.converged or max_sc - min_sc <= 1:
        # 常规收敛
        if winner_final_sig in ['清仓', '买入']:
            if len(result.round_history) <= 2:
                result.final_signal = winner_final_sig
                result.final_confidence = 0.90
            else:
                sc = SIGNAL_SCORE[winner_final_sig]
                result.final_signal = SIGNAL_ORDER[max(1, min(4, sc - 1))]
                result.final_confidence = 0.78
        else:
            result.final_signal = winner_final_sig
            result.final_confidence = 0.80
        result.winner = winner_proj

    else:
        # 未收敛，取胜方向
        if convictions[winner_proj].score > 60:
            result.final_signal = current[winner_proj]
            result.final_confidence = 0.70
        else:
            result.final_signal = '观望'
            result.final_confidence = 0.50
        result.winner = winner_proj

    result.log(f"最终: {result.final_signal} (置信度{result.final_confidence:.0%}) "
               f"胜者:{result.winner}")

    return result
