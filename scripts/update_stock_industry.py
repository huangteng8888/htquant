#!/usr/bin/env python3
"""
将 analyst_reports 的行业数据回填 stock_info
- industry_gb: 东财行业（50%有数据，取个股研报中出现最多的）
- industry_cs: indv_indu_name（申万行业，26%有数据）
"""

import sqlite3
from collections import Counter

QDDB = '/mnt/data/金融数据/quantdb/quantdb.sqlite'

def main():
    qdb = sqlite3.connect(QDDB)

    # 统计每个(code, industry_name)出现次数，取最多的作为该股行业
    print("计算每个股票的最常见行业...")
    cur = qdb.execute("""
        SELECT code, market, industry_name, COUNT(*) as cnt
        FROM analyst_reports
        WHERE code IS NOT NULL AND industry_name IS NOT NULL AND industry_name != ''
        GROUP BY code, market, industry_name
    """)
    
    # 对于每个(code, market)，取cnt最大的行业
    stock_industry = {}
    for code, market, ind, cnt in cur:
        key = (code, market)
        if key not in stock_industry or cnt > stock_industry[key][1]:
            stock_industry[key] = (ind, cnt)

    print(f"共有 {len(stock_industry)} 只股票有行业数据")

    # 更新 stock_info
    updated = 0
    skipped = 0
    for (code, market), (industry, cnt) in stock_industry.items():
        if cnt < 3:  # 至少3篇研报覆盖才更新
            skipped += 1
            continue
        qdb.execute(
            "UPDATE stock_info SET industry_gb = ? WHERE code = ? AND market = ?",
            (industry, code, market)
        )
        updated += 1

    qdb.commit()
    print(f"✅ 行业更新完成: {updated} 只股票更新，{skipped} 只因研报少于3篇跳过")

    # 统计填充率
    cur = qdb.execute("SELECT COUNT(*) FROM stock_info WHERE industry_gb IS NOT NULL")
    filled = cur.fetchone()[0]
    cur = qdb.execute("SELECT COUNT(*) FROM stock_info")
    total = cur.fetchone()[0]
    print(f"   stock_info 行业填充率: {filled}/{total} ({100*filled/total:.1f}%)")

    # 同样处理申万行业 (indv_indu_name)
    print("\n计算申万行业...")
    cur = qdb.execute("""
        SELECT code, market, indv_indu_name, COUNT(*) as cnt
        FROM analyst_reports
        WHERE code IS NOT NULL AND indv_indu_name IS NOT NULL AND indv_indu_name != ''
        GROUP BY code, market, indv_indu_name
    """)
    sw_industry = {}
    for code, market, ind, cnt in cur:
        key = (code, market)
        if key not in sw_industry or cnt > sw_industry[key][1]:
            sw_industry[key] = (ind, cnt)
    print(f"共有 {len(sw_industry)} 只股票有申万行业数据")

    updated_sw = 0
    skipped_sw = 0
    for (code, market), (industry, cnt) in sw_industry.items():
        if cnt < 3:
            skipped_sw += 1
            continue
        qdb.execute(
            "UPDATE stock_info SET industry_cs = ? WHERE code = ? AND market = ?",
            (industry, code, market)
        )
        updated_sw += 1

    qdb.commit()
    print(f"✅ 申万行业更新完成: {updated_sw} 只股票更新，{skipped_sw} 只跳过")

    cur = qdb.execute("SELECT COUNT(*) FROM stock_info WHERE industry_cs IS NOT NULL")
    filled_sw = cur.fetchone()[0]
    print(f"   stock_info 申万行业填充率: {filled_sw}/{total} ({100*filled_sw/total:.1f}%)")

    # 展示行业分布
    print("\n=== stock_info 行业分布 (前15) ===")
    cur = qdb.execute("""
        SELECT industry_gb, COUNT(*) as cnt
        FROM stock_info
        WHERE industry_gb IS NOT NULL
        GROUP BY industry_gb
        ORDER BY cnt DESC LIMIT 15
    """)
    for row in cur:
        print(f"  {row[0]}: {row[1]}")

    qdb.close()

if __name__ == '__main__':
    main()
