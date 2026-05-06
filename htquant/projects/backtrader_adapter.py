"""
backtrader 适配器
提供策略回测支持
"""
import logging
from typing import Any, Dict, List
from pathlib import Path
import subprocess
import sys

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS, STOCK_CODE_MAPPING
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
            
            qcode_info = STOCK_CODE_MAPPING.get(stock_code)
            if not qcode_info:
                return ProjectResult(project_name="backtrader", success=False, data=None, error=f"未知的股票代码: {stock_code}")
            
            qcode, stock_name = qcode_info
            result = self._run_ma_backtest(qcode, stock_name)
            
            if result['success']:
                signal = self._backtest_signal(result)
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
    
    def _backtest_signal(self, result: Dict[str, Any]) -> Dict[str, Any]:
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
        
        evidence = [
            "策略收益=%.1f%%" % strat_return,
            "超额=%.1f%%" % excess
        ]
        
        return {'signal': signal, 'reason': reason, 'evidence': evidence}
