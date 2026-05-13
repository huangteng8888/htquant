"""
htquant 查询分发器
将用户查询分发给正确的量化项目适配器
"""
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum

from .config import PROJECT_PATHS, HORIZON, SIGNALS, STOCK_CODE_MAPPING

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """查询类型"""
    TECHNICAL_ANALYSIS = "technical_analysis"   # 技术分析
    BACKTEST = "backtest"                       # 策略回测
    FACTOR_ANALYSIS = "factor_analysis"        # 因子分析
    STRATEGY_SIGNAL = "strategy_signal"         # 策略信号
    PORTFOLIO = "portfolio"                     # 组合建议
    DEBATE_REQUEST = "debate_request"           # 辩论请求


@dataclass
class Query:
    """查询请求"""
    stock_codes: List[str]                      # 股票代码列表
    query_type: QueryType                       # 查询类型
    horizon: str = "medium"                      # short/medium/long
    strategy: Optional[str] = None              # 指定策略
    force_debate: bool = False                  # 强制触发辩论
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectResult:
    """单个项目的结果"""
    project_name: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    confidence: float = 1.0                      # 置信度 0~1
    signal: str = "观望"                         # 操作信号
    reason: str = ""                            # 理由
    evidence: List[str] = field(default_factory=list)  # 证据列表


class Dispatcher:
    """
    查询分发器
    
    负责：
    1. 解析用户查询类型
    2. 调用对应的项目适配器
    3. 收集并返回结果
    """
    
    def __init__(self):
        self.projects = {}
        self._init_projects()
    
    def _init_projects(self):
        """初始化各项目适配器"""
        from .projects.qlib_adapter import QlibAdapter
        from .projects.backtrader_adapter import BacktraderAdapter
        from .projects.momentum_adapter import MomentumAdapter
        from .projects.finrl_adapter import FinrlAdapter
        from .projects.freqtrade_adapter import FreqtradeAdapter
        from .projects.vnpy_adapter import VnpyAdapter
        from .projects.tradingagents_adapter import TradingAgentsAdapter
        from .projects.fincept_adapter import FinceptAdapter
        from .projects.gsquant_adapter import GsQuantAdapter
        from .projects.lean_adapter import LeanAdapter
        from .projects.yanbao_adapter import YanbaoReportAdapter
        from .projects.financial_services_adapter import FinancialServicesAdapter

        self.projects = {
            "qlib":            QlibAdapter(str(PROJECT_PATHS.qlib)),
            "backtrader":      BacktraderAdapter(str(PROJECT_PATHS.backtrader)),
            "momentum":        MomentumAdapter(),
            "finrl":           FinrlAdapter(),
            "freqtrade":       FreqtradeAdapter(str(PROJECT_PATHS.freqtrade)),
            "vnpy":            VnpyAdapter(str(PROJECT_PATHS.vnpy)),
            "tradingagents":   TradingAgentsAdapter(str(PROJECT_PATHS.tradingagents)),
            "fincept":         FinceptAdapter(),
            "gs_quant":        GsQuantAdapter(),
            "lean":            LeanAdapter(),
            "yanbao_reports":  YanbaoReportAdapter(),
            "financial_services": FinancialServicesAdapter(),
        }
        
        # 检查哪些项目可用
        self.available = {}
        for name, adapter in self.projects.items():
            if adapter.is_available():
                self.available[name] = adapter
                logger.info(f"[htquant] 项目可用: {name}")
            else:
                logger.warning(f"[htquant] 项目不可用: {name}")
    
    def dispatch(self, query: Query) -> Dict[str, ProjectResult]:
        """
        分发查询到各项目

        Args:
            query: Query对象

        Returns:
            Dict[项目名, ProjectResult]
        """
        results = {}

        # 根据查询类型决定调用哪些项目
        target_projects = self._get_target_projects(query)

        for project_name in target_projects:
            if project_name not in self.available:
                results[project_name] = ProjectResult(
                    project_name=project_name,
                    success=False,
                    data=None,
                    error=f"项目 {project_name} 不可用"
                )
                continue

            adapter = self.available[project_name]
            try:
                result = adapter.execute(query)
                results[project_name] = result
            except Exception as e:
                logger.error(f"[htquant] {project_name} 执行失败: {e}")
                results[project_name] = ProjectResult(
                    project_name=project_name,
                    success=False,
                    data=None,
                    error=str(e)
                )

        return results

    def dispatch_per_stock(self, query: Query) -> Dict[str, Dict[str, ProjectResult]]:
        """
        按股票分发查询（per-stock 模式）

        对 query.stock_codes 中的每只股票，分别调用所有 adapter，
        确保每只股票获得独立的 adapter 信号（而非批量级信号）。

        Returns:
            Dict[stock_code, Dict[adapter_name, ProjectResult]]
        """
        # Query 和 ProjectResult 都定义在同一个文件，直接引用
        stock_codes = query.stock_codes or []
        target_projects = self._get_target_projects(query)

        # 初始化：每只股票一个空结果字典
        per_stock_results: Dict[str, Dict[str, ProjectResult]] = {
            code: {} for code in stock_codes
        }

        # 对每只股票单独构建 Query 并调用所有 adapter
        for code in stock_codes:
            single_query = Query(
                stock_codes=[code],
                query_type=query.query_type,
                metadata={**query.metadata, 'batch_code': code}
            )

            for project_name in target_projects:
                if project_name not in self.available:
                    per_stock_results[code][project_name] = ProjectResult(
                        project_name=project_name,
                        success=False,
                        data=None,
                        error=f"项目 {project_name} 不可用"
                    )
                    continue

                adapter = self.available[project_name]
                try:
                    result = adapter.execute(single_query)
                    per_stock_results[code][project_name] = result
                except Exception as e:
                    logger.error(f"[htquant] {project_name} 执行失败 ({code}): {e}")
                    per_stock_results[code][project_name] = ProjectResult(
                        project_name=project_name,
                        success=False,
                        data=None,
                        error=str(e)
                    )

        return per_stock_results
    
    def _get_target_projects(self, query: Query) -> List[str]:
        """根据查询类型确定目标项目"""
        mapping = {
            QueryType.TECHNICAL_ANALYSIS: ["qlib", "lean"],
            QueryType.BACKTEST: ["backtrader", "vnpy", "lean"],
            QueryType.FACTOR_ANALYSIS: ["qlib", "gs_quant"],
            QueryType.STRATEGY_SIGNAL: ["qlib", "momentum", "lean", "backtrader",
                                        "vnpy", "finrl", "fincept", "gs_quant",
                                        "yanbao_reports", "financial_services",
                                        "freqtrade", "tradingagents"],
            QueryType.PORTFOLIO: ["qlib", "backtrader", "momentum", "lean"],
            QueryType.DEBATE_REQUEST: ["qlib", "momentum", "lean", "tradingagents",
                                       "backtrader", "vnpy", "yanbao_reports",
                                       "financial_services"],
        }
        return mapping.get(query.query_type, ["qlib"])
    
    def dispatch_single(self, project_name: str, query: Query) -> ProjectResult:
        """单独调用某个项目"""
        if project_name not in self.available:
            return ProjectResult(
                project_name=project_name,
                success=False,
                data=None,
                error=f"项目 {project_name} 不可用"
            )
        
        adapter = self.available[project_name]
        return adapter.execute(query)
    
    def list_available(self) -> List[str]:
        """返回可用的项目列表"""
        return list(self.available.keys())


# 全局分发器实例
_dispatcher: Optional[Dispatcher] = None


def get_dispatcher() -> Dispatcher:
    """获取全局分发器实例"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher
