"""
Shark 2.0 — Paper Trading Engine.
Simplified virtual exchange for strategy testing.
Tracks positions, PnL, fees, and supports market/limit orders.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TAKER_FEE = 0.0005
MAKER_FEE = 0.00015


@dataclass
class Position:
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    leverage: int = 1
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    opened_at: float = field(default_factory=time.time)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    amount: float
    price: Optional[float]
    leverage: int
    entry_context: Dict[str, Any] = field(default_factory=dict)


class PaperEngine:
    """Virtual exchange for backtesting and paper trading."""

    def __init__(self, initial_balance: float = 100.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.equity = initial_balance
        self.positions: Dict[str, Position] = {}
        self.orders: List[Order] = []
        self.trade_history: List[Dict] = []
        self.latest_prices: Dict[str, float] = {}
        self.accumulated_fees: float = 0.0
        self.realized_pnl: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def update_price(self, symbol: str, price: float):
        self.latest_prices[symbol] = price
        self._recalculate_pnl(symbol)

    def get_price(self, symbol: str) -> float:
        return self.latest_prices.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def create_order(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        order_type: str = "market",
        amount: float = 0.0,
        price: Optional[float] = None,
        leverage: int = 1,
        reduce_only: bool = False,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        entry_context: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Execute an order immediately (paper matching)."""
        current_price = price or self.get_price(symbol)
        if current_price <= 0:
            return {"status": "error", "message": f"No price for {symbol}"}

        oid = str(uuid.uuid4())[:12]
        fee_rate = TAKER_FEE

        if reduce_only:
            return self._close_position(symbol, side, amount, current_price, entry_context or {})

        # Open new position
        notional = amount * current_price
        margin = notional / leverage
        fee = notional * fee_rate

        if self.balance < margin + fee:
            return {"status": "error", "message": f"Insufficient balance: need {margin+fee:.2f}, have {self.balance:.2f}"}

        pos_side = "long" if side.lower() == "buy" else "short"
        pos = Position(
            symbol=symbol,
            side=pos_side,
            size=amount,
            entry_price=current_price,
            leverage=leverage,
            margin_used=margin,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        self.positions[symbol] = pos
        self.balance -= fee
        self.accumulated_fees += fee
        self.total_trades += 1

        order = Order(
            order_id=oid,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=current_price,
            leverage=leverage,
            entry_context=entry_context or {},
        )
        self.orders.append(order)

        return {
            "status": "ok",
            "order_id": oid,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": current_price,
            "notional": notional,
            "margin": margin,
            "fee": fee,
            "leverage": leverage,
        }

    def _close_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        ctx: Dict,
    ) -> Dict[str, Any]:
        """Close an existing position."""
        pos = self.positions.get(symbol)
        if not pos:
            return {"status": "error", "message": f"No position for {symbol}"}

        close_size = min(amount, pos.size) if amount > 0 else pos.size
        fee = close_size * price * TAKER_FEE

        if pos.side == "long":
            pnl = close_size * (price - pos.entry_price)
        else:
            pnl = close_size * (pos.entry_price - price)

        realized_net = pnl - fee

        pos.size -= close_size
        self.balance += pos.margin_used * (close_size / (close_size + pos.size + 1e-12))
        self.balance += realized_net
        self.accumulated_fees += fee
        self.realized_pnl += realized_net

        if realized_net > 0:
            self.winning_trades += 1

        self.trade_history.append({
            "symbol": symbol,
            "side": pos.side,
            "entry": pos.entry_price,
            "exit": price,
            "size": close_size,
            "pnl": pnl,
            "fee": fee,
            "realized_net": realized_net,
            "time": time.time(),
        })

        if pos.size <= 1e-12:
            del self.positions[symbol]

        self._recalculate_equity()
        return {
            "status": "ok",
            "symbol": symbol,
            "realized_pnl": pnl,
            "realized_net": realized_net,
            "fee": fee,
        }

    # ------------------------------------------------------------------
    # PnL
    # ------------------------------------------------------------------
    def _recalculate_pnl(self, symbol: str):
        pos = self.positions.get(symbol)
        if not pos:
            return
        price = self.get_price(symbol)
        if price <= 0:
            return
        if pos.side == "long":
            pos.unrealized_pnl = pos.size * (price - pos.entry_price)
        else:
            pos.unrealized_pnl = pos.size * (pos.entry_price - price)
        self._recalculate_equity()

    def _recalculate_equity(self):
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        self.equity = self.balance + unrealized

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def total_margin(self) -> float:
        return sum(p.margin_used for p in self.positions.values())

    def get_status(self) -> Dict[str, Any]:
        return {
            "balance": self.balance,
            "equity": self.equity,
            "positions": len(self.positions),
            "realized_pnl": self.realized_pnl,
            "accumulated_fees": self.accumulated_fees,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "total_margin": self.total_margin,
        }
