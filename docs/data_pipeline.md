# QuantDB 每日数据入库工作流

## 完整数据流

```
交易日 15:00 (北京时间)
    │
    ├─ 15:58  TDX 发布 vipdoc/hsjday.zip（全量历史快照，每日覆盖）
    │
    ├─ 16:05  cronjob 触发 quantdb_update.py
    │             │
    │             ├─ [1/5] wait_for_updated_zip()
    │             │       ├─ HTTP HEAD → Last-Modified
    │             │       ├─ 对比 state 文件中的 last_lm
    │             │       ├─ 未更新? → sleep 10min → 重试（最多等3小时至19:05）
    │             │       └─ 周末: 检查 zip 有无周五大版本更新，无则跳过
    │             │
    │             ├─ [2/5] download_and_extract_zip()
    │             │       └─ 下载(508MB) → 解压到 /mnt/data/金融数据/hsjday/lday/
    │             │
    │             ├─ [3/5] update_from_tdx_file()  ← 核心增量入库
    │             │       ├─ Phase1 (快速): 读文件末32字节获取最后日期
    │             │       │       与 DB 中 (market,code) 的 latest_date 比较
    │             │       │       仅当 file_last > db_date 时进入 Phase2
    │             │       └─ Phase2 (精确): 逐条解析 .day 文件，增量写入 DB
    │             │               batch=5000条 INSERT OR IGNORE
    │             │
    │             ├─ [3.5/5] L1 数据质量验证（自动）
    │             │       ├─ 覆盖率检查（vs 前一交易日，drop > 5% 告警）
    │             │       ├─ 新记录数合理性（<4500 或 >13500 告警）
    │             │       ├─ 最新交易日 OHLC 合法性
    │             │       └─ TDX 文件交叉抽检（随机5股）
    │             │
    │             ├─ [4/5] detect_extreme_events_incremental()
    │             │       ├─ 仅对最近 N 天新增记录检测（避免全量重扫）
    │             │       ├─ W{20,50,100,252} × HIGH/LOW × TOUCH/BREAK
    │             │       └─ INSERT OR IGNORE（自动去重）
    │             │
    │             └─ [5/5] 保存同步状态
    │                     ├─ .tdx_sync_state (last_lm / last_date / last_success)
    │                     └─ meta 表 (last_update)
    │
    └─ 完成 (全程约 2~5 分钟，含 508MB 下载)
```

---

## 存量数据校验策略

### 三层验证体系

| 层级 | 执行频率 | 验证内容 | 耗时 | 用途 |
|------|---------|---------|------|------|
| **L1 增量** | **每次入库后自动** | 覆盖率/OOHC/抽检5股 | <5s | 防入库破坏 |
| **L2 全面** | 每日/每周 | OHLC/跳变/极值统计/覆盖率/gap | ~30s | 异常发现 |
| **L3 深度** | 按需/周级 | TDX 文件逐股逐条对比 | ~5min/50股 | 第三方验证 |

### L1 告警规则（自动阻塞风险）

```
覆盖率下降 > 5%     → 告警 + 挂起极值检测
新记录数 < 4500    → 警告（可能是数据源问题）
新记录数 > 13500   → 警告（可能是重复入库）
OHLC 非法(high<low) → 告警
TDX 交叉不一致      → 警告
```

### L2 告警规则（人工介入）

```
OHLC 近年非法(2000后) > 0条  → 需人工检查（1990s早期允许）
普通股票价格日跳 > 50%      → 需人工确认（ETF/配股分拆除外）
日覆盖率 < 70%              → 告警（大量停牌/退市/数据源问题）
极值事件数异常（低于历史均值50%）→ 可能漏检
```

### L3 不一致处理

```
DB 缺失记录     → 补录 + 日志记录
TDX 缺失记录    → DB 有数据但 TDX 无（TDX 清理过旧数据，以 DB 为准）
价格差异 > 0.02 → 记录不一致，日志，人工复核（通常 TD 解析误差）
```

---

## 第三方数据源验证方案

### 为什么需要第三方验证？

TDX 数据是单一数据源，存在以下风险：
- 数据清洗导致部分历史记录丢失（ETF分红、科创板股票）
- 特定日期的价格修正（复权处理方式不同）
- 交易所数据与 TDX 数据的系统性偏差

### 推荐第三方数据源

| 来源 | 覆盖 | 可靠性 | 可达性 | 说明 |
|------|------|--------|--------|------|
| **akshare** | A股全量 | ⭐⭐⭐ | ❌ 当前不可达 | 免费，最全 |
| **baostock** | A股全量 | ⭐⭐⭐ | ❌ 当前不可达 | 免费，复权数据 |
| **东方财富** | A股+港美股 | ⭐⭐⭐ | ⚠️ 不稳定 | 实时行情 |
| **tushare** | A股全量 | ⭐⭐⭐⭐ | ⚠️ 需要token | 专业数据 |
| **Yahoo Finance** | 港美股为主 | ⭐⭐⭐⭐ | ⚠️ 可能断线 | 港股ADR参考 |
| **新浪/腾讯** | A股实时 | ⭐⭐ | ❌ 当前不可达 | 实时补充 |

### 验证触发条件

```
1. 首次全量入库后：随机抽检 50 股 vs TDX 文件（已执行 L3 ✅）
2. 覆盖率异常时：对比前一交易日记录数 vs baostock/akshare
3. 重大价格事件（涨跌停/配股）：交叉验证 3 日 OHLC
4. 季度末/年末：批量验证指数成分股权重（防止权重数据错误）
```

### 验证执行方式（当前网络条件）

```
当前限制: GitHub/NPM不通，akshare/eastmoney/baostock均不可达
可用方案:
  1. 定期在 /home/ht/github/openocta/ 中运行 Python 脚本，
     通过 New-API 代理访问 akshare（需要 API Key）
  2. 手动验证：用 openocta/gateway 定时抓取关键股票数据
  3. 备选：使用 gate-monitor 的 HTTP 请求能力定期采样验证
```

### 数据差异处理流程

```
发现差异（DB vs 第三方）
    │
    ├─ 差异 < 0.1%: 记录到 validation_log，视为正常波动
    ├─ 差异 0.1%~1%: 记录告警，标记该股需要复核
    └─ 差异 > 1%:
           ├─ 优先相信 TDX（当前主数据源，已通过 L3 验证）
           ├─ 记录到 diff_log (code, date, field, db_val, tdx_val, ext_val)
           └─ 人工复核后决定是否修正
```

---

## 文件清单

```
~/github/htquant/scripts/
├── quantdb_update.py    # 主更新脚本（含 L1 验证）
└── quantdb_validate.py  # 独立验证模块（L1/L2/L3 + gap分析）

状态文件:
/mnt/data/金融数据/quantdb/.tdx_sync_state  # zip Last-Modified 记录
```

## cronjob 配置

```
job_id:    5446691a2ea6
时间:      5 16 * * 1-5  (周一~五 16:05 北京)
工作目录:  /home/ht/github/htquant
日志:      ~/github/htquant/logs/quantdb_update.log
脚本:      quantdb_update.py
```

## 使用示例

```bash
# 正常增量更新（自动 L1 验证）
python3 scripts/quantdb_update.py

# 快速检查 zip 状态
python3 scripts/quantdb_update.py --dry-run

# 强制立即下载（跳过等待）
python3 scripts/quantdb_update.py --force

# 全量极值重扫（罕见）
python3 scripts/quantdb_update.py --full-scan

# 独立验证
python3 scripts/quantdb_validate.py --level 1         # 增量验证
python3 scripts/quantdb_validate.py --level 2 --days 30  # 全面验证
python3 scripts/quantdb_validate.py --level 3 --limit 50 # 深度验证
python3 scripts/quantdb_validate.py --analyze-gaps       # gap分析
python3 scripts/quantdb_validate.py --stock 000001       # 单股诊断
python3 scripts/quantdb_validate.py --recent 10          # 最近10交易日
```

---

## 数据库当前状态（2026-05-10）

```
stock_daily:       28,837,038 条  11,941 只股  1990-12-19 ~ 2026-05-08
extreme_events:    24,154,104 条  16 种事件类型  W{20,50,100,252}
market_index:      55,794 条  10 大指数
DB 大小:           11.39 GB
最近交易日覆盖:    9,406 只 / 11,941 只 = 78.8%
L3 验证（50股）:   ✅ 21,530 条 TDX 记录，0 不一致
```