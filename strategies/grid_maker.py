"""
Grid Maker Strategy — passivbot-style martingale grid with ATR spacing,
trailing take-profit, and 15-minute unstuck timer.

Philosophy:
  - Initial entry uses 1% of equity as margin.
  - Three DCA tiers triggered by adverse price movement:
      Tier 0 (initial): 1%   equity margin, 1x  multiplier
      Tier 1:           2%   adverse,        2x  multiplier
      Tier 2:           5%   adverse,        3x  multiplier
      Tier 3:           10%  adverse,        5x  multiplier
  - Grid spacing is ATR-based so tier distances scale with volatility.
  - Take-profit uses a trailing mechanism: track the best price seen and
    exit when price retraces 30% from that extreme.
  - If a position lives longer than 15 minutes without triggering TP/SL,
    force-close at market (unstuck).
  - async run() provides a background monitoring loop that checks DCA
    conditions, updates trailing stops, and enforces the unstuck timer.

Config keys (YAML / config_manager):
  grid_maker:
    enabled: true
    symbols: ["BTC/USDT", "ETH/USDT"]
    initial_margin_frac: 0.01          # 1% of equity
    tier1_adverse_pct: 0.02            # 2% adverse
    tier2_adverse_pct: 0.05            # 5% adverse
    tier3_adverse_pct: 0.10            # 10% adverse
    tier1_margin_mult: 2.0
    tier2_margin_mult: 3.0
    tier3_margin_mult: 5.0
    trailing_tp_retrace_pct: 0.30      # 30% retrace from extreme
    unstuck_minutes: 15
    atr_period: 14
    atr_spacing_mult: 1.0              # multiplier on ATR for grid step
    max_positions_per_symbol: 1
    cooldown_sec: 5.0
    leverage: 10
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategy.base import BaseStrategy
from src.core.events import TickEvent, SignalEvent
from src.core.paper_engine import paper_engine
from src.core.risk_engine import risk_engine
from src.core.config_manager import config_manager
from src.core.globals import bot_context
from src.utils.logger import log
from src.utils.atr import compute_atr_from_candles


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class GridState:
    """Per-symbol state for one grid-martingale position."""
    symbol: str
    side: str = "buy"                        # "buy"=long, "sell"=short
    entry_price: float = 0.0
    entry_time: float = 0.0
    base_margin: float = 0.0                 # initial margin used
    total_contracts: float = 0.0
    tier: int = 0                            # 0 = initial, 1/2/3 = DCA
    avg_entry_price: float = 0.0             # volume-weighted average
    best_price: float = 0.0                  # extreme favourable price seen
    entry_ref_price: float = 0.0             # reference for adverse calc
    last_dca_ts: float = 0.0
    trailing_tp_price: float = 0.0           # current trailing stop level
    tp_triggered: bool = False
    unstuck_deadline: float = 0.0            # epoch seconds


@dataclass
class GridConfig:
    """Runtime config snapshot loaded from config_manager."""
    enabled: bool = False
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT"])
    initial_margin_frac: float = 0.01
    tier1_adverse_pct: float = 0.02
    tier2_adverse_pct: float = 0.05
    tier3_adverse_pct: float = 0.10
    tier1_margin_mult: float = 2.0
    tier2_margin_mult: float = 3.0
    tier3_margin_mult: float = 5.0
    trailing_tp_retrace_pct: float = 0.30
    unstuck_minutes: float = 15.0
    atr_period: int = 14
    atr_spacing_mult: float = 1.0
    max_positions_per_symbol: int = 1
    cooldown_sec: float = 5.0
    leverage: int = 10


def _load_grid_config() -> GridConfig:
    """Read grid_maker section from config_manager or return safe defaults."""
    cfg = GridConfig()
    try:
        gc = getattr(config_manager.get_config(), "grid_maker", None)
        if gc is None:
            return cfg
        cfg.enabled = bool(getattr(gc, "enabled", False))
        cfg.symbols = list(getattr(gc, "symbols", ["BTC/USDT"]) or ["BTC/USDT"])
        cfg.initial_margin_frac = float(getattr(gc, "initial_margin_frac", 0.01) or 0.01)
        cfg.tier1_adverse_pct = float(getattr(gc, "tier1_adverse_pct", 0.02) or 0.02)
        cfg.tier2_adverse_pct = float(getattr(gc, "tier2_adverse_pct", 0.05) or 0.05)
        cfg.tier3_adverse_pct = float(getattr(gc, "tier3_adverse_pct", 0.10) or 0.10)
        cfg.tier1_margin_mult = float(getattr(gc, "tier1_margin_mult", 2.0) or 2.0)
        cfg.tier2_margin_mult = float(getattr(gc, "tier2_margin_mult", 3.0) or 3.0)
        cfg.tier3_margin_mult = float(getattr(gc, "tier3_margin_mult", 5.0) or 5.0)
        cfg.trailing_tp_retrace_pct = float(getattr(gc, "trailing_tp_retrace_pct", 0.30) or 0.30)
        cfg.unstuck_minutes = float(getattr(gc, "unstuck_minutes", 15.0) or 15.0)
        cfg.atr_period = int(getattr(gc, "atr_period", 14) or 14)
        cfg.atr_spacing_mult = float(getattr(gc, "atr_spacing_mult", 1.0) or 1.0)
        cfg.max_positions_per_symbol = int(getattr(gc, "max_positions_per_symbol", 1) or 1)
        cfg.cooldown_sec = float(getattr(gc, "cooldown_sec", 5.0) or 5.0)
        cfg.leverage = int(getattr(gc, "leverage", 10) or 10)
    except Exception:
        pass
    return cfg


def _adverse_frac(side: str, ref_price: float, current_price: float) -> float:
    """Return adverse move fraction (>=0)."""
    ref = max(float(ref_price), 1e-12)
    cur = float(current_price)
    if str(side).lower() in ("buy", "long"):
        return max(0.0, (ref - cur) / ref)
    return max(0.0, (cur - ref) / ref)


def _trailing_tp_level(side: str, best_price: float, retrace_pct: float) -> float:
    """Compute the trailing stop price from the best extreme."""
    bp = float(best_price)
    r = max(0.0, min(1.0, float(retrace_pct)))
    if str(side).lower() in ("buy", "long"):
        return bp * (1.0 - r)
    return bp * (1.0 + r)


def _is_tp_hit(side: str, current_price: float, tp_level: float) -> bool:
    if str(side).lower() in ("buy", "long"):
        return current_price <= tp_level
    return current_price >= tp_level


# ---------------------------------------------------------------------------
# GridMakerStrategy
# ---------------------------------------------------------------------------

class GridMakerStrategy(BaseStrategy):
    """
    Passivbot-inspired martingale grid strategy.

    - Watches symbols via on_tick for price updates.
    - async run() is the background loop that evaluates DCA conditions,
      trailing TP, and the 15-minute unstuck deadline.
    - Emits SignalEvents which flow through the engine's risk + sizing pipeline.
    """

    def __init__(self) -> None:
        super().__init__("GridMaker")
        self._cfg: GridConfig = _load_grid_config()
        # Per-symbol grid state keyed by symbol
        self._grids: Dict[str, GridState] = {}
        # Price window for ATR calc (rolling OHLC-style via tick last prices)
        self._price_windows: Dict[str, Deque[float]] = {}
        self._price_timestamps: Dict[str, Deque[float]] = {}
        # Cooldown / fire control
        self._last_signal_ts: Dict[str, float] = {}
        # Background task handle
        self._run_task: Optional[asyncio.Task] = None
        # Cached ATR per symbol  (period in ticks ≈ period * avg_ticks_per_candle)
        self._atr_cache: Dict[str, float] = {}
        self._atr_ts: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _refresh_cfg(self) -> None:
        self._cfg = _load_grid_config()

    @property
    def cfg(self) -> GridConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # ATR estimation from tick-level price window
    # ------------------------------------------------------------------

    def _update_price_window(self, symbol: str, price: float) -> None:
        now = time.time()
        if symbol not in self._price_windows:
            self._price_windows[symbol] = deque(maxlen=200)
            self._price_timestamps[symbol] = deque(maxlen=200)
        self._price_windows[symbol].append(float(price))
        self._price_timestamps[symbol].append(now)

    def _estimate_atr_pct(self, symbol: str, last_price: float) -> float:
        """
        Approximate ATR as percentage from a rolling tick window.
        Uses max-min range over the last N ticks as a rough substitute.
        Falls back to risk_engine's symbol_atr_pct if available.
        """
        pw = self._price_windows.get(symbol)
        if pw and len(pw) >= 10:
            chunk = list(pw)[-14:]
            hi = max(chunk)
            lo = min(chunk)
            if last_price > 0:
                return (hi - lo) / last_price
        # fallback
        ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
        if ap > 0:
            return ap
        return 0.005  # default 0.5%

    def _min_grid_step(self, symbol: str, last_price: float) -> float:
        """ATR-based minimum adverse move required between DCA tiers."""
        atr_pct = self._estimate_atr_pct(symbol, last_price)
        return max(atr_pct * self.cfg.atr_spacing_mult, 0.002)  # floor 0.2%

    # ------------------------------------------------------------------
    # Position / state helpers
    # ------------------------------------------------------------------

    def _get_grid_state(self, symbol: str) -> Optional[GridState]:
        return self._grids.get(symbol)

    def _has_open_grid(self, symbol: str) -> bool:
        gs = self._grids.get(symbol)
        if gs is None:
            return False
        # also check paper_engine for actual position
        pos = paper_engine.positions.get(symbol)
        if pos and float(pos.get("size", 0.0) or 0.0) > 1e-12:
            return True
        # stale grid state with no paper position: clean up
        if gs.total_contracts <= 1e-12:
            self._grids.pop(symbol, None)
            return False
        return True

    def _ensure_grid_for_symbol(self, symbol: str) -> GridState:
        if symbol not in self._grids:
            self._grids[symbol] = GridState(symbol=symbol)
        return self._grids[symbol]

    # ------------------------------------------------------------------
    # Signal emission
    # ------------------------------------------------------------------

    def _emit_entry(
        self,
        symbol: str,
        side: str,
        price: float,
        margin: float,
        leverage: int,
        gs: GridState,
        dca_tier: int = 0,
    ) -> None:
        """Emit an opening or DCA entry signal."""
        notional = margin * float(leverage)
        cs = paper_engine.contract_size_for_symbol(symbol)
        contracts = notional / max(price * cs, 1e-12)
        if contracts <= 1e-12:
            return

        # use best bid/ask for limit entries when possible
        bb, ba = paper_engine._best_bid_ask(symbol)
        if side == "buy":
            limit_px = bb if bb > 0 else price * 0.9995
        else:
            limit_px = ba if ba > 0 else price * 1.0005

        if dca_tier > 0:
            # DCA entries use market orders for speed
            order_type = "market"
            entry_px = price
        else:
            order_type = "limit"
            entry_px = limit_px

        ctx = {
            "strategy": self.name,
            "grid_maker": True,
            "grid_tier": dca_tier,
            "grid_entry_price": float(price),
            "grid_base_margin": float(margin),
            "trailing_tp_retrace_pct": float(self.cfg.trailing_tp_retrace_pct),
            "position_silo": "GRID_MAKER",
            "take_profit_limit_price": float(gs.trailing_tp_price) if gs.trailing_tp_price > 0 else 0.0,
        }
        if gs.entry_ref_price > 0:
            ctx["grid_ref_price"] = float(gs.entry_ref_price)

        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=entry_px if order_type == "limit" else price,
                amount=float(contracts),
                leverage=int(leverage),
                post_only=(order_type == "limit"),
                margin_mode="cross",
                entry_context=ctx,
            )
        )
        log.warning(
            f"[GridMaker] {side.upper()} {symbol} tier={dca_tier} "
            f"price≈{price:.4f} margin≈{margin:.2f}U lev={leverage}x "
            f"contracts≈{contracts:.6f}"
        )

    def _emit_exit(self, symbol: str, gs: GridState, reason: str) -> None:
        """Emit a reduce-only close for the entire grid position."""
        pos = paper_engine.positions.get(symbol)
        if not pos or float(pos.get("size", 0.0) or 0.0) <= 1e-12:
            return
        pos_side = str(pos.get("side", "long")).lower()
        close_side = "sell" if pos_side == "long" else "buy"
        amt = abs(float(pos["size"]))
        lev = max(1, int(pos.get("leverage", self.cfg.leverage) or self.cfg.leverage))

        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side=close_side,
                order_type="market",
                price=0.0,
                amount=amt,
                leverage=lev,
                reduce_only=True,
                margin_mode="cross",
                entry_context={
                    "strategy": self.name,
                    "grid_maker": True,
                    "exit_reason": reason,
                    "grid_tier": gs.tier,
                },
            )
        )
        log.warning(
            f"[GridMaker] EXIT {symbol} reason={reason} "
            f"size={amt:.6f} avg_entry={gs.avg_entry_price:.4f}"
        )

    # ------------------------------------------------------------------
    # DCA logic
    # ------------------------------------------------------------------

    def _dca_tier_params(self, tier_index: int) -> Tuple[float, float]:
        """Return (adverse_pct_threshold, margin_multiplier) for tier."""
        tier = max(0, int(tier_index))
        if tier == 0:
            return 0.0, 1.0
        if tier == 1:
            return self.cfg.tier1_adverse_pct, self.cfg.tier1_margin_mult
        if tier == 2:
            return self.cfg.tier2_adverse_pct, self.cfg.tier2_margin_mult
        return self.cfg.tier3_adverse_pct, self.cfg.tier3_margin_mult

    def _check_dca_conditions(
        self,
        symbol: str,
        last_price: float,
        gs: GridState,
        now: float,
    ) -> bool:
        """
        Return True if we should fire a DCA entry.
        Conditions:
          - Tier < 3 (max 3 DCA fills)
          - Adverse move >= next tier threshold (ATR-adjusted)
          - Cooldown elapsed since last DCA
        """
        if gs.tier >= 3:
            return False

        next_tier = gs.tier + 1
        adv_threshold, _ = self._dca_tier_params(next_tier)
        if adv_threshold <= 0:
            return False

        # ensure minimum grid step from ATR
        min_step = self._min_grid_step(symbol, last_price)
        adv_threshold = max(adv_threshold, gs.tier * min_step)

        ref = float(gs.entry_ref_price) if gs.entry_ref_price > 0 else float(gs.entry_price)
        if ref <= 0:
            return False

        adv = _adverse_frac(gs.side, ref, last_price)
        if adv < adv_threshold - 1e-9:
            return False

        cd = max(0.5, self.cfg.cooldown_sec)
        if now - gs.last_dca_ts < cd:
            return False

        return True

    def _apply_dca(
        self,
        symbol: str,
        last_price: float,
        gs: GridState,
        now: float,
    ) -> None:
        """Execute one DCA tier entry."""
        next_tier = gs.tier + 1
        _, margin_mult = self._dca_tier_params(next_tier)

        eq = max(float(risk_engine.current_balance or 0.0), 1e-9)
        add_margin = gs.base_margin * margin_mult
        # cap additive margin to available balance
        add_margin = min(add_margin, eq * 0.15)

        # emit DCA entry
        self._emit_entry(
            symbol=symbol,
            side=gs.side,
            price=last_price,
            margin=add_margin,
            leverage=self.cfg.leverage,
            gs=gs,
            dca_tier=next_tier,
        )

        # update state
        gs.tier = next_tier
        gs.last_dca_ts = now

        # update weighted average entry
        old_avg = float(gs.avg_entry_price) if gs.avg_entry_price > 0 else float(gs.entry_price)
        old_qty = float(gs.total_contracts)
        # approximate new contracts from margin
        cs = paper_engine.contract_size_for_symbol(symbol)
        new_notional = add_margin * self.cfg.leverage
        new_qty = new_notional / max(last_price * cs, 1e-12)

        if old_qty + new_qty > 1e-12:
            gs.avg_entry_price = (
                old_avg * old_qty + last_price * new_qty
            ) / (old_qty + new_qty)
        gs.total_contracts = old_qty + new_qty

    # ------------------------------------------------------------------
    # Trailing TP & unstuck
    # ------------------------------------------------------------------

    def _update_trailing_best(self, gs: GridState, last_price: float) -> None:
        """Update best-price extreme for trailing TP."""
        side = str(gs.side).lower()
        if side in ("buy", "long"):
            if last_price > gs.best_price or gs.best_price <= 0:
                gs.best_price = last_price
        else:
            if last_price < gs.best_price or gs.best_price <= 0:
                gs.best_price = last_price

    def _update_trailing_tp(self, gs: GridState) -> None:
        """Recompute trailing TP level from best_price."""
        if gs.best_price <= 0:
            return
        gs.trailing_tp_price = _trailing_tp_level(
            gs.side,
            gs.best_price,
            self.cfg.trailing_tp_retrace_pct,
        )

    def _check_tp(self, gs: GridState, last_price: float) -> bool:
        if gs.trailing_tp_price <= 0:
            return False
        return _is_tp_hit(gs.side, last_price, gs.trailing_tp_price)

    def _check_unstuck(self, gs: GridState, now: float) -> bool:
        if gs.unstuck_deadline <= 0:
            return False
        return now >= gs.unstuck_deadline

    # ------------------------------------------------------------------
    # Entry trigger
    # ------------------------------------------------------------------

    def _should_enter(self, symbol: str, last_price: float, now: float) -> Tuple[bool, str]:
        """Return (enter, side) — basic entry gating for initial position."""
        if self._has_open_grid(symbol):
            return False, "buy"
        if symbol not in self.cfg.symbols:
            return False, "buy"
        # cooldown from last signal
        if now - self._last_signal_ts.get(symbol, 0.0) < self.cfg.cooldown_sec:
            return False, "buy"
        # simple directional heuristic: RSI-like from price window
        pw = self._price_windows.get(symbol)
        if pw and len(pw) >= 20:
            recent = list(pw)[-10:]
            older = list(pw)[-20:-10]
            if len(recent) >= 5 and len(older) >= 5:
                recent_avg = sum(recent) / len(recent)
                older_avg = sum(older) / len(older)
                if recent_avg < older_avg:
                    return True, "buy"   # price falling → go long
                else:
                    return True, "sell"  # price rising → go short
        # default: go long
        return True, "buy"

    # ------------------------------------------------------------------
    # Tick handler
    # ------------------------------------------------------------------

    async def on_tick(self, event: TickEvent) -> None:
        """Lightweight tick handler: maintain price window + ATR estimates."""
        symbol = event.symbol
        last_price = float(event.ticker.get("last", 0.0))
        if last_price <= 0:
            return
        self._update_price_window(symbol, last_price)

        # feed risk_engine ATR (help other strategies)
        pw = self._price_windows.get(symbol)
        if pw and len(pw) >= 5:
            chunk = list(pw)[-14:]
            atr_pct = max((max(chunk) - min(chunk)) / last_price, 1e-6)
            risk_engine.record_symbol_atr_pct(symbol, atr_pct)

    # ------------------------------------------------------------------
    # Background run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main background loop.
        Evaluates entry, DCA, trailing TP, and unstuck every tick cycle.
        """
        log.info("[GridMaker] Background run() started")
        self._refresh_cfg()

        while True:
            try:
                if not self.cfg.enabled:
                    await asyncio.sleep(5.0)
                    self._refresh_cfg()
                    continue

                now = time.time()
                symbols_to_scan = list(set(
                    list(self.cfg.symbols) +
                    list(self._grids.keys())
                ))

                for symbol in symbols_to_scan:
                    last_price = float(paper_engine.latest_prices.get(symbol, 0.0) or 0.0)
                    if last_price <= 0:
                        continue

                    # --- grid state management ---
                    gs = self._get_grid_state(symbol)
                    has_open = self._has_open_grid(symbol)

                    if has_open and gs is not None:
                        # update trailing best
                        self._update_trailing_best(gs, last_price)
                        self._update_trailing_tp(gs)

                        # check unstuck
                        if self._check_unstuck(gs, now):
                            log.warning(
                                f"[GridMaker] UNSTUCK {symbol} "
                                f"age={now - gs.entry_time:.0f}s "
                                f"tier={gs.tier}"
                            )
                            self._emit_exit(symbol, gs, "unstuck_timeout")
                            self._grids.pop(symbol, None)
                            self._last_signal_ts[symbol] = now
                            continue

                        # check trailing TP
                        if self._check_tp(gs, last_price):
                            log.warning(
                                f"[GridMaker] TRAILING TP {symbol} "
                                f"best={gs.best_price:.4f} "
                                f"tp={gs.trailing_tp_price:.4f} "
                                f"last={last_price:.4f}"
                            )
                            self._emit_exit(symbol, gs, "trailing_take_profit")
                            self._grids.pop(symbol, None)
                            self._last_signal_ts[symbol] = now
                            continue

                        # check DCA conditions
                        if self._check_dca_conditions(symbol, last_price, gs, now):
                            self._apply_dca(symbol, last_price, gs, now)
                            self._last_signal_ts[symbol] = now

                    elif not has_open:
                        # clean up stale state
                        if gs is not None and gs.total_contracts <= 1e-12:
                            self._grids.pop(symbol, None)

                        # check entry conditions
                        enter, side = self._should_enter(symbol, last_price, now)
                        if not enter:
                            continue

                        eq = max(float(risk_engine.current_balance or 0.0), 1e-9)
                        margin = eq * self.cfg.initial_margin_frac
                        if margin < 2.0:  # minimum 2 USDT margin
                            continue

                        # create grid state
                        gs = self._ensure_grid_for_symbol(symbol)
                        gs.side = side
                        gs.entry_price = last_price
                        gs.entry_time = now
                        gs.base_margin = margin
                        gs.tier = 0
                        gs.avg_entry_price = last_price
                        gs.best_price = last_price
                        gs.entry_ref_price = last_price
                        gs.trailing_tp_price = _trailing_tp_level(
                            side, last_price, self.cfg.trailing_tp_retrace_pct
                        )
                        gs.unstuck_deadline = now + self.cfg.unstuck_minutes * 60.0
                        gs.tp_triggered = False
                        gs.last_dca_ts = 0.0

                        self._emit_entry(
                            symbol=symbol,
                            side=side,
                            price=last_price,
                            margin=margin,
                            leverage=self.cfg.leverage,
                            gs=gs,
                            dca_tier=0,
                        )
                        self._last_signal_ts[symbol] = now

                # Periodic config refresh
                self._refresh_cfg()
                await asyncio.sleep(0.5)  # 500ms loop interval

            except asyncio.CancelledError:
                log.info("[GridMaker] run() cancelled")
                break
            except Exception as e:
                log.error(f"[GridMaker] run() error: {e}")
                await asyncio.sleep(2.0)

    async def start_background(self) -> None:
        """Called by engine to start the background monitoring loop."""
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
