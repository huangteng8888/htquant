"""
htquant 主入口
一行命令分析股票
"""
import argparse
import logging
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from htquant.dispatcher import Dispatcher, Query, QueryType, get_dispatcher
from htquant.aggregator import Aggregator, AggregatedResult, get_aggregator
from htquant.debate import DebateEngine, get_debate_engine
from htquant.scoring import ScoringEngine, StockScore, get_scoring_engine
from htquant.config import DEFAULT_STOCKS, STOCK_CODE_MAPPING

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


def analyze_stocks(stock_codes=None, horizon="medium", force_debate=False):
    """
    分析股票
    
    Args:
        stock_codes: 股票代码列表，默认分析7只
        horizon: short/medium/long
        force_debate: 强制触发辩论
    """
    if stock_codes is None:
        stock_codes = DEFAULT_STOCKS
    
    logger.info(f"开始分析 {len(stock_codes)} 只股票...")
    
    # 初始化组件
    dispatcher = get_dispatcher()
    aggregator = get_aggregator()
    scoring_engine = get_scoring_engine()
    
    available = dispatcher.list_available()
    logger.info(f"可用项目: {available}")
    
    all_scores = []
    all_aggregated = {}
    
    # 逐只分析
    for code in stock_codes:
        name = STOCK_CODE_MAPPING.get(code, (None, code))[1]
        logger.info(f"\n{'='*50}")
        logger.info(f"分析: {name}({code})")
        
        # 创建查询
        query = Query(
            stock_codes=[code],
            query_type=QueryType.STRATEGY_SIGNAL,
            horizon=horizon,
            force_debate=force_debate,
        )
        
        # 分发到各项目
        results = dispatcher.dispatch(query)
        
        # 聚合结果
        aggregated = aggregator.aggregate(results, code, horizon)
        all_aggregated[code] = aggregated
        
        # 检查是否需要辩论
        need_debate = aggregator.need_debate(aggregated) or force_debate
        
        if need_debate and aggregated.conflicts:
            logger.info(f"检测到冲突，触发辩论...")
            debate_engine = get_debate_engine()
            debate_results = debate_engine.debate_all(aggregated.conflicts, results)
            
            # 应用辩论结果
            for dr in debate_results:
                logger.info(f"辩论结论: {dr.final_signal}")
                aggregated.signal_medium = dr.final_signal
                aggregated.confidence = dr.final_confidence
        
        # 评分
        score = scoring_engine.score(aggregated)
        all_scores.append(score)
        
        # 打印结果
        print(f"\n{'='*60}")
        print(f"【{name}({code})】")
        print(f"  信号: {aggregated.signal_medium}")
        print(f"  仓位: {aggregated.position_weight*100:.1f}%")
        print(f"  置信度: {aggregated.confidence*100:.1f}%")
        print(f"  综合评分: {score.composite_score:.1f}/100")
        
        if aggregated.project_signals:
            print(f"  各项目信号:")
            for proj, sig in aggregated.project_signals.items():
                print(f"    - {proj}: {sig}")
        
        if aggregated.conflicts:
            print(f"  冲突:")
            for c in aggregated.conflicts:
                print(f"    - {c.project_a}({c.signal_a}) vs {c.project_b}({c.signal_b})")
        
        print(f"\n  理由:")
        for reason in aggregated.reasons[:3]:
            print(f"    {reason}")
    
    # 排序输出
    print(f"\n{'='*60}")
    print("综合评分排序:")
    ranked = scoring_engine.rank(all_scores)
    for i, s in enumerate(ranked, 1):
        print(f"  {i}. {s.stock_name}({s.stock_code}): {s.composite_score:.1f}分 - {s.signal}")
    
    return ranked


def main():
    parser = argparse.ArgumentParser(description='htquant - 量化研究聚合引擎')
    parser.add_argument('--stocks', nargs='+', default=None, help='股票代码')
    parser.add_argument('--horizon', choices=['short', 'medium', 'long'], default='medium')
    parser.add_argument('--debate', action='store_true', help='强制触发辩论')
    parser.add_argument('--list-projects', action='store_true', help='列出可用项目')
    
    args = parser.parse_args()
    
    if args.list_projects:
        dispatcher = get_dispatcher()
        available = dispatcher.list_available()
        print(f"可用项目: {', '.join(available)}")
        return
    
    analyze_stocks(args.stocks, args.horizon, args.debate)


if __name__ == "__main__":
    main()
