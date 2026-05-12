"""
backtrader 适配器
提供策略回测支持
"""
import logging
from typing import Any, Dict, List
from pathlib import Path
import subprocess
import sys
import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS, get_qcode
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class BacktraderAdapter(BaseAdapter):
    """backtrader回测引擎适配器"""
    
    def __init__(self, project_path: str):
        super().__init__(project_path)
        self.backtrader_env = PROJECT_PATHS.backtrader_env
    
    def _check_available(self) -> bool:
        """检查backtrader是否可用"""
        try:
            bt_check = subprocess.run(
                [sys.executable, '-c', 'import backtrader as bt; print(bt.__version__)'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if bt_check.returncode == 0:
                logger.info(f"[backtrader_adapter] backtrader可用: {bt_check.stdout.strip()}")
                return True
        except Exception as e:
            logger.warning(f"[backtrader_adapter] backtrader不可用: {e}")
        
        # 检查venv
        try:
            venv_python = Path(self.backtrader_env) / 'bin' / 'python'
            if venv_python.exists():
                bt_check = subprocess.run(
                    [str(venv_python), '-c', 'import backtrader as bt; print(bt.__version__)'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if bt_check.returncode == 0:
                    self._python = str(venv_python)
                    logger.info(f"[backtrader_adapter] backtrader可用(venv): {bt_check.stdout.strip()}")
                    return True
        except Exception as e:
            logger.warning(f"[backtrader_adapter] backtrader venv不可用: {e}")
        
        return False
    
    def execute(self, query: Query) -> ProjectResult:
        """执行backtrader回测"""
        try:
            stock_code = query.stock_codes[0] if query.stock_codes else None
            if not stock_code:
                return ProjectResult(project_name="backtrader", success=False, data=None, error="未指定股票代码")
            
            qcode_info = get_qcode(stock_code)
            if not qcode_info:
                return ProjectResult(project_name="backtrader", success=False, data=None, error=f"未知的股票代码: {stock_code}")
            
            qcode, stock_name = qcode_info
            result = self._run_ma_backtest(qcode, stock_name)
            
            if result['success']:
                signal = self._backtest_signal(result, query.metadata.get('extreme_event_type'))
                return ProjectResult(
                    project_name="backtrader",
                    success=True,
                    data=result,
                    confidence=result.get('confidence', 0.7),
                    signal=signal['signal'],
                    reason=signal['reason'],
                    evidence=signal['evidence']
                )
            else:
                return ProjectResult(project_name="backtrader", success=False, data=None, error=result.get('error', '回测失败'))
                
        except Exception as e:
            logger.error(f"[backtrader_adapter] 执行失败: {e}")
            return ProjectResult(project_name="backtrader", success=False, data=None, error=str(e))
    
    def _run_ma_backtest(self, qcode: str, stock_name: str) -> Dict[str, Any]:
        """运行MA双叉策略回测"""
        try:
            script = """
import sys
import warnings
warnings.filterwarnings("ignore")

from pathlib import Path
import qlib
qlib.init(provider_uri=str(Path.home() / ".qlib/qlib_data/cn_data_new2"))

from qlib.data import D
import numpy as np

df = D.features(["REPLACE_QCODE"], ["$close"], start_time="2024-01-01", end_time="2026-05-06")
close = df["$close"]

ma5 = close.rolling(5).mean()
ma20 = close.rolling(20).mean()

position = 0
cash = 100000
shares = 0

for i in range(20, len(close)):
    price = close.iloc[i]
    prev_ma5 = ma5.iloc[i-1]
    curr_ma5 = ma5.iloc[i]
    prev_ma20 = ma20.iloc[i-1]
    curr_ma20 = ma20.iloc[i]
    
    if prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20 and position == 0:
        shares = cash // price
        cash -= shares * price
        position = 1
    
    elif prev_ma5 >= prev_ma20 and curr_ma5 < curr_ma20 and position == 1:
        cash += shares * price
        shares = 0
        position = 0

final_value = cash + shares * close.iloc[-1]
strategy_return = (final_value - 100000) / 100000 * 100
buy_hold = (close.iloc[-1] - close.iloc[20]) / close.iloc[20] * 100
excess = strategy_return - buy_hold

print("RESULT:strategy_return=%.2f:buy_hold=%.2f:excess=%.2f" % (strategy_return, buy_hold, excess))
""".replace("REPLACE_QCODE", qcode)
            
            python_exec = getattr(self, '_python', sys.executable)
            result = subprocess.run(
                [python_exec, '-c', script],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode != 0:
                return {'success': False, 'error': result.stderr[:200] if result.stderr else '回测执行失败'}
            
            for line in result.stdout.strip().split('\n'):
                if line.startswith('RESULT:'):
                    parts = line[7:].split(':')
                    data = {}
                    for p in parts:
                        if '=' in p:
                            k, v = p.split('=')
                            data[k] = float(v)
                    return {
                        'success': True,
                        'strategy_return': data.get('strategy_return', 0),
                        'buy_hold_return': data.get('buy_hold', 0),
                        'excess_return': data.get('excess', 0),
                        'confidence': 0.75,
                    }
            
            return {'success': False, 'error': '无法解析回测结果: ' + result.stdout[:100]}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _backtest_signal(self, result: Dict[str, Any],
                         extreme_event_type: str = None,
                         rsi14: float = None) -> Dict[str, Any]:
        """基于回测结果给出信号"""
        excess = result.get('excess_return', 0)
        strat_return = result.get('strategy_return', 0)
        
        if excess > 20:
            signal = "买入"
            reason = "策略超额收益%.1f%%，MA双叉策略显著有效" % excess
        elif excess > 10:
            signal = "增持"
            reason = "策略超额收益%.1f%%，趋势跟踪有效" % excess
        elif excess > 0:
            signal = "持有"
            reason = "策略小幅跑赢基准%.1f%%" % excess
        elif excess > -10:
            signal = "观望"
            reason = "策略跑输基准%.1f%%，效果不明显" % excess
        else:
            signal = "减持"
            reason = "策略大幅跑输%.1f%%，MA策略不适用" % excess
        
        # 极值事件信号调整
        signal, reason = self._apply_extreme_adjustment(signal, reason, extreme_event_type, rsi14)
        
        evidence = [
            "策略收益=%.1f%%" % strat_return,
            "超额=%.1f%%" % excess
        ]
        if extreme_event_type:
            evidence.append(f"极值事件={extreme_event_type}")
        
        return {'signal': signal, 'reason': reason, 'evidence': evidence}
    
    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event_type: str, rsi: float) -> tuple:
        """
        根据极值事件类型调整backtrader信号
        - W20/W50 LOW触低（RSI<50）：短期趋势向下，做多→做空
        - W20/W50 HIGH触高：市场实际下跌，强化短空
        - W100/W252：中长期均值回归，保持或增强
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
            # W20/W50触高：市场实际下跌，应强制短空
            if signal in ['增持', '买入']:
                signal = '清仓'
                reason += ' [极值强制:HIGH事件禁止做多，清仓]'
            elif signal == '观望':
                signal = '减持'
                reason += ' [极值强制:HIGH事件，转向做空]'
            elif signal == '持有':
                signal = '减持'
                reason += ' [极值强制:HIGH事件，转向做空]'

        elif extreme_event_type and extreme_event_type.startswith('W100_') and '_HIGH' in extreme_event_type:
            if signal in ['增持', '买入', '持有']:
                signal = '清仓'
                reason += ' [极值强制:W100触高均值回归强，强制清仓]'
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

    def historical_signal(self, stock_code: str, date_str: str,
                          hist_data: dict = None) -> dict:
        """
        回测流水线专用接口 — 直接用 pandas 计算 MA 双叉策略。
        hist_data: {closes, highs, lows, volumes}
        """
        import numpy as np

        if hist_data is None:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': 'Backtrader无hist_data'}

        closes = hist_data.get('closes', [])
        highs  = hist_data.get('highs', closes)
        lows   = hist_data.get('lows', closes)

        if len(closes) < 60:
            return {'signal': '观望', 'confidence': 0.50,
                    'reason': f'数据不足({len(closes)}天)'}

        closes = np.array(closes, dtype=float)
        highs  = np.array(highs, dtype=float)
        lows   = np.array(lows, dtype=float)

        # MA 双叉
        ma5  = self._ma(closes, 5)
        ma20 = self._ma(closes, 20)

        if len(ma5) < 2 or len(ma20) < 2:
            return {'signal': '观望', 'confidence': 0.55, 'reason': 'MA数据不足'}

        prev_ma5, curr_ma5  = ma5[-2], ma5[-1]
        prev_ma20, curr_ma20 = ma20[-2], ma20[-1]

        # RSI
        rsi14 = self._rsi(closes, 14)

        # 信号
        if prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20:
            signal, confidence = '增持', 0.75
            reason = 'MA5上穿MA20，金叉'
        elif prev_ma5 >= prev_ma20 and curr_ma5 < curr_ma20:
            signal, confidence = '减持', 0.75
            reason = 'MA5下穿MA20，死叉'
        else:
            signal, confidence = '持有', 0.55
            reason = f'MA中性(MA5={curr_ma5:.2f}, MA20={curr_ma20:.2f})'

        return {
            'signal': signal,
            'confidence': confidence,
            'reason': reason,
            'rsi14': round(rsi14, 1),
            'ma5': round(curr_ma5, 2),
            'ma20': round(curr_ma20, 2),
        }

    def _ma(self, prices: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(prices, np.nan)
        result[period-1:] = np.convolve(prices, np.ones(period)/period, mode='valid')
        return result

    def _rsi(self, prices: np.ndarray, period: int = 14) -> float:
        deltas = np.diff(prices)
        gain = np.where(deltas > 0, deltas, 0)
        loss = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gain[-period:])
        avg_loss = np.mean(loss[-period:])
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
