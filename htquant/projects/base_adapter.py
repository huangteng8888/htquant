"""
htquant 项目适配器基类
"""
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
