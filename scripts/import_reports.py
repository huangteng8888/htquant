#!/usr/bin/env python3
"""将 reports.db 的研报数据导入 quantdb.analyst_reports"""

import sqlite3

QDDB = '/mnt/data/金融数据/quantdb/quantdb.sqlite'
RDB = '/tmp/reports.db'

def code_to_market(code):
    if not code or len(code) != 6:
        return None
    try:
        n = int(code)
        if 600000 <= n <= 605999 or 688000 <= n <= 689999:
            return 'sh'
        if n <= 1999:
            return 'sz'
        if n <= 2999:
            return 'sz'
        if 300000 <= n <= 301999:
            return 'sz'
        if 400000 <= n <= 404999:
            return 'bj'
        if 430000 <= n <= 899999:
            return 'bj'
        return 'sz'
    except:
        return None

def main():
    qdb = sqlite3.connect(QDDB)
    rdb = sqlite3.connect(RDB)

    # 建表
    qdb.execute('DROP TABLE IF EXISTS analyst_reports')
    qdb.execute('''
    CREATE TABLE analyst_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        info_code TEXT NOT NULL UNIQUE,
        market TEXT, code TEXT, stock_name TEXT, report_type TEXT NOT NULL,
        industry_name TEXT, indv_indu_name TEXT, publish_date TEXT NOT NULL,
        title TEXT, org_name TEXT, author TEXT, em_rating_code TEXT,
        em_rating_name TEXT, rating_change TEXT, rating_value REAL,
        predict_eps_this REAL, predict_eps_next REAL, predict_eps_next2 REAL,
        pdf_path TEXT, source TEXT DEFAULT "eastmoney",
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    ''')
    for col, idx_name in [
        ('code', 'idx_ar_code'),
        ('report_type', 'idx_ar_type'),
        ('em_rating_name', 'idx_ar_rating'),
        ('publish_date', 'idx_ar_date'),
        ('industry_name', 'idx_ar_industry'),
    ]:
        qdb.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON analyst_reports({col})')

    rating_map = {'买入': 5, '增持': 4, '持有': 3, '中性': 2, '减持': 1, '卖出': 0, '回避': 1}

    rdb.row_factory = sqlite3.Row
    cur = rdb.execute('''
        SELECT info_code, stock_code, stock_name, report_type,
               industry_name, indv_indu_name,
               publish_date, title, org_name, author,
               em_rating_code, em_rating_name, rating_change, em_rating_value,
               predict_this_year_eps, predict_next_year_eps, predict_next_two_year_eps,
               pdf_path
        FROM reports
    ''')

    batch = []
    total = 0
    for row in cur:
        code = row['stock_code']
        market = code_to_market(code)
        rating_name = row['em_rating_name']
        rating_val = rating_map.get(rating_name) if rating_name else None

        batch.append((
            row['info_code'], market, code or None, row['stock_name'],
            row['report_type'], row['industry_name'], row['indv_indu_name'],
            row['publish_date'], row['title'], row['org_name'], row['author'],
            row['em_rating_code'], rating_name, row['rating_change'], rating_val,
            row['predict_this_year_eps'], row['predict_next_year_eps'], row['predict_next_two_year_eps'],
            row['pdf_path'],
        ))
        if len(batch) >= 5000:
            qdb.executemany('''
                INSERT OR IGNORE INTO analyst_reports
                    (info_code,market,code,stock_name,report_type,industry_name,indv_indu_name,
                     publish_date,title,org_name,author,em_rating_code,em_rating_name,
                     rating_change,rating_value,predict_eps_this,predict_eps_next,predict_eps_next2,pdf_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', batch)
            total += len(batch)
            print(f'  已写入 {total} 条...', flush=True)
            batch = []

    if batch:
        qdb.executemany('''
            INSERT OR IGNORE INTO analyst_reports
                (info_code,market,code,stock_name,report_type,industry_name,indv_indu_name,
                 publish_date,title,org_name,author,em_rating_code,em_rating_name,
                 rating_change,rating_value,predict_eps_this,predict_eps_next,predict_eps_next2,pdf_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', batch)
        total += len(batch)

    qdb.commit()
    rdb.close()
    qdb.close()
    print(f'DONE: {total} 条写入 quantdb.analyst_reports')

if __name__ == '__main__':
    main()
