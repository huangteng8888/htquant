#!/usr/bin/env python3
"""
7只股票完整分析示例
演示htquant多项目聚合+辩论机制
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from htquant.dispatcher import Dispatcher, Query, QueryType, get_dispatcher
from htquant.aggregator import Aggregator, get_aggregator
from htquant.debate import DebateEngine, get_debate_engine
from htquant.scoring import ScoringEngine, get_scoring_engine
from htquant.config import DEFAULT_STOCKS, STOCK_CODE_MAPPING


def analyze_7stocks():
    """分析7只验证股票"""
    stocks = DEFAULT_STOCKS  # ['000901', '300777', '688089', '300896', '301071', '600422', '300363']
    
    print("="*70)
    print("htquant 量化研究聚合引擎 - 7只股票分析")
    print("="*70)
    print(f"分析标的: {[STOCK_CODE_MAPPING.get(c, (None,c))[1] for c in stocks]}")
    print()
    
    # 初始化
    dispatcher = get_dispatcher()
    aggregator = get_aggregator()
    scoring_engine = get_scoring_engine()
    
    print(f"可用项目: {dispatcher.list_available()}")
    print()
    
    results = {}
    
    for code in stocks:
        name = STOCK_CODE_MAPPING.get(code, (None, code))[1]
        print(f"\n{'='*60}")
        print(f"【{name}({code})】")
        
        query = Query(
            stock_codes=[code],
            query_type=QueryType.STRATEGY_SIGNAL,
            horizon="medium"
        )
        
        # 分发
        project_results = dispatcher.dispatch(query)
        
        # 聚合
        agg = aggregator.aggregate(project_results, code)
        
        # 检查冲突
        if aggregator.need_debate(agg):
            print(f"  -> 检测到冲突，触发辩论...")
            debate_engine = get_debate_engine()
            debate_results = debate_engine.debate_all(agg.conflicts, project_results)
            
            # 优先采用收敛辩论结果，否则保留加权投票结果
            converged_results = [dr for dr in debate_results if dr.converged]
            if converged_results:
                # 有收敛 → 取最接近"持有"的收敛结果
                priorities = aggregator.SIGNAL_PRIORITY
                best = min(
                    converged_results,
                    key=lambda dr: abs(priorities.get(dr.final_signal, 2) - 2.5)
                )
                agg.signal_medium = best.final_signal
                agg.confidence = best.final_confidence
                print(f"    -> 收敛结果: {best.final_signal}")
            else:
                # 无收敛 → 检查是否有修正
                priorities = aggregator.SIGNAL_PRIORITY
                orig_priority = priorities.get(agg.signal_medium, 2)
                best_debate = max(
                    debate_results,
                    key=lambda dr: priorities.get(dr.final_signal, 2)
                )
                debate_priority = priorities.get(best_debate.final_signal, 2)
                if debate_priority > orig_priority:
                    agg.signal_medium = best_debate.final_signal
                    agg.confidence = best_debate.final_confidence
                    print(f"    -> 辩论修正: {best_debate.final_signal}")
                else:
                    print(f"    -> 无收敛，保留聚合信号")
        
        # 评分
        score = scoring_engine.score(agg)
        
        results[code] = {
            'name': name,
            'signal': agg.signal_medium,
            'weight': agg.position_weight,
            'confidence': agg.confidence,
            'score': score,
            'project_signals': agg.project_signals,
        }
        
        # 输出
        print(f"  信号: {agg.signal_medium}")
        print(f"  仓位: {agg.position_weight*100:.1f}%")
        print(f"  评分: {score.composite_score:.1f}/100")
        
        if agg.project_signals:
            print(f"  各项目:")
            for proj, sig in agg.project_signals.items():
                print(f"    - {proj}: {sig}")
        
        if agg.conflicts:
            print(f"  冲突:")
            for c in agg.conflicts:
                print(f"    - {c.project_a}({c.signal_a}) vs {c.project_b}({c.signal_b})")
    
    # 综合排序
    print(f"\n{'='*60}")
    print("综合评分排序:")
    
    sorted_results = sorted(
        results.items(),
        key=lambda x: x[1]['score'].composite_score,
        reverse=True
    )
    
    total_weight = 0
    for i, (code, data) in enumerate(sorted_results, 1):
        print(f"  {i}. {data['name']}({code}): {data['score'].composite_score:.1f}分")
        print(f"     信号={data['signal']} 仓位={data['weight']*100:.1f}%")
        total_weight += data['weight']
    
    print(f"\n建议总仓位: {total_weight*100:.1f}%")
    
    # 详细报告
    print(f"\n{'='*60}")
    print("详细操作建议:")
    for code, data in sorted_results:
        signal_cn = {
            "买入": "强烈推荐买入",
            "增持": "建议增持",
            "持有": "可继续持有",
            "观望": "建议观望",
            "减持": "建议减持",
            "清仓": "建议清仓",
        }
        action = signal_cn.get(data['signal'], '待观察')
        print(f"  {data['name']}({code}): {action}")


if __name__ == "__main__":
    analyze_7stocks()
