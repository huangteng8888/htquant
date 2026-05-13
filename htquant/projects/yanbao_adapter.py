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

    数据源优先级：
    1. reports.db（yanbao_query SDK）— 近90天研报
    2. quantdb.analyst_reports（fallback）— 近365天研报（数据源不同，置信度×0.8）

    信号映射（东财评级 → htquant 5-tier）：
      强烈推荐/买入    → 增持  (Buy)
      推荐/增持       → 买入  (Overweight)
      中性/持有       → 持有  (Hold)
      减持/卖出       → 减持  (Underweight/Sell)
    """

    # quantdb analyst_reports 评级映射（em_rating_name → 5-tier）
    QUANTDB_RATING_MAP = {
        '买入': '增持', '增持': '增持',
        '推荐': '买入', '谨慎推荐': '持有',
        '中性': '持有', '持有': '持有',
        '减持': '减持', '卖出': '减持',
        '强烈买入': '增持', '强烈推荐': '增持',
        '同步大势': '持有', '跟随大势': '持有',
    }

    def __init__(
        self,
        project_path: str = "/home/ht/github/yanbao2-analytics",
        cache_path: str = "/tmp/yanbao_signals.db",
        days: int = 90,  # 扩大到90天
        quantdb_path: str = "/mnt/data/金融数据/quantdb/quantdb.sqlite",
    ):
        super().__init__(project_path)
        self.days = days
        self.cache_path = cache_path
        self.quantdb_path = quantdb_path
        self._rq = None
        self._quantdb_conn = None

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
            logger.warning(f"[YanbaoReportAdapter] reports.db 不可用: {e}")
            return False

    def _get_quantdb_rating(self, stock_code: str, days: int = 365) -> Optional[dict]:
        """从 quantdb.analyst_reports 获取评级（fallback，数据源不一致，置信度×0.8）"""
        try:
            import sqlite3
            cutoff = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
            conn = sqlite3.connect(self.quantdb_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT code, stock_name, org_name, em_rating_name, rating_value, publish_date
                FROM analyst_reports
                WHERE code = ?
                  AND publish_date >= ?
                  AND rating_value IS NOT NULL
                ORDER BY publish_date DESC
            """, (stock_code, cutoff))
            rows = cur.fetchall()
            conn.close()

            if not rows:
                return None

            ratings = [r[4] for r in rows if r[4] is not None]
            labels = [r[3] for r in rows if r[3]]
            brokers = list(dict.fromkeys(r[2] for r in rows if r[2]))

            avg_rating = sum(ratings) / len(ratings) if ratings else None

            # 信号生成（quantdb 用自己的评级映射）
            signal_label = 'NEUTRAL'
            if labels:
                label_count = {}
                for lbl in labels:
                    mapped = self.QUANTDB_RATING_MAP.get(lbl, '持有')
                    label_count[mapped] = label_count.get(mapped, 0) + 1
                if label_count:
                    signal_label = max(label_count, key=label_count.get)

            return {
                'stock_code': stock_code,
                'stock_name': rows[0][1] if rows[0][1] else stock_code,
                'coverage': len(rows),
                'avg_rating': round(avg_rating, 3) if avg_rating else None,
                'max_rating': max(ratings) if ratings else None,
                'min_rating': min(ratings) if ratings else None,
                'brokers': brokers[:10],
                'latest_date': rows[0][5] if rows[0][5] else '',
                'signal_label': signal_label,
                'source': 'quantdb',  # 标记为 fallback 数据源
            }
        except Exception as e:
            logger.warning(f"[YanbaoReportAdapter] quantdb fallback 失败 {stock_code}: {e}")
            return None

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
                confidence=0.30,
            )

        stock_codes = query.stock_codes
        if not stock_codes:
            return ProjectResult(
                project_name="yanbao_reports",
                success=True,
                data={},
                signal="观望",
                confidence=0.30,
                reason="无股票代码",
            )

        results = {}
        signals = []
        evidence_list = []

        for code in stock_codes:
            try:
                rating_data = self._rq.get_rating_signal(code, days=self.days)
                source = 'reports.db'

                # Fallback: reports.db 无数据 → 查 quantdb.analyst_reports
                if not (rating_data and rating_data.get("coverage", 0) > 0):
                    rating_data = self._get_quantdb_rating(code, days=365)
                    source = 'quantdb'

                if rating_data and rating_data.get("coverage", 0) > 0:
                    rating_data['source'] = source
                    results[code] = rating_data
                    signal = self._label_to_signal(rating_data.get("signal_label", "HOLD"))
                    signals.append(signal)

                    cnt = rating_data.get("coverage", 0)
                    avg_rating = rating_data.get("avg_rating", "N/A")
                    latest_date = rating_data.get("latest_date", "")
                    brokers = rating_data.get("brokers", [])
                    fb_note = " [quantdb]" if source == "quantdb" else ""
                    ev = (
                        f"{code}近{self.days}天共{cnt}篇({latest_date})，"
                        f"均评{avg_rating}，{len(brokers)}家券商{fb_note}。"
                    )
                    evidence_list.append(ev)
                else:
                    results[code] = None
            except Exception as e:
                logger.warning(f"[YanbaoReportAdapter] 获取 {code} 研报信号失败: {e}")
                results[code] = None

        if not signals:
            # 真实无研报数据：返回低置信度观望（而非 conf=0.0 导致权重归零）
            return ProjectResult(
                project_name="yanbao_reports",
                success=True,
                data=results,
                signal="观望",
                confidence=0.35,
                reason="近90天 reports.db 无研报覆盖，且 quantdb 365天内也无数据",
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
        """将 signal_label（中文或BUY/SELL/HOLD英文）转为 htquant 信号。"""
        if label in ('增持', '买入', '持有', '减持', '清仓'):
            return label
        mapping = {
            "BUY": "增持",
            "OVERWEIGHT": "买入",
            "NEUTRAL": "持有",
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
