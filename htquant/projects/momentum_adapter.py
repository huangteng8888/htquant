"""
动量策略适配器
基于短期/中期/长期价格动量进行排序和信号判断
弥补均值回归和趋势跟踪的盲区
"""
import logging
from typing import Any, Dict
from pathlib import Path
import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import QLIB_DATA_PATH, get_qcode
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class MomentumAdapter(BaseAdapter):
    """动量策略适配器"""
    
    def __init__(self, project_path: str = ""):
        super().__init__(project_path)
        self.qlib_initialized = False
        self._data_path = QLIB_DATA_PATH
    
    def _check_available(self) -> bool:
        """检查是否可用（依赖qlib）"""
        try:
            import qlib
            from qlib.data import D
            qlib.init(provider_uri=self._data_path)
            self._qlib = qlib
            self._D = D
            self.qlib_initialized = True
            return True
        except Exception as e:
            logger.warning(f"[momentum_adapter] 不可用: {e}")
            return False
    
    def execute(self, query: Query) -> ProjectResult:
        """执行动量分析"""
        if not self.qlib_initialized:
            return ProjectResult(
                project_name="momentum",
                success=False,
                data=None,
                error="qlib未初始化"
            )
        
        try:
            stock_code = query.stock_codes[0] if query.stock_codes else None
            if not stock_code:
                return ProjectResult(project_name="momentum", success=False, data=None, error="未指定股票代码")
            
            qcode_info = get_qcode(stock_code)
            if not qcode_info:
                return ProjectResult(project_name="momentum", success=False, data=None, error=f"未知股票代码: {stock_code}")
            
            qcode, stock_name = qcode_info
            data = self._calc_momentum(qcode)
            
            if data is None:
                return ProjectResult(project_name="momentum", success=False, data=None, error="无法获取数据")
            
            # 获取极值事件类型
            extreme_event_type = query.metadata.get('extreme_event_type')
            
            signal = self._momentum_signal(data, extreme_event_type)
            
            return ProjectResult(
                project_name="momentum",
                success=True,
                data=data,
                confidence=0.8,
                signal=signal['signal'],
                reason=signal['reason'],
                evidence=signal['evidence']
            )
            
            return ProjectResult(
                project_name="momentum",
                success=True,
                data=data,
                confidence=0.8,
                signal=signal['signal'],
                reason=signal['reason'],
                evidence=signal['evidence']
            )
            
        except Exception as e:
            logger.error(f"[momentum_adapter] 执行失败: {e}")
            return ProjectResult(project_name="momentum", success=False, data=None, error=str(e))
    
    def _calc_momentum(self, qcode: str) -> Dict[str, Any]:
        """计算动量指标"""
        try:
            df = self._D.features(
                [qcode],
                ['$close', '$volume'],
                start_time='2023-01-01',
                end_time='2026-05-06'
            )
            
            if df is None or len(df) < 250:
                return None
            
            close = df['$close']
            
            # 各周期动量
            mom_1w = (close.iloc[-1] / close.iloc[-5] - 1) * 100 if len(close) >= 5 else 0
            mom_1m = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else 0
            mom_3m = (close.iloc[-1] / close.iloc[-65] - 1) * 100 if len(close) > 65 else 0
            mom_6m = (close.iloc[-1] / close.iloc[-130] - 1) * 100 if len(close) > 130 else 0
            mom_1y = (close.iloc[-1] / close.iloc[-245] - 1) * 100 if len(close) > 245 else 0
            
            # 动量强度：加权综合（短期权重更高）
            mom_score = mom_1w * 0.3 + mom_1m * 0.3 + mom_3m * 0.25 + mom_1y * 0.15
            
            # 动量一致性：近1周 vs 近1月的比值（判断加速/减速）
            mom_accel = mom_1w * 4 - mom_1m
            
            # 波动率
            returns = close.pct_change().dropna()
            volatility = returns.rolling(20).std().iloc[-1] * np.sqrt(245) * 100
            
            # RSI
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = (100 - 100 / (1 + rs)).iloc[-1]
            
            return {
                'mom_1w': mom_1w,
                'mom_1m': mom_1m,
                'mom_3m': mom_3m,
                'mom_6m': mom_6m,
                'mom_1y': mom_1y,
                'mom_score': mom_score,
                'mom_accel': mom_accel,
                'volatility': volatility,
                'rsi': rsi,
            }
            
        except Exception as e:
            logger.error(f"[momentum] 计算失败: {e}")
            return None
    
    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event_type: str, rsi: float) -> tuple:
        """
        根据极值事件类型调整动量信号
        
        核心修正（基于2026-05-07回测数据）：
        - W20/W50 LOW触低：短期趋势延续，动量不应看多（momentum反而看空）
        - W20/W50 HIGH触高：短期趋势延续，动量不应看空（momentum反而看多）
        - W100/W252：中长期均值回归，动量信号保持
        """
        if not extreme_event_type:
            return signal, reason
        
        is_short_extreme = extreme_event_type and extreme_event_type.startswith(('W20_', 'W50_'))
        
        if is_short_extreme and '_LOW' in extreme_event_type:
            # W20/W50触低：短期趋势向下，做多→做空（RSI<50时翻转）
            if rsi < 25:
                if signal == '买入':
                    signal = '清仓'
                    reason += ' [极值翻转:W20/W50触低RSI<25，做空]'
                elif signal == '增持':
                    signal = '减持'
                    reason += ' [极值翻转:W20/W50触低RSI<25，做空]'
                elif signal == '持有':
                    signal = '减持'
                    reason += ' [极值翻转:W20/W50触低RSI<25，做空]'
            elif rsi < 50:
                if signal == '买入':
                    signal = '减持'
                    reason += ' [极值翻转:W20/W50触低RSI<50，做空]'
                elif signal == '增持':
                    signal = '减持'
                    reason += ' [极值翻转:W20/W50触低RSI<50，做空]'
                elif signal == '持有':
                    signal = '观望'
                    reason += ' [极值调整:W20/W50触低RSI<50，中性]'
        
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
            # W100触高：均值回归明显(-9.71%)，强制清仓
            if signal in ['增持', '买入', '持有']:
                signal = '清仓'
                reason += ' [极值强制:W100触高均值回归强，强制清仓]'
            elif signal == '观望':
                signal = '减持'
                reason += ' [极值强制:W100触高，转向做空]'
        
        elif extreme_event_type and extreme_event_type.startswith('W100_') and '_LOW' in extreme_event_type:
            # W100触低：均值回归微弱，避免做空
            if signal in ['减持', '清仓']:
                signal = '观望'
                reason += ' [极值调整:W100触低均值回归弱，避免做空]'
        
        # W252事件：均值回归极强
        elif 'W252_LOW' in extreme_event_type and '_LOW' in extreme_event_type:
            if signal in ['减持', '清仓']:
                signal = '增持'
                reason += ' [极值增强:W252触低均值回归，升为增持]'
            elif signal == '观望':
                signal = '持有'
                reason += ' [极值增强:W252触低均值回归]'
        
        elif 'W252_HIGH' in extreme_event_type and '_HIGH' in extreme_event_type:
            if signal in ['买入', '增持']:
                signal = '减持'
                reason += ' [极值增强:W252触高均值回归，降为减持]'
            elif signal == '观望':
                signal = '减持'
                reason += ' [极值增强:W252触高均值回归]'
        
        return signal, reason

    def _momentum_signal(self, data: Dict[str, Any],
                        extreme_event_type: str = None) -> Dict[str, Any]:
        """动量信号判断"""
        mom_score = data['mom_score']
        mom_1m = data['mom_1m']
        mom_1w = data['mom_1w']
        mom_accel = data['mom_accel']
        rsi = data['rsi']
        
        if mom_score > 30 and mom_1m > 10:
            if rsi < 75:
                signal = "买入"
                reason = "动量强势(月涨%.1f%%, RSI=%.1f < 75)，趋势确认" % (mom_1m, rsi)
            else:
                signal = "增持"
                reason = "动量强势但RSI=%.1f偏高，持有观察" % rsi
        elif mom_score > 15 and mom_1m > 5:
            signal = "增持"
            reason = "动量正向(月涨%.1f%%)，可增持" % mom_1m
        elif mom_score > 0:
            signal = "持有"
            reason = "动量中性偏正(mom_score=%.1f)" % mom_score
        elif mom_score > -15 and mom_1m > -10:
            signal = "观望"
            reason = "动量偏弱但未极端，月跌%.1f%%" % mom_1m
        else:
            signal = "减持"
            reason = "动量极弱(月跌%.1f%%, 年跌%.1f%%)" % (mom_1m, data['mom_1y'])
        
        if mom_accel > 15:
            reason += "，且有加速迹象(+%.1f%%)" % mom_accel
        
        # 极值事件调整
        signal, reason = self._apply_extreme_adjustment(signal, reason, extreme_event_type, rsi)
        
        evidence = [
            "1周=%.1f%% 1月=%.1f%% 3月=%.1f%% 1年=%.1f%%" % (
                data['mom_1w'], data['mom_1m'], data['mom_3m'], data['mom_1y']
            ),
            "动量评分=%.1f" % mom_score,
            "RSI=%.1f" % rsi,
        ]
        if extreme_event_type:
            evidence.append(f"极值事件={extreme_event_type}")
        
        return {'signal': signal, 'reason': reason, 'evidence': evidence}
    
    @classmethod
    def analyze_all(cls, stock_codes: list) -> Dict[str, Dict[str, Any]]:
        """批量分析多只股票的动量"""
        try:
            import qlib
            from qlib.data import D
            qlib.init(provider_uri=QLIB_DATA_PATH)
            
            adapter = cls("")
            adapter._qlib = qlib
            adapter._D = D
            adapter.qlib_initialized = True
            
            results = {}
            for code in stock_codes:
                qcode_info = get_qcode(code)
                if qcode_info:
                    qcode, name = qcode_info
                    data = adapter._calc_momentum(qcode)
                    if data:
                        results[code] = data
            
            return results
            
        except Exception as e:
            logger.error(f"[momentum] 批量分析失败: {e}")
            return {}
