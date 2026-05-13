# -*- coding: utf-8 -*-
"""
TradingAgents Adapter — Multi-Agent LLM Trading Framework

继承自 BaseAdapter，通过 TauricResearch/TradingAgents 的多Agent系统
（Research Manager → Trader → Portfolio Manager）生成信号。

信号映射 (PortfolioRating 5-tier → htquant 5-tier):
  Buy        → 增持    (强烈看多)
  Overweight → 买入    (适度看多)
  Hold       → 持有
  Underweight→ 减持    (适度看空)
  Sell       → 清仓    (强烈看空)

使用方式:
  1. 预计算批量信号:  TradingAgentsAdapter.batch_precompute(...)
  2. 回测: adapter = TradingAgentsAdapter(cache_path='/tmp/ta_signals.db')
  3. 实时: adapter = TradingAgentsAdapter(live=True)

依赖:
  pip install stockstats yfinance
  export OPENAI_API_KEY 等
"""

import os
import json
import sqlite3
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np

from ..dispatcher import Query, ProjectResult
from ..config import PROJECT_PATHS, STOCK_CODE_MAPPING
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# ─── Signal Mapping ───────────────────────────────────────────────────────────

RATING_TO_SIGNAL = {
    'Buy':         '增持',
    'Overweight':  '买入',
    'Hold':        '持有',
    'Underweight': '减持',
    'Sell':        '清仓',
}

RATING_CONFIDENCE = {
    'Buy':         0.82,
    'Overweight':  0.72,
    'Hold':        0.55,
    'Underweight': 0.72,
    'Sell':        0.82,
}

# ─── Adapter ─────────────────────────────────────────────────────────────────

class TradingAgentsAdapter(BaseAdapter):
    """
    TradingAgents 多Agent信号适配器。
    
    支持两种模式:
    - 回测模式 (默认): 读预计算的 SQLite 缓存，无缓存返回"观望"
    - 实时模式 (live=True): 调用 LLM Agent 生成信号（每次 10-30 秒）
    
    预计算示例:
      TradingAgentsAdapter.batch_precompute(
          ['000001', '000901'],
          '2023-01-01', '2025-06-01',
          cache_path='/tmp/ta_signals.db',
          max_workers=2)
    """
    
    DEFAULT_CACHE = '/tmp/ta_signals.db'
    
    def __init__(self, project_path: str = "", cache_path: str = "",
                 live: bool = False):
        super().__init__(project_path)
        self._project_path = project_path or PROJECT_PATHS.tradingagents
        self._cache_path  = cache_path   or self.DEFAULT_CACHE
        self._live        = live
        self._conn: Optional[sqlite3.Connection] = None
        
        # 延迟初始化 LLM 客户端（避免启动时加载）
        self._ta_graph = None
    
    def _check_available(self) -> bool:
        """检查 TradingAgents 依赖是否可用"""
        try:
            # 不导入完整模块（太重），只检查关键路径
            ta_path = Path(self._project_path)
            if not ta_path.exists():
                logger.warning(f"[TradingAgents] 项目路径不存在: {self._project_path}")
                return False
            
            # 检查 data cache 目录
            cache_dir = Path(self._cache_path).parent
            cache_dir.mkdir(parents=True, exist_ok=True)
            
            # 初始化 SQLite 表
            self._init_db()
            
            logger.info(f"[TradingAgents] 适配器就绪，缓存: {self._cache_path}")
            return True
            
        except Exception as e:
            logger.warning(f"[TradingAgents] 不可用: {e}")
            return False
    
    def _init_db(self):
        conn = sqlite3.connect(self._cache_path, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ta_signals (
                stock_code TEXT NOT NULL,
                date_str    TEXT NOT NULL,
                rating      TEXT NOT NULL,
                reason      TEXT,
                confidence  REAL,
                computed_at TEXT,
                PRIMARY KEY (stock_code, date_str)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ta_lookup
            ON ta_signals(stock_code, date_str)
        """)
        conn.commit()
        conn.close()
    
    def _ensure_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self._cache_path, timeout=10)
    
    def _load_llm_client(self):
        """延迟加载 LLM 客户端（仅实时模式）"""
        if self._ta_graph is not None:
            return
        
        try:
            import sys
            sys.path.insert(0, str(self._project_path))
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG
            
            config = DEFAULT_CONFIG.copy()
            config['max_debate_rounds'] = 1
            # 使用便宜的模型
            config['deep_think_llm']  = os.environ.get('TA_DEEP_MODEL',  'gpt-4.1-mini')
            config['quick_think_llm'] = os.environ.get('TA_QUICK_MODEL', 'gpt-4.1-mini')
            # 默认用 yfinance（无需额外 API key）
            config['data_vendors'] = {
                "core_stock_apis":     "yfinance",
                "technical_indicators":"yfinance",
                "fundamental_data":   "yfinance",
                "news_data":          "yfinance",
            }
            
            self._ta_graph = TradingAgentsGraph(debug=False, config=config)
            logger.info("[TradingAgents] LLM 客户端初始化成功")
        except ImportError as e:
            raise ImportError(
                f"[TradingAgents] 缺少依赖: {e}。"
                "请安装: pip install stockstats yfinance -f "
                "~/github/TradingAgents"
            )
    
    def _parse_rating_from_decision(self, decision_text: str) -> tuple:
        """从 TradingAgents 输出的 markdown 中解析 rating"""
        for line in decision_text.split('\n'):
            line = line.strip()
            if '**Rating**' in line or '**rating**' in line:
                for r in RATING_TO_SIGNAL:
                    if r in line:
                        return r, line
        
        # 备用：搜索 Action 行
        for line in decision_text.split('\n'):
            if '**Action**' in line or 'BUY' in line.upper() or 'SELL' in line.upper():
                if 'Buy' in line:   return 'Buy', line
                if 'Sell' in line:  return 'Sell', line
                if 'Hold' in line:  return 'Hold', line
        
        return 'Hold', ''
    
    def _call_llm(self, stock_code: str, date_str: str) -> Dict[str, Any]:
        """调用 TradingAgents LLM（实时模式，每次 10-30 秒）"""
        self._load_llm_client()
        
        # 格式化股票代码（转换为 Yahoo Finance 格式）
        ticker = STOCK_CODE_MAPPING.get(stock_code, stock_code)
        if not any(c.isdigit() for c in ticker):
            ticker = f"{ticker}.SS"  # 上海
        
        try:
            _, decision = self._ta_graph.propagate(ticker, date_str)
            rating, reason_raw = self._parse_rating_from_decision(decision)
            
            return {
                'rating':     rating,
                'reason':     decision[:800],
                'confidence': RATING_CONFIDENCE.get(rating, 0.55),
            }
        except Exception as e:
            logger.error(f"[TradingAgents] API 调用失败 {stock_code}/{date_str}: {e}")
            return {'rating': 'Hold', 'reason': f'API错误: {str(e)[:100]}',
                    'confidence': 0.50}

    def _call_rest_api_fallback(self, stock_code: str) -> Dict[str, Any]:
        """缓存空时，从 AI-Trader REST API 获取最新信号作为 fallback"""
        try:
            import httpx
            # 获取该股票的最新信号
            url = f"http://localhost:8000/api/signals/grouped?limit=10"
            resp = httpx.get(url, timeout=10.0)
            if resp.status_code != 200:
                return {'rating': None, 'reason': f'AI-Trader API错误:{resp.status_code}', 'confidence': 0.0}

            data = resp.json()
            # 在 agents 信号中找匹配该股票的
            ticker = STOCK_CODE_MAPPING.get(stock_code, stock_code)
            for agent_data in data.get('agents', []):
                for sig in agent_data.get('signals', []):
                    sym = sig.get('symbol', '')
                    if ticker.upper().replace('.SS', '').replace('.SZ', '') in sym.upper().replace('.SS', '').replace('.SZ', ''):
                        rating = sig.get('rating', 'Hold')
                        reason = sig.get('reason', sig.get('message', ''))[:300]
                        return {
                            'rating': rating,
                            'reason': f"[TA-REST:{rating}] {reason}",
                            'confidence': RATING_CONFIDENCE.get(rating, 0.55),
                        }
            return {'rating': None, 'reason': 'AI-Trader缓存空且无匹配信号', 'confidence': 0.0}
        except Exception as e:
            return {'rating': None, 'reason': f'REST fallback失败:{str(e)[:60]}', 'confidence': 0.0}

    def execute(self, query: Query) -> ProjectResult:
        """
        执行查询（来自 dispatcher）。
        
        实时查询时调用 LLM（慢），批量回测时应使用 batch_precompute + 历史信号接口。
        """
        stock = query.stock_codes[0] if query.stock_codes else ''
        date_str = query.metadata.get('date_str', datetime.now().strftime('%Y-%m-%d'))
        
        if self._live:
            result = self._call_llm(stock, date_str)
            signal = RATING_TO_SIGNAL.get(result['rating'], '持有')
            return ProjectResult(
                project_name='tradingagents',
                success=True,
                data=result,
                signal=signal,
                confidence=result.get('confidence', 0.55),
                reason=f"[TA:{result['rating']}] {result.get('reason','')[:300]}",
            )
        
        # 回测/缓存模式
        try:
            self._ensure_conn()
            row = self._conn.execute(
                "SELECT rating,reason,confidence FROM ta_signals "
                "WHERE stock_code=? AND date_str=?",
                (stock, date_str)).fetchone()
            
            if row is None:
                # Fallback 1: 尝试从 AI-Trader REST API 获取
                rest_result = self._call_rest_api_fallback(stock)
                if rest_result.get('rating'):
                    signal = RATING_TO_SIGNAL.get(rest_result['rating'], '持有')
                    return ProjectResult(
                        project_name='tradingagents',
                        success=True,
                        data={'rating': rest_result['rating'], 'source': 'rest_api'},
                        signal=signal,
                        confidence=rest_result.get('confidence', 0.50),
                        reason=rest_result.get('reason', ''),
                    )
                return ProjectResult(
                    project_name='tradingagents',
                    success=True,
                    data=None,
                    error=f'缓存无数据: {stock}/{date_str}',
                    signal='观望',
                    confidence=0.45,
                    reason='TradingAgents预计算缓存无数据，REST API也无匹配信号（标记为低权重）',
                )
            
            rating, reason, confidence = row
            signal = RATING_TO_SIGNAL.get(rating, '持有')
            return ProjectResult(
                project_name='tradingagents',
                success=True,
                data={'rating': rating},
                signal=signal,
                confidence=confidence or RATING_CONFIDENCE.get(rating, 0.55),
                reason=f"[TA评级:{rating}] {reason[:200]}" if reason else f"[TA评级:{rating}]",
            )
        except Exception as e:
            return ProjectResult(
                project_name='tradingagents',
                success=False,
                data=None,
                error=str(e),
                signal='观望',
                confidence=0.50,
            )
    
    def historical_signal(self, stock_code: str, date_str: str,
                           hist_data: dict = None) -> Dict[str, Any]:
        """
        回测流水线专用接口 — 读缓存或返回观望。

        不调用 LLM，直接查 SQLite。
        hist_data 参数保留（兼容性），但 TradingAgents 不使用它。
        """
        try:
            self._ensure_conn()
            row = self._conn.execute(
                "SELECT rating,reason,confidence FROM ta_signals "
                "WHERE stock_code=? AND date_str=?",
                (stock_code, date_str)).fetchone()

            if row is None:
                return {'signal': '观望', 'confidence': 0.50,
                        'reason': 'TA缓存无数据', 'rating': None}

            rating, reason, confidence = row
            return {
                'signal':     RATING_TO_SIGNAL.get(rating, '持有'),
                'confidence': confidence or RATING_CONFIDENCE.get(rating, 0.55),
                'reason':     f"[TA:{rating}] {reason[:200]}" if reason else f"[TA:{rating}]",
                'rating':     rating,
            }
        except Exception as e:
            return {'signal': '观望', 'confidence': 0.50, 'reason': str(e), 'rating': None}

    def _apply_extreme_adjustment(self, signal: str, reason: str,
                                   extreme_event: str, rsi: float) -> tuple:
        """极值事件调整（TradingAgents 的LLM评级在极值时需要修正）"""
        if not extreme_event:
            return signal, reason

        is_short = extreme_event and extreme_event.startswith(('W20_', 'W50_'))

        if is_short and '_LOW' in extreme_event and rsi < 50:
            if signal in ('增持', '买入'):
                return '减持', reason + ' [TA极值:W20/W50触低，做空]'
            elif signal == '持有':
                return '减持', reason + ' [TA极值:W20/W50触低，做空]'

        elif is_short and '_HIGH' in extreme_event:
            if signal in ('增持', '买入'):
                return '清仓', reason + ' [TA极值:HIGH禁止做多]'
            elif signal in ('观望', '持有'):
                return '减持', reason + ' [TA极值:HIGH中性转空]'

        elif extreme_event and extreme_event.startswith('W100_') and '_HIGH' in extreme_event:
            if signal in ('增持', '买入', '持有'):
                return '清仓', reason + ' [TA极值:W100触高]'

        return signal, reason
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def __del__(self):
        self.close()
    
    # ─── 批量预计算 ──────────────────────────────────────────────────────────
    
    @staticmethod
    def batch_precompute(stock_codes: list, start_date: str, end_date: str,
                         cache_path: str = "", max_workers: int = 2,
                         max_events_per_stock: int = 2000,
                         dry_run: bool = False) -> pd.DataFrame:
        """
        批量预计算 TradingAgents 信号。
        
        警告: 每次调用约 10-30 秒 LLM API，量大耗时极长。
        建议先用 dry_run=True 测试，再用 max_events_per_stock 限制。
        
        预计算完成后，回测时会自动从 SQLite 缓存读取。
        
        Args:
          stock_codes: 股票代码列表
          start_date:  开始日期 YYYY-MM-DD
          end_date:    结束日期 YYYY-MM-DD
          cache_path:  SQLite 缓存路径
          max_workers: 并发线程数（默认2，避免速率限制）
          max_events_per_stock: 每股票最大事件数
          dry_run:     True=只打印计划不执行
        """
        cache_path = cache_path or TradingAgentsAdapter.DEFAULT_CACHE
        
        dates = pd.bdate_range(start_date, end_date)
        date_strs = [d.strftime('%Y-%m-%d') for d in dates]
        total = len(stock_codes) * len(date_strs)
        
        print(f"\n{'='*60}")
        print(f"TradingAgents 批量预计算计划")
        print(f"{'='*60}")
        print(f"  股票: {len(stock_codes)} 只")
        print(f"  区间: {start_date} ~ {end_date} ({len(date_strs)} 交易日)")
        print(f"  总调用: {total} 次")
        print(f"  预计耗时: {total * 15 / 3600:.1f} 小时 (按每次15秒)")
        print(f"  缓存: {cache_path}")
        print(f"  干跑: {dry_run}")
        print(f"{'='*60}\n")
        
        if dry_run:
            print("干跑模式，无实际调用")
            return pd.DataFrame()
        
        # 初始化数据库
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cache_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ta_signals (
                stock_code TEXT NOT NULL, date_str TEXT NOT NULL,
                rating TEXT NOT NULL, reason TEXT, confidence REAL, computed_at TEXT,
                PRIMARY KEY (stock_code, date_str))
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ta ON ta_signals(stock_code, date_str)")
        conn.commit()
        
        def compute_one(code: str, ds: str) -> tuple:
            """单次计算（在线调用 LLM）"""
            # 检查缓存
            row = conn.execute(
                "SELECT rating FROM ta_signals WHERE stock_code=? AND date_str=?",
                (code, ds)).fetchone()
            if row:
                return code, ds, row[0], 'cached', 0
            
            # 调用 LLM
            try:
                import sys
                sys.path.insert(0, str(Path.home() / 'github/TradingAgents'))
                from tradingagents.graph.trading_graph import TradingAgentsGraph
                from tradingagents.default_config import DEFAULT_CONFIG
                
                config = DEFAULT_CONFIG.copy()
                config['max_debate_rounds'] = 1
                config['deep_think_llm']  = os.environ.get('TA_DEEP_MODEL',  'gpt-4.1-mini')
                config['quick_think_llm'] = os.environ.get('TA_QUICK_MODEL', 'gpt-4.1-mini')
                config['data_vendors'] = {
                    "core_stock_apis":     "yfinance",
                    "technical_indicators":"yfinance",
                    "fundamental_data":   "yfinance",
                    "news_data":          "yfinance",
                }
                
                ticker = STOCK_CODE_MAPPING.get(code, code)
                if not any(c.isdigit() for c in ticker):
                    ticker = f"{ticker}.SS"
                
                ta = TradingAgentsGraph(debug=False, config=config)
                _, decision = ta.propagate(ticker, ds)
                
                # 解析 rating
                rating = 'Hold'
                for line in decision.split('\n'):
                    for r in ['Buy', 'Overweight', 'Hold', 'Underweight', 'Sell']:
                        if f'**{r}' in line or f'**rating**' in line.lower() and r in line:
                            rating = r
                
                return code, ds, rating, 'computed', 1
                
            except Exception as e:
                return code, ds, 'Hold', f'error:{str(e)[:60]}', 0
        
        # 重组为 (code, date_str) 列表
        tasks = []
        for code in stock_codes:
            for ds in date_strs[:max_events_per_stock]:
                tasks.append((code, ds))
        
        print(f"开始计算 {len(tasks)} 个事件（Ctrl+C 中断安全，结果已缓存）...\n")
        
        computed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(compute_one, code, ds): (code, ds)
                       for code, ds in tasks}
            
            for future in as_completed(futures):
                code, ds, rating, status, counted = future.result()
                
                if counted:
                    conn.execute(
                        "INSERT OR REPLACE INTO ta_signals VALUES (?,?,?,?,?,?)",
                        (code, ds, rating, status, RATING_CONFIDENCE.get(rating, 0.55),
                         datetime.now().isoformat()))
                    conn.commit()
                    computed += 1
                    print(f"  [{computed}] {code} {ds} → {rating}")
        
        conn.close()
        
        print(f"\n完成。共计算 {computed} 个新信号，缓存: {cache_path}")
        
        # 返回加载结果
        return TradingAgentsAdapter._load_cache_df(cache_path, stock_codes, start_date, end_date)
    
    @staticmethod
    def _load_cache_df(cache_path: str, stock_codes: list,
                       start_date: str, end_date: str) -> pd.DataFrame:
        conn = sqlite3.connect(cache_path)
        df = pd.read_sql(
            "SELECT * FROM ta_signals WHERE stock_code IN (%s) "
            "AND date_str BETWEEN ? AND ?" % ','.join('?' * len(stock_codes)),
            conn, params=stock_codes + [start_date, end_date])
        conn.close()
        return df
