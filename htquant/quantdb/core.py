# -*- coding: utf-8 -*-
"""
quantdb core — 量化统一数据库核心接口

支持:
1. 批量导入通达信 .day 文件到 SQLite (PRAGMA优化，35分钟全量)
2. 查询单只/全市场日线数据
3. 极值事件检测与记录
4. adapter 信号持久化
5. 辩论结果存档

数据库路径: /mnt/data/金融数据/quantdb/quantdb.sqlite
"""

import sqlite3
import struct
import os
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

from .schema import SCHEMA_SQL, IMPORT_PRAGMAS, NORMAL_PRAGMAS

logger = logging.getLogger(__name__)

DB_PATH = Path("/mnt/data/金融数据/quantdb/quantdb.sqlite")
TXT_DAY_DIR = Path("/mnt/data/金融数据/hsjday/lday/")
MARKET_DAY_DIRS = {
    "sh": Path("/mnt/data/金融数据/sh/lday/"),
    "sz": Path("/mnt/data/金融数据/sz/lday/"),
    "bj": Path("/mnt/data/金融数据/bj/lday/"),
}

# 通达信 .day 格式: 每条32字节, little-endian, 8个uint32
# 0:日期, 1:开盘(分), 2:最高(分), 3:最低(分), 4:收盘(分), 5:成交额, 6:成交量, 7:保留
TDX_FORMAT = "<8I"
TDX_RECORD_SIZE = 32


def parse_date_int(date_int: int) -> str:
    """YYYYMMDD int → 'YYYY-MM-DD' str"""
    return f"{date_int // 10000:04d}-{(date_int % 10000) // 100:02d}-{date_int % 100:02d}"


def read_tdx_records(filepath: Path) -> List[Dict]:
    """读取单个 .day 文件, 返回 [{trade_date, open, high, low, close, volume, amount}]"""
    records = []
    with open(filepath, "rb") as f:
        while True:
            data = f.read(TDX_RECORD_SIZE)
            if len(data) < TDX_RECORD_SIZE:
                break
            fields = struct.unpack(TDX_FORMAT, data)
            date_int = fields[0]
            if date_int == 0:
                continue
            records.append({
                "trade_date": parse_date_int(date_int),
                "open":   fields[1] / 100.0,
                "high":   fields[2] / 100.0,
                "low":    fields[3] / 100.0,
                "close":  fields[4] / 100.0,
                "amount": fields[5],
                "volume": fields[6],
            })
    return records


def parse_tdx_filename(fname: str) -> Tuple[str, str]:
    """
    解析通达信文件名 → (market, code)
    支持: sh000001.day → ('sh', '000001')
         bj430017.day → ('bj', '430017')
    """
    m = re.match(r"^([a-z]{2})(\d{6})\.day$", fname, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2)
    return None, None


class QuantDB:
    """量化统一数据库"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ── 上下文管理器 ──────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self, pragmas: bool = True):
        """SQLite 连接上下文"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if pragmas:
            conn.executescript(NORMAL_PRAGMAS)
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    # ── 数据导入 ──────────────────────────────────────────────────────────────

    def import_all(self, show_progress: bool = True) -> Dict[str, Any]:
        """
        全量导入所有通达信 .day 数据到 SQLite
        使用 IMPORT_PRAGMAS 优化, 约35分钟完成全量
        """
        stats = {"stocks": 0, "records": 0, "errors": 0, "skipped": 0}

        # 首次: 设置优化 PRAGMA
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(IMPORT_PRAGMAS)
        conn.executescript(SCHEMA_SQL)
        conn.commit()

        # 收集所有 .day 文件
        all_files: List[Tuple[Path, str, str]] = []
        for market, day_dir in MARKET_DAY_DIRS.items():
            if not day_dir.exists():
                logger.warning(f"[quantdb] 目录不存在: {day_dir}")
                continue
            for fname in os.listdir(day_dir):
                m, code = parse_tdx_filename(fname)
                if m and code:
                    all_files.append((day_dir / fname, m, code))

        total = len(all_files)
        if show_progress:
            print(f"[quantdb] 发现 {total} 个 .day 文件, 开始导入...")

        for i, (fpath, market, code) in enumerate(all_files):
            if show_progress and (i % 500 == 0 or i == total - 1):
                print(f"  进度 {i+1}/{total}  ({i*100//total}%)  "
                      f"— 已导入 {stats['stocks']} 股 {stats['records']:,} 条", flush=True)

            try:
                records = read_tdx_records(fpath)
                if not records:
                    stats["skipped"] += 1
                    continue

                rows = [
                    (market, code, r["trade_date"],
                     r["open"], r["high"], r["low"], r["close"],
                     r["volume"], r["amount"])
                    for r in records
                ]

                conn.executemany(
                    """INSERT OR REPLACE INTO stock_daily
                       (market, code, trade_date, open, high, low, close, volume, amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows
                )
                conn.commit()
                stats["stocks"] += 1
                stats["records"] += len(rows)

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 5:
                    logger.error(f"[quantdb] 导入失败 {market}{code}: {e}")

        conn.close()

        # 导入完成后重建索引
        if show_progress:
            print("[quantdb] 重建索引...")
        self._rebuild_indexes()

        if show_progress:
            print(f"[quantdb] ✅ 导入完成: {stats['stocks']} 股 "
                  f"{stats['records']:,} 条记录 "
                  f"(错误 {stats['errors']}, 跳过 {stats['skipped']})")
            sz = self.db_path.stat().st_size
            print(f"[quantdb] 数据库大小: {sz/1024**3:.2f} GB")

        return stats

    def _rebuild_indexes(self):
        """重建所有索引"""
        with self._conn() as conn:
            for idx_sql in [
                "REINDEX;",
                "ANALYZE;",
            ]:
                conn.executescript(idx_sql)
            conn.commit()

    # ── 日线查询 ─────────────────────────────────────────────────────────────

    def get_daily(self,
                  code: str,
                  start_date: Optional[str] = None,
                  end_date: Optional[str] = None,
                  market: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        查询单只股票的日线数据

        Args:
            code: 6位股票代码 (不含市场前缀, 如 '000001')
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD
            market: 'sh'/'sz'/'bj', 自动推断

        Returns:
            dict with keys: dates, opens, highs, lows, closes, volumes, amounts
            or None if no data
        """
        if market is None:
            market = self._detect_market(code)

        with self._conn() as conn:
            date_cond, date_params = self._date_filter(start_date, end_date)
            rows = conn.execute(
                f"""SELECT trade_date, open, high, low, close, volume, amount
                    FROM stock_daily
                    WHERE market=? AND code=? AND {date_cond}
                    ORDER BY trade_date""",
                (market, code) + tuple(date_params)
            ).fetchall()

        if not rows:
            return None

        return {
            "dates":  [r["trade_date"] for r in rows],
            "opens":  [r["open"]       for r in rows],
            "highs":  [r["high"]       for r in rows],
            "lows":   [r["low"]        for r in rows],
            "closes": [r["close"]      for r in rows],
            "volumes":[r["volume"]     for r in rows],
            "amounts":[r["amount"]    for r in rows],
        }

    def get_daily_df(self,
                     code: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     market: Optional[str] = None):
        """返回 pandas DataFrame"""
        import pandas as pd
        if market is None:
            market = self._detect_market(code)
        with self._conn() as conn:
            date_cond, date_params = self._date_filter(start_date, end_date)
            df = pd.read_sql(
                f"""SELECT trade_date as datetime, open, high, low, close, volume, amount
                    FROM stock_daily
                    WHERE market=? AND code=? AND {date_cond}
                    ORDER BY trade_date""",
                conn,
                params=(market, code) + tuple(date_params),
                index_col="datetime",
                parse_dates=["datetime"],
            )
        return df

    def get_market_snapshot(self, trade_date: str) -> Dict[str, Dict]:
        """
        获取某日全市场快照

        Returns:
            { 'sh000001': {'name': ..., 'close': ...}, ... }
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT market || code as symbol, close, volume, amount
                   FROM stock_daily
                   WHERE trade_date = ?
                   ORDER BY market, code""",
                (trade_date,)
            ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    # 极值事件检测参数
    THRESHOLD_PCT = 0.01  # 1% 容差：|当日高低-窗口高低| / close < 1% 即算触及
    FWD_5  = 5
    FWD_20 = 20
    FWD_60 = 60

    def _detect_events_for_stock(self, code: str, market: str) -> List[Dict]:
        """检测单只股票的全部极值事件"""
        data = self.get_daily(code, market=market)
        if not data or len(data["closes"]) < 260:
            return []

        dates   = data["dates"]
        closes  = data["closes"]
        highs   = data["highs"]
        lows    = data["lows"]
        volumes = data["volumes"]
        n = len(closes)

        events = []
        for wi, window in enumerate([20, 50, 100, 252]):
            if n <= window + self.FWD_60:
                continue

            for idx in range(window, n - self.FWD_60):
                close = closes[idx]
                high  = highs[idx]
                low   = lows[idx]
                date  = dates[idx]

                window_high = max(highs[idx-window:idx])
                window_low  = min(lows[idx-window:idx])

                # ── 触及判断（1%容差阈值）────────────────────────────
                high_touch = (abs(high - window_high) / close < self.THRESHOLD_PCT
                              if window_high > 0 else False)
                low_touch  = (abs(low  - window_low)  / close < self.THRESHOLD_PCT
                              if window_low  > 0 else False)
                high_break = high > window_high
                low_break  = low  < window_low

                # ── 前瞻收益 ────────────────────────────────────────
                f5  = (closes[idx+self.FWD_5]  - close) / close if idx+self.FWD_5  <= n-1 else None
                f20 = (closes[idx+self.FWD_20] - close) / close if idx+self.FWD_20 <= n-1 else None
                f60 = (closes[idx+self.FWD_60] - close) / close if idx+self.FWD_60 <= n-1 else None

                # ── RSI(14) ────────────────────────────────────────
                gains  = [max(0, closes[j]-closes[j-1]) for j in range(max(1, idx-14), idx+1)]
                losses = [max(0, closes[j-1]-closes[j]) for j in range(max(1, idx-14), idx+1)]
                avg_gain = sum(gains) / 14 if gains else 0.0
                avg_loss = sum(losses) / 14 if losses else 0.0
                rsi = 100 - (100 / (1 + avg_gain/avg_loss)) if avg_loss else 100.0

                avg_vol = sum(volumes[idx-window:idx]) / window if window > 0 else 1
                vol_ratio = volumes[idx] / avg_vol if avg_vol else 1.0

                # ── 事件分类 ────────────────────────────────────────
                event_type = None
                if   window == 252 and high_break: event_type = 'W252_HIGH_BREAK'
                elif window == 252 and low_break:  event_type = 'W252_LOW_BREAK'
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

                if event_type:
                    events.append({
                        "market": market, "code": code, "date": date,
                        "event_type": event_type,
                        "window_high": window_high, "window_low": window_low,
                        "close": close,
                        "f5": f5, "f20": f20, "f60": f60,
                        "rsi": rsi, "vol_ratio": vol_ratio,
                    })

        return events

    def detect_extreme_events(self,
                             codes: Optional[List[str]] = None,
                             show_progress: bool = True) -> int:
        """
        扫描全市场极值事件并写入 extreme_events 表

        事件类型:
          W{20,50,100,252}_{LOW,HIGH}_{TOUCH,BREAK}
        触及判断: |当日高/低价 - 窗口高/低价| / close < 1%
        写入: 每股票批量 INSERT OR REPLACE (高效)
        """
        if codes is None:
            codes = self.list_codes()

        total = len(codes)
        count = 0
        BATCH = 5000  # 每5000条写入一次

        # 批量缓冲区
        batch = []

        for i, code in enumerate(codes):
            if show_progress and (i % 200 == 0 or i == total - 1):
                print(f"  极值扫描 {i+1}/{total} ({i*100//total}%)  已写入 {count} 条", flush=True)

            market = self._detect_market(code)
            events = self._detect_events_for_stock(code, market)

            for ev in events:
                batch.append((
                    ev["market"], ev["code"], ev["date"], ev["event_type"],
                    ev["window_high"], ev["window_low"], ev["close"],
                    ev["f5"], ev["f20"], ev["f60"],
                    round(ev["rsi"], 2), round(ev["vol_ratio"], 2)
                ))

                if len(batch) >= BATCH:
                    count += self._batch_insert_events(batch)
                    batch = []

        # 剩余记录
        if batch:
            count += self._batch_insert_events(batch)
            batch = []

        if show_progress:
            print(f"[quantdb] 极值事件检测完成: {count} 条")
        return count

    def _batch_insert_events(self, batch: List[tuple]) -> int:
        """批量插入极值事件（INSERT OR IGNORE 自动去重）"""
        if not batch:
            return 0
        with self._conn() as conn:
            # 记录插入前的行数
            before = conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
            conn.executemany(
                """INSERT OR IGNORE INTO extreme_events
                   (market, code, trade_date, event_type,
                    window_high, window_low, close_at_event,
                    forward_5d_ret, forward_20d_ret, forward_60d_ret,
                    rsi14_at_event, volume_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                batch
            )
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
            return after - before

    def _upsert_event(self, market, code, date, event_type,
                      wh, wl, close, f5, f20, f60, rsi, vol_ratio) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO extreme_events
                   (market, code, trade_date, event_type, window_high, window_low,
                    close_at_event, forward_5d_ret, forward_20d_ret, forward_60d_ret,
                    rsi14_at_event, volume_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (market, code, date, event_type, wh, wl, close,
                 f5, f20, f60, round(rsi,2), round(vol_ratio,2))
            )
            conn.commit()
            return cur.rowcount

    # ── 信号记录 ─────────────────────────────────────────────────────────────

    def log_signal(self, adapter: str, code: str, date: str,
                   signal: str, confidence: float = 0.5,
                   reason: str = "", extreme_event: str = None,
                   rsi: float = None, price: float = None,
                   market: str = None) -> None:
        if market is None:
            market = self._detect_market(code)
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO adapter_signals
                   (market, code, trade_date, adapter_name, signal, confidence,
                    reason, extreme_event, rsi14, price_at_signal)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (market, code, date, adapter, signal, confidence,
                 reason, extreme_event, rsi, price)
            )
            conn.commit()

    def get_signals(self, code: str, date: str = None,
                    adapter: str = None) -> List[Dict]:
        with self._conn() as conn:
            q = """SELECT * FROM adapter_signals
                   WHERE market=? AND code=?"""
            params = [self._detect_market(code), code]
            if date:
                q += " AND trade_date=?"
                params.append(date)
            if adapter:
                q += " AND adapter_name=?"
                params.append(adapter)
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # ── 辩论结果 ─────────────────────────────────────────────────────────────

    def log_debate(self, code: str, date: str, signal: str,
                   conviction: float, participants: Dict[str, str],
                   extreme_event: str = None, forward_5: float = None,
                   forward_20: float = None, forward_60: float = None) -> None:
        import json
        market = self._detect_market(code)
        is_correct = None
        if forward_20 is not None:
            if signal in ("减持","清仓") and forward_20 < 0:
                is_correct = 1
            elif signal in ("增持","买入") and forward_20 > 0:
                is_correct = 1
            elif signal in ("减持","清仓") and forward_20 > 0:
                is_correct = 0
            elif signal in ("增持","买入") and forward_20 < 0:
                is_correct = 0

        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO debate_results
                   (market, code, trade_date, debate_signal, conviction,
                    participant_signals, extreme_event,
                    forward_5d_ret, forward_20d_ret, forward_60d_ret, is_correct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (market, code, date, signal, conviction,
                 json.dumps(participants), extreme_event,
                 forward_5, forward_20, forward_60, is_correct)
            )
            conn.commit()

    # ── 工具 ────────────────────────────────────────────────────────────────

    def list_codes(self, market: str = None) -> List[str]:
        """
        返回股票代码列表（不含市场前缀，如 '000001', '430017'）

        market=None 时返回所有市场的代码（可能重复，同一代码在sh和sz各有一条）
        market='sh'/'sz'/'bj' 时只返回该市场
        """
        with self._conn() as conn:
            if market:
                rows = conn.execute(
                    "SELECT DISTINCT code FROM stock_daily WHERE market=? ORDER BY code",
                    (market,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT code FROM stock_daily ORDER BY code"
                ).fetchall()
        return [r[0] for r in rows]

    def _detect_market(self, code: str) -> str:
        """
        从 stock_daily 表查询该 code 实际属于哪个市场

        某些代码在两个市场都有记录（如 000001:
        sh=上证指数, sz=平安银行），返回记录数更多的市场
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT market FROM stock_daily
                   WHERE code=?
                   GROUP BY market
                   ORDER BY COUNT(*) DESC
                   LIMIT 1""",
                (code,)
            ).fetchone()
        return row[0] if row else 'sh'  # fallback

    def _date_filter(self, start, end) -> Tuple[str, list]:
        """返回 (WHERE子句, [参数列表])"""
        clauses, params = [], []
        if start:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end:
            clauses.append("trade_date <= ?")
            params.append(end)
        return (" AND ".join(clauses), params) if clauses else ("1=1", [])

    def stats(self) -> Dict[str, Any]:
        """返回数据库统计信息"""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
            stocks = conn.execute(
                "SELECT COUNT(DISTINCT market||code) FROM stock_daily"
            ).fetchone()[0]
            min_d  = conn.execute("SELECT MIN(trade_date) FROM stock_daily").fetchone()[0]
            max_d  = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()[0]
            events = conn.execute(
                "SELECT COUNT(*) FROM extreme_events"
            ).fetchone()[0]
            signals= conn.execute(
                "SELECT COUNT(*) FROM adapter_signals"
            ).fetchone()[0]
            sz = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_records": total,
            "total_stocks":  stocks,
            "date_range":    f"{min_d} ~ {max_d}",
            "db_size_gb":    round(sz/1024**3, 2),
            "extreme_events": events,
            "signals":        signals,
        }

    def quick_test(self) -> bool:
        """快速测试数据库是否可用"""
        try:
            with self._conn() as conn:
                r = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()
                logger.info(f"[quantdb] 可用: {r[0]:,} 条记录")
            return True
        except Exception as e:
            logger.error(f"[quantdb] 测试失败: {e}")
            return False


if __name__ == "__main__":
    import sys
    db = QuantDB()
    print("=== quantdb 统计 ===")
    s = db.stats()
    for k, v in s.items():
        print(f"  {k}: {v}")
