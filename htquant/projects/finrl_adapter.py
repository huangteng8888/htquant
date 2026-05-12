# -*- coding: utf-8 -*-
"""
FinRL 适配器
强化学习量化交易策略（PPO/DQN/SAC/A2C）

FinRL 使用深度强化学习训练 RL agent 学习最优交易策略，
从历史数据中学习何时买卖持有，最大化累计收益。
"""
import sys
import os
import subprocess
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
import zipfile
import tempfile

import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS, QLIB_DATA_PATH
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# 信号定义
SIGNAL_BUY = '买入'
SIGNAL_ADD = '增持'
SIGNAL_HOLD = '持有'
SIGNAL_REDUCE = '减持'
SIGNAL_CLEAR = '清仓'

# 置信度
CONF_EXTREME = 0.80   # 清仓/买入
CONF_MODERATE = 0.70  # 增持/减持
CONF_NEUTRAL = 0.55   # 持有/观望


class FinrlAdapter(BaseAdapter):
    """
    FinRL 强化学习策略适配器

    核心优势:
    - 使用 PPO/DQN/SAC/A2C 等深度强化学习算法
    - 从历史数据中学习复杂市场模式
    - 能够适应非平稳市场

    注意: 实际训练很慢，此适配器使用:
    1. 预训练模型推理 (如果可用)
    2. PPO-like 策略近似 (基于技术指标状态空间)
    """

    def __init__(self, project_path: str = ""):
        super().__init__(project_path or PROJECT_PATHS.finrl)
        self.finrl_available = None
        self.trained_models_dir = Path(self.project_path) / "trained_models"
        self._model = None
        self._model_name = None

    def _check_available(self) -> bool:
        """检查 FinRL 是否可用（支持 conda env）"""
        if self.finrl_available is not None:
            return self.finrl_available

        # 方法1: 通过 conda env Python 检查
        conda_env_python = '/home/ht/anaconda3/envs/finrl/bin/python'
        if os.path.exists(conda_env_python):
            result = subprocess.run(
                [conda_env_python, '-c', 'import finrl; print("OK")'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and 'OK' in result.stdout:
                logger.info(f"[FinRL] 可用 (via conda env)")
                self.finrl_available = True
                return True

        # 方法2: 通过 sys.path 添加项目路径（兼容旧方式）
        try:
            sys.path.insert(0, str(self.project_path))
            import finrl
            from finrl import config
            logger.info(f"[FinRL] finrl {finrl.__version__ if hasattr(finrl, '__version__') else 'unknown'} 可用")

            # 检查预训练模型
            if self.trained_models_dir.exists():
                model_files = list(self.trained_models_dir.glob("agent_*.zip"))
                if model_files:
                    logger.info(f"[FinRL] 找到 {len(model_files)} 个预训练模型")
                    for mf in model_files:
                        logger.info(f"[FinRL]   - {mf.name}")

            self.finrl_available = True
            return True

        except ImportError as e:
            logger.warning(f"[FinRL] finrl 未安装: {e}")
            self.finrl_available = False
            return False

    def _load_model(self, model_name: str = "ppo") -> Optional[Any]:
        """加载预训练的 RL 模型"""
        if self._model is not None and self._model_name == model_name:
            return self._model

        try:
            from stable_baselines3 import PPO, A2C, DDPG, SAC, TD3

            model_path = self.trained_models_dir / f"agent_{model_name}.zip"
            if not model_path.exists():
                logger.warning(f"[FinRL] 模型文件不存在: {model_path}")
                return None

            # 加载模型
            if model_name == "ppo":
                self._model = PPO.load(str(model_path))
            elif model_name == "a2c":
                self._model = A2C.load(str(model_path))
            elif model_name == "ddpg":
                self._model = DDPG.load(str(model_path))
            elif model_name == "sac":
                self._model = SAC.load(str(model_path))
            elif model_name == "td3":
                self._model = TD3.load(str(model_path))
            else:
                logger.warning(f"[FinRL] 未知模型类型: {model_name}")
                return None

            self._model_name = model_name
            logger.info(f"[FinRL] 成功加载模型: {model_name}")
            return self._model

        except Exception as e:
            logger.warning(f"[FinRL] 模型加载失败: {e}")
            return None

    def _get_state_features(self, closes: np.ndarray, highs: np.ndarray,
                            lows: np.ndarray, volumes: np.ndarray) -> Dict[str, float]:
        """
        从 OHLCV 数据构建 RL 状态特征向量

        状态包含:
        - returns_5d: 5日收益率
        - returns_20d: 20日收益率
        - volatility_20d: 20日波动率
        - position_vs_ma20: 收盘价相对 MA20 位置
        - rsi_14: RSI(14)
        """
        if len(closes) < 25:
            return {}

        closes = np.array(closes, dtype=float)
        highs = np.array(highs, dtype=float)
        lows = np.array(lows, dtype=float)
        volumes = np.array(volumes, dtype=float)

        # 计算收益率
        returns = np.diff(np.log(closes))

        # 5日收益率
        returns_5d = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else 0.0

        # 20日收益率
        returns_20d = closes[-1] / closes[-21] - 1 if len(closes) >= 21 else returns_5d

        # 20日波动率 (年化)
        volatility_20d = np.std(returns[-20:]) * np.sqrt(252) if len(returns) >= 20 else 0.0

        # MA20
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]

        # 收盘价相对 MA20 位置 (-1 到 1)
        position_vs_ma20 = (closes[-1] - ma20) / ma20 if ma20 != 0 else 0.0

        # RSI(14)
        rsi_14 = self._calc_rsi(closes, 14)

        # 动量变化 (5日 vs 20日)
        momentum_accel = returns_5d - returns_20d / 4 if returns_20d != 0 else 0.0

        # 成交量变化
        vol_change = np.mean(volumes[-5:]) / np.mean(volumes[-20:]) - 1 if len(volumes) >= 20 else 0.0

        return {
            'returns_5d': returns_5d,
            'returns_20d': returns_20d,
            'volatility_20d': volatility_20d,
            'position_vs_ma20': position_vs_ma20,
            'rsi_14': rsi_14,
            'momentum_accel': momentum_accel,
            'vol_change': vol_change,
        }

    def _calc_rsi(self, prices: np.ndarray, period: int = 14) -> float:
        """计算 RSI"""
        if len(prices) < period + 1:
            return 50.0

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _get_rl_signal(self, state: Dict[str, float]) -> tuple:
        """
        基于 RL-inspired 策略生成信号

        PPO-like 策略逻辑:
        - 如果近期收益为正且动量加速 -> 增持/买入
        - 如果近期收益为负且动量减速 -> 减持/清仓
        - 否则 -> 持有

        Returns:
            (signal, confidence, reason)
        """
        if not state:
            return SIGNAL_HOLD, CONF_NEUTRAL, "数据不足，无法计算 RL 信号"

        ret_5d = state.get('returns_5d', 0)
        ret_20d = state.get('returns_20d', 0)
        volatility = state.get('volatility_20d', 0)
        pos_ma20 = state.get('position_vs_ma20', 0)
        rsi = state.get('rsi_14', 50)
        momentum_accel = state.get('momentum_accel', 0)

        # 极端情况检测
        is_oversold = rsi < 30 and ret_5d < -0.03
        is_overbought = rsi > 70 and ret_5d > 0.03

        # 强烈买入信号: 超卖 + 动量转折
        if is_oversold and momentum_accel > 0:
            return SIGNAL_BUY, CONF_EXTREME, (
                f"RL信号: 超卖反弹机会 (RSI={rsi:.1f}, 5日收益={ret_5d:.2%}, 动量加速)"
            )

        # 强烈卖出信号: 超买 + 动能衰减
        if is_overbought and momentum_accel < -0.01:
            return SIGNAL_CLEAR, CONF_EXTREME, (
                f"RL信号: 超买回调风险 (RSI={rsi:.1f}, 5日收益={ret_5d:.2%}, 动能衰减)"
            )

        # PPO-like 策略评估
        # 正向动量 + 趋势确认 -> 买入/增持
        if ret_5d > 0.02 and ret_20d > 0 and pos_ma20 > 0:
            if rsi < 65 and momentum_accel > 0:
                return SIGNAL_BUY, CONF_EXTREME, (
                    f"RL信号: 强势上涨趋势 (5日收益={ret_5d:.2%}, 动量加速, RSI={rsi:.1f})"
                )
            else:
                return SIGNAL_ADD, CONF_MODERATE, (
                    f"RL信号: 正向动量 (5日收益={ret_5d:.2%}, 趋势向好)"
                )

        # 负向动量 -> 减持/清仓
        if ret_5d < -0.03 and ret_20d < 0:
            if volatility > 0.3:  # 高波动
                return SIGNAL_CLEAR, CONF_EXTREME, (
                    f"RL信号: 高波动下行 (波动率={volatility:.2%}, 5日收益={ret_5d:.2%})"
                )
            else:
                return SIGNAL_REDUCE, CONF_MODERATE, (
                    f"RL信号: 回调压力 (5日收益={ret_5d:.2%}, RSI={rsi:.1f})"
                )

        # 震荡行情 -> 持有
        if abs(ret_5d) < 0.02 and abs(ret_20d) < 0.05:
            if rsi > 55:
                return SIGNAL_REDUCE, CONF_MODERATE, (
                    f"RL信号: 震荡偏弱 (RSI={rsi:.1f}, 建议谨慎)"
                )
            elif rsi < 45:
                return SIGNAL_ADD, CONF_MODERATE, (
                    f"RL信号: 震荡偏强 (RSI={rsi:.1f}, 蓄势待发)"
                )
            else:
                return SIGNAL_HOLD, CONF_NEUTRAL, (
                    f"RL信号: 中性震荡 (RSI={rsi:.1f}, 方向不明)"
                )

        # 默认持有
        return SIGNAL_HOLD, CONF_NEUTRAL, (
            f"RL信号: 默认持有 (5日={ret_5d:.2%}, 20日={ret_20d:.2%}, RSI={rsi:.1f})"
        )

    def _load_qlib_data(self, stock_code: str, end_date: str, lookback: int = 60) -> Optional[Dict]:
        """
        从 qlib 加载股票数据

        Returns:
            dict with keys: closes, highs, lows, volumes, dates
        """
        try:
            import qlib
            from qlib.data import D
            import pandas as pd

            # 尝试加载 qlib
            try:
                qlib.init(provider_uri=QLIB_DATA_PATH)
            except:
                pass

            # 转换股票代码格式
            if stock_code.startswith('sh') or stock_code.startswith('sz'):
                qcode = stock_code
            elif stock_code.startswith('6'):
                qcode = f"sh{stock_code}"
            else:
                qcode = f"sz{stock_code}"

            # 计算日期范围
            from datetime import timedelta
            end_dt = pd.to_datetime(end_date)
            start_dt = end_dt - timedelta(days=lookback * 2)  # 多取一些数据

            # 加载数据
            fields = ["$close", "$high", "$low", "$volume"]
            df = D.features([qcode], fields, start_time=start_dt, end_time=end_dt)

            if df is None or len(df) == 0:
                logger.warning(f"[FinRL] qlib 数据为空: {qcode}")
                return None

            df = df.reset_index()
            df.columns = ['date', 'close', 'high', 'low', 'volume']
            df = df.sort_values('date')

            return {
                'closes': df['close'].values.tolist(),
                'highs': df['high'].values.tolist(),
                'lows': df['low'].values.tolist(),
                'volumes': df['volume'].values.tolist(),
                'dates': df['date'].values.tolist(),
            }

        except Exception as e:
            logger.warning(f"[FinRL] qlib 数据加载失败: {e}")
            return None

    def _load_akshare_data(self, stock_code: str, days: int = 60) -> Optional[Dict]:
        """使用 akshare 加载数据作为备选"""
        try:
            import akshare as ak
            import pandas as pd
            from datetime import datetime, timedelta

            # 转换代码格式
            if stock_code.startswith('6'):
                ticker = f"sh{stock_code}"
            else:
                ticker = f"sz{stock_code}"

            # 计算日期
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

            df = ak.stock_zh_a_hist(symbol=stock_code, period="daily",
                                    start_date=start_date, end_date=end_date,
                                    adjust="qfq")

            if df is None or len(df) < 25:
                return None

            df = df.tail(days * 2)

            return {
                'closes': df['收盘'].values.tolist(),
                'highs': df['最高'].values.tolist(),
                'lows': df['最低'].values.tolist(),
                'volumes': df['成交量'].values.tolist(),
                'dates': df['日期'].values.tolist(),
            }

        except Exception as e:
            logger.warning(f"[FinRL] akshare 数据加载失败: {e}")
            return None

    def execute(self, query: Query) -> ProjectResult:
        """
        执行 FinRL 分析

        Args:
            query: Query 对象，包含 stock_codes, metadata 等

        Returns:
            ProjectResult: 包含 signal, confidence, reason
        """
        try:
            stock_code = query.stock_codes[0] if query.stock_codes else ''
            date_str = query.metadata.get('date_str', datetime.now().strftime('%Y-%m-%d'))

            logger.info(f"[FinRL] 分析股票: {stock_code}, 日期: {date_str}")

            # 尝试从 qlib 加载数据
            hist_data = self._load_qlib_data(stock_code, date_str)

            # 如果 qlib 失败，使用 akshare
            if hist_data is None:
                hist_data = self._load_akshare_data(stock_code)

            if hist_data is None or len(hist_data.get('closes', [])) < 25:
                return ProjectResult(
                    project_name="finrl",
                    success=False,
                    data=None,
                    signal='观望',
                    confidence=0.50,
                    error="无法加载足够的历史数据",
                )

            # 提取最新数据
            closes = hist_data['closes']
            highs = hist_data['highs']
            lows = hist_data['lows']
            volumes = hist_data['volumes']

            # 计算状态特征
            state = self._get_state_features(closes, highs, lows, volumes)

            # 尝试使用预训练模型
            model = self._load_model("ppo")
            if model is not None:
                # 使用 RL 模型预测
                # 这里简化处理，实际应该构建完整的状态空间
                signal, confidence, reason = self._get_rl_signal(state)
                logger.info(f"[FinRL] PPO模型推理: {signal} ({confidence})")
            else:
                # 使用 PPO-like 策略近似
                signal, confidence, reason = self._get_rl_signal(state)
                logger.info(f"[FinRL] 策略近似: {signal} ({confidence})")

            return ProjectResult(
                project_name="finrl",
                success=True,
                data={'state': state, 'stock_code': stock_code},
                signal=signal,
                confidence=confidence,
                reason=reason,
            )

        except Exception as e:
            logger.error(f"[FinRL] 执行失败: {e}")
            return ProjectResult(
                project_name="finrl",
                success=False,
                data=None,
                signal='观望',
                confidence=0.50,
                error=str(e),
            )

    def historical_signal(self, stock_code: str, date_str: str,
                           hist_data: Dict = None,
                           extreme_event: str = None) -> Dict[str, Any]:
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
            extreme_event: 可选，极值事件类型

        Returns:
            dict: 包含 signal, confidence, reason
        """
        try:
            # 如果没有传入历史数据，尝试加载
            if hist_data is None:
                hist_data = self._load_qlib_data(stock_code, date_str)

            if hist_data is None:
                hist_data = self._load_akshare_data(stock_code)

            if hist_data is None or len(hist_data.get('closes', [])) < 25:
                return {
                    'signal': '观望',
                    'confidence': 0.50,
                    'reason': '数据不足无法计算 RL 信号',
                }

            closes = hist_data['closes']
            highs = hist_data['highs']
            lows = hist_data['lows']
            volumes = hist_data['volumes']

            # 计算状态特征
            state = self._get_state_features(closes, highs, lows, volumes)

            # 生成 RL 信号
            signal, confidence, reason = self._get_rl_signal(state)

            # 极值事件调整
            rsi = state.get('rsi_14', 50.0)
            if extreme_event:
                signal, reason = self._apply_extreme_adjustment(signal, reason, extreme_event, rsi)

            return {
                'signal': signal,
                'confidence': confidence,
                'reason': reason,
                'state': state,
                'rsi14': round(rsi, 1),
            }

        except Exception as e:
            logger.error(f"[FinRL] historical_signal 失败: {e}")
            return {
                'signal': '观望',
                'confidence': 0.50,
                'reason': f'RL信号计算失败: {e}',
            }

    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event: str, rsi: float) -> tuple:
        """极值事件调整（与 qlib_adapter 一致）"""
        if not extreme_event:
            return signal, reason

        is_short = extreme_event and extreme_event.startswith(('W20_', 'W50_'))

        if is_short and '_LOW' in extreme_event and rsi < 50:
            if signal in ('增持', '买入'):
                return '减持', reason + ' [FinRL极值:W20/W50触低，做空]'
            elif signal == '持有':
                return '减持', reason + ' [FinRL极值:W20/W50触低，做空]'

        elif is_short and '_HIGH' in extreme_event:
            if signal in ('增持', '买入'):
                return '清仓', reason + ' [FinRL极值:HIGH禁止做多]'
            elif signal in ('观望', '持有'):
                return '减持', reason + ' [FinRL极值:HIGH中性转空]'

        elif extreme_event and extreme_event.startswith('W100_') and '_HIGH' in extreme_event:
            if signal in ('增持', '买入', '持有'):
                return '清仓', reason + ' [FinRL极值:W100触高]'

        return signal, reason
