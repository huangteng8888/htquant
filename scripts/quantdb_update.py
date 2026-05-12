#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quantdb_update.py — 量化数据库增量更新（智能重试版）

数据时效链:
  A股收盘(15:00 Beijing) → TDX发布zip(约15:58) → cron触发(16:05)
                                                         ↓
                                    zip未发布则每10分钟重试，最长等3小时(直到19:05)
                                                         ↓
                               zip已更新 → 下载 → 解压 → 增量入库 → 极值检测

重试策略:
  - 对比 HTTP Last-Modified: 等待 zip 出现新版本再下载
  - 每次检查间隔 10 分钟，最长等待 3 小时（覆盖交易日延迟场景）
  - 等待期间无需盯守，脚本自己循环

Crontab (每日 16:05 执行):
  5 16 * * 1-5 cd ~/github/htquant && python3 scripts/quantdb_update.py >> logs/quantdb_update.log 2>&1

Usage:
  python3 scripts/quantdb_update.py              # 正常增量更新（含重试）
  python3 scripts/quantdb_update.py --dry-run  # 仅检查 zip 状态
  python3 scripts/quantdb_update.py --force     # 跳过重试立即下载（测试用）
  python3 scripts/quantdb_update.py --full-scan # 全量极值事件重扫
"""

import argparse
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# 确保 scripts/ 在 path 中（供 quantdb_validate 导入）
sys.path.insert(0, str(Path(__file__).parent))

# ── 路径配置 ──────────────────────────────────────────────────────────────────
DB_PATH     = Path('/mnt/data/金融数据/quantdb/quantdb.sqlite')
TDX_BASE    = Path('/mnt/data/金融数据/hsjday/lday/')
TDX_URL     = 'https://data.tdx.com.cn/vipdoc/hsjday.zip'
STATE_FILE  = Path('/mnt/data/金融数据/quantdb/.tdx_sync_state')

# ── 重试参数 ─────────────────────────────────────────────────────────────────
RETRY_INTERVAL_MINUTES = 10   # 检查间隔
MAX_WAIT_MINUTES      = 180  # 最长等待3小时（15:58 → 19:05 覆盖所有延迟场景）

NORMAL_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-131072;
PRAGMA temp_store=MEMORY;
"""


# ── 状态管理 ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """加载上次同步状态"""
    if not STATE_FILE.exists():
        return {'last_lm': None, 'last_date': None, 'last_success': None}
    try:
        import json
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {'last_lm': None, 'last_date': None, 'last_success': None}


def save_state(state: dict):
    """保存同步状态"""
    try:
        import json
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    except Exception as e:
        print(f"  [WARN] 状态保存失败: {e}")


# ── HTTP 检查 ─────────────────────────────────────────────────────────────────

def check_zip_head(url: str) -> dict:
    """只发 HEAD 请求，返回 Last-Modified 和 Content-Length"""
    req = urllib.request.Request(url, method='HEAD')
    req.add_header('User-Agent', 'Mozilla/5.0')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {
                'lm': resp.headers.get('Last-Modified', ''),
                'cl': int(resp.headers.get('Content-Length', 0)),
                'ok': True,
            }
    except Exception as e:
        return {'lm': '', 'cl': 0, 'ok': False, 'err': str(e)}


def is_weekend(dt: datetime = None) -> bool:
    """判断是否周末（无交易数据发布）"""
    if dt is None:
        dt = datetime.now()
    return dt.weekday() >= 5  # 5=Saturday, 6=Sunday


def lm_to_datetime(lm_str: str) -> datetime:
    """HTTP Last-Modified → datetime（UTC）"""
    try:
        return datetime.strptime(lm_str, '%a, %d %b %Y %H:%M:%S %Z')
    except Exception:
        return None


# ── TDX 文件解析 ─────────────────────────────────────────────────────────────

def parse_tdx_record(data: bytes) -> dict:
    """解析单条通达信 .day 记录 (32字节)"""
    if len(data) < 32:
        return None
    dt    = struct.unpack('<I', data[0:4])[0]
    open_ = struct.unpack('<I', data[4:8])[0]  / 100.0
    high  = struct.unpack('<I', data[8:12])[0] / 100.0
    low   = struct.unpack('<I', data[12:16])[0] / 100.0
    close = struct.unpack('<I', data[16:20])[0] / 100.0
    amount= struct.unpack('<q', data[20:28])[0]
    vol   = struct.unpack('<I', data[28:32])[0]
    year, rest = divmod(dt // 10000, 100)
    month, day = divmod(rest, 100)
    date_str = f'{year:04d}-{month:02d}-{day:02d}'
    return {'date': date_str, 'open': open_, 'high': high,
            'low': low, 'close': close, 'volume': vol, 'amount': amount}


def tdx_filename_to_code(filename: str) -> tuple:
    """从 TDX 文件名解析市场+代码"""
    name = filename.replace('.day', '')
    if name.startswith('sh'):
        return 'sh', name[2:]
    elif name.startswith('sz'):
        return 'sz', name[2:]
    elif name.startswith('bj'):
        return 'bj', name[2:]
    return 'sh', name


def _get_file_last_date(fp: Path) -> str:
    """只读文件末32字节，高效获取文件内最后日期"""
    try:
        with open(fp, 'rb') as f:
            f.seek(-32, 2)
            last_32 = f.read(32)
        if len(last_32) < 32:
            return '1990-01-01'
        dt = struct.unpack('<I', last_32[0:4])[0]
        year, rest = divmod(dt // 10000, 100)
        month, day = divmod(rest, 100)
        if year < 1990 or year > 2100:
            return '1990-01-01'
        return f'{year:04d}-{month:02d}-{day:02d}'
    except Exception:
        return '1990-01-01'


def _load_db_latest_dates(db_path: Path) -> dict:
    """加载全量 (market,code) → latest_date 字典"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute("""
        SELECT market, code, MAX(trade_date)
        FROM stock_daily GROUP BY market, code
    """).fetchall()
    conn.close()
    return {(m, c): d for m, c, d in rows}


# ── 智能等待并下载 ────────────────────────────────────────────────────────────

def wait_for_updated_zip(url: str, state: dict, force: bool = False) -> dict:
    """
    等待 zip 更新：
      - 对比 HTTP Last-Modified vs 上次成功下载的 Last-Modified
      - 如果相同：等待 RETRY_INTERVAL_MINUTES 后重试，最长等 MAX_WAIT_MINUTES
      - 如果不同/强制：立即返回，开始下载
      - 周末（周五19:05后 ~ 周一14:00前）：直接跳过等待
    
    返回: {'action': 'download'|'skip'|'wait', 'lm': str, 'wait_count': int, 'msg': str}
    """
    now_local = datetime.now()
    is_wknd = is_weekend(now_local)

    if is_wknd:
        print(f"  今日为周末({now_local.strftime('%A')})，跳过等待")
        # 周末：检查zip是否已更新为周一数据（周五晚发布）
        info = check_zip_head(url)
        if not info['ok']:
            print(f"  TDX服务器不可达({info.get('err','?')})，跳过本次更新")
            return {'action': 'skip', 'lm': None, 'wait_count': 0,
                    'msg': '周末且服务器不可达'}
        lm_remote = lm_to_datetime(info['lm'])
        lm_last   = lm_to_datetime(state['last_lm']) if state['last_lm'] else None
        if lm_remote and lm_last and lm_remote <= lm_last:
            print(f"  zip未更新({info['lm']})，周末无需等待，跳过")
            return {'action': 'skip', 'lm': info['lm'], 'wait_count': 0,
                    'msg': f'周末zip未更新({info["lm"]})'}
        print(f"  zip已更新({info['lm']})，周末含新数据，准备下载")
        return {'action': 'download', 'lm': info['lm'], 'wait_count': 0, 'msg': '周末新数据'}

    # ── 工作日逻辑 ───────────────────────────────────────────────────────────
    last_lm = state.get('last_lm')
    wait_count = 0
    deadline = now_local + timedelta(minutes=MAX_WAIT_MINUTES)

    print(f"  当前zip Last-Modified: {state.get('last_lm', '未知')}")
    print(f"  等待策略: 每{RETRY_INTERVAL_MINUTES}分钟检查一次，最长等{MAX_WAIT_MINUTES}分钟")
    print(f"  截止时间: {deadline.strftime('%H:%M')}")

    while True:
        info = check_zip_head(url)
        if not info['ok']:
            print(f"  [{wait_count}] TDX服务器不可达({info.get('err','?')})，{RETRY_INTERVAL_MINUTES}分钟后重试...")
        else:
            lm_remote = info['lm']
            is_updated = (
                force or
                last_lm is None or
                lm_remote != last_lm
            )
            if is_updated:
                print(f"  [OK] zip已更新({lm_remote})，开始下载")
                return {'action': 'download', 'lm': lm_remote, 'wait_count': wait_count, 'msg': 'updated'}
            else:
                next_check = (datetime.now() + timedelta(minutes=RETRY_INTERVAL_MINUTES)).strftime('%H:%M')
                print(f"  [{wait_count}] zip未更新({lm_remote})，等待{RETRY_INTERVAL_MINUTES}分钟，"
                      f"下次检查{next_check}...")

        # 超时检查
        if datetime.now() >= deadline:
            print(f"  [WARN] 已等待{MAX_WAIT_MINUTES}分钟仍未更新，跳过本次")
            return {'action': 'skip', 'lm': None, 'wait_count': wait_count,
                    'msg': f'等待{MAX_WAIT_MINUTES}分钟超时'}

        wait_count += 1
        time.sleep(RETRY_INTERVAL_MINUTES * 60)


def download_and_extract_zip(url: str, target_dir: Path, chunk_size: int = 8192) -> dict:
    """
    下载 zip 并解压覆盖到 target_dir
    返回: {'ok': bool, 'files': int, 'bytes': int, 'err': str}
    """
    import io
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        with urllib.request.urlopen(req, timeout=300) as resp:
            total_bytes = 0
            chunks = []
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
            zip_data = b''.join(chunks)

        print(f"  下载完成: {total_bytes / 1024**2:.1f} MB，开始解压...")

        # 解压到临时目录，再原子性移动到 TDX_BASE
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(io.BytesIO(zip_data), 'r') as zf:
                members = [m for m in zf.namelist() if m.endswith('.day')]
                zf.extractall(tmpdir)
            print(f"  解压完成: {len(members)} 个 .day 文件")

            # TDX zip 使用 Windows 风格路径分隔符 (sz\lday\sz159147.day)，
            # extractall 在 Linux 上会创建 "sz\lday" 这样的字面目录名。
            # 用 rglob('**/*.day') 递归查找所有深度的 .day 文件，
            # 然后只取文件名（去掉任何路径分量）以避免 copy 到目标时路径错误。
            day_files = list(Path(tmpdir).rglob('*.day'))
            print(f"  找到 {len(day_files)} 个 .day 文件（含嵌套目录）")

            # 原子性替换：先清空旧文件，再复制新文件
            old_files = list(target_dir.glob('*.day'))
            for f in old_files:
                f.unlink()
            for fp in day_files:
                # fp.name 可能在 Linux 上包含字面反斜杠（如 sz\lday\sz159147.day），
                # 因为 TDX zip 使用 Windows 风格路径而非目录结构。
                # 只取文件名最后一段作为目标文件名。
                raw_name = fp.name
                final_name = raw_name.split('\\')[-1].split('/')[-1]
                shutil.copy2(fp, target_dir / final_name)

        return {'ok': True, 'files': len(members), 'bytes': total_bytes}
    except Exception as e:
        return {'ok': False, 'files': 0, 'bytes': 0, 'err': str(e)}


# ── 增量导入 ─────────────────────────────────────────────────────────────────

def update_from_tdx_file(db_path: Path, dry_run: bool = False) -> dict:
    """
    扫描 TDX 目录，增量导入新记录（Phase1快速过滤 + Phase2精确解析）
    """
    stats = {'files': 0, 'records': 0, 'skipped': 0, 'errors': 0}

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(NORMAL_PRAGMAS)

    files = list(TDX_BASE.glob('*.day'))
    stats['files'] = len(files)

    db_latest = _load_db_latest_dates(db_path)
    files_needing_check = []
    for fp in files:
        mkt, code = tdx_filename_to_code(fp.name)
        db_date = db_latest.get((mkt, code), '1990-01-01')
        file_last = _get_file_last_date(fp)
        if file_last > db_date:
            files_needing_check.append((fp, mkt, code, db_date))

    if dry_run:
        conn.close()
        stats['records'] = len(files_needing_check)
        return stats

    batch, BATCH_SIZE = [], 5000
    for fp, mkt, code, db_date in files_needing_check:
        try:
            with open(fp, 'rb') as f:
                raw = f.read()
        except Exception:
            stats['errors'] += 1
            continue

        new_records = []
        for i in range(0, len(raw), 32):
            rec = parse_tdx_record(raw[i:i+32])
            if rec and rec['date'] > db_date:
                new_records.append(rec)

        if not new_records:
            stats['skipped'] += 1
            continue

        for rec in new_records:
            batch.append((
                mkt, code, rec['date'], rec['open'], rec['high'],
                rec['low'], rec['close'], rec['volume'], rec['amount']
            ))

        if len(batch) >= BATCH_SIZE:
            conn.executemany(
                """INSERT OR IGNORE INTO stock_daily
                   (market,code,trade_date,open,high,low,close,volume,amount)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                batch
            )
            conn.commit()
            stats['records'] += len(batch)
            batch = []

    if batch:
        conn.executemany(
            """INSERT OR IGNORE INTO stock_daily
               (market,code,trade_date,open,high,low,close,volume,amount)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            batch
        )
        conn.commit()
        stats['records'] += len(batch)

    conn.close()
    return stats


# ── 极值检测 ─────────────────────────────────────────────────────────────────

def _compute_rsi_numpy(closes, period=14):
    import numpy as np
    rsi = np.full_like(closes, 50.0, dtype=np.float64)
    if len(closes) < period + 1:
        return rsi
    deltas = np.diff(closes, axis=0)
    gains  = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gains  = np.convolve(gains,  np.ones(period)/period, mode='valid')
    avg_losses = np.convolve(losses, np.ones(period)/period, mode='valid')
    n = len(avg_losses)
    for i in range(n):
        al = avg_losses[i]
        ag = avg_gains[i]
        rsi[i + period] = 100.0 - (100.0 / (1.0 + ag / al)) if al > 0 else 100.0
    return rsi


def _rolling_max(arr, window):
    import numpy as np
    n = len(arr)
    out = np.empty(n, dtype=arr.dtype)
    out[:window] = arr[:window]
    for i in range(window, n):
        out[i] = max(arr[i-window:i])
    return out


def _rolling_min(arr, window):
    import numpy as np
    n = len(arr)
    out = np.empty(n, dtype=arr.dtype)
    out[:window] = arr[:window]
    for i in range(window, n):
        out[i] = min(arr[i-window:i])
    return out


def detect_extreme_events_incremental(db_path: Path, days: int = 30) -> int:
    """
    增量极值检测：只处理 last_update 之后新增的记录
    """
    import numpy as np

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_update'"
    ).fetchone()
    last_update = row[0] if row else '1990-01-01'
    conn.close()

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    stocks = conn.execute("""
        SELECT DISTINCT market, code FROM stock_daily
        WHERE trade_date >= ?
    """, (cutoff,)).fetchall()
    conn.close()

    count = 0
    BATCH, event_batch = 10000, []
    write_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_conn.execute("PRAGMA cache_size=-131072")

    for mkt, code in stocks:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount
            FROM stock_daily WHERE market=? AND code=? AND trade_date>=?
            ORDER BY trade_date
        """, (mkt, code, cutoff)).fetchall()
        conn.close()

        if len(rows) < 1:
            continue

        oldest_new = rows[0][0]
        hist_conn = sqlite3.connect(str(db_path))
        hist_rows = hist_conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount
            FROM stock_daily
            WHERE market=? AND code=? AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 252
        """, (mkt, code, oldest_new)).fetchall()
        hist_conn.close()
        hist_rows = list(reversed(hist_rows))

        combined = hist_rows + rows
        if len(combined) < 20:
            continue

        dates   = [r[0] for r in combined]
        highs   = np.array([r[3] for r in combined], dtype=np.float64)
        lows    = np.array([r[2] for r in combined], dtype=np.float64)
        closes  = np.array([r[4] for r in combined], dtype=np.float64)
        volumes = np.array([r[5] for r in combined], dtype=np.float64)
        new_start = len(hist_rows)

        vol_sma   = np.convolve(volumes, np.ones(20)/20, mode='same')
        vol_ratio = np.where(vol_sma > 0, volumes / vol_sma, 1.0)
        rsi_arr   = _compute_rsi_numpy(closes, 14)

        WINDOWS  = [20, 50, 100, 252]
        THRESHOLD = 0.01
        n = len(closes)

        idx5  = np.minimum(np.arange(n) + 5,  n - 1)
        idx20 = np.minimum(np.arange(n) + 20, n - 1)
        idx60 = np.minimum(np.arange(n) + 60, n - 1)
        f5_arr  = (closes[idx5]  - closes) / np.maximum(closes, 1e-10)
        f20_arr = (closes[idx20] - closes) / np.maximum(closes, 1e-10)
        f60_arr = (closes[idx60] - closes) / np.maximum(closes, 1e-10)

        stock_events = []
        for w in WINDOWS:
            if n <= w:
                continue
            wh = _rolling_max(highs, w)
            wl = _rolling_min(lows, w)

            h_ok = highs >= wh
            r_h  = np.abs(highs - wh) / np.maximum(closes, 1e-10)
            h_mask = h_ok & (r_h < THRESHOLD)
            h_mask[:new_start] = False

            l_ok = lows <= wl
            r_l  = np.abs(lows - wl) / np.maximum(closes, 1e-10)
            l_mask = l_ok & (r_l < THRESHOLD)
            l_mask[:new_start] = False

            for idx_set, evt_type in [(np.where(h_mask)[0], f'W{w}_HIGH_TOUCH'),
                                       (np.where(l_mask)[0], f'W{w}_LOW_TOUCH')]:
                for i in idx_set:
                    stock_events.append((
                        mkt, code, dates[i],
                        evt_type,
                        round(float(wh[i]), 4), round(float(wl[i]), 4),
                        round(float(closes[i]), 4),
                        round(float(f5_arr[i]), 6), round(float(f20_arr[i]), 6),
                        round(float(f60_arr[i]), 6),
                        round(float(rsi_arr[i]), 2), round(float(vol_ratio[i]), 2),
                    ))

        event_batch.extend(stock_events)

        if len(event_batch) >= BATCH:
            before = write_conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
            write_conn.executemany(
                """INSERT OR IGNORE INTO extreme_events
                   (market,code,trade_date,event_type,window_high,window_low,
                    close_at_event,forward_5d_ret,forward_20d_ret,forward_60d_ret,
                    rsi14_at_event,volume_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                event_batch
            )
            write_conn.commit()
            after = write_conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
            count += (after - before)
            event_batch = []

    if event_batch:
        before = write_conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
        write_conn.executemany(
            """INSERT OR IGNORE INTO extreme_events
               (market,code,trade_date,event_type,window_high,window_low,
                close_at_event,forward_5d_ret,forward_20d_ret,forward_60d_ret,
                rsi14_at_event,volume_ratio)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            event_batch
        )
        write_conn.commit()
        after = write_conn.execute("SELECT COUNT(*) FROM extreme_events").fetchone()[0]
        count += (after - before)

    write_conn.close()
    return count


# ── 主程序 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='quantdb 增量更新（智能重试版）')
    parser.add_argument('--dry-run',  action='store_true', help='仅检查zip状态，不下载不写入')
    parser.add_argument('--force',    action='store_true', help='跳过等待立即下载（测试用）')
    parser.add_argument('--full-scan', action='store_true', help='全量极值事件重新扫描')
    parser.add_argument('--days',     type=int, default=30, help='极值回扫天数 (默认30)')
    args = parser.parse_args()

    now = datetime.now()
    print(f"\n[{now:%Y-%m-%d %H:%M:%S}] quantdb_update 启动")
    print(f"  DB:      {DB_PATH}")
    print(f"  TDX:     {TDX_URL}")
    print(f"  dry_run: {args.dry_run}  force: {args.force}")

    # 加载状态
    state = load_state()
    print(f"  上次同步: {state.get('last_success','首次运行')}  lm={state.get('last_lm','?')}")

    # ── Step 1: 智能等待 zip 更新 ────────────────────────────────────────────
    print(f"\n[1/5] 检查 TDX zip 更新状态...")
    result = wait_for_updated_zip(TDX_URL, state, force=args.force)

    if result['action'] == 'skip':
        print(f"  → 跳过: {result['msg']}")
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 退出（无需更新）")
        return

    if args.dry_run:
        print(f"  → zip已更新({result['lm']})，dry-run模式，不下载")
        return

    if result['wait_count'] > 0:
        print(f"  → 等待了 {result['wait_count']} 个周期 ({result['wait_count']*RETRY_INTERVAL_MINUTES}分钟) 后zip更新")

    # ── Step 2: 下载并解压 zip ───────────────────────────────────────────────
    print(f"\n[2/5] 下载 TDX zip...")
    dl = download_and_extract_zip(TDX_URL, TDX_BASE)
    if not dl['ok']:
        print(f"  下载失败: {dl['err']}")
        return
    print(f"  完成: {dl['files']} 个 .day 文件 ({dl['bytes']/1024**2:.1f} MB)")

    # ── Step 3: 增量导入数据库 ───────────────────────────────────────────────
    print(f"\n[3/5] 增量导入 stock_daily...")
    stats = update_from_tdx_file(DB_PATH, dry_run=False)
    print(f"  文件总数:   {stats['files']}")
    print(f"  新增记录:   {stats['records']:,}")
    print(f"  跳过(无变化): {stats['skipped']}")
    print(f"  错误:       {stats['errors']}")

    if stats['records'] == 0:
        print(f"  注意: 无新增记录，可能是zip内容与DB一致")

    # ── Step 3.5: L1 数据质量验证 ───────────────────────────────────────────
    print(f"\n[3.5/5] L1 数据质量验证...")
    from quantdb_validate import validate_incremental
    v = validate_incremental(DB_PATH, new_records=stats['records'], verbose=True)
    if not v.get('passed', True):
        print(f"  🔴 验证失败: {v.get('alerts', [])}")
    else:
        print(f"  验证结果: ✅ 通过")

    # ── Step 4: 极值事件增量检测 ─────────────────────────────────────────────
    if args.full_scan:
        print(f"\n[4/5] 全量极值事件重新扫描...")
        from htquant.quantdb import QuantDB
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM extreme_events")
        conn.commit()
        conn.close()
        db = QuantDB()
        count = db.detect_extreme_events(codes=None, show_progress=True)
    else:
        print(f"\n[4/5] 极值事件增量检测...")
        count = detect_extreme_events_incremental(DB_PATH, days=args.days)

    print(f"  极值事件: +{count:,} 条")

    # ── Step 5: 更新状态和时间戳 ─────────────────────────────────────────────
    print(f"\n[5/5] 保存同步状态...")
    new_state = {
        'last_lm':      result['lm'],
        'last_date':    datetime.now().strftime('%Y-%m-%d'),
        'last_success': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    save_state(new_state)

    # 更新 meta
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_update', ?)",
        (datetime.now().strftime('%Y-%m-%d %H:%M'),)
    )
    conn.commit()
    conn.close()

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 更新完成!")
    print(f"  新增行情: {stats['records']:,} 条")
    print(f"  极值事件: +{count:,} 条")
    print(f"  zip更新:  {result['lm']}")


if __name__ == '__main__':
    main()
