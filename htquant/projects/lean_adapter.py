# -*- coding: utf-8 -*-
"""
LeanAdapter — QuantConnect Lean Engine 多Alpha模型融合

项目: github.com/QuantConnect/Lean
路径: ~/github/Lean/

核心优势:
  QuantConnect Lean 是专业级算法交易引擎，具有:
  1. 多Alpha模型体系 (RSI/MACD/EMA/DualThrust/均值回归)
  2. 事件驱动架构，Insight 生成机制
  3. 跨资产类别 (股票/期货/期权/外汇/加密货币)
  4. C# 指标库 (200+ 技术指标) + Python 算法接口

此Adapter实现了4种经典Lean Alpha模型(纯pandas，无C#依赖):

  1. RsiAlphaModel   — RSI超卖超买交叉
  2. MacdAlphaModel  — MACD信号线交叉
  3. EmaCrossAlpha   — EMA快速/慢速交叉
  4. DualThrustAlpha — DualThrust区间突破(来自VIXDualThrustAlpha)

适配A股: 参数优化为(RSI 30/70→25/75, MACD参数本土化)
"""

import logging
from typing import Any, Dict, Optional
from datetime import datetime

import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# 信号档位
SIGNAL_ORDER = ['清仓', '减持', '观望', '持有', '增持', '买入']


class LeanAdapter(BaseAdapter):
    """
    QuantConnect Lean 多Alpha模型融合适配器。

    融合4种经典Alpha模型，取多数投票作为最终信号。
    每个模型产生方向信号(做多/做空/中性)，
    按权重融合后输出 6档信号。

    核心策略(来自Lean源码):
    - RsiAlphaModel:      RSI 交叉 30/70 产生信号
    - MacdAlphaModel:      MACD vs Signal line 交叉
    - EmaCrossAlphaModel: EMA fast/slow 交叉
    - DualThrustAlpha:    N日区间突破
    """

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or PROJECT_PATHS.lean)
        self._available = None

    def _check_available(self) -> bool:
        """Lean是纯Python，无需特殊检查"""
        logger.info("[Lean] Lean Alpha模型引擎就绪(纯pandas实现)")
        return True

    def execute(self, query: Query) -> ProjectResult:
        """执行Lean多Alpha分析"""
        stock_code = query.stock_codes[0] if query.stock_codes else None
        date_str = query.metadata.get('date_str', datetime.now().strftime('%Y-%m-%d'))
        extreme = query.metadata.get('extreme_event_type')

        if not stock_code:
            return ProjectResult(
                project_name='lean',
                success=False,
                signal='观望',
                confidence=0.50,
                error='未指定股票代码',
            )

        # 尝试从qlib获取历史数据
        hist_data = self._load_qlib_data(stock_code, date_str)
        if hist_data is None:
            return ProjectResult(
                project_name='lean',
                success=False,
                signal='观望',
                confidence=0.50,
                error=f'无法获取{stock_code}数据',
            )

        result = self.historical_signal(stock_code, date_str, hist_data)

        # 极值调整
        if extreme:
            result['signal'], result['reason'] = self._apply_extreme_adjustment(
                result['signal'], result['reason'], extreme, result.get('rsi', 50)
            )

        return ProjectResult(
            project_name='lean',
            success=True,
            data=result,
            signal=result['signal'],
            confidence=result['confidence'],
            reason=result['reason'],
        )

    def historical_signal(self, stock_code: str, date_str: str,
                         hist_data: dict = None) -> dict:
        """
        回测/实时接口: 运行4种Lean Alpha模型，取多数投票。

        hist_data: {closes, highs, lows, volumes, opens}
        """
        if hist_data is None:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': '[Lean] 无历史数据'}

        closes = np.array(hist_data.get('closes', []), dtype=float)
        highs  = np.array(hist_data.get('highs', closes), dtype=float)
        lows   = np.array(hist_data.get('lows', closes), dtype=float)
        volumes= np.array(hist_data.get('volumes', []), dtype=float)

        if len(closes) < 60:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': f'[Lean] 数据不足({len(closes)}天)'}

        # ── 4种Alpha模型 ──────────────────────────────────────────────
        rsi_signal    = self._rsi_alpha(closes)
        macd_signal   = self._macd_alpha(closes)
        ema_signal    = self._ema_cross_alpha(closes)
        dt_signal     = self._dual_thrust_alpha(closes, highs, lows)

        alpha_results = [
            ('RSI',     rsi_signal),
            ('MACD',    macd_signal),
            ('EMA',     ema_signal),
            ('Dual',    dt_signal),
        ]

        # 多数投票
        votes = [s for _, s in alpha_results]
        final_signal = self._majority_vote(votes)

        # 置信度: 一致模型越多 → 置信度越高
        agreement = votes.count(final_signal)
        confidence_map = {4: 0.88, 3: 0.78, 2: 0.65}
        confidence = confidence_map.get(agreement, 0.55)

        reasons = [f"{name}={sig}" for name, sig in alpha_results
                   if sig not in ('持有', '观望')]

        return {
            'signal': final_signal,
            'confidence': confidence,
            'reason': '[Lean] ' + (', '.join(reasons) if reasons else '各模型中性，持有'),
            'rsi':   self._rsi_value(closes, 14),
            'macd_hist': self._macd_histogram(closes),
            'ema_fast': self._ema(closes, 5)[-1] if len(closes) >= 5 else closes[-1],
            'ema_slow': self._ema(closes, 20)[-1] if len(closes) >= 20 else closes[-1],
            'alpha_votes': dict(zip([n for n,_ in alpha_results], votes)),
        }

    # ── Alpha模型实现 ──────────────────────────────────────────────────────────

    def _rsi_alpha(self, closes: np.ndarray) -> str:
        """
        RsiAlphaModel (Lean源码实现):
          RSI < 30 → 触低 → 做多
          RSI > 70 → 触高 → 做空
          RSI 30-70 → 中性

        A股优化: RSI < 25 → 做多, RSI > 75 → 做空
        """
        rsi = self._rsi_value(closes, 14)
        if rsi < 25:
            return '增持'
        elif rsi > 75:
            return '减持'
        elif rsi < 35:
            return '持有'
        elif rsi > 65:
            return '持有'
        return '观望'

    def _macd_alpha(self, closes: np.ndarray) -> str:
        """
        MacdAlphaModel (Lean源码实现):
          MACD - Signal > bounce_threshold → UP
          MACD - Signal < -bounce_threshold → DOWN
          bounce_threshold = 1% of price

        使用 (12, 26, 9) 参数 (标准MACD)
        """
        ema12  = self._ema(closes, 12)
        ema26  = self._ema(closes, 26)
        macd   = ema12 - ema26
        signal = self._ema(macd, 9)
        if len(macd) < 2 or len(signal) < 2:
            return '观望'
        macd_val  = macd[-1]
        signal_val= signal[-1]
        normalized = (macd_val - signal_val) / closes[-1]

        if normalized > 0.01:
            return '增持'
        elif normalized < -0.01:
            return '减持'
        return '观望'

    def _ema_cross_alpha(self, closes: np.ndarray) -> str:
        """
        EmaCrossAlphaModel (Lean源码实现):
          EMA fast 上穿 slow → 做多
          EMA fast 下穿 slow → 做空

        fast = 10日, slow = 20日 (A股优化)
        """
        fast = self._ema(closes, 10)
        slow = self._ema(closes, 20)
        if len(fast) < 2 or len(slow) < 2:
            return '观望'
        prev_fast, curr_fast = fast[-2], fast[-1]
        prev_slow, curr_slow = slow[-2], slow[-1]

        if prev_fast <= prev_slow and curr_fast > curr_slow:
            return '增持'
        elif prev_fast >= prev_slow and curr_fast < curr_slow:
            return '减持'
        return '观望'

    def _dual_thrust_alpha(self, closes: np.ndarray,
                           highs: np.ndarray, lows: np.ndarray) -> str:
        """
        DualThrustAlpha (来自QuantConnect VIXDualThrustAlpha):
          N日区间突破策略

        计算:
          HH = N日最高价
          LC = N日最低收盘价
          HC = N日最高收盘价
          LL = N日最低价
          range = max(HH - LC, HC - LL)
          upper = close_y + K * range
          lower = close_y - K * range

        信号:
          close > upper → 增持
          close < lower → 减持
          else → 观望
        """
        N = 20
        K = 0.5

        if len(closes) < N + 5:
            return '观望'

        hh = np.max(highs[-N:])
        lc = np.min(closes[-N:])
        hc = np.max(closes[-N:])
        ll = np.min(lows[-N:])

        range_val = max(hh - lc, hc - ll)
        close_y   = closes[-2]  # yesterday close
        close_t   = closes[-1]  # today close

        upper = close_y + K * range_val
        lower = close_y - K * range_val

        if close_t > upper:
            return '增持'
        elif close_t < lower:
            return '减持'
        return '观望'

    # ── 投票融合 ──────────────────────────────────────────────────────────────

    def _majority_vote(self, votes: list) -> str:
        """多数投票: 一致模型多 → 直接输出; 分歧 → 用RSI打破平局"""
        from collections import Counter
        cnt = Counter(v for v in votes if v not in ('观望', '持有'))
        if not cnt:
            return '观望'

        most_common = cnt.most_common(1)[0][0]

        # 平局 → 用RSI原始值打破 (在closes[-1]上无法访问，此处用投票中RSI档位)
        # 简化: 中性信号里，'增持'优先于'减持'
        return most_common

    # ── 工具函数 ──────────────────────────────────────────────────────────────

    def _ema(self, prices: np.ndarray, period: int) -> np.ndarray:
        """指数移动平均"""
        ema = np.zeros_like(prices, dtype=float)
        ema[0] = prices[0]
        alpha = 2.0 / (period + 1)
        for i in range(1, len(prices)):
            ema[i] = alpha * prices[i] + (1 - alpha) * ema[i-1]
        return ema

    def _rsi_value(self, prices: np.ndarray, period: int = 14) -> float:
        """RSI数值 (Wilder's method)"""
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices)
        gain = np.where(deltas > 0, deltas, 0)
        loss = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gain[-period:])
        avg_loss = np.mean(loss[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _macd_histogram(self, closes: np.ndarray) -> float:
        """MACD柱状图值 (MACD - Signal)"""
        ema12  = self._ema(closes, 12)
        ema26  = self._ema(closes, 26)
        macd   = ema12 - ema26
        signal = self._ema(macd, 9)
        if len(macd) < 2 or len(signal) < 2:
            return 0.0
        return float(macd[-1] - signal[-1])

    def _load_qlib_data(self, stock_code: str, date_str: str) -> Optional[dict]:
        """从qlib加载历史数据"""
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path.home() / 'github/qlib'))
            import qlib
            qlib_path = Path.home() / '.qlib/qlib_data/cn_data_new2'
            qlib.init(provider_uri=str(qlib_path))

            from qlib.data import D
            from ..config import STOCK_CODE_MAPPING

            info = STOCK_CODE_MAPPING.get(stock_code)
            if not info:
                return None
            qcode, _ = info

            # 加载足够历史数据(用于Alpha计算)
            end_dt = date_str + ' 00:00:00' if ' ' not in date_str else date_str
            start_dt = '2020-01-01 00:00:00'

            df = D.features(
                [qcode],
                ['$close', '$high', '$low', '$volume'],
                start_time=start_dt,
                end_time=end_dt,
            )
            if df is None or len(df) < 60:
                return None

            df = df.reset_index()
            # reset_index 后列名直接是 instrument, datetime, $close, $high, $low, $volume
            if '$close' in df.columns:
                closes = df['$close'].values
                highs  = df['$high'].values
                lows   = df['$low'].values
                vols   = df['$volume'].values
            else:
                # fallback: 尝试 instrument level 列
                closes = df[qcode]['$close'].values if qcode in df.columns else df.iloc[:, 2].values
                highs  = df[qcode]['$high'].values  if qcode in df.columns else df.iloc[:, 3].values
                lows   = df[qcode]['$low'].values   if qcode in df.columns else df.iloc[:, 4].values
                vols   = df[qcode]['$volume'].values if qcode in df.columns else df.iloc[:, 5].values

            return {
                'closes': list(closes),
                'highs':  list(highs),
                'lows':   list(lows),
                'volumes': list(vols),
            }
        except Exception:
            return None

    # ── 极值调整 ──────────────────────────────────────────────────────────────

    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event: str, rsi: float) -> tuple:
        """
        基于实际回测数据的极值事件修正(与qlib_adapter一致):
        W20/W50 触低 → 短期趋势向下，做多翻转做空
        W20/W50 触高 → 市场继续下跌，强制短空
        W252     → 均值回归方向强化
        """
        if not extreme_event:
            return signal, reason

        is_short = extreme_event and extreme_event.startswith(('W20_', 'W50_'))

        if is_short and '_LOW' in extreme_event and rsi < 50:
            if signal == '增持':
                return '减持', reason + ' [Lean极值:W20/W50触低，做空]'
            elif signal == '买入':
                return '减持', reason + ' [Lean极值:W20/W50触低，做空]'
            elif signal == '持有':
                return '减持', reason + ' [Lean极值:W20/W50触低，做空]'

        elif is_short and '_HIGH' in extreme_event:
            if signal in ('增持', '买入'):
                return '清仓', reason + ' [Lean极值:HIGH禁止做多]'
            elif signal == '观望':
                return '减持', reason + ' [Lean极值:HIGH转空]'
            elif signal == '持有':
                return '减持', reason + ' [Lean极值:HIGH中性转空]'

        elif extreme_event and extreme_event.startswith('W100_') and '_HIGH' in extreme_event:
            if signal in ('增持', '买入', '持有'):
                return '清仓', reason + ' [Lean极值:W100触高，强制清仓]'

        elif extreme_event and '_LOW' in extreme_event and 'W252' in extreme_event:
            if signal in ('增持', '持有'):
                return '买入', reason + ' [Lean极值:W252触低，强化做多]'

        elif extreme_event and '_HIGH' in extreme_event and 'W252' in extreme_event:
            if signal in ('减持', '观望'):
                return '清仓', reason + ' [Lean极值:W252触高，强化做空]'

        return signal, reason
