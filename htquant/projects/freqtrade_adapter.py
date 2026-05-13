# -*- coding: utf-8 -*-
"""
FreqtradeAdapter — 加密货币高频策略

⚠️ 注意：Freqtrade 是加密货币交易所做市/趋势策略框架，
A股量化不适用此适配器。仅在用户查询加密货币时启用。

核心优势：
  - 币安/OKX 等交易所做市
  - 高频网格策略
  - 趋势跟踪（仅适用于BTC/USDT等主流币）

A股相关度：★☆☆☆☆（几乎不适用）
"""

import logging
import subprocess
import sys
from typing import Any
from datetime import datetime

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class FreqtradeAdapter(BaseAdapter):
    """
    Freqtrade 加密货币策略适配器。
    
    用途受限：仅用于加密货币，不适用于A股。
    当用户查询数字货币时，使用此适配器提供信号。
    
    核心策略：
    - 趋势跟踪（MACD + EMA）
    - 网格做市
    - RSI 极端信号
    """

    CRYPTO_PAIRS = {
        'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT',
        'BNB': 'BNBUSDT', 'XRP': 'XRPUSDT',
        'SOL': 'SOLUSDT', 'ADA': 'ADAUSDT',
        'DOGE': 'DOGEUSDT', 'DOT': 'DOTUSDT',
        'MATIC': 'MATICUSDT', 'AVAX': 'AVAXUSDT',
    }

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or PROJECT_PATHS.freqtrade)

    def _check_available(self) -> bool:
        """检查 freqtrade 是否可用（支持 pip install -e 本地repo）"""
        try:
            import freqtrade
            logger.info(f"[Freqtrade] 可用版本: {freqtrade.__version__}")
            return True
        except ImportError:
            pass

        # 尝试从本地 GitHub repo 安装
        repo_path = self.project_path
        if repo_path.exists():
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-e', str(repo_path), '--quiet'],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                try:
                    import freqtrade
                    logger.info(f"[Freqtrade] 已从本地repo安装，版本: {freqtrade.__version__}")
                    return True
                except ImportError:
                    pass
            else:
                logger.warning(f"[Freqtrade] 本地安装失败: {result.stderr[:200]}")

        logger.warning("[Freqtrade] 未安装 (pip install freqtrade)")
        return False

    def execute(self, query: Query) -> ProjectResult:
        """
        执行加密货币分析。
        ⚠️ 仅在查询加密货币时返回有效信号。
        """
        stock = query.stock_codes[0] if query.stock_codes else ''
        date_str = query.metadata.get('date_str', datetime.now().strftime('%Y-%m-%d'))

        # 识别加密货币
        pair = self._identify_crypto(stock)
        if pair is None:
            # 非加密货币 → 降级为观望（不视为失败，避免打断聚合流程）
            return ProjectResult(
                project_name='freqtrade',
                success=True,
                signal='观望',
                confidence=0.45,
                reason=f'{stock} 非加密货币（freqtrade 专用）',
                data={'asset_type': 'non_crypto', 'degraded': True},
            )

        # 获取数据并计算信号
        signal_data = self._get_crypto_signal(pair, date_str)

        return ProjectResult(
            project_name='freqtrade',
            success=signal_data['success'],
            data=signal_data,
            signal=signal_data.get('signal', '观望'),
            confidence=signal_data.get('confidence', 0.55),
            reason=signal_data.get('reason', ''),
        )

    def historical_signal(self, stock_code: str, date_str: str,
                         hist_data: dict = None) -> dict:
        """
        历史信号接口 — Freqtrade 不适用于A股回测。
        仅用于加密货币回测。
        """
        pair = self._identify_crypto(stock_code)
        if pair is None:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': f'{stock_code}不是加密货币'}

        if hist_data is None:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': 'Freqtrade无历史数据，需要在线获取'}

        closes = hist_data.get('closes', [])
        if len(closes) < 30:
            return {'signal': '观望', 'confidence': 0.50, 'reason': '数据不足'}

        signal_data = self._calc_signal(closes, hist_data.get('highs', closes),
                                         hist_data.get('lows', closes),
                                         hist_data.get('volumes', []))
        signal_data['pair'] = pair
        return signal_data

    def _identify_crypto(self, code: str) -> str:
        """识别是否为加密货币代码"""
        code_upper = code.upper()
        if code_upper in self.CRYPTO_PAIRS:
            return self.CRYPTO_PAIRS[code_upper]
        for name, pair in self.CRYPTO_PAIRS.items():
            if name in code_upper or pair.replace('USDT', '') in code_upper:
                return pair
        return None

    def _get_crypto_signal(self, pair: str, date_str: str) -> dict:
        """获取加密货币信号（在线）"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(pair)
            hist = ticker.history(period='3mo')
            if hist is None or len(hist) < 30:
                return {'success': False, 'signal': '观望', 'confidence': 0.50,
                        'reason': f'{pair} 历史数据不足'}

            closes = hist['Close'].values
            highs  = hist['High'].values
            lows   = hist['Low'].values
            volumes = hist['Volume'].values

            return self._calc_signal(closes, highs, lows, volumes)

        except Exception as e:
            return {'success': False, 'signal': '观望', 'confidence': 0.50,
                    'reason': f'数据获取失败: {e}'}

    def _calc_signal(self, closes, highs, lows, volumes) -> dict:
        """
        计算加密货币交易信号。

        策略：MACD + RSI + 成交量确认的趋势跟踪。
        适用于高波动加密货币市场。
        """
        import numpy as np

        closes = np.array(closes)
        if len(closes) < 26:
            return {'success': True, 'signal': '观望', 'confidence': 0.50,
                    'reason': '数据不足，观望'}

        # MACD (12, 26, 9)
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd = ema12 - ema26
        signal_line = self._ema(macd, 9)
        macd_hist = macd[-1] - signal_line[-1]

        # RSI
        delta = np.diff(closes)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = np.mean(gain[-15:])
        avg_loss = np.mean(loss[-15:])
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # 成交量确认
        vol_ma = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        vol_ratio = volumes[-1] / vol_ma if vol_ma > 0 else 1.0

        # 信号逻辑
        signal = '持有'
        confidence = 0.55
        reasons = []

        # MACD 金叉/死叉
        if macd_hist > 0 and rsi < 70:
            signal = '增持'
            confidence = 0.72
            reasons.append('MACD看涨')
        elif macd_hist < 0 and rsi > 30:
            signal = '减持'
            confidence = 0.72
            reasons.append('MACD看跌')

        # RSI 极端
        if rsi > 80:
            signal = '清仓'
            confidence = 0.82
            reasons.append(f'RSI极度超买={rsi:.0f}')
        elif rsi > 70:
            if signal in ['增持', '持有']:
                signal = '减持'
                confidence = 0.75
                reasons.append(f'RSI超买={rsi:.0f}')
        elif rsi < 20:
            signal = '买入'
            confidence = 0.82
            reasons.append(f'RSI极度超卖={rsi:.0f}')
        elif rsi < 30:
            if signal in ['减持', '持有']:
                signal = '增持'
                confidence = 0.75
                reasons.append(f'RSI超卖={rsi:.0f}')

        # 成交量放大确认
        if vol_ratio > 2.0:
            reasons.append(f'成交量放大×{vol_ratio:.1f}')

        reason = f"[Freqtrade/{pair}] " + ('，'.join(reasons) if reasons else '趋势中性')

        return {'success': True, 'signal': signal, 'confidence': confidence,
                'reason': reason, 'rsi': rsi, 'macd_hist': macd_hist,
                'vol_ratio': vol_ratio}

    def _ema(self, prices, period):
        """计算指数移动平均"""
        import numpy as np
        prices = np.array(prices)
        ema = np.zeros_like(prices)
        ema[0] = prices[0]
        alpha = 2 / (period + 1)
        for i in range(1, len(prices)):
            ema[i] = alpha * prices[i] + (1 - alpha) * ema[i-1]
        return ema
