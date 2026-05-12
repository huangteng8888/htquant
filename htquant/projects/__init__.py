# ─── 项目适配器注册表 ──────────────────────────────────────────────────────────

ADAPTERS = {}


def register_adapter(name: str, adapter_class):
    """注册项目适配器（供 Dispatcher 自动发现）"""
    ADAPTERS[name] = adapter_class


# 延迟导入避免循环依赖
def _register_all():
    from .tradingagents_adapter import TradingAgentsAdapter
    from .backtrader_adapter import BacktraderAdapter
    from .freqtrade_adapter import FreqtradeAdapter
    from .vnpy_adapter import VnpyAdapter
    from .qlib_adapter import QlibAdapter
    from .finrl_adapter import FinrlAdapter
    from .yanbao_adapter import YanbaoReportAdapter

    register_adapter('tradingagents', TradingAgentsAdapter)
    register_adapter('backtrader', BacktraderAdapter)
    register_adapter('freqtrade', FreqtradeAdapter)
    register_adapter('vnpy', VnpyAdapter)
    register_adapter('qlib', QlibAdapter)
    register_adapter('finrl', FinrlAdapter)
    register_adapter('yanbao_reports', YanbaoReportAdapter)


# 自动注册
try:
    _register_all()
except Exception as e:
    import logging
    logging.getLogger(__name__).warning(f"适配器自动注册失败: {e}")
from abc import ABC, abstractmethod
from typing import Any, Optional
from pathlib import Path
import logging

from ..dispatcher import Query, ProjectResult

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """项目适配器基类"""
    
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.available = None  # 缓存可用性检查
    
    def is_available(self) -> bool:
        """检查项目是否可用"""
        if self.available is not None:
            return self.available
        
        self.available = self._check_available()
        return self.available
    
    @abstractmethod
    def _check_available(self) -> bool:
        """实际检查逻辑，子类实现"""
        pass
    
    @abstractmethod
    def execute(self, query: Query) -> ProjectResult:
        """执行查询并返回结果"""
        pass
    
    def _validate_query(self, query: Query) -> bool:
        """验证查询是否适用于本项目"""
        return True
