# -*- coding: utf-8 -*-
"""
研报信号适配器 — YanbaoReportAdapter

通过 yanbao_query SDK 从 reports.db 获取券商研报评级信号，
为辩论引擎提供 analyst_rating 维度的证据和信号。

信号映射（东财评级 → htquant 5-tier）：
  强烈推荐/买入    → 增持  (Buy)
  推荐/增持       → 买入  (Overweight)
  中性/持有       → 持有  (Hold)
  减持/卖出       → 减持  (Underweight/Sell)

使用方式：
  1. 实时信号: adapter = YanbaoReportAdapter()
  2. 单股查询: result = adapter.execute(query)
  3. 批量预计算: YanbaoReportAdapter.batch_precompute(stock_codes)

依赖：
  pip install yfinance（可选，用于补充市场数据）
  PYTHONPATH 需包含 ~/github/yanbao2-analytics/src
"""

import sys
import logging
from pathlib import Path
from typing import Any, Optional
from datetime import date, timedelta

from ..dispatcher import Query, ProjectResult, QueryType
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# ─── Rating Mapping ──────────────────────────────────────────────────────────

# 东财评级 → htquant 信号
EM_RATING_TO_SIGNAL = {
    '强烈推荐': '增持',
    '买入':     '买入',
    '推荐':     '买入',
    '增持':     '增持',
    '中性':     '持有',
    '持有':     '持有',
    '减持':     '减持',
    '卖出':     '清仓',
    '强烈卖出': '清仓',
}

# 各评级的置信度权重
RATING_CONFIDENCE = {
    '增持':  0.75,
    '买入':  0.70,
    '持有':  0.55,
    '减持':  0.50,
    '清仓':  0.45,
}


def _load_report_query():
    """延迟导入 yanbao_query，避免循环依赖。"""
    try:
        sys.path.insert(0, '/home/ht/github/yanbao2-analytics/src')
        from yanbao_query import ReportQuery
        return ReportQuery()
    except Exception as e:
        logger.error(f"加载 yanbao_query 失败: {e}")
        return None


class YanbaoReportAdapter(BaseAdapter):
    """
    研报信号适配器

    从 reports.db 的 analyst_reports 表中读取券商研报评级，
    聚合近 N 天的评级情况，生成信号和证据。
    """

    def __init__(
        self,
        project_path: str = "/home/ht/github/yanbao2-analytics",
        cache_path: str = "/tmp/yanbao_signals.db",
        days: int = 30,
    ):
        super().__init__(project_path)
        self.days = days
        self.cache_path = cache_path
        self._rq = None

    def _check_available(self) -> bool:
        """检查 reports.db 是否可达。"""
        try:
            rq = _load_report_query()
            if rq is None:
                return False
            # 简单查询验证
            rq.get_rating_signal("600519", days=7)
            self._rq = rq
            return True
        except Exception as e:
            logger.warning(f"YanbaoReportAdapter 不可用: {e}")
            return False

    def execute(self, query: Query) -> ProjectResult:
        """
        执行研报信号查询。

        对 Query.stock_codes 中的每只股票，获取近 self.days 天的研报评级聚合信号。
        """
        if not self.is_available():
            return ProjectResult(
                project_name="yanbao_reports",
                success=False,
                data=None,
                error="reports.db 不可用或 yanbao_query 加载失败",
                signal="观望",
                confidence=0.0,
            )

        stock_codes = query.stock_codes
        if not stock_codes:
            return ProjectResult(
                project_name="yanbao_reports",
                success=True,
                data={},
                signal="观望",
                confidence=0.0,
                reason="无股票代码",
            )

        results = {}
        signals = []
        evidence_list = []

        for code in stock_codes:
            try:
                rating_data = self._rq.get_rating_signal(code, days=self.days)
                if rating_data and rating_data.get("coverage", 0) > 0:
                    results[code] = rating_data
                    signal = self._label_to_signal(rating_data.get("signal_label", "HOLD"))
                    signals.append(signal)
                    # 收集证据
                    cnt = rating_data.get("coverage", 0)
                    avg_rating = rating_data.get("avg_rating", "N/A")
                    latest_date = rating_data.get("latest_date", "")
                    brokers = rating_data.get("brokers", [])
                    ev = (
                        f"{code}近{self.days}天共{cnt}篇({latest_date})，"
                        f"均评{avg_rating}，{len(brokers)}家券商。"
                    )
                    evidence_list.append(ev)
                else:
                    results[code] = None
            except Exception as e:
                logger.warning(f"获取 {code} 研报信号失败: {e}")
                results[code] = None

        if not signals:
            return ProjectResult(
                project_name="yanbao_reports",
                success=True,
                data=results,
                signal="观望",
                confidence=0.0,
                reason="近30天无研报覆盖",
            )

        # 聚合信号：取最乐观的信号
        signal_priority = {'增持': 0, '买入': 1, '持有': 2, '减持': 3, '清仓': 4}
        avg_signal = min(signals, key=lambda s: signal_priority.get(s, 2))
        confidence = RATING_CONFIDENCE.get(avg_signal, 0.55)

        reason = f"来自{len(signals)}只股票的研报信号聚合；" + " ".join(evidence_list[:3])

        return ProjectResult(
            project_name="yanbao_reports",
            success=True,
            data=results,
            signal=avg_signal,
            confidence=confidence,
            reason=reason[:500],
            evidence=evidence_list,
        )

    def _label_to_signal(self, label: str) -> str:
        """将 signal_label (BUY/SELL/HOLD) 转为 htquant 信号。"""
        mapping = {
            "BUY": "增持",
            "OVERWEIGHT": "买入",
            "HOLD": "持有",
            "UNDERWEIGHT": "减持",
            "SELL": "清仓",
        }
        return mapping.get(label.upper(), "持有")

    @staticmethod
    def batch_precompute(
        stock_codes: list[str],
        days: int = 30,
        output_path: str = "/tmp/yanbao_signals_batch.json",
    ) -> dict:
        """
        批量预计算研报信号（用于回测预处理）。

        返回: {stock_code: rating_data}
        """
        import json
        rq = _load_report_query()
        if rq is None:
            return {}

        results = {}
        for code in stock_codes:
            try:
                rd = rq.get_rating_signal(code, days=days)
                if rd:
                    results[code] = rd
            except Exception as e:
                logger.warning(f"batch_precompute {code}: {e}")

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"batch_precompute 完成: {len(results)}/{len(stock_codes)} 只股票")
        return results
