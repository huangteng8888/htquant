#!/usr/bin/env python3
"""
研报数据同步脚本
- 从 192.168.1.21 复制 reports.db 到本地
- 增量更新 quantdb.analyst_reports
- 更新 stock_info 行业字段

用法: python3 sync_reports.py
"""
import os, sqlite3, shutil
from datetime import datetime

QDDB = '/mnt/data/金融数据/quantdb/quantdb.sqlite'
REMOTE_PATH = r'\\I5-9500\research\meta\reports.db'
LOCAL_PATH = '/tmp/reports_sync.db'
LOG_PATH = '/home/ht/github/htquant/logs/sync_reports.log'

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')

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
        if 430000 <= n <= 899999:
            return 'bj'
        return 'sz'
    except:
        return None

rating_map = {'买入': 5, '增持': 4, '持有': 3, '中性': 2, '减持': 1, '卖出': 0, '回避': 1}

def sync_reports():
    log('开始同步研报数据')

    # Step 1: 通过 SMB 复制 reports.db
    try:
        from impacket.smbconnection import SMBConnection
        conn = SMBConnection('192.168.1.21', '192.168.1.21', timeout=30)
        conn.login('Ubuntu', '123456')
        tree = conn.connectTree('research')

        # 获取远程文件大小
        fh = conn.openFile(tree, '/meta/reports.db', desired_access=0x800000)  # GENERIC_READ
        size = conn.queryInfo(tree, fh)['fields']['allocation_size']
        conn.closeFile(tree, fh)
        log(f'远程 reports.db 大小: {size:,} bytes')
    except Exception as e:
        log(f'SMB连接失败: {e}，跳过复制步骤（使用本地缓存）')
        if not os.path.exists(LOCAL_PATH):
            log(f'本地缓存也不存在: {LOCAL_PATH}')
            return

    # Step 2: 复制文件（仅当远程更新时）
    try:
        shutil.copy2(REMOTE_PATH, LOCAL_PATH)
        log(f'reports.db 已复制到 {LOCAL_PATH}')
    except Exception as e:
        log(f'文件复制失败: {e}，使用现有本地缓存')
        if not os.path.exists(LOCAL_PATH):
            log('错误：无可用数据源')
            return

    # Step 3: 增量写入 analyst_reports
    qdb = sqlite3.connect(QDDB)
    rdb = sqlite3.connect(LOCAL_PATH)
    rdb.row_factory = sqlite3.Row

    # 获取已有 info_code
    existing = set()
    cur = qdb.execute("SELECT info_code FROM analyst_reports")
    for row in cur:
        existing.add(row[0])

    # 读取新数据
    cur = rdb.execute("""
        SELECT info_code, stock_code, stock_name, report_type,
               industry_name, indv_indu_name,
               publish_date, title, org_name, author,
               em_rating_code, em_rating_name, rating_change, em_rating_value,
               predict_this_year_eps, predict_next_year_eps, predict_next_two_year_eps,
               pdf_path
        FROM reports
    """)

    batch = []
    skipped = 0
    for row in cur:
        if row['info_code'] in existing:
            skipped += 1
            continue
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

    if batch:
        qdb.executemany("""
            INSERT OR IGNORE INTO analyst_reports
                (info_code,market,code,stock_name,report_type,industry_name,indv_indu_name,
                 publish_date,title,org_name,author,em_rating_code,em_rating_name,
                 rating_change,rating_value,predict_eps_this,predict_eps_next,predict_eps_next2,pdf_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, batch)
        qdb.commit()
        log(f'✅ 新增 {len(batch)} 条，跳过已有 {skipped} 条')
    else:
        log(f'✅ 无新数据（跳过 {skipped} 条已存在）')

    rdb.close()

    # Step 4: 更新 stock_info 行业（取最新）
    cur = qdb.execute("""
        SELECT code, market, industry_name, COUNT(*) as cnt
        FROM analyst_reports
        WHERE code IS NOT NULL AND industry_name IS NOT NULL AND industry_name != ''
        GROUP BY code, market, industry_name
    """)
    stock_industry = {}
    for code, market, ind, cnt in cur:
        key = (code, market)
        if key not in stock_industry or cnt > stock_industry[key][1]:
            stock_industry[key] = (ind, cnt)

    updated = 0
    for (code, market), (industry, cnt) in stock_industry.items():
        if cnt >= 3:
            qdb.execute("UPDATE stock_info SET industry_gb = ? WHERE code = ? AND market = ?",
                        (industry, code, market))
            updated += 1
    qdb.commit()

    # 申万行业
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
    updated_sw = 0
    for (code, market), (industry, cnt) in sw_industry.items():
        if cnt >= 3:
            qdb.execute("UPDATE stock_info SET industry_cs = ? WHERE code = ? AND market = ?",
                        (industry, code, market))
            updated_sw += 1
    qdb.commit()
    qdb.close()

    total = qdb.execute("SELECT COUNT(*) FROM analyst_reports").fetchone()[0] if False else None
    log(f'✅ 行业更新: {updated} 只(东财) + {updated_sw} 只(申万)')

    total = sqlite3.connect(QDDB).execute("SELECT COUNT(*) FROM analyst_reports").fetchone()[0]
    log(f'✅ quantdb.analyst_reports 现有 {total} 条')

if __name__ == '__main__':
    sync_reports()
