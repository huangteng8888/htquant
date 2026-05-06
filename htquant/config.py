"""
htquant 配置模块
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# qlib 数据路径
QLIB_DATA_PATH = os.environ.get(
    "QLIB_DATA_PATH",
    str(Path.home() / ".qlib/qlib_data/cn_data_new2")
)

# 各量化项目路径
@dataclass
class ProjectPaths:
    qlib: str = str(Path.home() / "github/qlib")
    backtrader: str = str(Path.home() / "github/backtrader")
    backtrader_env: str = str(Path.home() / "github/backtrader-env")
    finrl: str = str(Path.home() / "github/FinRL")
    freqtrade: str = str(Path.home() / "github/freqtrade")
    vnpy: str = str(Path.home() / "github/vnpy")

PROJECT_PATHS = ProjectPaths()

# 持仓周期定义
@dataclass
class HorizonConfig:
    short_term_days: int = 5      # 短线 < 1周
    medium_term_days: int = 22    # 中线 1周~1月
    long_term_days: int = 65      # 长线 > 3月

HORIZON = HorizonConfig()

# 策略类型
STRATEGY_TYPES = [
    "trend_following",    # 趋势跟踪 (backtrader MA策略)
    "mean_reversion",     # 均值回归 (qlib RSI)
    "momentum",           # 动量策略
    "value_investing",    # 价值投资
    "sentiment",          # 情绪分析
    "factor_quant",       # 因子量化 (qlib)
    "rl_trading",         # 强化学习 (FinRL)
]

# 操作信号
SIGNALS = ["买入", "增持", "持有", "减持", "清仓", "观望"]

# 辩论配置
DEBATE_CONFIG = {
    "max_rounds": 3,           # 最多辩论轮数
    "confidence_threshold": 0.7,  # 置信度阈值，超过则停止辩论
    "convergence_window": 2,   # 连续2轮一致则收敛
}

# 支持的股票代码格式
STOCK_CODE_MAPPING = {
    "000001": ("sz000001", "平安银行"),
    "000901": ("sz000901", "航天科技"),
    "300777": ("sz300777", "中简科技"),
    "688089": ("sh688089", "嘉必优"),
    "300896": ("sz300896", "爱美客"),
    "301071": ("sz301071", "力量钻石"),
    "600422": ("sh600422", "昆药集团"),
    "300363": ("sz300363", "博腾股份"),
    # 可扩展更多...
}

# 默认分析的7只验证股票
DEFAULT_STOCKS = ['000901', '300777', '688089', '300896', '301071', '600422', '300363']

def get_qcode(stock_code: str) -> Optional[tuple]:
    """将6位股票代码转为qlib格式 (qcode, name)"""
    return STOCK_CODE_MAPPING.get(stock_code)

def ensure_dirs():
    """确保必要目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

ensure_dirs()
