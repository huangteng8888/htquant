# -*- coding: utf-8 -*-
"""
FinancialServicesAdapter — Anthropic Claude for Financial Services 回测信号

数据来源：financial-services/project_records/backtest_results.json
内容：7只股票 × RSI/MACD/布林带 三策略历史回测（2019-2026）
      best_sharpe 作为核心信号指标

信号映射（基于 best_sharpe）：
  >= 0.50  → 增持  (conf 0.70)
  >= 0.30  → 持有  (conf 0.65)
  >= 0.10  → 观望  (conf 0.60)
  >= 0.00  → 观望  (conf 0.55)
  <  0.00  → 减持  (conf min(0.70, 0.60 + best_sharpe))

A 股相关度：★★★☆☆（回测结果，辅助参考）
"""

import logging
import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class FinancialServicesAdapter(BaseAdapter):
    """
    Anthropic Financial Services 回测结果适配器。

    读取 backtest_results.json，基于最优策略的 Sharpe Ratio
    生成中长期技术面信号。
    """

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or str(PROJECT_PATHS.financial_services))
        self._backtest_path: Optional[Path] = None
        self._backtest_data: Optional[Dict] = None

    def _check_available(self) -> bool:
        """检查 backtest_results.json 是否存在"""
        candidates = [
            Path(self.project_path) / "project_records" / "backtest_results.json",
            Path.home() / "github" / "financial-services" / "project_records" / "backtest_results.json",
        ]
        for p in candidates:
            if p.exists():
                self._backtest_path = p
                self._load_data()
                logger.info(f"[FinancialServices] 可用，数据来源: {p}")
                return True

        logger.warning("[FinancialServices] backtest_results.json 未找到")
        return False

    def _load_data(self) -> None:
        """加载回测数据（只读一次）"""
        if self._backtest_data is not None or self._backtest_path is None:
            return
        try:
            with open(self._backtest_path, encoding="utf-8") as f:
                self._backtest_data = json.load(f)
            logger.info(
                f"[FinancialServices] 已加载 {len(self._backtest_data.get('stocks', {}))} 只股票回测数据"
            )
        except Exception as e:
            logger.warning(f"[FinancialServices] 数据加载失败: {e}")
            self._backtest_data = {}

    def execute(self, query: Query) -> ProjectResult:
        """执行回测信号查询"""
        if not self.is_available():
            return ProjectResult(
                project_name="financial_services",
                success=False,
                signal="观望",
                confidence=0.50,
                error="backtest_results.json 不可用",
            )

        stock_code = query.stock_codes[0] if query.stock_codes else None
        if not stock_code:
            return ProjectResult(
                project_name="financial_services",
                success=False,
                signal="观望",
                confidence=0.50,
                error="未指定股票代码",
            )

        # 直接查原始代码（backtest_results.json 用 6 位原始代码作 key）
        stock_data = self._backtest_data.get("stocks", {}).get(stock_code)

        if not stock_data:
            return ProjectResult(
                project_name="financial_services",
                success=False,
                signal="观望",
                confidence=0.50,
                error=f"股票 {stock_code} 无回测数据（仅支持 000901/300777/688089/300896/301071/600422/300363）",
            )

        return self._build_result(stock_code, stock_data)

    def _build_result(self, stock_code: str, data: Dict[str, Any]) -> ProjectResult:
        """基于回测数据构建信号"""
        strategies = data.get("strategies", {})
        best_sharpe = data.get("best_sharpe", 0.0)
        best_return = data.get("best_return", 0.0)
        best_strategy = data.get("best_strategy", "未知")

        # 信号映射
        signal, confidence = self._sharpe_to_signal(best_sharpe)

        # 构建理由
        strategy_details = []
        for name, s in strategies.items():
            ret = s.get("total_return", 0)
            sh = s.get("sharpe", 0)
            strategy_details.append(f"{name}收益率{ret:+.1f}%/Sharpe{sh:.3f}")

        reason = (
            f"[Claude FS 回测] 最优策略:{best_strategy} "
            f"收益率{best_return:+.1f}% Sharpe={best_sharpe:.3f}；"
            + " | ".join(strategy_details)
        )

        return ProjectResult(
            project_name="financial_services",
            success=True,
            data={
                "stock_code": stock_code,
                "best_strategy": best_strategy,
                "best_return": best_return,
                "best_sharpe": best_sharpe,
                "strategies": strategies,
                "signal": signal,
                "confidence": confidence,
            },
            signal=signal,
            confidence=confidence,
            reason=reason,
        )

    def _sharpe_to_signal(self, sharpe: float) -> tuple:
        """
        将 best_sharpe 转换为信号和置信度。

        Sharpe > 0 表示策略在风险调整后有正收益。
        """
        if sharpe >= 0.50:
            return "增持", 0.70
        elif sharpe >= 0.30:
            return "持有", 0.65
        elif sharpe >= 0.10:
            return "观望", 0.60
        elif sharpe >= 0.00:
            return "观望", 0.55
        else:
            # 负Sharpe：越低越看空，置信度随之下调
            confidence = max(0.40, min(0.70, 0.60 + sharpe))
            return "减持", round(confidence, 2)
