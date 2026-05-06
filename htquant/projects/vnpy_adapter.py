"""
vnpy 适配器（stub）
国产量化交易框架
"""
from ..dispatcher import Query, ProjectResult
from .base_adapter import BaseAdapter

class VnpyAdapter(BaseAdapter):
    """vnpy交易执行适配器"""
    
    def _check_available(self) -> bool:
        """检查vnpy是否可用"""
        try:
            import vnpy
            return True
        except ImportError:
            return False
    
    def execute(self, query: Query) -> ProjectResult:
        """执行vnpy分析"""
        return ProjectResult(
            project_name="vnpy",
            success=False,
            data=None,
            error="vnpy适配器待实现"
        )
