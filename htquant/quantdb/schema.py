# -*- coding: utf-8 -*-
"""
quantdb 数据库 schema 定义

SQLite 主库: /mnt/data/金融数据/quantdb/quantdb.sqlite
"""

SCHEMA_SQL = """
-- ─────────────────────────────────────────────────────────────────────────────
-- 表1: stock_daily — 日线行情 (核心表)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT    NOT NULL CHECK(market IN ('sh','sz','bj')),
    code        TEXT    NOT NULL CHECK(LENGTH(code)=6),
    trade_date  TEXT    NOT NULL CHECK(trade_date LIKE '____-__-__'),
    open        REAL    NOT NULL CHECK(open >= 0),
    high        REAL    NOT NULL CHECK(high >= 0),
    low         REAL    NOT NULL CHECK(low >= 0),
    close       REAL    NOT NULL CHECK(close >= 0),
    volume      INTEGER NOT NULL CHECK(volume >= 0),
    amount      INTEGER NOT NULL CHECK(amount >= 0),
    prev_close  REAL    CHECK(prev_close IS NULL OR prev_close >= 0),
    chg_pct     REAL,
    UNIQUE(market, code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_sd_market_code
    ON stock_daily(market, code);
CREATE INDEX IF NOT EXISTS idx_sd_trade_date
    ON stock_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_sd_market_code_date
    ON stock_daily(market, code, trade_date);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表2: stock_info — 股票基础信息
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_info (
    market          TEXT    NOT NULL CHECK(market IN ('sh','sz','bj')),
    code            TEXT    NOT NULL NULL CHECK(LENGTH(code)=6),
    name            TEXT,
    list_date       TEXT,
    delist_date     TEXT,
    stock_type      TEXT    CHECK(stock_type IN ('A股','B股','指数','ETF','债券')),
    industry         TEXT,
    PRIMARY KEY (market, code)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表3: extreme_events — 极值事件 (htquant 辩论引擎核心)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS extreme_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market          TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    -- event_type: W{20|50|100|252}_{LOW|HIGH}_TOUCH
    window_high     REAL,
    window_low      REAL,
    close_at_event  REAL,
    forward_5d_ret  REAL,
    forward_20d_ret REAL,
    forward_60d_ret REAL,
    rsi14_at_event  REAL,
    volume_ratio    REAL,
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(market, code, trade_date, event_type)
);

CREATE INDEX IF NOT EXISTS idx_ee_code_date
    ON extreme_events(market, code, trade_date);
CREATE INDEX IF NOT EXISTS idx_ee_event_type
    ON extreme_events(event_type);
CREATE INDEX IF NOT EXISTS idx_ee_trade_date
    ON extreme_events(trade_date);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表4: adapter_signals — 各 adapter 每日信号记录
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS adapter_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market          TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    adapter_name    TEXT    NOT NULL,
    signal          TEXT    NOT NULL,
    confidence      REAL    CHECK(confidence BETWEEN 0 AND 1),
    reason          TEXT,
    extreme_event   TEXT,
    rsi14           REAL,
    price_at_signal REAL,
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(market, code, trade_date, adapter_name)
);

CREATE INDEX IF NOT EXISTS idx_as_code_date
    ON adapter_signals(market, code, trade_date);
CREATE INDEX IF NOT EXISTS idx_as_adapter
    ON adapter_signals(adapter_name, trade_date);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表5: debate_results — 辩论裁决结果
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS debate_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market          TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    debate_signal   TEXT    NOT NULL,
    conviction      REAL,
    participant_signals  TEXT,
    extreme_event   TEXT,
    forward_5d_ret  REAL,
    forward_20d_ret REAL,
    forward_60d_ret REAL,
    is_correct      INTEGER,
    -- is_correct: 1=做对, 0=做错, NULL=未到结算日
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(market, code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_dr_trade_date
    ON debate_results(trade_date);
CREATE INDEX IF NOT EXISTS idx_dr_code_date
    ON debate_results(market, code, trade_date);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表6: market_index — 主要指数日线 (上证/深证/沪深300/创业板等)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_index (
    index_code  TEXT    NOT NULL,
    trade_date  TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    amount      INTEGER,
    chg_pct     REAL,
    PRIMARY KEY (index_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_mi_trade_date
    ON market_index(trade_date);

-- ─────────────────────────────────────────────────────────────────────────────
-- 表7: meta — 数据库元信息
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""

# 导入优化的 PRAGMA
IMPORT_PRAGMAS = """
PRAGMA synchronous = OFF;
PRAGMA journal_mode = MEMORY;
PRAGMA cache_size = -2097152;
PRAGMA temp_store = MEMORY;
PRAGMA locking_mode = NORMAL;
PRAGMA page_size = 4096;
"""

# 正常运行 PRAGMA
NORMAL_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -65536;
PRAGMA temp_store = DEFAULT;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
"""
