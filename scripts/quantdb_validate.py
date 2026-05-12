#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quantdb_validate.py — QuantDB 数据质量验证模块

分层验证策略:
  Level 1 (每次入库后自动执行): 轻量级合法性 + 增量对比
  Level 2 (每日/每周定时): 全量 OHLC + 覆盖率 + 跳变检测
  Level 3 (按需/周级): 历史样本与 TDX 文件交叉验证

用法:
  python3 scripts/quantdb_validate.py --level 1           # 增量验证
  python3 scripts/quantdb_validate.py --level 2           # 全面验证
  python3 scripts/quantdb_validate.py --level 3           # 深度验证(含文件对比)
  python3 scripts/quantdb_validate.py --stock 000001       # 单股诊断
  python3 scripts/quantdb_validate.py --recent 5           # 最近N交易日报告
  python3 scripts/quantdb_validate.py --analyze-gaps        # 全量gap分析
"""

import argparse
import sqlite3
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── 路径配置 ──────────────────────────────────────────────────────────────────
DB_PATH  = Path('/mnt/data/金融数据/quantdb/quantdb.sqlite')
TDX_BASE = Path('/mnt/data/金融数据/hsjday/lday/')

# ── 告警阈值 ─────────────────────────────────────────────────────────────────
THRESHOLD_PRICE_JUMP_PCT   = 50.0    # 单日价格跳变告警 (%)
THRESHOLD_COVERAGE_DROP_PCT= 5.0     # 覆盖率异常下降告警 (%)
THRESHOLD_OHLC_ERROR_PCT   = 0.1     # OHLC非法率告警 (%)
THRESHOLD_MISSING_DAYS      = 5      # 连续缺失交易日告警

# 近年涨跌停限制（A股）
PRICE_LIMIT = {
    'ST': 0.05,     # ST股票 ±5%
    'NORMAL': 0.10, # 普通股票 ±10%（2020/08后创业板±20%）
    'BIOTECH': 0.20,# 科创板/创业板 ±20%
}


# ─────────────────────────────────────────────────────────────────────────────
# 辅助: TDX 文件解析
# ─────────────────────────────────────────────────────────────────────────────

def tdx_filename_to_code(fn: str) -> Tuple[str, str]:
    name = fn.replace('.day', '')
    if name.startswith('sh'): return 'sh', name[2:]
    if name.startswith('sz'): return 'sz', name[2:]
    if name.startswith('bj'): return 'bj', name[2:]
    return 'sh', name


def read_tdx_last_n(fp: Path, n: int = 30) -> List[dict]:
    """高效读取 TDX 文件最近 N 条记录（无需全文件读）"""
    try:
        with open(fp, 'rb') as f:
            f.seek(0, 2)
            file_size = f.tell()
            # 计算需要读多少字节（最多 n 条 + 可能的不完整末尾）
            bytes_to_read = min(n * 32 + 32, file_size)
            f.seek(file_size - bytes_to_read)
            data = f.read()
    except Exception:
        return []

    records = []
    for i in range(0, len(data) - 31, 32):
        rec = _parse(data[i:i+32])
        if rec:
            records.append(rec)

    # 取最后 n 条（保留最后 n 条）
    return records[-n:] if len(records) > n else records


def _parse(data: bytes) -> Optional[dict]:
    if len(data) < 32:
        return None
    try:
        dt = struct.unpack('<I', data[0:4])[0]
        open_  = struct.unpack('<I', data[4:8])[0]  / 100.0
        high   = struct.unpack('<I', data[8:12])[0] / 100.0
        low    = struct.unpack('<I', data[12:16])[0] / 100.0
        close  = struct.unpack('<I', data[16:20])[0] / 100.0
        amount = struct.unpack('<q', data[20:28])[0]
        vol    = struct.unpack('<I', data[28:32])[0]
        dt_val = struct.unpack('<I', data[0:4])[0]
        year, ym = divmod(dt_val, 10000)
        month, day = divmod(ym, 100)
        if year < 1990 or year > 2100:
            return None
        date_str = f'{year:04d}-{month:02d}-{day:02d}'
        return {'date': date_str, 'open': open_, 'high': high,
                'low': low, 'close': close, 'volume': vol, 'amount': amount}
    except Exception:
        return None


def read_tdx_all(fp: Path) -> List[dict]:
    """读取 TDX 文件全部记录"""
    try:
        with open(fp, 'rb') as f:
            data = f.read()
    except Exception:
        return []
    records = []
    for i in range(0, len(data) - 31, 32):
        rec = _parse(data[i:i+32])
        if rec:
            records.append(rec)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Level 1: 增量验证（每次入库后自动调用）
# ─────────────────────────────────────────────────────────────────────────────

def validate_incremental(db_path: Path, new_records: int = 0,
                         verbose: bool = True) -> Dict:
    """
    Level 1 增量验证：轻量级，适合每次入库后自动调用
    验证内容：
      1. 最新交易日覆盖率（vs 上一个交易日）
      2. 新记录数合理性
      3. OHLC 合法性（最新 N 条）
      4. 与 TDX 文件交叉抽检（最新 5 股）
    """
    results = {'level': 1, 'passed': True, 'alerts': [], 'warnings': []}

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # ── 1. 最新交易日覆盖率对比 ──────────────────────────────────────────────
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily"
    ).fetchone()[0]
    prev   = conn.execute("""
        SELECT MAX(trade_date) FROM stock_daily
        WHERE trade_date < ?
    """, (latest,)).fetchone()[0]

    latest_stocks = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM stock_daily WHERE trade_date=?",
        (latest,)
    ).fetchone()[0]
    prev_stocks   = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM stock_daily WHERE trade_date=?",
        (prev,)
    ).fetchone()[0] if prev else 0

    cov_drop = (prev_stocks - latest_stocks) / max(prev_stocks, 1) * 100
    results['latest_date']    = latest
    results['latest_stocks'] = latest_stocks
    results['prev_stocks']    = prev_stocks
    results['coverage_drop'] = round(cov_drop, 2)

    if cov_drop > THRESHOLD_COVERAGE_DROP_PCT:
        results['alerts'].append(
            f"覆盖率异常: {prev}({prev_stocks}只) → {latest}({latest_stocks}只) "
            f"下降{cov_drop:.1f}%"
        )
        results['passed'] = False

    if verbose:
        print(f"[L1] 最新交易日: {latest}, {latest_stocks} 只有数据 "
              f"(vs {prev}: {prev_stocks}只, 变化{latest_stocks-prev_stocks:+d})")

    # ── 2. 新记录数合理性 ──────────────────────────────────────────────────
    if new_records > 0:
        # 正常每个交易日约 9500 条新记录（停牌/退市约 2500 只无更新）
        expected = 9000
        if new_records < expected * 0.5:
            results['warnings'].append(
                f"新记录数偏少: {new_records} 条 (预期>{expected*0.5:.0f})"
            )
        elif new_records > expected * 1.5:
            results['warnings'].append(
                f"新记录数偏多: {new_records} 条 (预期<{expected*1.5:.0f})，需人工确认"
            )
        if verbose:
            print(f"[L1] 新增记录: {new_records:,} 条")

    # ── 3. OHLC 合法性（最新 2000 条）──────────────────────────────────────
    bad_ohlc = conn.execute("""
        SELECT COUNT(*) FROM stock_daily
        WHERE trade_date=? AND open>0 AND high>0 AND low>0 AND close>0
          AND high < low
    """, (latest,)).fetchone()[0]

    bad_rate = bad_ohlc / max(latest_stocks, 1) * 100
    results['ohlc_errors_latest'] = bad_ohlc
    results['ohlc_error_rate']    = round(bad_rate, 3)

    if bad_ohlc > 0 and verbose:
        print(f"[L1] OHLC非法(high<low): {bad_ohlc} 条 ({bad_rate:.2f}%)")

    if bad_rate > THRESHOLD_OHLC_ERROR_PCT:
        results['alerts'].append(
            f"OHLC非法率过高: {bad_ohlc}条 ({bad_rate:.2f}%)"
        )
        results['passed'] = False

    # ── 4. TDX 文件交叉抽检（随机 5 股）─────────────────────────────────────
    samples = conn.execute("""
        SELECT market, code FROM stock_daily
        WHERE trade_date=?
        ORDER BY RANDOM() LIMIT 5
    """, (latest,)).fetchall()

    tdx_mismatches = []
    for mkt, code in samples:
        fp = TDX_BASE / f'{mkt}{code}.day'
        if not fp.exists():
            continue
        tdx_recs = read_tdx_last_n(fp, 1)
        if not tdx_recs:
            continue
        db_rec = conn.execute("""
            SELECT open, high, low, close
            FROM stock_daily
            WHERE market=? AND code=? AND trade_date=?
        """, (mkt, code, latest)).fetchone()
        if not db_rec:
            continue
        tdx = tdx_recs[0]
        # 允许 ±0.01 的 TDX 解析误差
        for field, db_val, tdx_val in [
            ('close', db_rec[3], tdx['close']),
            ('high',  db_rec[1], tdx['high']),
            ('low',   db_rec[2], tdx['low']),
        ]:
            if abs(db_val - tdx_val) > 0.02:
                tdx_mismatches.append(
                    f"{mkt}{code} {field}: DB={db_val} TDX={tdx_val}"
                )

    if tdx_mismatches:
        results['warnings'].append(f"TDX交叉抽检: {'; '.join(tdx_mismatches)}")
        if verbose:
            for mm in tdx_mismatches:
                print(f"[L1] ⚠️ {mm}")

    conn.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Level 2: 全面验证（每日/每周定时调用）
# ─────────────────────────────────────────────────────────────────────────────

def validate_full(db_path: Path, days: int = 30, verbose: bool = True) -> Dict:
    """
    Level 2 全面验证：覆盖 OHLC、跳变、缺失、极值事件统计
    """
    results = {'level': 2, 'passed': True, 'alerts': [], 'warnings': [],
               'price_jumps': [], 'coverage': {}, 'issues': []}

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    latest = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()[0]
    start  = (datetime.strptime(latest, '%Y-%m-%d') -
               timedelta(days=days)).strftime('%Y-%m-%d')

    if verbose:
        print(f"[L2] 验证区间: {start} ~ {latest} (最近{days}天)")

    # ── 1. OHLC 全量检查 ────────────────────────────────────────────────────
    bad_ohlc = conn.execute("""
        SELECT market, code, trade_date, open, high, low, close
        FROM stock_daily
        WHERE trade_date >= ? AND open>0 AND high>0 AND low>0 AND close>0
          AND (high < low OR close > high OR close < low)
        LIMIT 20
    """, (start,)).fetchall()

    bad_ohlc_cnt = conn.execute("""
        SELECT COUNT(*) FROM stock_daily
        WHERE trade_date >= ? AND open>0 AND high>0 AND low>0 AND close>0
          AND (high < low OR close > high OR close < low)
    """, (start,)).fetchone()[0]

    results['ohlc_errors'] = bad_ohlc_cnt
    if verbose:
        print(f"[L2] OHLC非法(含high<low/close>high/close<low): {bad_ohlc_cnt} 条")

    # 区分历史异常（早期1990s无涨跌停）和近年异常
    recent_bad = [(m, c, d, o, h, l, cl) for m, c, d, o, h, l, cl in bad_ohlc
                  if d >= '2000-01-01']
    if recent_bad:
        results['alerts'].append(
            f"近年OHLC非法: {len(recent_bad)} 条（含1990s早期市场除外）"
        )
        for b in recent_bad[:3]:
            results['issues'].append(f"  {b[0]}{b[1]} {b[2]}: O={b[3]} H={b[4]} L={b[5]} C={b[6]}")

    # ── 2. 日覆盖率统计 ─────────────────────────────────────────────────────
    cov = conn.execute("""
        SELECT trade_date, COUNT(DISTINCT code) as stocks
        FROM stock_daily
        WHERE trade_date >= ?
        GROUP BY trade_date
        ORDER BY trade_date DESC
    """, (start,)).fetchall()

    total_distinct = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM stock_daily"
    ).fetchone()[0]
    results['total_registered_stocks'] = total_distinct

    cov_list = [(d, c, round(c/total_distinct*100, 1))
                for d, c in cov]
    results['coverage'] = {d: {'stocks': c, 'pct': p} for d, c, p in cov_list}

    low_cov = [d for d, c, p in cov_list if p < 70]
    if low_cov:
        results['warnings'].append(f"低覆盖率(<70%): {low_cov[:5]}")
        if verbose:
            print(f"[L2] 低覆盖率(<70%): {low_cov[:5]}")

    if verbose:
        print(f"[L2] 日均覆盖: {sum(c for _,c,_ in cov_list)/max(len(cov_list),1):.0f} 只 "
              f"({sum(p for _,_,p in cov_list)/max(len(cov_list),1):.1f}%)")

    # ── 3. 价格跳变检测 ─────────────────────────────────────────────────────
    jumps = conn.execute("""
        SELECT a.market, a.code, a.trade_date, a.close, b.close,
               ROUND((a.close - b.close) / NULLIF(b.close,0) * 100, 2) as pct
        FROM stock_daily a
        JOIN stock_daily b ON a.market=b.market AND a.code=b.code
            AND b.trade_date = date(a.trade_date, '-1 day')
        WHERE a.trade_date >= ?
          AND b.close > 0.1
          AND ABS((a.close - b.close) / b.close) > ?
        ORDER BY ABS((a.close - b.close) / b.close) DESC
        LIMIT 10
    """, (start, THRESHOLD_PRICE_JUMP_PCT / 100)).fetchall()

    results['price_jumps'] = [f"{m}{c} {d}: {p4:.2f}→{p3:.2f} ({p5:+.1f}%)"
                              for m, c, d, p3, p4, p5 in jumps]
    if verbose and jumps:
        print(f"[L2] 价格跳变(>{THRESHOLD_PRICE_JUMP_PCT}%):")
        for m, c, d, old, new, pct in jumps[:5]:
            print(f"      {m}{c} {d}: {old:.2f}→{new:.2f} ({pct:+.1f}%)")

    # 排除ETF（允许大跳变：配股/分拆/合并）
    equity_jumps = [(m,c,d,o,n,p) for m,c,d,o,n,p in jumps
                    if not c.startswith('88')]
    if equity_jumps:
        results['warnings'].append(
            f"普通股票价格跳变>{THRESHOLD_PRICE_JUMP_PCT}%: {len(equity_jumps)}条"
        )
        results['passed'] = False

    # ── 4. 极值事件统计合理性 ──────────────────────────────────────────────
    ev_stats = conn.execute("""
        SELECT event_type, COUNT(*) as cnt
        FROM extreme_events
        WHERE trade_date >= ?
        GROUP BY event_type
        ORDER BY cnt DESC
    """, (start,)).fetchall()

    if verbose:
        print(f"[L2] 极值事件(最近{days}天):")
        total_ev = 0
        for t, c in ev_stats:
            total_ev += c
            if c > 1000:
                print(f"      {t}: {c:,}")
        print(f"      合计: {total_ev:,}")

    results['extreme_events'] = {t: c for t, c in ev_stats}

    # ── 5. 连续休市检测（防交易日数据断链）────────────────────────────────
    all_dates = sorted([d for d, in conn.execute(
        "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date>=? ORDER BY trade_date",
        (start,)).fetchall()])

    gaps = []
    for i in range(1, len(all_dates)):
        d1 = datetime.strptime(all_dates[i-1], '%Y-%m-%d')
        d2 = datetime.strptime(all_dates[i],   '%Y-%m-%d')
        gap = (d2 - d1).days
        if 2 <= gap <= 7:  # 工作日gap为1，周末gap=2-3
            # 如果gap>=4 说明有连续休市（节假日），可接受
            if gap >= 4:
                gaps.append(f"{all_dates[i-1]}→{all_dates[i]} ({gap}天, 节假日)")

    if gaps and verbose:
        print(f"[L2] 非连续交易日:")
        for g in gaps[:5]:
            print(f"      {g}")

    conn.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Level 3: 深度验证（与 TDX 文件逐股对比）
# ─────────────────────────────────────────────────────────────────────────────

def validate_vs_tdx(db_path: Path, stock: str = None,
                    limit: int = 50, verbose: bool = True) -> Dict:
    """
    Level 3 深度验证：与 TDX 文件逐字节对比
    用于：
      - 发现导入漏录/错录
      - 验证指定股票的数据完整性
      - 排查异常股票
    """
    results = {'level': 3, 'stock': stock, 'mismatches': [], 'missing': [],
               'extra': [], 'alerts': []}

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # 确定要检查的股票列表
    if stock:
        # 解析市场前缀
        if stock.startswith(('sh', 'sz', 'bj')):
            mkt, code = stock[:2], stock[2:]
        else:
            # 尝试从DB查找
            row = conn.execute(
                "SELECT market, code FROM stock_daily WHERE code=? LIMIT 1",
                (stock,)
            ).fetchone()
            if row:
                mkt, code = row
            else:
                print(f"未找到股票: {stock}")
                return results
        stock_filter = [(mkt, code)]
    else:
        # 随机抽样
        stock_filter = conn.execute("""
            SELECT market, code FROM stock_daily
            WHERE trade_date >= '2026-01-01'
            GROUP BY market, code
            ORDER BY RANDOM()
            LIMIT ?
        """, (limit,)).fetchall()

    total_compared = 0
    total_mismatch = 0

    for mkt, code in stock_filter:
        fp = TDX_BASE / f'{mkt}{code}.day'
        if not fp.exists():
            if verbose:
                print(f"  [L3] 文件不存在: {mkt}{code}")
            continue

        tdx_recs = read_tdx_all(fp)
        if not tdx_recs:
            continue

        db_recs = {
            r[0]: r for r in conn.execute("""
                SELECT trade_date, open, high, low, close, volume
                FROM stock_daily
                WHERE market=? AND code=?
            """, (mkt, code)).fetchall()
        }

        mismatches = []
        for tdx in tdx_recs:
            d = tdx['date']
            db = db_recs.get(d)
            if db is None:
                results['missing'].append(f"{mkt}{code} {d}: TDX有但DB无")
                continue
            # 允许 ±0.01 误差（TDX解析问题）
            for field, tdx_val, db_idx in [
                ('close', tdx['close'], 4),
                ('high',  tdx['high'],  2),
                ('low',   tdx['low'],   3),
                ('open',  tdx['open'],  1),
                ('volume',tdx['volume'],5),
            ]:
                if abs(tdx_val - db[db_idx]) > 0.02 and field != 'volume':
                    mismatches.append(
                        f"{d} {field}: TDX={tdx_val} DB={db[db_idx]}"
                    )
        if mismatches:
            results['mismatches'].append((f"{mkt}{code}", mismatches))
            total_mismatch += len(mismatches)

        total_compared += len(tdx_recs)

    if verbose:
        print(f"[L3] 对比完成: {total_compared} 条TDX记录")
        if results['mismatches']:
            print(f"     不一致: {len(results['mismatches'])} 只股票")
            for code, mms in results['mismatches'][:5]:
                print(f"     {code}:")
                for mm in mms[:3]:
                    print(f"       {mm}")
        if results['missing']:
            print(f"     DB缺失: {len(results['missing'])} 条")
        if not results['mismatches'] and not results['missing']:
            print(f"     ✅ 完全一致")

    results['total_compared'] = total_compared
    results['total_mismatch'] = total_mismatch

    if total_mismatch > 0:
        results['alerts'].append(
            f"{total_mismatch} 条不一致 (占{total_mismatch/max(total_compared,1)*100:.2f}%)"
        )

    conn.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Gap 分析（全量）
# ─────────────────────────────────────────────────────────────────────────────

def analyze_coverage_gaps(db_path: Path, verbose: bool = True) -> Dict:
    """
    分析全量数据的覆盖率 gap：
      - 哪些股票历史有数据但近期无数据（退市/停牌）
      - 哪些股票有记录断层
    """
    results = {'active': [], 'inactive': [], 'gaps': []}

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    latest = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()[0]

    # 统计每只股票的首末日期和记录数
    stock_span = conn.execute("""
        SELECT market, code,
               MIN(trade_date) as first_date,
               MAX(trade_date) as last_date,
               COUNT(*) as cnt
        FROM stock_daily
        GROUP BY market, code
    """).fetchall()

    active_thresh = (datetime.strptime(latest, '%Y-%m-%d') -
                     timedelta(days=30)).strftime('%Y-%m-%d')

    for mkt, code, first, last, cnt in stock_span:
        if last >= active_thresh:
            results['active'].append((mkt, code, first, last, cnt))
        else:
            results['inactive'].append((mkt, code, first, last, cnt,
                                        last + '无数据'))

    results['active_count'] = len(results['active'])
    results['inactive_count'] = len(results['inactive'])

    if verbose:
        print(f"[Gap] 有数据股票: {results['active_count']}")
        print(f"[Gap] 近期无数据: {results['inactive_count']} 只")
        print(f"[Gap] 最新日期:   {latest}")
        if results['inactive']:
            # 按无数据时长排序
            inactive_sorted = sorted(
                results['inactive'],
                key=lambda x: x[3]
            )
            print(f"[Gap] 长期无数据股票 (末日期 < {active_thresh}):")
            for mkt, code, first, last, cnt, _ in inactive_sorted[-10:]:
                days_since = (datetime.strptime(latest, '%Y-%m-%d') -
                             datetime.strptime(last, '%Y-%m-%d')).days
                print(f"      {mkt}{code}: {first}~{last} ({cnt}条, "
                      f"距今{days_since}天无数据)")
            results['inactive_sample'] = inactive_sorted[-10:]

    conn.close()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 汇总报告
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: Dict, level: int):
    """打印验证报告摘要"""
    print(f"\n{'='*60}")
    print(f"QuantDB 数据质量验证报告  [{datetime.now():%Y-%m-%d %H:%M}]  Level-{level}")
    print(f"{'='*60}")

    status = "✅ 通过" if results.get('passed', True) else "❌ 失败"
    print(f"状态: {status}")

    alerts = results.get('alerts', [])
    warnings = results.get('warnings', [])

    if alerts:
        print(f"\n🔴 告警 ({len(alerts)} 项):")
        for a in alerts:
            print(f"   • {a}")

    if warnings:
        print(f"\n🟡 警告 ({len(warnings)} 项):")
        for w in warnings:
            print(f"   • {w}")

    if not alerts and not warnings:
        print(f"\n✅ 无异常")

    print(f"\n关键指标:")
    for k, v in results.items():
        if k in ('alerts', 'warnings', 'issues', 'passed', 'level'):
            continue
        if isinstance(v, dict):
            continue
        if isinstance(v, list) and len(v) > 3:
            continue
        print(f"   {k}: {v}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='QuantDB 数据质量验证')
    parser.add_argument('--level',    type=int, default=1,
                        help='验证级别 (1=增量, 2=全面, 3=深度)')
    parser.add_argument('--days',     type=int, default=30,
                        help='验证天数 (level 2, 默认30)')
    parser.add_argument('--stock',    type=str, default=None,
                        help='指定股票代码 (如 sh000001 或 000001)')
    parser.add_argument('--limit',    type=int, default=50,
                        help='抽样数量 (level 3, 默认50)')
    parser.add_argument('--analyze-gaps', action='store_true',
                        help='全量gap分析')
    parser.add_argument('--recent',   type=int, default=0,
                        help='最近N个交易日覆盖率报告')
    args = parser.parse_args()

    if args.analyze_gaps:
        print("\n[Gap Analysis] 全量覆盖率分析...")
        r = analyze_coverage_gaps(DB_PATH)
        print_summary(r, 0)
        return

    if args.recent > 0:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        latest = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily"
        ).fetchone()[0]
        cov = conn.execute("""
            SELECT trade_date, COUNT(*) as cnt,
                   COUNT(DISTINCT code) as stocks
            FROM stock_daily
            WHERE trade_date >= date(?, '-{} days')
            GROUP BY trade_date
            ORDER BY trade_date DESC
        """.format(args.recent), (latest,)).fetchall()
        conn.close()
        print(f"\n最近 {args.recent} 个交易日覆盖率:")
        for d, cnt, stocks in cov:
            print(f"  {d}: {stocks:>5} 只 / {cnt:>6} 条")
        return

    if args.level == 1:
        r = validate_incremental(DB_PATH, verbose=True)
    elif args.level == 2:
        r = validate_full(DB_PATH, days=args.days, verbose=True)
    elif args.level == 3:
        r = validate_vs_tdx(DB_PATH, stock=args.stock,
                            limit=args.limit, verbose=True)
    else:
        print("无效 --level (1/2/3)")
        return

    print_summary(r, args.level)


if __name__ == '__main__':
    main()
