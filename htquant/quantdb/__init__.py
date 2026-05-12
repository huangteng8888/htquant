# -*- coding: utf-8 -*-
"""
quantdb — 量化统一数据库

路径: /mnt/data/金融数据/quantdb/
├── quantdb.sqlite     # 主数据库 (SQLite, ~4GB, 2800万条)
├── quantdb_duckdb.duckdb  # 分析库 (DuckDB, 可选)
└── parquet/           # 列式存储归档

用法:
    from htquant.quantdb import QuantDB
    db = QuantDB()

    # 全量导入 (首次运行, 约35分钟)
    db.import_all()

    # 查询单只股票
    df = db.get_daily_df('000001', '2024-01-01', '2025-01-01')

    # 全市场快照
    snap = db.get_market_snapshot('2025-05-09')

    # 极值事件检测
    db.detect_extreme_events()

    # 记录 adapter 信号
    db.log_signal('qlib', '000001', '2025-05-09', '增持', 0.78)

    # 记录辩论结果
    db.log_debate('000001', '2025-05-09', '清仓', 0.82,
                  {'qlib': '清仓', 'momentum': '观望'},
                  extreme_event='W50_HIGH_TOUCH', forward_20=-0.06)
"""

from .core import QuantDB
from .schema import *

__all__ = ['QuantDB']
