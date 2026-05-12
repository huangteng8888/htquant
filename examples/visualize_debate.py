#!/usr/bin/env python3
"""
辩论过程可视化示例 v3 - 混合模式
v3一对多广播 -> v1一对一收敛
展示每个量化项目从初始评估到多轮分歧重估的完整过程
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from htquant.dispatcher import Dispatcher, Query, QueryType, get_dispatcher
from htquant.aggregator import Aggregator, get_aggregator
from htquant.debate_hybrid import HybridDebateEngine
from htquant.debate_v2 import detect_conflicts
from htquant.scoring import ScoringEngine, get_scoring_engine
from htquant.config import DEFAULT_STOCKS, STOCK_CODE_MAPPING

# 信号档位
SIGNAL_ORDER = ["清仓", "减持", "观望", "持有", "增持", "买入"]
SIGNAL_PRIORITY = {s: i for i, s in enumerate(SIGNAL_ORDER)}


def signal_arrow(a, b):
    if a == b:
        return "―"
    da = SIGNAL_PRIORITY.get(a, 2) - SIGNAL_PRIORITY.get(b, 2)
    return "↑" if da < 0 else "↓"


def visualize_hybrid(code, name, project_results, score, hybrid_result, conflicts):
    """为一个股票生成完整的混合辩论可视化"""
    lines = []
    width = 70

    lines.append("━" * width)
    lines.append(f"【{name}({code})】 混合辩论可视化")
    lines.append("━" * width)

    # ===== 阶段1: 初始评估 =====
    lines.append("\n[阶段1] 初始评估")
    for proj, result in project_results.items():
        sig = result.signal
        reason = result.reason or "—"
        if len(reason) > 50:
            reason = reason[:47] + "..."
        lines.append(f"  {proj:<12} → {sig:<4}  ({reason})")

    agg = get_aggregator().aggregate(project_results, code)
    lines.append(f"\n  {'聚合信号':<12} → {agg.signal_medium:<4}  (置信度: {agg.confidence:.0%})")

    initial_signals = {proj: result.signal for proj, result in project_results.items()}
    initial_reasons = {proj: result.reason or "" for proj, result in project_results.items()}
    conflicts = detect_conflicts(initial_signals, initial_reasons, code)
    if conflicts:
        lines.append(f"  {'冲突':<12} = {len(conflicts)}组")

    # ===== 阶段2: v3 一对多广播 =====
    if not conflicts:
        lines.append("\n[阶段2] 无需辩论 (无冲突)")
        v3 = None
    else:
        v3 = hybrid_result.v3_result if hybrid_result else None
        if not v3:
            lines.append("\n[阶段2] 辩论引擎未运行")
        else:
            lines.append(f"\n[阶段2] v3 一对多广播 ({len(v3.rounds)}轮)")

            evo = v3.get_signal_evolution()
            max_rounds = len(v3.rounds)

            # 演进矩阵
            lines.append("\n  信号演进矩阵:")
            header = f"  {'项目':<12}" + "".join(f"  第{i}轮" for i in range(max_rounds))
            lines.append(header)
            lines.append("  " + "─" * (12 + max_rounds * 6))

            for proj, path in evo.items():
                if len(path) == max_rounds + 1:
                    row = f"  {proj:<12}"
                    for ri in range(max_rounds):
                        sig = path[ri + 1]
                        arrow = signal_arrow(path[ri], path[ri + 1])
                        changed = "*" if path[ri] != path[ri + 1] else ""
                        row += f"  {sig:>3}{arrow}{changed}"
                    lines.append(row)

            if v3.converged:
                lines.append(f"\n  ✓ v3第{v3.converged_round}轮收敛  (置信度: {v3.final_confidence:.0%})")
                lines.append(f"  各项目最终信号: {v3.final_signals}")
            else:
                lines.append(f"\n  ✗ v3未收敛，残留冲突")
                lines.append(f"  各项目最终信号: {v3.final_signals}")

            # 详细轮次
            for ri, round_obj in enumerate(v3.rounds, 1):
                is_converge = (ri == v3.converged_round)
                end = " ◄ 收敛" if is_converge else (" ◄ 最终" if ri == max_rounds else "")
                lines.append(f"\n  ┌─ v3第{ri}轮{end}" + "─" * (50 - len(end)) + "┐")
                for msg in round_obj.messages:
                    chg = "✦" if msg.revised_signal != msg.original_signal else "○"
                    dist = abs(SIGNAL_PRIORITY.get(msg.original_signal, 2) - SIGNAL_PRIORITY.get(msg.revised_signal, 2))
                    dist_str = f"(差{dist})" if dist > 0 else ""
                    lines.append(f"  │ {chg} [{msg.project:<12}] {msg.original_signal} {signal_arrow(msg.original_signal, msg.revised_signal)} {msg.revised_signal} {dist_str}")
                    rsn = msg.how_other_evidence_changed_mind
                    if len(rsn) > 50:
                        rsn = rsn[:47] + "..."
                    lines.append(f"  │     → {rsn}")
                lines.append("  └" + "─" * 58 + "┘")

    # ===== 阶段3: v1 一对一（如需） =====
    if hybrid_result and hybrid_result.v1_results:
        lines.append(f"\n[阶段3] v1 一对一收敛 ({len(hybrid_result.v1_results)}组)")
        for v1r in hybrid_result.v1_results:
            pair = f"{v1r.initial_conflicts[0].project_a}/{v1r.initial_conflicts[0].project_b}" if v1r.initial_conflicts else "?"
            converged_mark = "✓" if v1r.converged else "✗"
            lines.append(f"\n  ── {pair} {converged_mark} ──")
            for ri, round_obj in enumerate(v1r.rounds, 1):
                lines.append(f"\n  ┌─ v1第{ri}轮" + "─" * 54 + "┐")
                for msg in round_obj.messages:
                    chg = "✦" if msg.revised_signal != msg.original_signal else "○"
                    dist = abs(SIGNAL_PRIORITY.get(msg.original_signal, 2) - SIGNAL_PRIORITY.get(msg.revised_signal, 2))
                    dist_str = f"(差{dist})" if dist > 0 else ""
                    lines.append(f"  │ {chg} [{msg.project:<12}] {msg.original_signal} {signal_arrow(msg.original_signal, msg.revised_signal)} {msg.revised_signal} {dist_str}")
                    rsn = msg.how_other_evidence_changed_mind
                    if len(rsn) > 50:
                        rsn = rsn[:47] + "..."
                    lines.append(f"  │     → {rsn}")
                lines.append("  └" + "─" * 58 + "┘")
            lines.append(f"\n  最终: {v1r.final_signal} {'(收敛)' if v1r.converged else '(未收敛)'}")
    elif not (hybrid_result and hybrid_result.v1_results) and v3 and not v3.converged:
        lines.append(f"\n[阶段3] v1 一对一收敛 (无残留冲突)")

    # ===== 阶段4: 最终结论 =====
    final_sig = hybrid_result.final_signal if hybrid_result else agg.signal_medium
    final_conf = hybrid_result.final_confidence if hybrid_result else agg.confidence
    final_method = hybrid_result.converged_method if hybrid_result else "none"

    lines.append(f"\n[阶段4] 最终结论")
    lines.append(f"  最终信号: {final_sig}  (置信度: {final_conf:.0%})")
    lines.append(f"  收敛方式: {final_method}")
    lines.append(f"  评分: {score.composite_score:.1f}/100")

    action_map = {
        "买入": "强烈推荐买入", "增持": "建议增持",
        "持有": "可继续持有", "观望": "建议观望",
        "减持": "建议减持", "清仓": "建议清仓",
    }
    lines.append(f"  操作建议: {action_map.get(final_sig, '待观察')}")

    return "\n".join(lines)


def analyze_hybrid():
    print("=" * 70)
    print("htquant 混合辩论分析 (v3一对多 -> v1一对一)")
    print("=" * 70)

    dispatcher = get_dispatcher()
    aggregator = get_aggregator()
    scoring_engine = get_scoring_engine()
    hybrid_engine = HybridDebateEngine(dispatcher)

    stocks = DEFAULT_STOCKS
    print(f"分析标的: {[STOCK_CODE_MAPPING.get(c, (None, c))[1] for c in stocks]}")
    print(f"可用项目: {dispatcher.list_available()}")
    print()

    all_summary = []
    for code in stocks:
        name = STOCK_CODE_MAPPING.get(code, (None, code))[1]

        query = Query(stock_codes=[code], query_type=QueryType.STRATEGY_SIGNAL, horizon="medium")
        project_results = dispatcher.dispatch(query)
        agg = aggregator.aggregate(project_results, code)

        initial_signals = {proj: result.signal for proj, result in project_results.items()}
        initial_reasons = {proj: result.reason or "" for proj, result in project_results.items()}
        conflicts = detect_conflicts(initial_signals, initial_reasons, code)

        hybrid_result = None
        if conflicts:
            hybrid_result = hybrid_engine.debate(
                stock_code=code,
                horizon="medium",
                all_results=project_results,
                initial_signals=initial_signals,
                initial_reasons=initial_reasons,
            )
            agg.signal_medium = hybrid_result.final_signal
            agg.confidence = hybrid_result.final_confidence

        score = scoring_engine.score(agg)

        viz = visualize_hybrid(code, name, project_results, score, hybrid_result, conflicts)
        print(viz)
        print()

        all_summary.append({
            'code': code,
            'name': name,
            'signal': hybrid_result.final_signal if hybrid_result else agg.signal_medium,
            'confidence': hybrid_result.final_confidence if hybrid_result else agg.confidence,
            'method': hybrid_result.converged_method if hybrid_result else 'none',
            'score': score.composite_score,
        })

    # 汇总
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    print(f"{'代码':<8} {'名称':<8} {'信号':<6} {'置信度':<8} {'收敛方式':<20} {'评分':<6}")
    print("-" * 70)
    for s in all_summary:
        print(f"{s['code']:<8} {s['name']:<8} {s['signal']:<6} {s['confidence']:.0%}       {s['method']:<20} {s['score']:.1f}")


if __name__ == "__main__":
    analyze_hybrid()
