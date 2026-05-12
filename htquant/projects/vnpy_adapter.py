"""
vnpy 适配器
国产量化交易框架
提供CTA策略（R-Breaker、DualThrust、ATR突破）
"""
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional
from pathlib import Path
import numpy as np
import pandas as pd

from ..dispatcher import Query, ProjectResult
from ..config import QLIB_DATA_PATH, get_qcode
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class VnpyAdapter(BaseAdapter):
    """vnpy量化交易框架适配器

    提供CTA策略:
    - R-Breaker: 经典日内PivotPoint策略
    - DualThrust: 区间突破策略
    - ATR突破: 波动率突破策略
    """

    def __init__(self, project_path: str):
        super().__init__(project_path)
        self.vnpy_initialized = False
        self._data_path = QLIB_DATA_PATH

    def _check_available(self) -> bool:
        """检查vnpy是否可用（支持conda env）"""
        # 方法1: 通过 conda env Python 检查
        conda_env_python = '/home/ht/anaconda3/envs/vnpy/bin/python'
        if os.path.exists(conda_env_python):
            result = subprocess.run(
                [conda_env_python, '-c', 'import vnpy; print(getattr(vnpy, "__version__", "unknown"))'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                logger.info(f"[vnpy_adapter] vnpy可用，版本: {version} (via conda env)")
                self.vnpy_initialized = True
                # 动态导入 vnpy 到当前 Python（跨版本，需要手动加路径）
                sys.path.insert(0, '/home/ht/anaconda3/envs/vnpy/lib/python3.12/site-packages')
                import vnpy
                self._vnpy = vnpy
                return True

        # 方法2: 直接 import（需要已安装）
        try:
            import vnpy
            self._vnpy = vnpy
            self.vnpy_initialized = True
            logger.info(f"[vnpy_adapter] vnpy可用，版本: {vnpy.__version__}")
            return True
        except ImportError as e:
            logger.warning(f"[vnpy_adapter] vnpy不可用: {e}")
            return False

    def execute(self, query: Query) -> ProjectResult:
        """执行vnpy CTA策略分析"""
        if not self.vnpy_initialized and not self._check_available():
            return ProjectResult(
                project_name="vnpy",
                success=False,
                data=None,
                error="vnpy未安装或不可用"
            )

        try:
            stock_code = query.stock_codes[0] if query.stock_codes else None
            if not stock_code:
                return ProjectResult(
                    project_name="vnpy",
                    success=False,
                    data=None,
                    error="未指定股票代码"
                )

            qcode_info = get_qcode(stock_code)
            if not qcode_info:
                return ProjectResult(
                    project_name="vnpy",
                    success=False,
                    data=None,
                    error=f"未知的股票代码: {stock_code}"
                )

            qcode, stock_name = qcode_info

            # 获取极值事件类型
            extreme_event_type = query.metadata.get('extreme_event_type')

            # 加载数据并运行CTA策略
            result = self._run_cta_strategy(qcode, stock_name, query.horizon)

            if result['success']:
                signal_data = self._generate_signal(result, extreme_event_type)
                return ProjectResult(
                    project_name="vnpy",
                    success=True,
                    data=result,
                    confidence=signal_data['confidence'],
                    signal=signal_data['signal'],
                    reason=signal_data['reason'],
                    evidence=signal_data['evidence']
                )
            else:
                return ProjectResult(
                    project_name="vnpy",
                    success=False,
                    data=None,
                    error=result.get('error', 'CTA策略执行失败')
                )

        except Exception as e:
            logger.error(f"[vnpy_adapter] 执行失败: {e}")
            return ProjectResult(
                project_name="vnpy",
                success=False,
                data=None,
                error=str(e)
            )

    def historical_signal(self, stock_code: str, date_str: str, hist_data: dict = None) -> dict:
        """
        回测专用接口

        Args:
            stock_code: 股票代码
            date_str: 日期字符串 (YYYY-MM-DD)
            hist_data: 历史数据 dict，包含:
                - closes: 收盘价列表
                - highs: 最高价列表
                - lows: 最低价列表
                - volumes: 成交量列表

        Returns:
            dict: 包含 signal, confidence, reason
        """
        try:
            if hist_data is None:
                hist_data = self._load_qlib_data(stock_code, date_str)

            if hist_data is None or len(hist_data.get('closes', [])) < 10:
                return {
                    'signal': '持有',
                    'confidence': 0.50,
                    'reason': '数据不足无法计算CTA信号',
                }

            closes = hist_data['closes']
            highs = hist_data['highs']
            lows = hist_data['lows']
            volumes = hist_data.get('volumes', [0] * len(closes))

            # 转换为numpy数组
            closes = np.array(closes)
            highs = np.array(highs)
            lows = np.array(lows)
            volumes = np.array(volumes)

            # 使用R-Breaker策略
            signal, confidence, reason = self._r_breaker_signal(closes, highs, lows)

            # 计算 RSI
            closes_for_rsi = np.array(closes[-60:]) if len(closes) >= 60 else np.array(closes)
            rsi14 = self._calc_rsi(closes_for_rsi, 14)

            return {
                'signal': signal,
                'confidence': confidence,
                'reason': reason,
                'rsi14': round(rsi14, 1),
            }

        except Exception as e:
            logger.error(f"[vnpy_adapter] historical_signal失败: {e}")
            return {
                'signal': '持有',
                'confidence': 0.50,
                'reason': f'CTA信号计算失败: {e}',
            }

    def _load_qlib_data(self, stock_code: str, end_date: str = None) -> Optional[Dict]:
        """从qlib加载历史数据
        
        stock_code: qlib格式代码，如 'sz000901' 或 'sh600422'
        """
        try:
            import qlib
            from qlib.data import D

            qlib.init(provider_uri=self._data_path)

            # stock_code 已经是 qlib 格式（qcode），直接使用
            qcode = stock_code

            # 默认加载最近250个交易日数据
            start_time = '2025-01-01'
            end_time = end_date or '2026-05-08'

            df = D.features(
                [qcode],
                ['$close', '$high', '$low', '$volume'],
                start_time=start_time,
                end_time=end_time
            )

            if df is None or len(df) == 0:
                return None

            return {
                'closes': df['$close'].tolist(),
                'highs': df['$high'].tolist(),
                'lows': df['$low'].tolist(),
                'volumes': df['$volume'].tolist(),
            }

        except Exception as e:
            logger.warning(f"[vnpy_adapter] qlib数据加载失败: {e}")
            return None

    def _run_cta_strategy(self, qcode: str, stock_name: str, horizon: str) -> Dict[str, Any]:
        """运行CTA策略"""
        try:
            # 加载数据（qcode 已经是 qlib 格式如 'sz000901'，直接传）
            data = self._load_qlib_data(qcode)

            if data is None or len(data['closes']) < 5:
                return {'success': False, 'error': '数据不足'}

            closes = np.array(data['closes'])
            highs = np.array(data['highs'])
            lows = np.array(data['lows'])
            volumes = np.array(data['volumes'])

            # 策略参数
            N = 20  # ATR周期

            # 计算ATR
            atr = self._calculate_atr(highs, lows, closes, N)

            # 获取昨日数据用于R-Breaker
            if len(closes) < 3:
                return {'success': False, 'error': '数据不足进行R-Breaker'}

            # 使用最近的数据
            high_y = highs[-2]
            low_y = lows[-2]
            close_y = closes[-2]

            # 今日数据
            high_t = highs[-1]
            low_t = lows[-1]
            close_t = closes[-1]

            # 计算R-Breaker pivot点
            pivot = (high_y + low_y + close_y) / 3
            s1 = 2 * pivot - high_y
            s2 = pivot - (high_y - low_y)
            s3 = low_y - 2 * (high_y - pivot)
            r1 = 2 * pivot - low_y
            r2 = pivot + (high_y - low_y)
            r3 = high_y + 2 * (pivot - low_y)

            # 判断信号
            signal_type = "hold"
            confidence = 0.55

            # 检查是否向上突破阻力位
            if close_t > r3:
                signal_type = "strong_sell"
                confidence = 0.82
            elif close_t > r2:
                signal_type = "sell"
                confidence = 0.72
            elif close_t > r1:
                signal_type = "sell"
                confidence = 0.72
            # 检查是否向下突破支撑位
            elif close_t < s3:
                signal_type = "strong_buy"
                confidence = 0.82
            elif close_t < s2:
                signal_type = "buy"
                confidence = 0.72
            elif close_t < s1:
                signal_type = "buy"
                confidence = 0.72
            else:
                signal_type = "hold"
                confidence = 0.55

            # 如果昨日数据不足，使用DualThrust作为备用
            if len(closes) < 60:
                signal_type_dt, confidence_dt = self._dual_thrust_signal(closes, highs, lows)
                if signal_type == "hold":
                    signal_type = signal_type_dt
                    confidence = confidence_dt

            return {
                'success': True,
                'stock_name': stock_name,
                'signal_type': signal_type,
                'confidence': confidence,
                'pivot': pivot,
                's1': s1, 's2': s2, 's3': s3,
                'r1': r1, 'r2': r2, 'r3': r3,
                'close_t': close_t,
                'atr': atr,
                'high_y': high_y, 'low_y': low_y, 'close_y': close_y,
                'closes': closes.tolist(),  # 用于 RSI 计算
            }

        except Exception as e:
            logger.error(f"[vnpy_adapter] CTA策略运行失败: {e}")
            return {'success': False, 'error': str(e)}

    def _calculate_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 20) -> float:
        """计算ATR (Average True Range)"""
        if len(highs) < period + 1:
            return 0.0

        trs = []
        for i in range(1, len(highs)):
            high_low = highs[i] - lows[i]
            high_close = abs(highs[i] - closes[i - 1])
            low_close = abs(lows[i] - closes[i - 1])
            tr = max(high_low, high_close, low_close)
            trs.append(tr)

        if len(trs) < period:
            return 0.0

        atr = np.mean(trs[-period:])
        return float(atr)

    def _r_breaker_signal(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> tuple:
        """
        R-Breaker策略信号生成

        Returns:
            tuple: (signal, confidence, reason)
        """
        if len(closes) < 3:
            return '持有', 0.50, '数据不足'

        # 昨日数据
        high_y = highs[-2]
        low_y = lows[-2]
        close_y = closes[-2]

        # 今日收盘
        close_t = closes[-1]

        # 计算Pivot点
        pivot = (high_y + low_y + close_y) / 3
        s1 = 2 * pivot - high_y
        s2 = pivot - (high_y - low_y)
        s3 = low_y - 2 * (high_y - pivot)
        r1 = 2 * pivot - low_y
        r2 = pivot + (high_y - low_y)
        r3 = high_y + 2 * (pivot - low_y)

        # 今日最高价和最低价
        high_t = highs[-1]
        low_t = lows[-1]

        # 判断信号
        # 强烈卖出信号：价格向上突破r3
        if high_t > r3 and close_t < r3:
            return '清仓', 0.82, f'R-Breaker强烈卖出: 向上突破r3({r3:.2f})后回落'

        # 卖出信号：向上突破r2
        if high_t > r2 and close_t < r2:
            return '减持', 0.72, f'R-Breaker卖出: 向上突破r2({r2:.2f})'

        # 卖出信号：向上突破r1
        if high_t > r1 and close_t < r1:
            return '减持', 0.72, f'R-Breaker卖出: 向上突破r1({r1:.2f})'

        # 强烈买入信号：价格向下突破s3
        if low_t < s3 and close_t > s3:
            return '买入', 0.82, f'R-Breaker强烈买入: 向下突破s3({s3:.2f})后反弹'

        # 买入信号：向下突破s2
        if low_t < s2 and close_t > s2:
            return '增持', 0.72, f'R-Breaker买入: 向下突破s2({s2:.2f})'

        # 买入信号：向下突破s1
        if low_t < s1 and close_t > s1:
            return '增持', 0.72, f'R-Breaker买入: 向下突破s1({s1:.2f})'

        # 持有
        return '持有', 0.55, f'R-Breaker持有: 价格在支撑({s1:.2f})和阻力({r1:.2f})之间'

    def _dual_thrust_signal(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, N: int = 20, K: float = 0.5) -> tuple:
        """
        DualThrust策略信号

        Args:
            closes: 收盘价数组
            highs: 最高价数组
            lows: 最低价数组
            N: 周期
            K: 系数

        Returns:
            tuple: (signal, confidence)
        """
        if len(closes) < N + 1:
            return '持有', 0.50

        # 取最近N天的数据
        recent_highs = highs[-N:]
        recent_lows = lows[-N:]
        recent_closes = closes[-N:]

        # 计算上下轨
        HH = np.max(recent_highs)  # N日最高价
        LC = np.min(recent_closes)  # N日最低收盘价
        HC = np.max(recent_closes)  # N日最高收盘价
        LL = np.min(recent_lows)   # N日最低价

        # 计算区间
        range_val = max(HH - LC, HC - LL)

        # 今日开盘价
        open_today = closes[-N]  # 近似

        # 计算上下轨
        upper = open_today + K * range_val
        lower = open_today - K * range_val

        # 今日收盘
        close_t = closes[-1]

        if close_t > upper:
            return '买入', 0.72
        elif close_t < lower:
            return '卖出', 0.72
        else:
            return '持有', 0.55

    def _generate_signal(self, result: Dict[str, Any], extreme_event_type: str) -> Dict[str, Any]:
        """根据CTA策略结果生成信号"""
        signal_type = result.get('signal_type', 'hold')
        confidence = result.get('confidence', 0.55)

        # 映射信号
        if signal_type == "strong_buy":
            signal = "买入"
            reason = f"CTA强烈买入: 向下突破s3支撑位"
        elif signal_type == "buy":
            signal = "增持"
            reason = f"CTA买入: 向下突破支撑位"
        elif signal_type == "strong_sell":
            signal = "清仓"
            reason = f"CTA强烈卖出: 向上突破r3阻力位"
        elif signal_type == "sell":
            signal = "减持"
            reason = f"CTA卖出: 向上突破阻力位"
        else:
            signal = "持有"
            reason = f"CTA持有: 价格在支撑和阻力之间震荡"

        # ATR信息
        atr = result.get('atr', 0)
        if atr > 0:
            reason += f", ATR={atr:.2f}"

        # RSI 用于极值调整
        closes = result.get('closes', [])
        rsi = self._calc_rsi(np.array(closes[-60:]) if closes else np.array([0]), 14)

        # 极值事件调整
        signal, reason = self._apply_extreme_adjustment(signal, reason, extreme_event_type, rsi)

        evidence = [
            f"策略=R-Breaker",
            f"信号={signal_type}",
            f" Pivot={result.get('pivot', 0):.2f}",
        ]
        if extreme_event_type:
            evidence.append(f"极值事件={extreme_event_type}")

        return {
            'signal': signal,
            'confidence': confidence,
            'reason': reason,
            'evidence': evidence
        }

    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event_type: str, rsi: float) -> tuple:
        """
        根据极值事件类型调整信号
        - W20/W50 LOW触低（RSI<50）：短期趋势向下，做多→做空
        - W20/W50 HIGH触高：市场实际下跌，强化短空
        - W100：中长期均值回归
        - W252：极强均值回归
        """
        if not extreme_event_type:
            return signal, reason

        is_short_extreme = extreme_event_type and extreme_event_type.startswith(('W20_', 'W50_'))

        if is_short_extreme and '_LOW' in extreme_event_type and rsi < 50:
            # W20/W50触低且RSI<50：短期趋势向下，做多→做空
            if signal == '买入':
                signal = '清仓'
                reason += ' [极值翻转:W20/W50触低，做空]'
            elif signal == '增持':
                signal = '减持'
                reason += ' [极值翻转:W20/W50触低，做空]'
            elif signal == '持有':
                signal = '减持'
                reason += ' [极值翻转:W20/W50触低，做空]'

        elif is_short_extreme and '_HIGH' in extreme_event_type:
            # W20/W50触高：强制短空
            if signal in ['增持', '买入']:
                signal = '清仓'
                reason += ' [极值强制:HIGH事件禁止做多，清仓]'
            elif signal in ['观望', '持有']:
                signal = '减持'
                reason += ' [极值强制:HIGH事件，转向做空]'

        elif extreme_event_type and extreme_event_type.startswith('W100_') and '_HIGH' in extreme_event_type:
            if signal in ['增持', '买入', '持有']:
                signal = '清仓'
                reason += ' [极值强制:W100触高均值回归强，清仓]'
            elif signal == '观望':
                signal = '减持'
                reason += ' [极值强制:W100触高，转向做空]'

        elif extreme_event_type and extreme_event_type.startswith('W100_') and '_LOW' in extreme_event_type:
            if signal in ['减持', '清仓']:
                signal = '观望'
                reason += ' [极值调整:W100触低均值回归弱，避免做空]'

        elif '_W252_' in extreme_event_type and '_HIGH' in extreme_event_type:
            if signal in ['增持', '买入', '持有', '观望']:
                signal = '清仓'
                reason += ' [极值强制:W252触高，均值回归极强]'
        elif '_W252_' in extreme_event_type and '_LOW' in extreme_event_type:
            if signal in ['减持', '清仓', '观望']:
                signal = '增持'
                reason += ' [极值增强:W252触低，均值回归极强]'

        return signal, reason

    def _calc_rsi(self, prices: np.ndarray, period: int = 14) -> float:
        """计算RSI"""
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices)
        gain = np.where(deltas > 0, deltas, 0.0)
        loss = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gain[-period:]))
        avg_loss = float(np.mean(loss[-period:]))
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
