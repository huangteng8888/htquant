# -*- coding: utf-8 -*-
"""
通达信(.day)数据读取器

数据格式: 每条32字节，little-endian
  [0:4]   date   (int, YYYYMMDD, e.g. 19901219)
  [4:8]   open   (int, 价格×100, e.g. 9605代表96.05元)
  [8:12]  high   (int, 价格×100)
  [12:16] low    (int, 价格×100)
  [16:20] close  (int, 价格×100)
  [20:24] amount (int, 成交额，单位元)
  [24:28] volume (int, 成交量，单位股)
  [28:32] reserved (4字节预留)

文件命名规则:
  shXXXXXX.day  — 上海主板
  szXXXXXX.day  — 深圳主板
  bjXXXXXXXX.day — 北交所

数据目录: /mnt/data/金融数据/hsjday/lday/
"""

import struct
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np
import pandas as pd

# ─── 路径配置 ────────────────────────────────────────────────────────────────

TDX_DAY_DIR = {
    'sh': Path('/mnt/data/金融数据/sh/lday/'),
    'sz': Path('/mnt/data/金融数据/sz/lday/'),
    'bj': Path('/mnt/data/金融数据/bj/lday/'),
}
TDX_DAY_DIR_ALL = Path('/mnt/data/金融数据/hsjday/lday/')  # 合并压缩包
RECORD_SIZE = 32  # 每条32字节
FORMAT = '<IIIIIIII'  # 8个uint32 little-endian

# ─── 股票代码映射 ────────────────────────────────────────────────────────────

def tdx_code_to_qlib_code(tdx_code: str) -> str:
    """
    通达信文件名前缀 → qlib/标准代码
    sh000001 → 000001 (上证指数)
    sz000001 → 000001 (000001股, 如万科)
    bj430017 → 830917 (北交所特殊处理)
    """
    if tdx_code.startswith('sh'):
        return tdx_code[2:]
    elif tdx_code.startswith('sz'):
        return tdx_code[2:]
    elif tdx_code.startswith('bj'):
        # 北交所: bjXXXXXX → 8XXXXXX (中国特色转换)
        return '8' + tdx_code[2:]
    return tdx_code


def code_to_tdx_path(code: str, market: str = None) -> Optional[Path]:
    """
    标准股票代码 → 通达信文件路径
    code:     6位标准股票代码，如 '000001', '300777'
    market:   可选，'sh'/'sz'/'bj'，如不指定则搜索所有市场
    
    返回: Path 或 None
    """
    code = code.strip()
    
    # 从 config 获取市场信息
    if market is None:
        try:
            from .config import get_qcode
            info = get_qcode(code)
            if info:
                market = info[0][:2]  # e.g. 'sz000001' → 'sz'
        except ImportError:
            pass  # 直接调用时跳过
    
    if market:
        path = TDX_DAY_DIR[market] / f'{market}{code}.day'
        if path.exists():
            return path
        return None
    
    # 搜索所有市场
    for m, dir_path in TDX_DAY_DIR.items():
        path = dir_path / f'{m}{code}.day'
        if path.exists():
            return path
    return None


def list_available_stocks() -> dict:
    """
    返回可用股票列表 {market: [codes...]}
    """
    result = {}
    for market, dir_path in TDX_DAY_DIR.items():
        if dir_path.exists():
            files = os.listdir(dir_path)
            codes = [f[len(market):-4] for f in files if f.endswith('.day') and f.startswith(market)]
            result[market] = sorted(codes)
    return result


# ─── 核心读取函数 ────────────────────────────────────────────────────────────

def read_day_file(filepath: Path) -> pd.DataFrame:
    """
    读取单个通达信.day文件，返回DataFrame

    Returns:
        columns: date, open, high, low, close, amount, volume
        index: date (datetime)
    """
    with open(filepath, 'rb') as f:
        raw = f.read()

    n = len(raw) // RECORD_SIZE
    dates, opens, highs, lows, closes, amounts, volumes = [], [], [], [], [], [], []

    for i in range(n):
        rec = struct.unpack(FORMAT, raw[i * RECORD_SIZE:(i + 1) * RECORD_SIZE])
        date_int = rec[0]
        year = date_int // 10000
        month = (date_int % 10000) // 100
        day = date_int % 100

        # 过滤无效日期
        if year < 1990 or year > 2030:
            continue

        dates.append(datetime(year, month, day))
        opens.append(rec[1] / 100.0)
        highs.append(rec[2] / 100.0)
        lows.append(rec[3] / 100.0)
        closes.append(rec[4] / 100.0)
        amounts.append(float(rec[5]))
        volumes.append(float(rec[6]))

    df = pd.DataFrame({
        '$open': opens, '$high': highs, '$low': lows,
        '$close': closes, '$volume': volumes, '$amount': amounts
    }, index=pd.DatetimeIndex(dates))
    df.index.name = 'datetime'
    return df


def get_stock_data(stock_code: str,
                   start_date: str = '1990-01-01',
                   end_date: str = '2030-01-01',
                   use_quantdb: bool = True) -> pd.DataFrame:
    """
    读取指定股票数据（带日期过滤）

    Args:
        stock_code: 6位股票代码，如 '000001'
        start_date: YYYY-MM-DD
        end_date:   YYYY-MM-DD
        use_quantdb: True=优先用 SQLite 数据库（推荐），False=直接读 .day 文件
    """
    # 优先: QuantDB 统一数据库
    if use_quantdb:
        try:
            from htquant.quantdb import QuantDB
            db = QuantDB()
            if db.quick_test():
                data = db.get_daily(stock_code, start_date, end_date)
                if data:
                    df = pd.DataFrame(data)
                    df.columns = ['$open', '$high', '$low', '$close', '$volume', '$amount', '$trade_date']
                    df = df[['trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount']]
                    df = df.rename(columns={'$trade_date': 'dates'}).set_index('dates')
                    df.index.name = None
                    return df
        except Exception:
            pass  # 回退到直接读文件

    # 回退: 直接读 .day 文件
    path = code_to_tdx_path(stock_code)
    if path is None:
        raise FileNotFoundError(f"找不到 {stock_code} 的 .day 文件")

    df = read_day_file(path)
    df = df[(df.index >= start_date) & (df.index <= end_date)]
    return df


def load_batch(stock_codes: List[str],
               start_date: str = '2020-01-01',
               end_date: str = '2025-12-31') -> Dict[str, pd.DataFrame]:
    """
    批量加载多只股票数据，返回 {code: df}
    """
    result = {}
    for code in stock_codes:
        try:
            df = get_stock_data(code, start_date, end_date)
            result[code] = df
        except FileNotFoundError:
            pass
    return result


def get_info(stock_code: str) -> dict:
    """获取股票基本信息"""
    path = code_to_tdx_path(stock_code)
    if path is None:
        return {}
    df = read_day_file(path)
    return {
        'stock_code': stock_code,
        'name': path.stem,
        'start_date': str(df.index[0].date()) if len(df) else None,
        'end_date': str(df.index[-1].date()) if len(df) else None,
        'records': len(df),
        'tdx_file': str(path),
    }


# ─── CLI 测试 ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    stocks = list_available_stocks()
    total = sum(len(v) for v in stocks.values())
    print(f"通达信数据总览:")
    print(f"  上海: {len(stocks.get('sh', []))} 只")
    print(f"  深圳: {len(stocks.get('sz', []))} 只")
    print(f"  北交所: {len(stocks.get('bj', []))} 只")
    print(f"  合计: {total} 只")
    print()

    # 从config读取测试股票
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from htquant.config import STOCK_CODE_MAPPING
    print("8只回测股票数据验证:")
    for code, (qcode, name) in STOCK_CODE_MAPPING.items():
        market = qcode[:2]
        fname = f'{market}{code}'
        try:
            df = get_stock_data(code, '2024-01-01', '2025-06-01')
            print(f"  {code}({name}): {df.index[0].date()}~{df.index[-1].date()}, {len(df)}条, 最新收盘={df['$close'].iloc[-1]:.2f}")
        except FileNotFoundError:
            print(f"  {code}({name}): 未找到")
