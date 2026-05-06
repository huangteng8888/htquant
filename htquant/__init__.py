"""
htquant - 量化研究聚合引擎
Multi-Agent Debate Aggregation for Quantitative Research

将同一问题分发给多个量化项目（qlib/backtrader/FinRL/freqtrade/vnpy），
收集各自结论，发现分歧时触发多轮辩论，最终给出综合策略。
"""
__version__ = "0.1.0"

from .dispatcher import Dispatcher
from .aggregator import Aggregator
from .debate import DebateEngine
from .scoring import ScoringEngine

__all__ = ["Dispatcher", "Aggregator", "DebateEngine", "ScoringEngine"]
