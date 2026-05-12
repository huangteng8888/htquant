# -*- coding: utf-8 -*-
"""
FinceptAdapter — Bloomberg Terminal Alternative

FinceptTerminal 是 Qt/C++ 桌面应用，无 Python API。
此适配器使用 qlib/akshare 作为数据源，模拟 Bloomberg Terminal 的
多数据源聚合分析能力。

功能:
  - 市场情绪（综合涨跌家数、板块资金流向）
  - 布林带位置（个股相对于市场平均的表现）
  - 动量情绪（短期动量 vs 中期动量）

依赖: akshare（用于实时市场宽度）、qlib（用于回测）
"""

import logging
from typing import Any, Dict, Optional

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

SIGNAL_ORDER = ['清仓', '减持', '观望', '持有', '增持', '买入']


class FinceptAdapter(BaseAdapter):
    """
    FinceptTerminal 数据聚合适配器。

    通过多维度市场数据生成综合信号：
      1. 市场宽度信号 — 整体市场情绪（上涨/下跌家数比）
      2. 布林带信号  — 个股在波动率通道中的位置
      3. 动量情绪    — 短中期动量分歧（市场内部结构）

    注意: 回测模式下使用 qlib 个股数据计算内部指标。
    """

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or PROJECT_PATHS.fincept)
        self._akshare_available = None

    def _check_available(self) -> bool:
        """akshare 用于实时市场宽度分析；qlib 用于回测"""
        try:
            import akshare as ak
            self._ak = ak
            self._akshare_available = True
            logger.info("[Fincept] akshare 可用，实时+回测双模式")
            return True
        except ImportError:
            self._akshare_available = False
            logger.info("[Fincept] akshare 未安装，仅回测模式（qlib）")
            return True  # qlib 回测模式始终可用

    def execute(self, query: Query) -> ProjectResult:
        """执行 Fincept 市场聚合分析"""
        try:
            stock = query.stock_codes[0] if query.stock_codes else ''
            date_str = query.metadata.get('date_str', '')
            extreme = query.metadata.get('extreme_event_type')

            # 尝试市场宽度（akshare，实时）
            breadth = self._market_breadth_signal() if self._akshare_available else None

            # 个股信号（qlib）
            hist_data = self._load_qlib_data(stock, date_str)
            if hist_data is None:
                return ProjectResult(
                    project_name='fincept',
                    success=False,
                    signal='观望',
                    confidence=0.50,
                    error=f'无法获取 {stock} 数据',
                )

            result = self.historical_signal(stock, date_str, hist_data)
            if breadth and breadth.get('signal') not in ('观望', '观望'):
                # 融合市场宽度与个股信号
                result = self._fuse_signals(result, breadth)

            # 极值调整
            if extreme:
                result['signal'], result['reason'] = self._apply_extreme_adjustment(
                    result['signal'], result['reason'], extreme,
                    result.get('rsi14', 50.0)
                )

            return ProjectResult(
                project_name='fincept',
                success=True,
                data=result,
                signal=result['signal'],
                confidence=result['confidence'],
                reason=result['reason'],
            )
        except Exception as e:
            return ProjectResult(
                project_name='fincept',
                success=False,
                signal='观望',
                confidence=0.50,
                error=str(e),
            )

    def historical_signal(self, stock_code: str, date_str: str,
                           hist_data: dict = None) -> dict:
        """
        回测专用接口 — 使用 qlib 数据计算市场情绪信号。

        计算三个维度:
          1. 布林带信号 — 价格在20日波动通道中的位置
          2. 动量情绪   — 5日 vs 20日动量分歧
          3. 成交量情绪 — 量比（当日成交量/20日均量）
        """
        if hist_data is None:
            hist_data = self._load_qlib_data(stock_code, date_str)

        if hist_data is None or len(hist_data.get('closes', [])) < 30:
            return {
                'signal': '观望', 'confidence': 0.50,
                'reason': '[Fincept] 数据不足',
            }

        import numpy as np
        closes  = np.array(hist_data['closes'], dtype=float)
        highs   = np.array(hist_data.get('highs', closes), dtype=float)
        lows    = np.array(hist_data.get('lows', closes), dtype=float)
        volumes = np.array(hist_data.get('volumes', [1]*len(closes)), dtype=float)

        # ── 1. 布林带信号 ────────────────────────────────────────────
        ma20    = np.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]
        std20   = np.std(closes[-20:]) if len(closes) >= 20 else 0.0
        bb_up   = ma20 + 2 * std20
        bb_low  = ma20 - 2 * std20
        bb_pos  = (closes[-1] - bb_low) / (bb_up - bb_low + 1e-10) if bb_up > bb_low else 0.5
        bb_z    = (closes[-1] - ma20) / (std20 + 1e-10) if std20 > 0 else 0.0

        # ── 2. 动量情绪 ──────────────────────────────────────────────
        mom5    = float(closes[-1] / closes[-6] - 1) if len(closes) >= 6 else 0.0
        mom20   = float(closes[-1] / closes[-21] - 1) if len(closes) >= 21 else mom5
        mom_div = mom5 - mom20  # 正=短期强于长期，负=短期弱于长期

        # ── 3. 成交量情绪 ─────────────────────────────────────────────
        vol_ma  = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1.0
        vol_ratio= float(volumes[-1] / vol_ma) if vol_ma > 0 else 1.0

        # ── 4. RSI ───────────────────────────────────────────────────
        deltas  = np.diff(closes)
        gain    = np.where(deltas > 0, deltas, 0.0)
        loss    = np.where(deltas < 0, -deltas, 0.0)
        avg_g   = float(np.mean(gain[-14:])) if len(gain) >= 14 else 0.0
        avg_l   = float(np.mean(loss[-14:])) if len(loss) >= 14 else 0.0
        rsi14   = 100.0 - (100.0 / (1.0 + avg_g / (avg_l + 1e-10))) if avg_l > 0 else 100.0

        # ── 信号生成 ─────────────────────────────────────────────────
        signals = []
        weights = []

        # 布林带信号
        if bb_z > 2.0:
            signals.append(('减持', 0.70, f'布林偏离+{bb_z:.1f}σ(超买)'))
            weights.append(0.70)
        elif bb_z < -2.0:
            signals.append(('增持', 0.70, f'布林偏离{bb_z:.1f}σ(超卖)'))
            weights.append(0.70)
        elif bb_pos > 0.85:
            signals.append(('减持', 0.58, f'布林位置{bb_pos:.0%}(偏强)'))
            weights.append(0.58)
        elif bb_pos < 0.15:
            signals.append(('增持', 0.58, f'布林位置{bb_pos:.0%}(偏弱)'))
            weights.append(0.58)

        # 动量情绪信号
        if mom_div > 0.03:
            signals.append(('增持', 0.62, f'动量偏多(5日{mom5:.1%} vs 20日{mom20:.1%})'))
            weights.append(0.62)
        elif mom_div < -0.03:
            signals.append(('减持', 0.62, f'动量偏空(5日{mom5:.1%} vs 20日{mom20:.1%})'))
            weights.append(0.62)

        # 成交量信号
        if vol_ratio > 2.5:
            signals.append(('增持', 0.60, f'放量×{vol_ratio:.1f}'))
            weights.append(0.60)
        elif vol_ratio < 0.4:
            signals.append(('减持', 0.58, f'缩量×{vol_ratio:.1f}'))
            weights.append(0.58)

        # 加权投票
        vote = {s: 0.0 for s in SIGNAL_ORDER}
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
            'confidence': min(0.88, final_conf),
            'reason': '[Fincept] ' + ' | '.join(reasons[:3]) if reasons else '[Fincept] 中性',
            'bb_z': round(float(bb_z), 2),
            'bb_pos': round(float(bb_pos), 3),
            'mom5': round(mom5, 4),
            'mom20': round(mom20, 4),
            'mom_div': round(mom_div, 4),
            'vol_ratio': round(vol_ratio, 2),
            'rsi14': round(rsi14, 1),
        }

    def _market_breadth_signal(self) -> dict:
        """市场宽度分析（akshare 实时）"""
        try:
            if not self._akshare_available:
                return {'signal': '观望', 'confidence': 0.50}
            df = self._ak.stock_zh_a_spot_em()
            rising  = int((df['涨跌幅'] > 0).sum())
            falling = int((df['涨跌幅'] < 0).sum())
            total   = rising + falling
            breadth = (rising - falling) / total if total > 0 else 0

            if breadth > 0.15:
                return {'signal': '增持', 'confidence': 0.65,
                        'reason': f'市场宽度{breadth:.1%}(涨{rising}家/跌{falling}家)'}
            elif breadth < -0.15:
                return {'signal': '减持', 'confidence': 0.65,
                        'reason': f'市场宽度{breadth:.1%}(涨{rising}家/跌{falling}家)'}
            else:
                return {'signal': '观望', 'confidence': 0.55,
                        'reason': f'市场宽度中性{breadth:.1%}'}
        except Exception:
            return {'signal': '观望', 'confidence': 0.50}

    def _fuse_signals(self, stock_signal: dict, breadth_signal: dict) -> dict:
        """融合个股信号与市场宽度信号"""
        s_sig = stock_signal['signal']
        s_conf= stock_signal['confidence']
        b_sig = breadth_signal.get('signal', '观望')
        b_conf= breadth_signal.get('confidence', 0.50)

        # 市场宽度权重较低（辅助）
        if b_sig == '增持' and s_sig in ('增持', '观望'):
            return {
                'signal': '增持',
                'confidence': min(0.82, s_conf * 0.7 + b_conf * 0.3),
                'reason': stock_signal['reason'] + ' [市场宽度确认]',
            }
        elif b_sig == '减持' and s_sig in ('减持', '观望'):
            return {
                'signal': '减持',
                'confidence': min(0.82, s_conf * 0.7 + b_conf * 0.3),
                'reason': stock_signal['reason'] + ' [市场宽度警示]',
            }
        return stock_signal

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
            if df is None or len(df) < 30:
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
                return '减持', reason + ' [Fincept极值:W20/W50触低，做空]'
            elif signal == '持有':
                return '减持', reason + ' [Fincept极值:W20/W50触低，做空]'

        elif is_short and '_HIGH' in extreme_event:
            if signal in ('增持', '买入'):
                return '清仓', reason + ' [Fincept极值:HIGH禁止做多]'
            elif signal in ('观望', '持有'):
                return '减持', reason + ' [Fincept极值:HIGH中性转空]'

        elif extreme_event and extreme_event.startswith('W100_') and '_HIGH' in extreme_event:
            if signal in ('增持', '买入', '持有'):
                return '清仓', reason + ' [Fincept极值:W100触高]'

        return signal, reason
