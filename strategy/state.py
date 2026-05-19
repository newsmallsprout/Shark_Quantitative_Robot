import math
from typing import List, Dict, Any
def _finite_float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default






def _position_list_for_state(runner: "StrategyRunner", prices: Dict[str, float]) -> List[dict]:
    out: List[dict] = []
    for sym, pos in runner.positions.items():
        display_entry = _finite_float(pos.get("display_entry", pos.get("entry", 0)))
        display_margin = _finite_float(pos.get("display_margin", pos.get("margin", 0)))
        display_leverage = _finite_float(pos.get("display_leverage", pos.get("leverage", 0)))
        px = display_entry
        if sym in prices:
            px = _finite_float(prices[sym], px)
        unrealized = _finite_float(pos.get("display_unrealized_pnl"), runner._gross_pnl_usd(sym, pos, px))
        pnl_pct = unrealized / max(display_margin, 1e-9) * 100
        out.append({
            "symbol": sym,
            "side": pos["side"],
            "size": _finite_float(pos["size"]),
            "entry_price": display_entry,
            "leverage": display_leverage,
            "margin": display_margin,
            "unrealized_pnl": _finite_float(unrealized),
            "pnl_pct": _finite_float(pnl_pct),
            "current_price": px,
            "entry_risk_tag": str(pos.get("entry_risk_tag", "")),
        })
    return out
def _trade_history_for_state(runner: "StrategyRunner") -> List[dict]:
    rows: List[dict] = []
    for t in runner._trade_history:
        rows.append({
            "symbol": t.get("symbol", ""),
            "side": t.get("side", ""),
            "entry_price": _finite_float(t.get("entry_price")),
            "exit_price": _finite_float(t.get("exit_price")),
            "size": _finite_float(t.get("size")),
            "leverage": _finite_float(t.get("leverage")),
            "margin": _finite_float(t.get("margin")),
            "realized_pnl": _finite_float(t.get("realized_pnl")),
            "pnl_pct": _finite_float(t.get("pnl_pct")),
            "reason": str(t.get("reason", "")),
            "fee_open": _finite_float(t.get("fee_open")),
            "fee_close": _finite_float(t.get("fee_close")),
            "gross_pnl": _finite_float(t.get("gross_pnl")),
            "opened_at": _finite_float(t.get("opened_at")),
            "closed_at": _finite_float(t.get("closed_at")),
        })
    return rows

import time, json
from api.routes import get_state
"""State update and equity recalculation."""
from typing import Dict

class StateMixin:
    def _recalc_equity(self, prices):
        locked = sum(p["margin"] for p in self.positions.values())
        unrealized = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos["entry"])
            unrealized += self._gross_pnl_usd(sym, pos, px)
        self.equity = self.balance + locked + unrealized

    def _fund_snapshot(self, prices: Dict[str, float]) -> Dict[str, float]:
        self._recalc_equity(prices)
        locked = sum(p["margin"] for p in self.positions.values())
        total_balance = self.balance + locked
        unrealized = self.equity - self.balance - locked
        return {
            "equity": self.equity,
            "free_cash": self.balance,
            "total_balance": total_balance,
            "margin_locked": locked,
            "unrealized": unrealized,
        }

    def _update_state(self, prices):
        self._recalc_equity(prices)
        locked = sum(p["margin"] for p in self.positions.values())
        uc = self._initial_capital
        # 余额 = 可用资金 + 锁定保证金（真实总资金）
        total_balance = self.balance + locked
        unrealized = self.equity - self.balance - locked

        get_state()["equity"] = self.equity
        get_state()["balance"] = total_balance  # 总资金净额
        get_state()["free_cash"] = self.balance  # 可用余额
        get_state()["initial_capital"] = uc
        get_state()["unrealized_pnl"] = unrealized
        get_state()["realized_pnl"] = self.realized_pnl
        get_state()["win_rate"] = self.wins / max(self.closed_trades, 1)  # 基于已平仓
        get_state()["positions"] = len(self.positions)
        get_state()["trades"] = self.trades
        get_state()["wins"] = self.wins
        get_state()["symbol_count"] = len(prices)
        get_state()["total_fees"] = self.total_fees
        get_state()["gross_realized"] = self.gross_realized  # 毛利累计

        # 定期快照到磁盘（崩溃恢复用，每10秒）
        if int(time.time()) % 10 == 0:
            try:
                from execution.prod_utils import save_snapshot
                save_snapshot(get_state())
            except Exception:
                pass
            # 持久化模拟盘状态到 Redis
            self._save_paper_state()
        get_state()["total_slippage"] = self.total_slippage
        get_state()["trade_history"] = _trade_history_for_state(self)
        get_state()["margin_locked"] = locked
        get_state()["position_list"] = _position_list_for_state(self, prices)

        fuse_active = bool(self._plan_gate and self._plan_gate.is_fused)
        get_state()["safety_blocked"] = fuse_active
        fr = ""
        if fuse_active and self._plan_gate:
            fr = str(getattr(self._plan_gate, "fuse_reason", "") or "")
        get_state()["fuse_reason"] = fr

        live_api_ok = True
        if self._live and self._live.active:
            live_api_ok = bool(self._live.is_healthy)
        get_state()["live_api_ok"] = live_api_ok

        block: Optional[dict] = None
        if fuse_active:
            block = {
                "code": "price_fuse",
                "detail": (fr[:300] if fr else "1分钟价格波动>3%，约5分钟内暂停新开仓"),
                "ts": time.time(),
            }
        elif not live_api_ok:
            block = {
                "code": "live_api",
                "detail": "Gate 连续报单错误≥3次，已暂停新开仓（持仓仍按价格管理）",
                "ts": time.time(),
            }
        elif self._last_tick_block:
            block = dict(self._last_tick_block)
        get_state()["last_tick_block"] = block

        if self._plan_gate:
            try:
                raw = self._plan_gate._redis.get("shark:planning:status")
                if raw:
                    get_state()["planning_status"] = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            except Exception:
                pass

        # 反思器状态供API/战报使用
        if self._reflector:
            get_state()["reflect"] = {
                "summary": self._reflector.summary(),
                "ai_boost": self._reflector.ai_boost,
                "stop_boost": self._reflector.stop_boost,
            }

        # 实盘状态
        if self._live:
            # 保留 toggle API 写入的 trading_enabled（竞态修复：get_state() 是唯一真相源）
            _prev_trading = get_state().get("live", {}).get("trading_enabled")
            get_state()["live"] = self._live.stats()
            if _prev_trading is not None:
                get_state()["live"]["trading_enabled"] = _prev_trading
            try:
                get_state()["live"]["balance"] = self._live.get_balance()
            except Exception:
                get_state()["live"]["balance"] = 0
            # 实盘模式：余额走交易所，但初始资金不变
            if self._live.active:
                get_state()["balance"] = get_state()["live"]["balance"]
                get_state()["equity"] = get_state()["live"]["balance"]
                get_state()["free_cash"] = get_state()["live"]["balance"]
                get_state()["unrealized_pnl"] = 0
                get_state()["margin_locked"] = sum(
                    p.get("margin", 0) for p in self._live.sync_positions(update_timestamp=False).values()
                ) if self._live else 0
                get_state()["trade_history"] = self._live.get_close_history(limit=100)

        # 待审批的进化修改：get_state() 是唯一真相源，tick从get_state()同步
        self._pending_evo_changes = get_state().get("evo_pending", [])

        if self._persistence:
            self._persistence.schedule_state_redis(
                {k: get_state()[k] for k in (
                    "equity", "balance", "free_cash", "initial_capital",
                    "unrealized_pnl", "realized_pnl", "win_rate", "positions",
                    "trades", "wins", "total_fees", "gross_realized", "margin_locked",
                    "symbol_count",
                ) if k in get_state()}
            )
