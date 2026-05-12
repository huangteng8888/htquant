# -*- coding: utf-8 -*-
"""
GsQuantAdapter — Goldman Sachs Quantitative Research Tools

gs_quant 是 Goldman Sachs 的量化研究工具包，主要用于:
  - 利率/信用/商品衍生品定价
  - 组合风险分析
  - 因子风险模型

对 A 股股票信号能力有限，此适配器提供:
  - 波动率信号（HV Rank：20日年化 vs 252日年化）
  - Bollinger Bands 布林带偏离
  - 成交量异常检测

依赖: gs_quant (pip install gs-quant) 或 qlib 数据（默认）
"""

import logging
from typing import Any, Dict, Optional

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

SIGNAL_ORDER = ['清仓', '减持', '观望', '持有', '增持', '买入']


class GsQuantAdapter(BaseAdapter):
    """
    GS Quant 风险因子适配器。

    主要分析维度:
    - 波动率偏离（HV 20d vs HV 252d，比值偏离正常区间）
    - 布林带偏离（价格相对于历史波动区间的位置）
    - 成交量异常（量比突增/突降）
    """

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or PROJECT_PATHS.gs_quant)
        self._gsq_available = None

    def _check_available(self) -> bool:
        """gs_quant 库本身非必须（可用 qlib 数据替代）"""
        try:
            import gs_quant
            logger.info(f"[GsQuant] gs_quant {gs_quant.__version__} 可用")
            self._gsq_available = True
        except ImportError:
            self._gsq_available = False
            logger.info("[GsQuant] gs_quant 未安装，使用 qlib 数据替代")
        return True  # 始终可用（qlib 作为备选）

    def execute(self, query: Query) -> ProjectResult:
        """执行 GS Quant 风险分析"""
        try:
            stock = query.stock_codes[0] if query.stock_codes else ''
            date_str = query.metadata.get('date_str', '')
            extreme = query.metadata.get('extreme_event_type')

            hist_data = self._load_qlib_data(stock, date_str)
            if hist_data is None:
                return ProjectResult(
                    project_name='gs_quant',
                    success=False,
                    signal='观望',
                    confidence=0.50,
                    error=f'无法获取 {stock} 数据',
                )

            result = self.historical_signal(stock, date_str, hist_data)

            # 极值调整
            if extreme:
                result['signal'], result['reason'] = self._apply_extreme_adjustment(
                    result['signal'], result['reason'], extreme,
                    result.get('rsi14', 50.0)
                )

            return ProjectResult(
                project_name='gs_quant',
                success=True,
                data=result,
                signal=result['signal'],
                confidence=result['confidence'],
                reason=result['reason'],
            )
        except Exception as e:
            return ProjectResult(
                project_name='gs_quant',
                success=False,
                signal='观望',
                confidence=0.50,
                error=str(e),
            )

    def historical_signal(self, stock_code: str, date_str: str,
                           hist_data: dict = None) -> dict:
        """
        回测专用接口 — 使用 qlib 历史数据计算风险因子信号。

        Args:
            stock_code: 股票代码
            date_str: 日期
            hist_data: 可选，{closes, highs, lows, volumes}，
                        不提供则自动从 qlib 加载
        """
        if hist_data is None:
            hist_data = self._load_qlib_data(stock_code, date_str)

        if hist_data is None or len(hist_data.get('closes', [])) < 60:
            return {
                'signal': '观望', 'confidence': 0.50,
                'reason': '[GsQuant] 数据不足无法计算波动率',
            }

        import numpy as np
        closes = np.array(hist_data['closes'], dtype=float)
        highs  = np.array(hist_data.get('highs', closes), dtype=float)
        lows   = np.array(hist_data.get('lows', closes), dtype=float)
        volumes= np.array(hist_data.get('volumes', [1]*len(closes)), dtype=float)

        # ── 1. 历史波动率偏离 ─────────────────────────────────────────
        returns = np.diff(np.log(closes))
        hv_20   = float(np.std(returns[-20:]) * np.sqrt(252)) if len(returns) >= 20 else 0.0
        hv_252  = float(np.std(returns) * np.sqrt(252)) if len(returns) >= 252 else hv_20
        hv_ratio= hv_20 / hv_252 if hv_252 > 0 else 1.0

        # ── 2. 布林带偏离 ─────────────────────────────────────────────
        ma20    = float(np.mean(closes[-20:])) if len(closes) >= 20 else closes[-1]
        std20   = float(np.std(closes[-20:])) if len(closes) >= 20 else 0.0
        bb_upper= ma20 + 2 * std20
        bb_lower= ma20 - 2 * std20
        bb_pos  = (closes[-1] - bb_lower) / (bb_upper - bb_lower + 1e-10) if bb_upper > bb_lower else 0.5
        bb_z    = (closes[-1] - ma20) / (std20 + 1e-10) if std20 > 0 else 0.0

        # ── 3. 成交量异常 ──────────────────────────────────────────────
        vol_ma20= float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1.0
        vol_ratio= float(volumes[-1] / vol_ma20) if vol_ma20 > 0 else 1.0

        # ── 4. RSI ─────────────────────────────────────────────────────
        deltas  = np.diff(closes)
        gain    = np.where(deltas > 0, deltas, 0.0)
        loss    = np.where(deltas < 0, -deltas, 0.0)
        avg_g   = float(np.mean(gain[-14:])) if len(gain) >= 14 else 0.0
        avg_l   = float(np.mean(loss[-14:])) if len(loss) >= 14 else 0.0
        rsi14   = 100.0 - (100.0 / (1.0 + avg_g / (avg_l + 1e-10))) if avg_l > 0 else 100.0

        # ── 信号生成 ───────────────────────────────────────────────────
        signals = []   # (signal, confidence, reason)
        weights = []   # 置信度权重

        # 波动率信号
        if hv_ratio > 1.8:
            signals.append(('减持', 0.78, f'HV比={hv_ratio:.2f}(短{hv_20:.0%}/长{hv_252:.0%})，波动率风险激增'))
            weights.append(0.78)
        elif hv_ratio > 1.4:
            signals.append(('持有', 0.60, f'HV比={hv_ratio:.2f}，波动率偏高'))
            weights.append(0.60)
        elif hv_ratio < 0.5:
            signals.append(('增持', 0.62, f'HV比={hv_ratio:.2f}，波动率收缩蓄势'))
            weights.append(0.62)
        else:
            signals.append(('持有', 0.52, f'HV比={hv_ratio:.2f}，波动率正常'))
            weights.append(0.52)

        # 布林带信号
        if bb_z > 2.0:
            signals.append(('减持', 0.72, f'布林带偏离+{bb_z:.1f}σ，超买信号'))
            weights.append(0.72)
        elif bb_z < -2.0:
            signals.append(('增持', 0.72, f'布林带偏离{bb_z:.1f}σ，超卖信号'))
            weights.append(0.72)
        elif bb_pos > 0.9:
            signals.append(('减持', 0.60, f'布林带位置{bb_pos:.0%}，接近上轨'))
            weights.append(0.60)
        elif bb_pos < 0.1:
            signals.append(('增持', 0.60, f'布林带位置{bb_pos:.0%}，接近下轨'))
            weights.append(0.60)

        # 成交量信号
        if vol_ratio > 3.0:
            signals.append(('增持', 0.65, f'成交量放大×{vol_ratio:.1f}，资金异动'))
            weights.append(0.65)
        elif vol_ratio < 0.3:
            signals.append(('减持', 0.60, f'成交量萎缩×{vol_ratio:.1f}，流动性枯竭'))
            weights.append(0.60)

        # 加权投票
        vote = {'增持': 0.0, '买入': 0.0, '持有': 0.0, '观望': 0.0, '减持': 0.0, '清仓': 0.0}
        for i, (sig, conf, _) in enumerate(signals):
            vote[sig] += conf * weights[i]
        total_w = sum(weights)
        if total_w > 0:
            final_signal = max(vote, key=vote.get)
            final_conf   = vote[final_signal] / total_w
        else:
            final_signal, final_conf = '观望', 0.50

        reasons = [r for _, _, r in signals]
        return {
            'signal': final_signal,
            'confidence': min(0.90, final_conf),
            'reason': '[GsQuant] ' + ' | '.join(reasons[:3]),
            'hv_ratio': round(hv_ratio, 3),
            'hv_20': round(hv_20, 4),
            'hv_252': round(hv_252, 4),
            'bb_z': round(bb_z, 2),
            'vol_ratio': round(vol_ratio, 2),
            'rsi14': round(rsi14, 1),
        }

    def _load_qlib_data(self, stock_code: str, end_date: str) -> Optional[dict]:
        """从 qlib 加载历史数据"""
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

            start = '2020-01-01 00:00:00'
            end   = end_date + ' 00:00:00' if end_date and ' ' not in end_date else end_date

            df = D.features([qcode], ['$close', '$high', '$low', '$volume'],
                            start_time=start, end_time=end)
            if df is None or len(df) < 60:
                return None

            df = df.reset_index()
            # reset_index 后列名直接是 instrument, datetime, $close, $high, $low, $volume
            if '$close' in df.columns:
                return {
                    'closes': list(df['$close'].values),
                    'highs':  list(df['$high'].values),
                    'lows':   list(df['$low'].values),
                    'volumes':list(df['$volume'].values),
                }
            # fallback: 尝试 instrument level 列
            if qcode in df.columns:
                return {
                    'closes': list(df[qcode]['$close'].values),
                    'highs':  list(df[qcode]['$high'].values),
                    'lows':   list(df[qcode]['$low'].values),
                    'volumes':list(df[qcode]['$volume'].values),
                }
            return None
        except Exception:
            return None

    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event: str, rsi: float) -> tuple:
        """极值事件调整（与 qlib_adapter 一致）"""
        if not extreme_event:
            return signal, reason

        is_short = extreme_event and extreme_event.startswith(('W20_', 'W50_'))

        if is_short and '_LOW' in extreme_event and rsi < 50:
            if signal in ('增持', '买入'):
                return '减持', reason + ' [GsQuant极值:W20/W50触低，做空]'
            elif signal == '持有':
                return '减持', reason + ' [GsQuant极值:W20/W50触低，做空]'

        elif is_short and '_HIGH' in extreme_event:
            if signal in ('增持', '买入'):
                return '清仓', reason + ' [GsQuant极值:HIGH禁止做多]'
            elif signal in ('观望', '持有'):
                return '减持', reason + ' [GsQuant极值:HIGH中性转空]'

        elif extreme_event and extreme_event.startswith('W100_') and '_HIGH' in extreme_event:
            if signal in ('增持', '买入', '持有'):
                return '清仓', reason + ' [GsQuant极值:W100触高]'

        return signal, reason
