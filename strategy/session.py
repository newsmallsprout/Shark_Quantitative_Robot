"""Session 持久化：余额/持仓/交易记录的保存恢复与重置。"""

from __future__ import annotations

import json
import os
import asyncio
import logging

_log = logging.getLogger(__name__)


class SessionMixin:
    """Mixin: 需要宿主类提供 balance, equity, positions, _trade_history, _persistence, _initial_capital 等属性。"""

    def switch_mode(self, mode: str) -> dict:
        from core.live import create_live_engine
        from api.routes import get_state
        _state = get_state()
        mode = mode.strip().lower()
        if mode not in ("paper", "live"):
            return {"error": f"无效模式: {mode}"}
        
        current_is_live = bool(getattr(self, '_live', None))
        target_is_live = (mode == "live")
        if current_is_live == target_is_live:
            return {"ok": True, "mode": mode, "balance": self.balance}

        if mode == "live":
            engine = create_live_engine(mode="live")
            if engine is None:
                return {"error": "实盘引擎初始化失败，请检查 GATE_API_KEY/SECRET 和网络"}
            
            # --- 备份模拟盘数据到内存 ---
            self._paper_positions = dict(self.positions)
            self._paper_trade_history = list(self._trade_history)
            self._paper_open_timestamps = dict(self._open_timestamps)
            self._paper_balance = self.balance
            self._paper_equity = self.equity
            self._paper_initial_capital = getattr(self, '_initial_capital', self.balance)
            self._paper_realized_pnl = getattr(self, 'realized_pnl', 0.0)
            self._paper_gross_realized = getattr(self, 'gross_realized', 0.0)
            self._paper_total_fees = getattr(self, 'total_fees', 0.0)
            self._paper_total_slippage = getattr(self, 'total_slippage', 0.0)
            self._paper_trades = getattr(self, 'trades', 0)
            self._paper_closed_trades = getattr(self, 'closed_trades', 0)
            self._paper_wins = getattr(self, 'wins', 0)

            # --- 实盘初始化测试与提现变量 ---
            self._live_test_status = "pending"
            self._last_transfer_pnl = getattr(self, 'realized_pnl', 0.0)

            # --- 自动开启实盘交易（继承当前配置或强制开启） ---
            self._live_trading_enabled = True

            self._clear_trading_session_state(clear_redis_history=False)
            self._live = engine
            try:
                exchange_total = engine.get_balance()
                locked = sum(p.get("margin", 0) for p in self.positions.values())
                self.balance = exchange_total - locked
                self._initial_capital = self.balance
                _state["initial_capital"] = self.balance
                _state["balance"] = self.balance
                _state["free_cash"] = self.balance
                _state["equity"] = self.balance
            except Exception:
                pass
            print(f"🔥 已切换到实盘模式 (余额=${self.balance:.2f})")
        else:
            self._live = None
            self._live_trading_enabled = False
            # 手动切回模拟盘时只切模式，不自动恢复交易开关
            self._paper_trading_enabled = False
            self._clear_trading_session_state(clear_redis_history=False)
            
            if hasattr(self, '_paper_balance'):
                self.balance = self._paper_balance
                self.equity = self._paper_equity
                self._initial_capital = self._paper_initial_capital
                self.realized_pnl = self._paper_realized_pnl
                self.gross_realized = self._paper_gross_realized
                self.total_fees = self._paper_total_fees
                self.total_slippage = self._paper_total_slippage
                self.trades = self._paper_trades
                self.closed_trades = self._paper_closed_trades
                self.wins = self._paper_wins
                
                self.positions = dict(getattr(self, '_paper_positions', {}))
                self._trade_history = list(getattr(self, '_paper_trade_history', []))
                self._open_timestamps = dict(getattr(self, '_paper_open_timestamps', {}))
                
                _state["balance"] = self.balance
                _state["equity"] = self.equity
                _state["free_cash"] = self.balance
                _state["initial_capital"] = self._initial_capital
                _state["realized_pnl"] = self.realized_pnl
                _state["gross_realized"] = self.gross_realized
                _state["total_fees"] = self.total_fees
                _state["total_slippage"] = self.total_slippage
                _state["trades"] = self.trades
                _state["wins"] = self.wins
                _state["trade_history"] = self._trade_history
                _state["positions"] = len(self.positions)
                _state["position_list"] = list(self.positions.values())
                _state["paper_trading"] = False
                _state["live_trading"] = False
            else:
                self._load_paper_state()
                _state["paper_trading"] = False
                _state["live_trading"] = False
            
            print(f"📋 已切换到模拟盘模式 (余额=${self.balance:.2f})")
        return {"ok": True, "mode": mode, "balance": self.balance}

    def _reset_paper(self, capital: float) -> None:
        from api.routes import get_state
        _state = get_state()
        self._clear_trading_session_state(clear_redis_history=True)
        self._paper_balance = capital
        self._paper_equity = capital
        self.balance = capital
        self.equity = capital
        self.static_equity = capital
        self.peak_static_equity = capital
        self._initial_capital = capital
        self.realized_pnl = 0.0
        self.gross_realized = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.trades = 0
        self.closed_trades = 0
        self.wins = 0
        self._alt_evo.clear()
        self._loss_replay_guard.clear()
        self._price_replan_last.clear()
        _state.update(balance=capital, equity=capital, free_cash=capital, initial_capital=capital,
                       realized_pnl=0.0, gross_realized=0.0, total_fees=0.0, total_slippage=0.0,
                       trades=0, wins=0, win_rate=0.0, margin_locked=0.0, positions=0,
                       position_list=[], trade_history=[])
        self._save_paper_state()
        self._clear_paper_db_records()
        self.balance = capital
        self.equity = capital
        print(f"[模拟盘] 已重置，初始资金=${capital:.2f}")

    def _force_close_all_positions(self, prices: dict) -> None:
        from api.routes import get_state
        _state = get_state()
        if not self.positions:
            return
        syms = list(self.positions.keys())
        count = 0
        for sym in syms:
            px = prices.get(sym, 0)
            if px <= 0:
                pos = self.positions.get(sym)
                px = pos["entry"] if pos else 0
            if px <= 0:
                continue
            try:
                self._close_position(sym, px, "进程关闭强平", 0, prices)
                count += 1
            except Exception as e:
                _log.error("shutdown close %s failed: %s", sym, e)
        self.balance = self._initial_capital + self.realized_pnl
        self.equity = self.balance
        self.static_equity = self.balance
        _state.update(balance=self.balance, equity=self.balance, free_cash=self.balance,
                       margin_locked=0.0, positions=0, position_list=[])
        self._save_paper_state()
        print(f"[关闭] 已强平 {count} 个持仓, 余额=${self.balance:.2f}")

    async def shutdown(self, prices: dict) -> None:
        self._force_close_all_positions(prices)
        await asyncio.sleep(0.5)

    def _clear_paper_db_records(self) -> None:
        if not self._persistence or not self._persistence.enabled_db():
            return
        try:
            async def _go():
                from persistence.session import create_engine_and_sessionmaker
                from sqlalchemy import text
                engine, _ = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL", "postgresql://shark:shark@db:5432/shark"))
                async with engine.begin() as conn:
                    await conn.execute(text("DELETE FROM balance_logs"))
                    await conn.execute(text("DELETE FROM trades"))
                    await conn.execute(text("DELETE FROM orders"))
                await engine.dispose()
                _log.info("paper db records cleared")
            asyncio.get_event_loop().create_task(_go())
        except Exception as e:
            _log.warning("clear paper db records failed: %s", e)

    def _save_paper_state(self) -> None:
        try:
            from api.routes import get_state
            _state = get_state()
            current_mode = str(_state.get("shark_mode", "paper") or "paper").lower()
            if current_mode == "live" or bool(getattr(self, "_live_trading_enabled", False)):
                return
            import redis as _redis
            _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            locked = sum(p["margin"] for p in self.positions.values())
            state = {
                "source_mode": "paper",
                "balance": self.balance + locked,
                "equity": self.equity,
                "static_equity": self.static_equity,
                "peak_static_equity": self.peak_static_equity,
                "initial_capital": self._initial_capital,
                "realized_pnl": self.realized_pnl,
                "gross_realized": self.gross_realized,
                "total_fees": self.total_fees,
                "total_slippage": self.total_slippage,
                "trades": self.trades,
                "closed_trades": self.closed_trades,
                "wins": self.wins,
                "trade_history": self._trade_history,
                "positions": self.positions,
                "open_timestamps": self._open_timestamps,
            }
            _r.set("shark:paper_state", json.dumps(state, ensure_ascii=False, default=str))
        except Exception as e:
            _log.warning("save paper state failed: %s", e)

    def _load_paper_state(self) -> bool:
        from api.routes import get_state
        _state = get_state()
        try:
            import redis as _redis
            _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            raw = _r.get("shark:paper_state")
            if not raw:
                return False
            state = json.loads(raw)
            if state.get("source_mode") != "paper":
                _log.warning("skip restoring legacy/ambiguous paper_state: source_mode=%s", state.get("source_mode"))
                return False
            self.balance = float(state.get("balance", self._initial_capital))
            self.equity = float(state.get("equity", self._initial_capital))
            self.static_equity = float(state.get("static_equity", self._initial_capital))
            self.peak_static_equity = float(state.get("peak_static_equity", self._initial_capital))
            self._initial_capital = float(state.get("initial_capital", self._initial_capital))
            self.realized_pnl = float(state.get("realized_pnl", 0))
            self.gross_realized = float(state.get("gross_realized", 0))
            self.total_fees = float(state.get("total_fees", 0))
            self.total_slippage = float(state.get("total_slippage", 0))
            self.trades = int(state.get("trades", 0))
            self.closed_trades = int(state.get("closed_trades", 0))
            self.wins = int(state.get("wins", 0))
            saved_history = state.get("trade_history", [])
            if isinstance(saved_history, list):
                self._trade_history = saved_history
            
            saved_positions = state.get("positions", {})
            if isinstance(saved_positions, dict):
                self.positions = saved_positions
                
            saved_open_ts = state.get("open_timestamps", {})
            if isinstance(saved_open_ts, dict):
                self._open_timestamps = saved_open_ts
                
            _state["positions"] = len(self.positions)
            _state["position_list"] = list(self.positions.values())
            _state["margin_locked"] = sum(p.get("margin", 0) for p in self.positions.values())
            _state["trade_history"] = self._trade_history
            _state["balance"] = self.balance
            _state["equity"] = self.equity
            _state["free_cash"] = self.balance
            _state["initial_capital"] = self._initial_capital
            _state["realized_pnl"] = self.realized_pnl
            _state["gross_realized"] = self.gross_realized
            _state["total_fees"] = self.total_fees
            _state["total_slippage"] = self.total_slippage
            _state["trades"] = self.trades
            _state["wins"] = self.wins
            print(f"[启动] 已恢复 {len(self._trade_history)} 条交易记录")
            return True
        except Exception as e:
            _log.warning("load paper state failed: %s", e)
            return False

    def _clear_trading_session_state(self, *, clear_redis_history: bool = False) -> None:
        from api.routes import get_state
        _state = get_state()
        self.positions.clear()
        self._trade_history.clear()
        self._open_timestamps.clear()
        self._ai_signal_cache.clear()
        self._regime_cache.clear()
        self._loss_replay_guard.clear()
        self._price_replan_last.clear()
        self.realized_pnl = 0.0
        self.gross_realized = 0.0
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.trades = 0
        self.closed_trades = 0
        self.wins = 0
        _state.update(positions=0, position_list=[], trade_history=[],
                       realized_pnl=0.0, gross_realized=0.0, total_fees=0.0,
                       total_slippage=0.0, margin_locked=0.0)
        if clear_redis_history:
            try:
                import redis as _redis
                _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
                _r.delete("shark:trade_history")
            except Exception as e:
                _log.error("clear paper redis history failed: %s", e)
