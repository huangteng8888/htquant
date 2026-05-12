# 股票代码与名称关联设计

## 设计目标

1. **代码 → 名称**：给定 `market+code`，快速查到当前名称
2. **名称历史**：任何历史日期，都能查到当时的名称（支持回溯分析）
3. **市场归属**：`sh`/`sz`/`bj` 三市场全量覆盖，含 ETF/债券/指数/期货/期权
4. **来源可靠**：以 baostock 为权威源，可追溯名称变更日期

---

## 表结构设计

### 1. stock_info — 当前基础信息（主表）

```sql
CREATE TABLE stock_info (
    market       TEXT    NOT NULL,          -- 'sh' | 'sz' | 'bj'
    code         TEXT    NOT NULL,          -- 6位代码，e.g. '000001'
    name         TEXT,                       -- 当前名称
    list_date    TEXT,                       -- 上市日期，e.g. '1991-04-03'
    delist_date  TEXT,                       -- 退市日期，未退市则 NULL
    stock_type   TEXT,                       -- 'A股' | 'B股' | 'ETF' | '债券' | '指数' | '期货' | '期权'
    industry_gb  TEXT,                       -- 证监会行业分类（GB/T 4754）
    industry_cs  TEXT,                       -- 申万行业分类
    status       TEXT    DEFAULT '上市',     -- '上市' | '退市' | '暂停'
    last_updated TEXT,                       -- 最后更新时间
    PRIMARY KEY (market, code)
);
```

**特点**：
- 以 `(market, code)` 为主键，与 `stock_daily`/`extreme_events` 一致
- `name` 字段存**当前最新名称**
- 名称变更时自动记录到 `stock_name_history`

---

### 2. stock_name_history — 名称变更历史（append-only）

```sql
CREATE TABLE stock_name_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market          TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT    NOT NULL,       -- 历史名称
    effective_date  TEXT    NOT NULL,       -- 名称生效日期（上市日或变更日）
    end_date        TEXT,                   -- 名称失效日期（下一变更日或退市日）
    change_type     TEXT,                   -- 'IPO' | 'rename' | 'delist' | 'suspend'
    source          TEXT    DEFAULT 'baostock',
    UNIQUE(market, code, effective_date, change_type)
);

CREATE INDEX idx_name_history_code ON stock_name_history(market, code, effective_date);
```

**特点**：
- `effective_date` + `end_date` 形成时间段，支持"某天某股叫什么名字"的历史查询
- `end_date = NULL` 表示当前在用名称
- 所有变更有迹可循，支持**事件驱动回溯**（如"该股更名期间是否恰好有极值事件"）

**历史查询示例**：
```sql
-- 查询 000001 在任意日期的名称
SELECT name, effective_date, end_date
FROM stock_name_history
WHERE market='sz' AND code='000001'
  AND effective_date <= '2019-01-01'
  AND (end_date IS NULL OR end_date > '2019-01-01')
ORDER BY effective_date DESC LIMIT 1;
```

---

### 3. index_constituents — 指数成分股权重

```sql
CREATE TABLE index_constituents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    index_code     TEXT    NOT NULL,       -- 'sh000300' 沪深300
    index_name     TEXT    NOT NULL,       -- 'hs300'
    mkt_code       TEXT,                   -- 'sh000300'
    market         TEXT    NOT NULL,       -- 成分股市场
    code           TEXT    NOT NULL,       -- 成分股代码
    weight         REAL,                   -- 权重（%）
    effective_date TEXT    NOT NULL,       -- 生效日期
    UNIQUE(index_code, market, code, effective_date)
);
```

**包含指数**：
- `sh000016` 上证50
- `sh000300` 沪深300
- `sh000905` 中证500

---

### 4. market_index — 主要指数日线行情

```sql
CREATE TABLE market_index (
    id           TEXT NOT NULL,    -- 'sh000001'（市场+代码）
    trade_date   TEXT NOT NULL,    -- '2026-05-08'
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    change_pct   REAL,
    PRIMARY KEY (id, trade_date)
);
```

---

## 与行情数据的关联方式

```
stock_info (market, code, name)
     │  ↕ 通过 (market, code) JOIN
stock_daily (market, code, trade_date, OHLCV)
     │  ↕ 同一 (market, code, trade_date)
extreme_events (market, code, trade_date, event_type)
     │
     └─ 同一 (market, code) 的名称查 stock_name_history
```

**统一查询**：
```sql
-- 查某日某股的名称（名称可能已变更）
SELECT d.trade_date, d.code, h.name as current_name,
       nh.name as name_at_date,
       d.close, e.event_type
FROM stock_daily d
JOIN stock_info si ON si.market=d.market AND si.code=d.code
LEFT JOIN stock_name_history nh
    ON nh.market=d.market AND nh.code=d.code
    AND nh.effective_date <= d.trade_date
    AND (nh.end_date IS NULL OR nh.end_date > d.trade_date)
LEFT JOIN extreme_events e
    ON e.market=d.market AND e.code=d.code AND e.trade_date=d.trade_date
WHERE d.trade_date BETWEEN '2024-01-01' AND '2026-05-08'
ORDER BY d.trade_date;
```

---

## 增量更新策略

### 每日 16:05 cronjob 自动执行

```
populate_stock_info.py
    │
    ├─ [1] 从 QuantDB stock_daily 获取当前存在的所有 (market,code)
    │       → 已有 12,146 只，无需枚举
    │
    ├─ [2] 逐股查询 baostock query_stock_basic
    │       → ~77ms/股 × 12,146 ≈ 15 分钟
    │       → 首次全量后，每日增量（仅变化的数百只）约 1 分钟
    │
    ├─ [3] 对比 stock_info 中已有名称 vs 百度新名称
    │       → 有变化 → 追加 name_history 记录 + 更新 stock_info.name
    │       → 无变化 → 跳过
    │
    └─ [4] 更新 stock_info 的 status/list_date/delist_date
```

### 名称变更自动追踪

```python
# 伪代码：名称变更检测逻辑
old_name = conn.execute(
    "SELECT name FROM stock_info WHERE market=? AND code=?", (mkt, code)
).fetchone()[0]

if old_name and old_name != new_name:
    # 1. 关闭旧记录
    conn.execute("""
        UPDATE stock_name_history
        SET end_date = ?
        WHERE market=? AND code=? AND end_date IS NULL
    """, (today, mkt, code))

    # 2. 插入新记录
    conn.execute("""
        INSERT INTO stock_name_history
            (market, code, name, effective_date, change_type)
        VALUES (?, ?, ?, ?, 'rename')
    """, (mkt, code, new_name, today))

    print(f"名称变更: {mkt}{code} {old_name} → {new_name}")
```

---

## 历史名称覆盖方式

### 方式 A：定期快照（推荐，适合初期填充）

每月末对全量股票调用一次 `populate_stock_info.py`，系统自动维护 name_history。

### 方式 B：一次性回溯填充

对于历史上已退市/更名的股票，baostock 在 `query_stock_basic` 的 `outDate` 字段只记录退市日期，名称历史需要在 `list_date ~ outDate` 区间内通过其他数据源补充。

**推荐回溯来源**（当前网络可达性）：
1. **akshare**（需通过 openocta/gateway）— 包含历史名称
2. **tushare**（需token）— 包含历史名称变更记录
3. **手动整理** — 对于重点持仓股票可手动补充

### 方式 C：懒加载（运行时实时查询）

在回测/分析时若发现 stock_name_history 中无记录，再从 baostock 实时拉取并缓存。

---

## QuantDB 当前 stock_info 状态（2026-05-11）

```
stock_info:        0 条（等待 populate_stock_info.py 首次全量填充）
stock_name_history: 未创建（首次填充后自动建表）
```

**填充进度**：background process running，12,146 只 × 77ms ≈ 15 分钟

---

## 使用示例

```python
from htquant.quantdb import QuantDB
db = QuantDB()

# 查询某只股票的当前名称
info = db.conn.execute("""
    SELECT market, code, name, list_date, stock_type, status
    FROM stock_info WHERE market='sh' AND code='600000'
""").fetchone()
print(f"{info[0]}{info[1]} {info[2]}")  # sh600000 浦发银行

# 查询某只股票在某历史日期的名称
def name_at_date(conn, mkt, code, date):
    row = conn.execute("""
        SELECT name FROM stock_name_history
        WHERE market=? AND code=? AND effective_date <= ?
          AND (end_date IS NULL OR end_date > ?)
        ORDER BY effective_date DESC LIMIT 1
    """, (mkt, code, date, date)).fetchone()
    return row[0] if row else None

print(name_at_date(db.conn, 'sh', '600000', '2015-01-01'))  # 浦发银行
print(name_at_date(db.conn, 'sh', '600000', '2000-01-01'))  # 可能有历史名称

# 查询某只ETF的成分股（通过 index_constituents）
constituents = db.conn.execute("""
    SELECT ic.market, ic.code, si.name, ic.weight
    FROM index_constituents ic
    JOIN stock_info si ON si.market=ic.market AND si.code=ic.code
    WHERE ic.index_code='sh000300'
      AND ic.effective_date = (
          SELECT MAX(effective_date) FROM index_constituents
          WHERE index_code='sh000300'
      )
    ORDER BY ic.weight DESC
    LIMIT 10
""").fetchall()
for m, c, n, w in constituents:
    print(f"  {w:.2f}% {m}{c} {n}")
```