"""
qlib 适配器
提供技术分析、因子分析支持
"""
import logging
from typing import Any, Dict, List
from pathlib import Path
import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import QLIB_DATA_PATH, STOCK_CODE_MAPPING, HORIZON
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class QlibAdapter(BaseAdapter):
    """qlib量化研究平台适配器"""
    
    def __init__(self, project_path: str):
        super().__init__(project_path)
        self.qlib_initialized = False
        self._data_path = QLIB_DATA_PATH
    
    def _check_available(self) -> bool:
        """检查qlib是否可用"""
        try:
            import qlib
            from qlib.data import D
            qlib.init(provider_uri=self._data_path)
            self._qlib = qlib
            self._D = D
            self.qlib_initialized = True
            logger.info(f"[qlib_adapter] qlib初始化成功，数据路径: {self._data_path}")
            return True
        except Exception as e:
            logger.warning(f"[qlib_adapter] qlib不可用: {e}")
            return False
    
    def execute(self, query: Query) -> ProjectResult:
        """执行qlib分析"""
        if not self.qlib_initialized:
            return ProjectResult(
                project_name="qlib",
                success=False,
                data=None,
                error="qlib未初始化"
            )
        
        try:
            # 获取第一只股票进行分析（简化）
            stock_code = query.stock_codes[0] if query.stock_codes else None
            if not stock_code:
                return ProjectResult(
                    project_name="qlib",
                    success=False,
                    data=None,
                    error="未指定股票代码"
                )
            
            qcode_info = STOCK_CODE_MAPPING.get(stock_code)
            if not qcode_info:
                return ProjectResult(
                    project_name="qlib",
                    success=False,
                    data=None,
                    error=f"未知的股票代码: {stock_code}"
                )
            
            qcode, stock_name = qcode_info
            
            # 获取数据
            df = self._D.features(
                [qcode],
                ['$close', '$volume'],
                start_time='2024-01-01',
                end_time='2026-05-06'
            )
            
            if df is None or len(df) == 0:
                return ProjectResult(
                    project_name="qlib",
                    success=False,
                    data=None,
                    error=f"无数据: {stock_code}"
                )
            
            close = df['$close']
            
            # 计算技术指标
            data = self._calc_technical(close, df['$volume'])
            data['stock_code'] = stock_code
            data['stock_name'] = stock_name
            
            # 根据查询类型返回不同结果
            if query.query_type.value == "factor_analysis":
                signal = self._factor_signal(data)
            elif query.query_type.value == "technical_analysis":
                signal = self._technical_signal(data)
            else:
                signal = self._default_signal(data)
            
            return ProjectResult(
                project_name="qlib",
                success=True,
                data=data,
                confidence=0.8,
                signal=signal['signal'],
                reason=signal['reason'],
                evidence=signal['evidence']
            )
            
        except Exception as e:
            logger.error(f"[qlib_adapter] 执行失败: {e}")
            return ProjectResult(
                project_name="qlib",
                success=False,
                data=None,
                error=str(e)
            )
    
    def _calc_technical(self, close, volume) -> Dict[str, Any]:
        """计算技术指标"""
        # 移动平均线
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        ma120 = close.rolling(120).mean()
        
        latest = close.iloc[-1]
        ma5v = ma5.iloc[-1]
        ma20v = ma20.iloc[-1]
        ma60v = ma60.iloc[-1]
        ma120v = ma120.iloc[-1]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi_value = rsi.iloc[-1]
        
        # 周期涨跌
        pct_1w = (close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
        pct_1m = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else 0
        pct_3m = (close.iloc[-1] / close.iloc[-65] - 1) * 100 if len(close) > 65 else 0
        pct_6m = (close.iloc[-1] / close.iloc[-130] - 1) * 100 if len(close) > 130 else 0
        pct_1y = (close.iloc[-1] / close.iloc[-245] - 1) * 100 if len(close) > 245 else 0
        
        # 量比
        vol_ma = volume.rolling(20).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_ma if vol_ma > 0 else 0
        
        # 趋势判断
        short_trend = latest > ma20v
        med_trend = ma20v > ma60v
        long_trend = ma60v > ma120v
        
        # MA多头排列
        ma_bullish = short_trend and med_trend and long_trend
        
        return {
            'close': latest,
            'ma5': ma5v,
            'ma20': ma20v,
            'ma60': ma60v,
            'ma120': ma120v,
            'rsi': rsi_value,
            'volume_ratio': vol_ratio,
            'pct_1week': pct_1w,
            'pct_1month': pct_1m,
            'pct_3month': pct_3m,
            'pct_6month': pct_6m,
            'pct_1year': pct_1y,
            'short_trend': short_trend,
            'med_trend': med_trend,
            'long_trend': long_trend,
            'ma_bullish': ma_bullish,
            'trend': 'up' if (short_trend and med_trend) else ('down' if not short_trend and not med_trend else 'neutral'),
        }
    
    def _technical_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """基于技术分析给出信号"""
        rsi = data['rsi']
        ma_bullish = data['ma_bullish']
        trend = data['trend']
        pct_1m = data['pct_1month']
        
        # 趋势跟踪逻辑
        if ma_bullish and rsi < 70:
            if rsi < 40:
                signal = "买入"
                reason = f"MA多头排列+RSI超卖({rsi:.1f})，强烈买入信号"
            else:
                signal = "增持"
                reason = f"MA多头排列，RSI={rsi:.1f}适中，看涨"
        elif trend == 'up' and rsi < 60:
            signal = "持有"
            reason = f"短线上升趋势，RSI={rsi:.1f}未过热"
        elif rsi > 75:
            signal = "减持"
            reason = f"RSI={rsi:.1f}严重超买，风险较大"
        elif trend == 'down' or data['pct_1month'] < -15:
            signal = "减持"
            reason = f"下降趋势+1月跌{pct_1m:.1f}%，动能弱"
        else:
            signal = "观望"
            reason = f"技术面中性，RSI={rsi:.1f}"
        
        evidence = [
            f"RSI={rsi:.1f}",
            f"MA多头{'是' if ma_bullish else '否'}",
            f"趋势={trend}",
            f"1月涨跌={pct_1m:+.1f}%"
        ]
        
        return {
            'signal': signal,
            'reason': reason,
            'evidence': evidence
        }
    
    def _factor_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """因子分析信号（均值回归视角）"""
        rsi = data['rsi']
        
        if rsi < 30:
            signal = "买入"
            reason = f"RSI={rsi:.1f}严重超卖，均值回归买点"
        elif rsi < 40:
            signal = "增持"
            reason = f"RSI={rsi:.1f}偏低，估值优势"
        elif rsi < 60:
            signal = "持有"
            reason = f"RSI={rsi:.1f}处于合理区间"
        elif rsi < 70:
            signal = "观望"
            reason = f"RSI={rsi:.1f}偏高，注意回调风险"
        else:
            signal = "减持"
            reason = f"RSI={rsi:.1f}超买，均值回归压力"
        
        return {
            'signal': signal,
            'reason': reason,
            'evidence': [f"RSI={rsi:.1f}"]
        }
    
    def _default_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """默认信号（综合判断）"""
        return self._technical_signal(data)
