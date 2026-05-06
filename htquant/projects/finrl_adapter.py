"""
FinRL 适配器（stub）
强化学习量化交易策略
"""
from ..dispatcher import Query, ProjectResult
from .base_adapter import BaseAdapter

class FinrlAdapter(BaseAdapter):
    """FinRL强化学习策略适配器"""
    
    def _check_available(self) -> bool:
        """检查FinRL是否可用"""
        try:
            import finrl
            return True
        except ImportError:
            return False
    
    def execute(self, query: Query) -> ProjectResult:
        """执行FinRL分析"""
        return ProjectResult(
            project_name="finrl",
            success=False,
            data=None,
            error="FinRL适配器待实现"
        )
