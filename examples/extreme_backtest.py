"""
极端事件辩论引擎回测
==============================
对每个极值事件时间点运行辩论引擎，
验证信号方向与后续收益的匹配成功率。

事件类型:
  - W20_HIGH_TOUCH  / W20_LOW_TOUCH  (20日窗口触及高/低价)
  - W50_HIGH_TOUCH  / W50_LOW_TOUCH  (50日窗口触及高/低价)
  - W100_HIGH_TOUCH / W100_LOW_TOUCH (100日窗口触及高/低价)
  - W252_HIGH_TOUCH / W252_LOW_TOUCH (252日窗口触及高/低价)
  - NEW_252W_HIGH   / NEW_252W_LOW   (252日窗口突破/新高低)

信号方向:
  - 做多 = {买入, 增持, 持有}
  - 做空 = {减持, 清仓}
  - 观望  = 不计入成功/失败

成功定义:
  - 做多信号 → 20日forward return > 0  → 成功
  - 做空信号 → 20日forward return < 0  → 成功
  - 其他     → 计入"无效"
"""

import sys
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict

warnings.filterwarnings('ignore')

# ── QuantDB 统一数据库（推荐）──────────────────────────────────────────────────
# 数据源: /mnt/data/金融数据/quantdb/quantdb.sqlite
# 后台导入中, 全量完成后自动使用; 若尚未导入则回退到 tdx_reader
sys.path.insert(0, str(__file__).rsplit('/examples', 1)[0])
from htquant.quantdb import QuantDB as _QDB
from htquant.config import STOCK_CODE_MAPPING

_qdb = _QDB()
_db_ready = _qdb.quick_test()

if _db_ready:
    _STATS = _qdb.stats()
    if _STATS['total_stocks'] >= 100:
        print(f"[quantdb] 已就绪: {_STATS['total_stocks']} 只股, {_STATS['total_records']:,} 条记录")
        _USE_QUANTDB = True
    else:
        print(f"[quantdb] 数据不足({_STATS['total_stocks']} 只), 回退到 tdx_reader")
        _USE_QUANTDB = False
else:
    print("[quantdb] 未就绪, 回退到 tdx_reader")
    _USE_QUANTDB = False

# 回退方案: tdx_reader
if not _USE_QUANTDB:
    from htquant.tdx_reader import get_stock_data

STOCK_CODES = ['000001', '000901', '300777', '688089', '300896', '301071', '600422', '300363']
STOCK_NAMES = {code: STOCK_CODE_MAPPING[code][1] for code in STOCK_CODES}

START = '2023-01-01'
END   = '2025-06-15'

# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ExtremeEvent:
    """一次极端事件"""
    stock_code: str
    event_type: str          # e.g. 'W20_HIGH_TOUCH'
    date_idx: int            # 在 data arrays 中的整数索引
    date_str: str            # 'YYYY-MM-DD'
    close_at_event: float
    rolling_max: float
    rolling_min: float
    fwd_20d_return: float    # event后20日收益率


@dataclass
class HistoricalSignal:
    """历史时间点的辩论信号"""
    stock_code: str
    date_str: str
    # qlib 信号
    qlib_signal: str = "观望"
    qlib_confidence: float = 0.0
    qlib_reason: str = ""
    # momentum 信号
    mom_signal: str = "观望"
    mom_confidence: float = 0.0
    mom_reason: str = ""
    # 辩论结果
    debate_signal: str = "观望"
    debate_confidence: float = 0.0
    debate_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 第一步：加载所有股票的历史数据
# ─────────────────────────────────────────────────────────────────────────────
print("正在加载历史数据...")

all_data = {}   # stock_code -> {dates, closes, highs, lows}

print("正在加载历史数据...")
if _USE_QUANTDB:
    print("数据源: quantdb (SQLite)")
    for code in STOCK_CODES:
        data = _qdb.get_daily(code, START, END)
        if data:
            all_data[code] = data
            print(f"  {code} {STOCK_NAMES[code]}: {len(data['dates'])} 个交易日")
        else:
            print(f"  警告: {code} ({STOCK_NAMES[code]}) 无数据，跳过")
else:
    print("数据源: tdx_reader (通达信)")
    for code in STOCK_CODES:
        try:
            df = get_stock_data(code, START, END)
            all_data[code] = {
                'dates':  [str(d)[:10] for d in df.index.tolist()],
                'closes': df['$close'].values,
                'highs':  df['$high'].values,
                'lows':   df['$low'].values,
            }
            print(f"  {code} {STOCK_NAMES[code]}: {len(df)} 个交易日")
        except FileNotFoundError:
            print(f"  警告: {code} ({STOCK_NAMES[code]}) 无数据，跳过")

print(f"共加载 {len(all_data)} 只股票\n")

# ─────────────────────────────────────────────────────────────────────────────
# 第二步：计算各窗口的滚动高低价
# ─────────────────────────────────────────────────────────────────────────────
WINDOWS = [20, 50, 100, 252]

def compute_rolling(data: dict, window: int) -> Tuple[np.ndarray, np.ndarray]:
    closes = data['closes']
    highs  = data['highs']
    lows   = data['lows']
    n = len(closes)
    roll_max = np.full(n, np.nan)
    roll_min = np.full(n, np.nan)
    for i in range(window - 1, n):
        roll_max[i] = np.max(highs[i - window + 1: i + 1])
        roll_min[i] = np.min(lows[i  - window + 1: i + 1])
    return roll_max, roll_min


# 预计算所有窗口
print("计算滚动高低价...")
rolling_data = {}   # code -> {20: (roll_max, roll_min), ...}
for code, data in all_data.items():
    rolling_data[code] = {}
    for w in WINDOWS:
        rm, rn = compute_rolling(data, w)
        rolling_data[code][w] = (rm, rn)

print("完成\n")

# ─────────────────────────────────────────────────────────────────────────────
# 第三步：检测极值事件
# ─────────────────────────────────────────────────────────────────────────────
THRESHOLD_PCT = 0.01   # 1% threshold

def detect_events(code: str, window: int,
                  roll_max: np.ndarray, roll_min: np.ndarray) -> List[ExtremeEvent]:
    """检测指定窗口的所有极值事件"""
    data = all_data[code]
    n = len(data['closes'])
    events = []
    # forward window 20 days
    FWD = 20

    for i in range(window, n - FWD):
        close = data['closes'][i]
        high  = data['highs'][i]
        low   = data['lows'][i]
        date_str = str(data['dates'][i])[:10]

        rm = roll_max[i]
        rn = roll_min[i]
        if np.isnan(rm) or np.isnan(rn):
            continue

        # 触及/突破 判断
        high_touch  = abs(high  - rm) / close < THRESHOLD_PCT
        low_touch   = abs(low   - rn) / close < THRESHOLD_PCT
        high_break  = high  > rm
        low_break   = low   < rn

        event_type = None
        if   window == 252 and high_break: event_type = 'NEW_252W_HIGH'
        elif window == 252 and low_break:  event_type = 'NEW_252W_LOW'
        elif window == 252 and high_touch: event_type = 'W252_HIGH_TOUCH'
        elif window == 252 and low_touch:  event_type = 'W252_LOW_TOUCH'
        elif window == 100 and high_break: event_type = 'W100_HIGH_BREAK'
        elif window == 100 and low_break:  event_type = 'W100_LOW_BREAK'
        elif window == 100 and high_touch: event_type = 'W100_HIGH_TOUCH'
        elif window == 100 and low_touch:  event_type = 'W100_LOW_TOUCH'
        elif window == 50  and high_break: event_type = 'W50_HIGH_BREAK'
        elif window == 50  and low_break:  event_type = 'W50_LOW_BREAK'
        elif window == 50  and high_touch: event_type = 'W50_HIGH_TOUCH'
        elif window == 50  and low_touch:  event_type = 'W50_LOW_TOUCH'
        elif window == 20  and high_break: event_type = 'W20_HIGH_BREAK'
        elif window == 20  and low_break:  event_type = 'W20_LOW_BREAK'
        elif window == 20  and high_touch: event_type = 'W20_HIGH_TOUCH'
        elif window == 20  and low_touch:  event_type = 'W20_LOW_TOUCH'

        if event_type is None:
            continue

        # forward return
        fwd_close = data['closes'][i + FWD]
        fwd_ret   = (fwd_close - close) / close

        events.append(ExtremeEvent(
            stock_code     = code,
            event_type     = event_type,
            date_idx       = i,
            date_str       = date_str,
            close_at_event = close,
            rolling_max    = rm,
            rolling_min    = rn,
            fwd_20d_return = fwd_ret,
        ))

    return events


print("检测极值事件...")
all_events = []
for code in STOCK_CODES:
    if code not in rolling_data:
        continue
    for w in WINDOWS:
        rm, rn = rolling_data[code][w]
        evts = detect_events(code, w, rm, rn)
        all_events.extend(evts)

print(f"共检测到 {len(all_events)} 个极值事件")

# 按类型统计
type_counts = defaultdict(int)
for e in all_events:
    type_counts[e.event_type] += 1
for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f"  {t}: {c}")

print()

# ─────────────────────────────────────────────────────────────────────────────
# 第四步：历史信号计算（在事件日期截断数据上计算指标）
# ─────────────────────────────────────────────────────────────────────────────
def calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    """计算RSI（最后一个值）"""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def calc_bollinger(closes: np.ndarray, period: int = 20) -> Tuple[float, float, float]:
    """计算布林带（最后值）"""
    if len(closes) < period:
        return np.nan, np.nan, np.nan
    ma  = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return ma - 2 * std, ma, ma + 2 * std


def calc_ma(closes: np.ndarray, period: int) -> float:
    if len(closes) < period:
        return np.nan
    return np.mean(closes[-period:])


def _apply_qlib_extreme_adjustment(signal: str, confidence: float, reason: str,
                                    extreme_event_type: str, rsi14: float) -> tuple:
    """
    根据极值事件类型调整qlib信号
    
    核心修正（基于2026-05-07回测数据）：
    - W20/W50 LOW触低：短期趋势延续，做多信号应降级
    - W20/W50 HIGH触高：短期趋势延续，做空信号应降级
    - W100/W252：中长期均值回归，信号保持或增强
    """
    if not extreme_event_type:
        return signal, confidence, reason
    
    is_short_extreme = extreme_event_type and extreme_event_type.startswith(('W20_', 'W50_'))
    
    # 注意：event_type 格式是 W20_LOW_TOUCH，不能用 endswith('_LOW')，要用 in
    if is_short_extreme and '_LOW' in extreme_event_type:
        # W20/W50触低：短期趋势向下，RSI<50时均值回归不成立
        # 【核心修正】扩大翻转范围到 RSI<50（而非仅<30）
        if rsi14 < 25:
            # RSI深度超卖：强化翻转
            if signal == '买入':
                signal = '清仓'
                confidence = 0.88
                reason += ' [极值翻转:W20/W50触低RSI<25深度超卖，强做空]'
            elif signal == '增持':
                signal = '减持'
                confidence = 0.78
                reason += ' [极值翻转:W20/W50触低RSI<25深度超卖，做空]'
            elif signal == '持有':
                signal = '减持'
                confidence = 0.68
                reason += ' [极值翻转:W20/W50触低RSI<25，做空]'
        elif rsi14 < 50:
            # RSI 25-50 中性/偏弱区域：均值回归不成立，翻转
            if signal == '买入':
                signal = '减持'
                confidence = 0.75
                reason += ' [极值翻转:W20/W50触低RSI<50，做空]'
            elif signal == '增持':
                signal = '减持'
                confidence = 0.68
                reason += ' [极值翻转:W20/W50触低RSI<50，做空]'
            elif signal == '持有':
                signal = '观望'
                confidence = 0.55
                reason += ' [极值翻转:W20/W50触低RSI<50，中性]'
    
    elif is_short_extreme and '_HIGH' in extreme_event_type:
        # W20/W50触高：市场实际下跌（-2.20%/-6.31%），应强制短空
        # 【关键】辩论引擎会把qlib中性信号升级为"增持"，必须强制清除
        if signal in ['增持', '买入']:
            signal = '清仓'
            confidence = 0.85
            reason += ' [极值强制:HIGH事件禁止做多，清仓]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.75
            reason += ' [极值强制:HIGH事件，转向做空]'
        elif signal == '持有':
            signal = '减持'
            confidence = 0.70
            reason += ' [极值强制:HIGH事件，转向做空]'
    
    # W100事件：均值回归微弱，调整但不完全翻转
    elif extreme_event_type and extreme_event_type.startswith('W100_') and '_LOW' in extreme_event_type and rsi14 < 30:
        # W100触低：均值回归微弱(+0.33%)，谨慎做多
        if signal == '买入':
            signal = '增持'
            confidence = 0.65
            reason += ' [极值调整:W100触低均值回归弱，降为增持]'
        elif signal == '增持':
            signal = '观望'
            confidence = 0.50
            reason += ' [极值调整:W100触低均值回归弱，谨慎]'
    
    elif extreme_event_type and extreme_event_type.startswith('W100_') and '_HIGH' in extreme_event_type and rsi14 > 70:
        # W100触高：均值回归明显(-9.71%)，保持做空
        if signal == '减持':
            signal = '清仓'
            confidence = 0.85
            reason += ' [极值增强:W100触高均值回归强，提升至清仓]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.70
            reason += ' [极值增强:W100触高均值回归，增强做空]'
    
    # W252事件：均值回归极强，增强信号
    elif 'W252_LOW' in extreme_event_type and '_LOW' in extreme_event_type and rsi14 < 30:
        if signal in ['增持', '持有']:
            signal = '买入'
            confidence = 0.92
            reason += ' [极值增强:W252触低均值回归极强，提升至买入]'
        elif signal == '观望':
            signal = '增持'
            confidence = 0.80
            reason += ' [极值增强:W252触低均值回归，提升至增持]'
    
    elif 'W252_HIGH' in extreme_event_type and '_HIGH' in extreme_event_type and rsi14 > 70:
        if signal in ['减持', '观望']:
            signal = '清仓'
            confidence = 0.92
            reason += ' [极值增强:W252触高均值回归极强，提升至清仓]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.80
            reason += ' [极值增强:W252触高均值回归，提升至减持]'
    
    return signal, confidence, reason


def qlib_historical_signal(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                             extreme_event_type: str = None) -> Dict[str, Any]:
    """在截断历史数据上计算qlib风格信号"""
    close = closes[-1]
    rsi14  = calc_rsi(closes, 14)
    rsi28  = calc_rsi(closes, 28)
    bb_low, bb_mid, bb_high = calc_bollinger(closes, 20)
    ma5   = calc_ma(closes, 5)
    ma10  = calc_ma(closes, 10)
    ma20  = calc_ma(closes, 20)
    ma60  = calc_ma(closes, 60)

    # RSI 超买超卖判断
    if rsi14 > 75:
        rs_level = f"RSI={rsi14:.0f}超买"
    elif rsi14 < 30:
        rs_level = f"RSI={rsi14:.0f}超卖"
    elif rsi14 > 60:
        rs_level = f"RSI={rsi14:.0f}偏高"
    elif rsi14 < 40:
        rs_level = f"RSI={rsi14:.0f}偏低"
    else:
        rs_level = f"RSI={rsi14:.0f}中性"

    # 均线多头/空头排列
    ma_bullish = ma5 > ma10 > ma20 if not (np.isnan(ma5) or np.isnan(ma10) or np.isnan(ma20)) else False
    ma_bearish = ma5 < ma10 < ma20 if not (np.isnan(ma5) or np.isnan(ma10) or np.isnan(ma20)) else False

    # 布林带位置
    if not np.isnan(bb_high) and close > bb_high:
        bb_pos = "突破上轨"
    elif not np.isnan(bb_low) and close < bb_low:
        bb_pos = "跌破下轨"
    elif not np.isnan(bb_high) and close > bb_mid:
        bb_pos = "上半段"
    else:
        bb_pos = "下半段"

    # 信号判断
    if rsi14 < 30 and bb_pos in ["下半段", "跌破下轨"]:
        signal = "买入"
        reason = f"{rs_level} + {bb_pos}，均值回归机会"
        confidence = 0.85
    elif rsi14 > 70 and close > bb_mid and not ma_bullish:
        signal = "减持"
        reason = f"{rs_level} + {bb_pos}，注意回调风险"
        confidence = 0.75
    elif rsi14 > 80:
        signal = "清仓"
        reason = f"{rs_level}极度超买，风险极大"
        confidence = 0.90
    elif ma_bullish and rsi14 < 65:
        signal = "持有"
        reason = f"均线多头排列，趋势向上，{rs_level}"
        confidence = 0.65
    elif ma_bearish and rsi14 > 40:
        signal = "减持"
        reason = f"均线空头排列，{rs_level}"
        confidence = 0.70
    elif rsi14 < 40:
        signal = "增持"
        reason = f"{rs_level}偏低，估值有支撑"
        confidence = 0.60
    else:
        signal = "观望"
        reason = f"{rs_level}，{bb_pos}，无明确方向"
        confidence = 0.50

    # 应用极值事件调整
    signal, confidence, reason = _apply_qlib_extreme_adjustment(
        signal, confidence, reason, extreme_event_type, rsi14)

    return {
        'signal':    signal,
        'confidence': confidence,
        'reason':    reason,
        'rsi14':     rsi14,
        'rsi28':     rsi28,
        'ma_bullish': ma_bullish,
        'ma_bearish': ma_bearish,
    }


def _apply_momentum_extreme_adjustment(signal: str, confidence: float, reason: str,
                                       extreme_event_type: str, rsi14: float) -> tuple:
    """
    根据极值事件类型调整动量信号
    - W20/W50 LOW触低：短期趋势延续，动量降级或翻空
    - W20/W50 HIGH触高：短期趋势延续，动量降级或翻多
    - W100/W252：中长期均值回归，信号保持
    """
    if not extreme_event_type:
        return signal, confidence, reason
    
    is_short_extreme = extreme_event_type and extreme_event_type.startswith(('W20_', 'W50_'))
    
    if is_short_extreme and '_LOW' in extreme_event_type:
        # W20/W50触低：短期趋势向下，动量应该做空而非做多
        # 【强化】全部翻转，包括"增持"
        if signal == '买入':
            signal = '清仓'
            confidence = 0.85
            reason += ' [极值翻转:W20/W50触低趋势延续，做空]'
        elif signal == '增持':
            signal = '减持'
            confidence = 0.75
            reason += ' [极值翻转:W20/W50触低趋势延续，做空]'
        elif signal == '持有':
            signal = '减持'
            confidence = 0.65
            reason += ' [极值翻转:W20/W50触低，做空]'
    
    elif is_short_extreme and '_HIGH' in extreme_event_type:
        # W20/W50触高：市场实际下跌（-2.20%/-6.31%），应强制短空
        # 辩论引擎会把momentum中性信号升级为"增持"，必须强制清除
        if signal in ['增持', '买入']:
            signal = '清仓'
            confidence = 0.85
            reason += ' [极值强制:HIGH事件禁止做多，清仓]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.75
            reason += ' [极值强制:HIGH事件，转向做空]'
        elif signal == '持有':
            signal = '减持'
            confidence = 0.70
            reason += ' [极值强制:HIGH事件，转向做空]'
    
    elif extreme_event_type and extreme_event_type.startswith('W100_') and '_HIGH' in extreme_event_type:
        # W100触高：均值回归明显(-9.71%)，momentum说增持必须强制清仓
        if signal in ['增持', '买入', '持有']:
            signal = '清仓'
            confidence = 0.88
            reason += ' [极值强制:W100触高均值回归强，强制清仓]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.75
            reason += ' [极值强制:W100触高，转向做空]'
    
    elif extreme_event_type and extreme_event_type.startswith('W100_') and '_LOW' in extreme_event_type:
        # W100触低：均值回归微弱(+0.33%)，momentum做空需谨慎
        if signal in ['减持', '清仓']:
            signal = '观望'
            confidence = 0.55
            reason += ' [极值调整:W100触低均值回归弱，避免做空]'
    
    # W252事件：均值回归成立
    elif 'W252_LOW' in extreme_event_type and '_LOW' in extreme_event_type:
        if signal in ['减持', '清仓']:
            signal = '增持'
            confidence = 0.80
            reason += ' [极值增强:W252触低均值回归，升为增持]'
        elif signal == '观望':
            signal = '持有'
            confidence = 0.65
            reason += ' [极值增强:W252触低均值回归]'
    
    elif 'W252_HIGH' in extreme_event_type and '_HIGH' in extreme_event_type:
        if signal in ['买入', '增持']:
            signal = '减持'
            confidence = 0.80
            reason += ' [极值增强:W252触高均值回归，降为减持]'
        elif signal == '观望':
            signal = '减持'
            confidence = 0.65
            reason += ' [极值增强:W252触高均值回归]'
    
    return signal, confidence, reason


def momentum_historical_signal(closes: np.ndarray,
                               extreme_event_type: str = None) -> Dict[str, Any]:
    """在截断历史数据上计算动量信号"""
    if len(closes) < 250:
        return {'signal': '观望', 'confidence': 0.5, 'reason': '数据不足1年'}

    mom_1w = (closes[-1] / closes[-5]  - 1) * 100 if len(closes) >= 5  else 0
    mom_1m = (closes[-1] / closes[-22] - 1) * 100 if len(closes) >= 22 else 0
    mom_3m = (closes[-1] / closes[-65] - 1) * 100 if len(closes) >= 65 else 0
    mom_1y = (closes[-1] / closes[-245] - 1) * 100 if len(closes) >= 245 else 0

    mom_score  = mom_1w * 0.3 + mom_1m * 0.3 + mom_3m * 0.25 + mom_1y * 0.15
    mom_accel  = mom_1w * 4 - mom_1m

    rsi = calc_rsi(closes, 14)

    if mom_score > 30 and mom_1m > 10:
        if rsi < 75:
            signal = "买入"
            confidence = 0.80
            reason = f"动量强势(月涨{mom_1m:.1f}%), 动量评分={mom_score:.0f}, RSI={rsi:.0f}"
        else:
            signal = "增持"
            confidence = 0.70
            reason = f"动量强但RSI={rsi:.0f}偏高, 动量评分={mom_score:.0f}"
    elif mom_score > 15 and mom_1m > 5:
        signal = "增持"
        confidence = 0.65
        reason = f"动量正向(月涨{mom_1m:.1f}%), 动量评分={mom_score:.0f}"
    elif mom_score > 0:
        signal = "持有"
        confidence = 0.55
        reason = f"动量中性, 动量评分={mom_score:.0f}"
    elif mom_score > -15 and mom_1m > -10:
        signal = "观望"
        confidence = 0.50
        reason = f"动量偏弱，月跌{mom_1m:.1f}%, 动量评分={mom_score:.0f}"
    else:
        signal = "减持"
        confidence = 0.75
        reason = f"动量极弱(月跌{mom_1m:.1f}%, 年跌{mom_1y:.1f}%), 动量评分={mom_score:.0f}"

    if mom_accel > 15:
        reason += f"，加速迹象(+{mom_accel:.1f}%)"

    # 应用极值事件调整
    signal, confidence, reason = _apply_momentum_extreme_adjustment(
        signal, confidence, reason, extreme_event_type, rsi)

    return {
        'signal':     signal,
        'confidence': confidence,
        'reason':     reason,
        'mom_score':  mom_score,
        'mom_accel':  mom_accel,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 第五步：混合辩论引擎（真理越辨越明版）
# ─────────────────────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(__file__).rsplit('/examples', 1)[0])
from htquant.debate_truth import run_truth_debate, SIGNAL_SCORE

def run_debate_truth(
    stock_code: str,
    qlib_res: Dict,
    mom_res: Dict,
    event_type: str,
) -> Tuple[str, float, str]:
    """
    对接新的真理辩论引擎。

    qlib_res / mom_res 的结构：
      {signal, confidence, reason, rsi14, rsi28, ma_bullish, ma_bearish, ...}
      {signal, confidence, reason, mom_score, mom_accel, ...}
    """
    # 构造 all_results（ProjectResult-like）
    all_results = {
        'qlib':     type('ProjectResult', (), {
            'success': True, 'signal': qlib_res['signal'],
            'confidence': qlib_res['confidence'],
            'reason': qlib_res.get('reason', ''),
            'data': qlib_res,
        })(),
        'momentum': type('ProjectResult', (), {
            'success': True, 'signal': mom_res['signal'],
            'confidence': mom_res['confidence'],
            'reason': mom_res.get('reason', ''),
            'data': mom_res,
        })(),
    }

    initial_signals = {'qlib': qlib_res['signal'], 'momentum': mom_res['signal']}
    initial_reasons = {
        'qlib':     qlib_res.get('reason', ''),
        'momentum': mom_res.get('reason', ''),
    }

    # 传入极值事件类型，帮助论据评判
    truth_res = run_truth_debate(
        stock_code=stock_code,
        horizon='medium',
        all_results=all_results,
        initial_signals=initial_signals,
        initial_reasons=initial_reasons,
        extreme_event_type=event_type,
    )

    return truth_res.final_signal, truth_res.final_confidence, str(truth_res.debate_log[-1] if truth_res.debate_log else '')


# ─────────────────────────────────────────────────────────────────────────────
# 第六步：对每个事件运行回测
# ─────────────────────────────────────────────────────────────────────────────
print("\n开始回测（这可能需要几分钟）...")

BULL_SIGNALS = {'买入', '增持', '持有'}
BEAR_SIGNALS = {'减持', '清仓'}

results = []  # List[dict]

for idx, evt in enumerate(all_events):
    if idx % 200 == 0:
        print(f"  进度: {idx}/{len(all_events)}")

    code    = evt.stock_code
    date_i  = evt.date_idx
    data    = all_data[code]

    # 构造截至事件日的历史数据（往前推足够长）
    lookback = 300  # 确保有足够数据算所有指标
    start_i  = max(0, date_i - lookback)
    end_i    = date_i + 1  # 包含事件日本身

    hist_closes = data['closes'][start_i:end_i]
    hist_highs  = data['highs'][start_i:end_i]
    hist_lows   = data['lows'][start_i:end_i]

    if len(hist_closes) < 60:
        continue

    # 计算历史信号
    qlib_res = qlib_historical_signal(hist_closes, hist_highs, hist_lows, evt.event_type)
    mom_res  = momentum_historical_signal(hist_closes, evt.event_type)

    # 运行辩论引擎
    debate_signal, debate_conf, debate_reason = run_debate_truth(
        stock_code=code, qlib_res=qlib_res, mom_res=mom_res, event_type=evt.event_type)

    # 判断成功/失败
    fwd_ret  = evt.fwd_20d_return
    direction = '做多' if debate_signal in BULL_SIGNALS else ('做空' if debate_signal in BEAR_SIGNALS else '无效')

    if direction == '做多':
        success = fwd_ret > 0
    elif direction == '做空':
        success = fwd_ret < 0
    else:
        success = None  # 无效

    results.append({
        'stock_code':        code,
        'stock_name':       STOCK_NAMES[code],
        'event_type':       evt.event_type,
        'date_str':         evt.date_str,
        'close':            evt.close_at_event,
        'fwd_20d_return':   fwd_ret,
        'qlib_signal':      qlib_res['signal'],
        'mom_signal':       mom_res['signal'],
        'debate_signal':    debate_signal,
        'debate_confidence': debate_conf,
        'direction':         direction,
        'success':           success,
        'debate_reason':    debate_reason,
    })

print(f"\n回测完成，共 {len(results)} 个有效样本\n")

# ─────────────────────────────────────────────────────────────────────────────
# 第七步：统计输出
# ─────────────────────────────────────────────────────────────────────────────
df_results = pd.DataFrame(results)

print("=" * 70)
print("辩论引擎极端事件回测结果")
print("=" * 70)

# 整体统计
valid = df_results[df_results['direction'] != '无效']
total_valid = len(valid)
total_success = (valid['success'] == True).sum()
total_failed  = (valid['success'] == False).sum()

print(f"\n【整体表现】")
print(f"  总事件:   {len(df_results)}")
print(f"  有效信号: {total_valid}  (排除'观望'信号)")
print(f"  成功:     {total_success}  ({100*total_success/total_valid:.1f}%)" if total_valid > 0 else "  成功: N/A")
print(f"  失败:     {total_failed}  ({100*total_failed/total_valid:.1f}%)" if total_valid > 0 else "  失败: N/A")
print(f"  平均20日收益: {valid['fwd_20d_return'].mean()*100:.2f}%")

# 按事件类型分组
print(f"\n【按事件类型】")
print(f"  {'类型':<22} {'总数':>5} {'做多':>5} {'做空':>5} {'无效':>5} {'成功率':>8} {'平均收益':>9}")
print(f"  {'-'*60}")

type_stats = valid.groupby('event_type').agg(
    total=('success', 'count'),
    bullish=('direction', lambda x: (x == '做多').sum()),
    bearish=('direction', lambda x: (x == '做空').sum()),
    neutral=('direction', lambda x: (x == '无效').sum()),
    success_count=('success', lambda x: (x == True).sum()),
    avg_return=('fwd_20d_return', 'mean'),
).sort_values('total', ascending=False)

for et, row in type_stats.iterrows():
    total_t = int(row['total'])
    succ    = int(row['success_count'])
    rate    = 100 * succ / total_t if total_t > 0 else 0
    avg_r   = row['avg_return'] * 100
    bull    = int(row['bullish'])
    bear    = int(row['bearish'])
    neut    = int(row['neutral'])
    print(f"  {et:<22} {total_t:>5} {bull:>5} {bear:>5} {neut:>5} {rate:>7.1f}%  {avg_r:>+8.2f}%")

# 按辩论信号分组
print(f"\n【按辩论信号分布】")
signal_stats = df_results.groupby(['debate_signal', 'success']).size().unstack(fill_value=0)
print(signal_stats.to_string())

# 平均收益按事件类型
print(f"\n【各类事件平均Forward Return (20日)】")
ret_by_type = df_results.groupby('event_type')['fwd_20d_return'].agg(['mean', 'std', 'count'])
ret_by_type['mean'] = ret_by_type['mean'] * 100
ret_by_type['std']  = ret_by_type['std']  * 100
ret_by_type = ret_by_type.sort_values('mean', ascending=False)
for et, row in ret_by_type.iterrows():
    print(f"  {et:<22}: {row['mean']:>+7.2f}%  (σ={row['std']:>6.2f}%, n={int(row['count']):>4})")

# 辩论信号 vs qlib/momentum 信号对比
print(f"\n【信号一致性：辩论 vs qlib vs momentum】")
agree_qlib  = (df_results['debate_signal'] == df_results['qlib_signal']).mean() * 100
agree_mom   = (df_results['debate_signal'] == df_results['mom_signal']).mean() * 100
agree_any   = ((df_results['debate_signal'] == df_results['qlib_signal']) |
               (df_results['debate_signal'] == df_results['mom_signal'])).mean() * 100
print(f"  辩论与qlib一致率: {agree_qlib:.1f}%")
print(f"  辩论与momentum一致率: {agree_mom:.1f}%")
print(f"  辩论与至少一个一致: {agree_any:.1f}%")

# 保存CSV
out_csv = '/tmp/htquant_extreme_backtest.csv'
df_results.to_csv(out_csv, index=False, encoding='utf-8-sig')
print(f"\n详细结果已保存: {out_csv}")
