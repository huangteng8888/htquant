#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
populate_stock_info.py — 从 baostock 拉取股票基础信息并写入 QuantDB

设计原则:
  - 每批次(100只)写入DB一次，避免最后才写导致全程无反馈
  - 每批次后立即打印进度，已写数据不会丢失
  - 任意中断后可重新运行，自动从 stock_info 最大ID继续

stock_info 表结构:
  (market, code) → 当前基本信息(含类型/行业/上市日期)
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import baostock as bs

# ── 路径配置 ─────────────────────────────────────────────────────────────────
DB_PATH = Path('/mnt/data/金融数据/quantdb/quantdb.sqlite')
SCRIPT_DIR = Path(__file__).parent

# ── 常量映射 ─────────────────────────────────────────────────────────────────
BS_TYPE_MAP = {
    '1':  'A股',
    '2':  'B股',
    '3':  '债券',
    '4':  '指数',
    '5':  'ETF',
    '6':  '期货',
    '7':  '期权',
}
STATUS_MAP = {
    '1': '上市',
    '0': '退市',
    '':  '上市',
}
TYPE_REVERSE = {v: k for k, v in BS_TYPE_MAP.items()}
STATUS_REVERSE = {v: k for k, v in STATUS_MAP.items()}


def bs_code_to_market_code(bs_code: str):
    """baostock代码 'sh.600000' → ('sh', '600000')"""
    if '.' not in bs_code:
        return bs_code[:2], bs_code[2:]
    return bs_code.split('.', 1)


# ── 数据库初始化 ──────────────────────────────────────────────────────────────
def init_tables(conn: sqlite3.Connection):
    conn.execute("PRAGMA journal_mode=WAL")

    # stock_info — 当前基础信息
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            market       TEXT    NOT NULL,
            code         TEXT    NOT NULL,
            name         TEXT,
            list_date    TEXT,
            delist_date  TEXT,
            stock_type   TEXT,
            industry_gb  TEXT,
            industry_cs  TEXT,
            status       TEXT    DEFAULT '上市',
            last_updated TEXT,
            PRIMARY KEY (market, code)
        )
    """)

    # stock_name_history — 名称变更历史
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_name_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market          TEXT    NOT NULL,
            code            TEXT    NOT NULL,
            name            TEXT    NOT NULL,
            effective_date  TEXT    NOT NULL,
            end_date        TEXT,
            change_type     TEXT,
            source          TEXT    DEFAULT 'baostock',
            UNIQUE(market, code, effective_date, change_type)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_name_history_code
        ON stock_name_history(market, code, effective_date)
    """)

    # index_constituents — 指数成分股权重
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_constituents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            index_code      TEXT    NOT NULL,
            index_name      TEXT,
            mkt_code        TEXT,
            market          TEXT    NOT NULL,
            code            TEXT    NOT NULL,
            weight          REAL,
            effective_date  TEXT    NOT NULL,
            UNIQUE(index_code, market, code, effective_date)
        )
    """)

    conn.commit()


# ── 核心: 批量查询 + 立即写入 ─────────────────────────────────────────────────
def populate_stock_info(verbose: bool = False, dry_run: bool = False):
    """
    策略: 分批查询，每批100只 → 立即写入DB
    中断后重新运行可从 stock_info 已有的记录继续（跳过已处理的代码）
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    init_tables(conn)

    # 获取 DB 中已有代码 → 跳过
    existing = set()
    rows = conn.execute(
        "SELECT market, code FROM stock_info"
    ).fetchall()
    for m, c in rows:
        existing.add((m, c))
    print(f"已有 stock_info: {len(existing)} 条，将跳过")

    # 获取 stock_daily 中实际存在的所有代码
    all_pairs = []
    rows = conn.execute(
        "SELECT DISTINCT market, code FROM stock_daily ORDER BY market, code"
    ).fetchall()
    for m, c in rows:
        if (m, c) not in existing:
            all_pairs.append((m, c))
    total = len(all_pairs)
    print(f"待查询: {total} 只（排除已有）")

    if total == 0:
        print("没有需要查询的股票，退出")
        conn.close()
        return

    # 转换为 baostock 格式
    all_codes = [f'{m}.{c}' for m, c in all_pairs]

    if dry_run:
        print(f"[DRY RUN] 跳过实际查询")
        conn.close()
        return

    # ── 全程一次 login，持续查询 ───────────────────────────────────────────
    bs.login()

    # ── 调试: 验证 baostock 连接 ──────────────────────────────────────────
    rs = bs.query_stock_basic(code='sh.600000')
    test_rows = []
    while rs.next():
        test_rows.append(rs.get_row_data())
    print(f"[调试] sh.600000 直接查询: {test_rows}")
    assert test_rows, "baostock 连接失败！"
    print("预热完成，开始批量查询...")

    # ── 分批处理: 每批100只，立即写入 ─────────────────────────────────────
    BATCH = 100
    total_inserted = 0
    total_errors = 0

    for batch_start in range(0, total, BATCH):
        batch_codes = all_codes[batch_start:batch_start + BATCH]
        batch_pairs = all_pairs[batch_start:batch_start + BATCH]

        results = []
        for bc in batch_codes:
            try:
                rs = bs.query_stock_basic(code=bc)
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    results.append(rows[0])
            except Exception as e:
                if verbose:
                    print(f"  错误 {bc}: {e}")
                total_errors += 1

        if batch_start == 0:
            print(f"[调试] 第1批查询结果数: {len(results)}")

        # ── 立即写入本批 ──────────────────────────────────────────────────
        inserted = 0
        for row, (mkt, code) in zip(results, batch_pairs):
            if not row or len(row) < 6:
                continue
            bs_code, name, ipo_date, out_date, bstype, status = row
            stock_type = BS_TYPE_MAP.get(bstype, bstype)
            status_val = STATUS_MAP.get(status, status)
            name_clean = name.strip() if name else ''

            try:
                conn.execute("""
                    INSERT OR REPLACE INTO stock_info
                        (market, code, name, list_date, delist_date,
                         stock_type, status, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (mkt, code, name_clean,
                      ipo_date or None, out_date or None,
                      stock_type, status_val,
                      datetime.now().strftime('%Y-%m-%d')))
                inserted += 1
            except Exception as e:
                if verbose:
                    print(f"  写入错误 {mkt}{code}: {e}")

        conn.commit()
        total_inserted += inserted

        pct = (batch_start + BATCH) / total * 100
        print(f"  进度 {batch_start + BATCH}/{total} ({pct:.0f}%) "
              f"本批{inserted}条 累计{total_inserted}条 错误{total_errors} "
              f"[{time.strftime('%H:%M:%S')}]")

        results.clear()

        # 每批次后稍作休息，防baostock限流
        if batch_start + BATCH < total:
            time.sleep(0.3)

    bs.logout()   # 全程只 login 一次，结束时 logout

    # ── 完成后: 写名称历史 + 行业 + 成分股权重 ─────────────────────────────
    print("\n写入名称历史记录...")
    written_hist = write_name_history(conn)
    print(f"  名称历史: {written_hist} 条")

    print("\n查询行业分类...")
    written_industry = populate_industry(conn, verbose=verbose)
    print(f"  行业更新: {written_industry} 条")

    print("\n查询指数成分股权重...")
    written_index = populate_index_constituents(conn, verbose=verbose)
    print(f"  成分股权重: {written_index} 条")

    # ── 最终统计 ─────────────────────────────────────────────────────────────
    final_cnt = conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
    hist_cnt = conn.execute("SELECT COUNT(*) FROM stock_name_history").fetchone()[0]
    print(f"\n完成! stock_info={final_cnt} name_history={hist_cnt}")

    conn.close()


def write_name_history(conn: sqlite3.Connection) -> int:
    """
    把 stock_info 中每只股票的当前名称作为第一条历史记录写入 name_history
    （effective_date = list_date, change_type = 'IPO'）
    只写入尚无历史记录的股票
    """
    rows = conn.execute("""
        SELECT market, code, name, list_date
        FROM stock_info
        WHERE list_date IS NOT NULL
          AND list_date != ''
          AND name IS NOT NULL
          AND name != ''
          AND (market, code) NOT IN (
              SELECT market, code FROM stock_name_history
          )
    """).fetchall()

    written = 0
    for market, code, name, list_date in rows:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO stock_name_history
                    (market, code, name, effective_date, change_type)
                VALUES (?, ?, ?, ?, 'IPO')
            """, (market, code, name, list_date))
            written += 1
        except Exception:
            pass
    conn.commit()
    return written


def populate_industry(conn: sqlite3.Connection, verbose: bool = False) -> int:
    """查询每只股票的证监会行业分类并更新 stock_info"""
    rows = conn.execute(
        "SELECT market, code FROM stock_info WHERE industry_gb IS NULL"
    ).fetchall()

    if not rows:
        return 0

    print(f"  行业分类: 需要查询 {len(rows)} 只")

    updated = 0
    bs.login()

    for i, (mkt, code) in enumerate(rows):
        bc = f'{mkt}.{code}'
        try:
            rs = bs.query_stock_industry(code=bc)
            ind_rows = []
            while rs.next():
                ind_rows.append(rs.get_row_data())
            if ind_rows and len(ind_rows[0]) >= 2:
                industry = ind_rows[0][1]
                conn.execute(
                    "UPDATE stock_info SET industry_gb=? "
                    "WHERE market=? AND code=?",
                    (industry, mkt, code)
                )
                updated += 1
        except Exception:
            pass

        if (i + 1) % 200 == 0:
            conn.commit()
            print(f"    进度 {i+1}/{len(rows)}")
            time.sleep(0.1)

    conn.commit()
    bs.logout()
    return updated


def populate_index_constituents(conn: sqlite3.Connection,
                                verbose: bool = False) -> int:
    """查询沪深300/上证50/中证500 成分股权重并写入"""
    index_map = {
        'sh.000300': ('sh000300', '沪深300'),
        'sh.000016': ('sh000016', '上证50'),
        'sh.000905': ('sh000905', '中证500'),
    }

    written = 0
    today = datetime.now().strftime('%Y-%m-%d')

    bs.login()
    for bc, (idx_code, idx_name) in index_map.items():
        try:
            rs = bs.query_hs300_stocks() if '000300' in bc else (
                bs.query_sz50_stocks() if '000016' in bc else
                bs.query_zz500_stocks()
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            for row in rows:
                if len(row) < 4:
                    continue
                code, name, date, weight = row[0], row[1], row[2], row[3]
                mkt = 'sh' if code.startswith(('6', '9')) else 'sz'
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO index_constituents
                            (index_code, index_name, mkt_code,
                             market, code, weight, effective_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (idx_code, idx_name, bc,
                          mkt, code, float(weight) if weight else None,
                          date or today))
                    written += 1
                except Exception:
                    pass
            print(f"  {idx_name}: {len(rows)} 只成分股")
        except Exception as e:
            if verbose:
                print(f"  {bc} 查询失败: {e}")

    conn.commit()
    bs.logout()
    return written


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='填充股票基础信息到 QuantDB')
    parser.add_argument('--dry-run', action='store_true', help='仅检查，不写入')
    parser.add_argument('--verbose', action='store_true', help='打印详细信息')
    args = parser.parse_args()

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] populate_stock_info 启动")
    print(f"  DB: {DB_PATH}")
    print(f"  dry_run: {args.dry_run}")

    try:
        populate_stock_info(verbose=args.verbose, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n中断，已写入的数据已保存，重新运行将从断点继续")
        raise


if __name__ == '__main__':
    main()
