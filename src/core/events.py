from dataclasses import dataclass
from typing import Any, Dict, Optional

class EventType:
    TICK = "TICK"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"

@dataclass
class Event:
    type: str

@dataclass
class TickEvent(Event):
    symbol: str
    ticker: Dict[str, Any]
    
    def __init__(self, symbol: str, ticker: Dict[str, Any]):
        self.type = EventType.TICK
        self.symbol = symbol
        self.ticker = ticker

@dataclass
class OrderBookEvent:
    symbol: str
    bids: list
    asks: list
    obi: float  # Order Book Imbalance (-1.0 to 1.0)

@dataclass
class SignalEvent(Event):
    strategy_name: str
    symbol: str
    side: str  # 'buy' or 'sell'
    order_type: str # 'market' or 'limit'
    price: float
    amount: float
    leverage: int
    reduce_only: bool = False
    berserker: bool = False
    post_only: bool = False
    margin_mode: str = "isolated"  # 物理逐仓隔离（全系统默认）
    entry_context: Optional[Dict[str, Any]] = None
    ai_win_rate: Optional[float] = None  # 狙击手：可选；未设置则不分流
    atr_value: Optional[float] = None

    def __init__(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        amount: float,
        leverage: int,
        reduce_only: bool = False,
        berserker: bool = False,
        post_only: bool = False,
        margin_mode: str = "isolated",
        entry_context: Optional[Dict[str, Any]] = None,
        ai_win_rate: Optional[float] = None,
        atr_value: Optional[float] = None,
    ):
        self.type = EventType.SIGNAL
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.price = price
        self.amount = amount
        self.leverage = leverage
        self.reduce_only = reduce_only
        self.berserker = berserker
        self.post_only = post_only
        self.margin_mode = margin_mode
        self.entry_context = entry_context
        self.ai_win_rate = ai_win_rate
        self.atr_value = atr_value
