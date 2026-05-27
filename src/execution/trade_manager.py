"""Shim: TradeManager lives in trading/ for v25; re-exported for v24 import paths."""
from trading.trade_manager import TradeManager  # noqa: F401

__all__ = ["TradeManager"]
