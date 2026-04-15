from fastapi import FastAPI, HTTPException, Body, UploadFile, File, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from src.core.config_manager import config_manager
from src.utils.logger import log
from src.ai.scorer import ai_scorer
from src.ai.regime import regime_classifier
from src.core.globals import bot_context
from src.config import SystemState
from src.core.risk_engine import berserker_max_leverage_for_symbol, risk_engine
from src.strategy.tuner import strategy_auto_tuner
import os
import shutil
import json
import asyncio
import time
import math
from typing import Optional


def _json_safe(obj):
    """Recursively sanitize payloads so FastAPI/Starlette never sees NaN/Infinity."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return [_json_safe(v) for v in obj]
    return obj


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(_json_safe(content))


app = FastAPI(title="Gate Attack Bot Config Center", default_response_class=SafeJSONResponse)


def _system_state_to_ui_mode(state: SystemState) -> str:
    if state == SystemState.LICENSE_LOCKED:
        return "HALTED"
    if state == SystemState.BERSERKER:
        return "BERSERKER"
    if state == SystemState.ATTACK:
        return "ATTACK"
    return "NEUTRAL"

# --- WebSocket Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(_json_safe(message))
            except Exception as e:
                log.error(f"Error broadcasting to WS client: {e}")
                self.disconnect(connection)

ws_manager = ConnectionManager()

@app.websocket("/ws/market_data")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Do not block forever on client messages — server push is one-way; timeouts keep the loop alive.
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=120.0)
                try:
                    json.loads(data)
                except json.JSONDecodeError:
                    pass
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

def _normalize_positions_for_ui(raw: list) -> list:
    from src.core.paper_engine import PaperTradingEngine, paper_engine

    out = []
    ex = bot_context.get_exchange()
    for p in raw or []:
        try:
            ep = float(p.get("entryPrice", p.get("entry_price", 0)))
            sz = float(p.get("size", 0))
            lev = float(p.get("leverage", 1)) or 1.0
            upnl = float(p.get("unrealizedPnl", p.get("unrealized_pnl", 0)))
            abs_sz = abs(sz)
            cs = float(p.get("contractSize", p.get("contract_size", 0)) or 0.0)
            if cs <= 0:
                cs = 1.0
            notional_open = ep * abs_sz * cs if ep and abs_sz else 0.0
            margin_initial = (notional_open / lev) if lev and notional_open else 0.0
            # 与合约 UI 一致：收益率默认按「初始保证金」口径（ROE）
            pct_on_margin = (upnl / margin_initial * 100) if margin_initial else 0.0
            pct_on_notional = (upnl / notional_open * 100) if notional_open else 0.0
            side_ui = str(p.get("side", "long")).lower()
            sym = str(p.get("symbol", "") or "")
            try:
                spec_cap = 0.0
                if ex and getattr(ex, "contract_specs_cache", None):
                    spec_cap = float((ex.contract_specs_cache.get(sym) or {}).get("leverage_max") or 0.0)
                if spec_cap > 0:
                    lev = min(lev, spec_cap)
            except Exception:
                pass
            rt, rm = paper_engine._fee_rates_for_symbol(sym)
            # 回本参考：Taker 进 + Maker 出；费率优先 Gate 合约 REST 同步值（雷达 sparkline 仍用）
            bep = PaperTradingEngine.break_even_exit_price(side_ui, ep, rt, rm)
            bd = paper_engine.get_display_tp_sl(sym)
            tpp = bd.get("take_profit")
            slp = bd.get("stop_loss")
            out.append({
                "symbol": p.get("symbol", ""),
                "side": p.get("side", "long"),
                "size": sz,
                "entryPrice": ep,
                "contract_size": cs,
                "notional_open_usdt": round(notional_open, 6),
                "initial_margin_usdt": round(margin_initial, 6),
                "take_profit_price": round(tpp, 8) if tpp and tpp > 0 else None,
                "stop_loss_price": round(slp, 8) if slp and slp > 0 else None,
                "break_even_price": round(bep, 8) if bep > 0 else 0.0,
                "unrealizedPnl": upnl,
                "pnlPercent": round(pct_on_margin, 2),
                "pnlPercentNotional": round(pct_on_notional, 4),
                "leverage": lev,
                "margin_mode": p.get("margin_mode", "cross"),
            })
        except Exception:
            continue
    return out


def _paper_positions_raw() -> list:
    from src.core.paper_engine import paper_engine

    rows = []
    for symbol, pos in (paper_engine.positions or {}).items():
        try:
            size = float(pos.get("size", 0.0) or 0.0)
            if size <= 0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "side": pos.get("side", "long"),
                    "size": size,
                    "entryPrice": float(pos.get("entry_price", 0.0) or 0.0),
                    "entry_price": float(pos.get("entry_price", 0.0) or 0.0),
                    "unrealizedPnl": float(pos.get("unrealized_pnl", 0.0) or 0.0),
                    "unrealized_pnl": float(pos.get("unrealized_pnl", 0.0) or 0.0),
                    "leverage": float(pos.get("leverage", 1.0) or 1.0),
                    "margin_mode": pos.get("margin_mode", "cross"),
                    "contract_size": float(pos.get("contract_size", 1.0) or 1.0),
                }
            )
        except Exception:
            continue
    return rows


async def _positions_for_ui(exchange) -> list:
    paper_rows = _paper_positions_raw()
    if paper_rows:
        return paper_rows
    try:
        return await exchange.fetch_positions()
    except Exception:
        return []


async def _account_financial_breakdown_async(exchange) -> dict:
    """总权益 / 冻结保证金 / 可用 / 累计手续费 — 纸面走 paper_engine，实盘走余额 + 持仓估算。"""
    from src.core.paper_engine import paper_engine

    if not exchange:
        return {}
    if bool(getattr(exchange, "use_paper_trading", False)):
        return paper_engine.financial_snapshot()
    try:
        raw_pos = await _positions_for_ui(exchange)
    except Exception:
        raw_pos = []
    try:
        bd = await exchange.fetch_balance()
    except Exception:
        bd = {}
    margin_locked = 0.0
    unreal = 0.0
    for p in raw_pos or []:
        try:
            ep = float(p.get("entryPrice", p.get("entry_price", 0)) or 0)
            sz = abs(float(p.get("size", 0) or 0))
            cs = float(p.get("contractSize", p.get("contract_size", 0)) or 0) or 1.0
            lev = float(p.get("leverage", 1) or 1) or 1.0
            if sz > 0 and ep > 0:
                margin_locked += ep * sz * cs / max(lev, 1e-9)
            unreal += float(p.get("unrealizedPnl", p.get("unrealized_pnl", 0)) or 0)
        except Exception:
            continue
    total_wallet = float((bd.get("total") or {}).get("USDT", 0) or 0)
    free_u = float((bd.get("free") or {}).get("USDT", 0) or 0)
    te = float(getattr(risk_engine, "total_equity", 0) or 0) or total_wallet
    if te <= 0:
        te = total_wallet if total_wallet > 0 else unreal
    avail = free_u if free_u > 1e-9 else max(float(te) - margin_locked, 0.0)
    fees = float(getattr(risk_engine, "accumulated_fee", 0) or 0)
    return {
        "total_equity": float(te),
        "margin_locked": float(margin_locked),
        "available_balance": float(avail),
        "total_fees_paid": float(fees),
        "total_unrealized_pnl": float(unreal),
        "session_realized_pnl_net": float(getattr(risk_engine, "realized_pnl", 0) or 0),
        "total_funding_paid_usdt": float(getattr(risk_engine, "accumulated_funding_fee", 0) or 0),
        "wallet_cash_ledger_usdt": float(getattr(risk_engine, "wallet_balance", 0) or 0),
    }


def _beta_neutral_hf_telemetry(engine) -> dict:
    try:
        beta_strategy = next((s for s in getattr(engine, "strategies", []) if s.name == "BetaNeutralHF"), None)
        if beta_strategy and hasattr(beta_strategy, "runtime_status"):
            rs = beta_strategy.runtime_status()
            return {
                "anchor_notional_delta": float(rs.get("anchor_notional_delta", 0.0) or 0.0),
                "anchor_deadband_threshold": float(rs.get("anchor_deadband_threshold", 0.0) or 0.0),
                "anchor_rebalance_suppressed": bool(rs.get("anchor_rebalance_suppressed", False)),
                "last_expected_tp_vs_cost": float(rs.get("last_expected_tp_vs_cost", 0.0) or 0.0),
            }
    except Exception:
        pass
    return {
        "anchor_notional_delta": 0.0,
        "anchor_deadband_threshold": 0.0,
        "anchor_rebalance_suppressed": False,
        "last_expected_tp_vs_cost": 0.0,
    }


def _trade_history_items_and_summary(limit: Optional[int] = None) -> tuple[list, dict, str]:
    d = config_manager.get_config().darwin.autopsy_dir
    items = []
    if not os.path.isdir(d):
        return [], {"total_count": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_total": 0.0}, d

    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log.warning(f"trade_history skip {name}: {e}")
            continue

        if raw.get("schema") not in ("darwin.trade_autopsy.v1", "darwin.trade_autopsy.v2"):
            continue

        pnl = raw.get("pnl") or {}
        path_stats = raw.get("path_stats") or {}
        exit_meta = raw.get("exit") or {}
        entry_snapshot = raw.get("entry_snapshot") or {}
        contract_size = float(raw.get("contract_size") or entry_snapshot.get("contract_size") or 1.0)
        closed_size = float(raw.get("closed_size") or 0)
        base_qty = float(raw.get("base_qty") or (closed_size * contract_size))
        entry_price = float(raw.get("entry_price") or 0)
        exit_price = float(raw.get("exit_price") or 0)

        items.append(
            {
                "file": name,
                "closed_at": float(raw.get("closed_at") or 0),
                "symbol": raw.get("symbol", ""),
                "side": raw.get("side", ""),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "closed_size": closed_size,
                "contract_size": contract_size,
                "base_qty": base_qty,
                "entry_notional_usdt": float(raw.get("entry_notional_usdt") or (base_qty * entry_price)),
                "exit_notional_usdt": float(raw.get("exit_notional_usdt") or (base_qty * exit_price)),
                "leverage": float(raw.get("leverage") or 0),
                "margin_mode": raw.get("margin_mode", ""),
                "gross_pnl": float(pnl.get("realized_gross") or 0),
                "fees": float(pnl.get("fees_allocated") or 0),
                "net_pnl": float(pnl.get("realized_net") or 0),
                "exit_reason": exit_meta.get("reason", ""),
                "trading_mode": exit_meta.get("trading_mode"),
                "duration_sec": float(raw.get("duration_sec") or 0),
                "max_favorable_unrealized": float(path_stats.get("max_favorable_unrealized") or 0),
                "max_adverse_unrealized": float(path_stats.get("max_adverse_unrealized") or 0),
                "entry_snapshot": entry_snapshot,
                "l1_at_signal": raw.get("l1_at_signal"),
                "schema": raw.get("schema"),
            }
        )

    items.sort(key=lambda x: x.get("closed_at") or 0, reverse=True)
    total_count = len(items)
    wins = sum(1 for x in items if float(x.get("net_pnl") or 0.0) > 0)
    losses = sum(1 for x in items if float(x.get("net_pnl") or 0.0) < 0)
    net_total = sum(float(x.get("net_pnl") or 0.0) for x in items)
    summary = {
        "total_count": total_count,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_count) if total_count > 0 else 0.0,
        "net_total": net_total,
    }
    if limit is not None:
        items = items[:limit]
    return items, summary, d


# Background task: push live gateway ticks (mainnet WS) to dashboard clients
async def ws_push_loop():
    while True:
        try:
            if ws_manager.active_connections:
                engine = bot_context.get_strategy_engine()
                exchange = bot_context.get_exchange()

                if engine and exchange:
                    from src.core import l1_fast_loop as _l1fl

                    symbols = config_manager.get_config().strategy.symbols
                    now = int(time.time())
                    candle_t = (now // 60) * 60
                    cfg = config_manager.get_config().exchange

                    payload = {
                        "type": "MARKET_UPDATE",
                        "data_feed": "gate_mainnet_usdt",
                        "sandbox_execution": cfg.sandbox_mode,
                        "data": {},
                    }

                    ticks = getattr(exchange, "latest_tick_by_symbol", {}) or {}
                    books = getattr(exchange, "latest_book_top", {}) or {}
                    spec_cache = getattr(exchange, "contract_specs_cache", {}) or {}

                    for symbol in symbols:
                        tick = ticks.get(symbol, {}) or {}
                        top = books.get(symbol, {}) or {}
                        cache = dict(spec_cache.get(symbol, {}) or {})

                        last_price = float(tick.get("last", 0) or 0)
                        if last_price <= 0 and top:
                            bb = float(top.get("best_bid", 0) or 0)
                            ba = float(top.get("best_ask", 0) or 0)
                            if bb > 0 and ba > 0:
                                last_price = (bb + ba) / 2.0
                        if last_price <= 0:
                            last_price = float(cache.get("mark_price", 0) or 0)

                        spread = float(top.get("spread", 0) or 0)

                        attack_strategy = next((s for s in engine.strategies if s.name == "CoreAttack"), None)
                        obi = getattr(attack_strategy, "latest_obi", 0.0) if attack_strategy else 0.0

                        from src.ai.analyzer import ai_context
                        ai_data = ai_context.get(symbol)
                        ai_score = ai_data.get("score", 50.0)
                        ai_reg = ai_data.get("regime", "OSCILLATING")
                        ai_reg_str = (
                            ai_reg.value if hasattr(ai_reg, "value") else str(ai_reg)
                        )
                        ai_reason = str(ai_data.get("reason", "") or "")

                        vol_24 = float(cache.get("volume_24h", cache.get("24h_volume", 0)) or 0)
                        change_24h_pct = float(cache.get("change_24h_pct", cache.get("change_percentage", 0)) or 0)
                        kline_tick = None
                        if last_price > 0:
                            kline_tick = {
                                "time": candle_t,
                                "interval_sec": 60,
                                "price": last_price,
                            }

                        row: dict = {
                            "last_price": last_price,
                            "best_bid": float(top.get("best_bid", 0) or 0),
                            "best_ask": float(top.get("best_ask", 0) or 0),
                            "spread": spread,
                            "obi": obi,
                            "ai_score": ai_score,
                            "ai_regime": ai_reg_str,
                            "ai_reason": ai_reason[:280] if ai_reason else "",
                            "kline_tick": kline_tick,
                            "target_leverage": risk_engine.recommended_grinder_leverage(symbol),
                            "berserker_max_leverage": berserker_max_leverage_for_symbol(symbol),
                            "atr_pct_snapshot": float(risk_engine.symbol_atr_pct.get(symbol, 0) or 0),
                            "ten_min_range_pct": float(risk_engine.ten_min_range_pct(symbol)),
                            "change_24h_pct": change_24h_pct,
                            "l1_cvd": _l1fl.cvd_compact(symbol),
                            "contract_specs": {
                                "symbol": symbol,
                                "funding_rate": float(cache.get("funding_rate", 0) or 0),
                                "mark_price": float(cache.get("mark_price", 0) or 0),
                                "index_price": float(cache.get("index_price", 0) or 0),
                                "volume_24h": vol_24,
                                "change_24h_pct": change_24h_pct,
                            },
                        }
                        fp_delta = _l1fl.footprint_ws_delta_maybe(symbol)
                        if fp_delta:
                            row["footprint_delta"] = fp_delta
                        payload["data"][symbol] = row

                    equity = risk_engine.current_balance
                    daily_pnl = risk_engine.daily_pnl
                    positions = await _positions_for_ui(exchange)
                    beta_telemetry = _beta_neutral_hf_telemetry(engine)

                    trade_summary = _trade_history_items_and_summary(limit=None)[1]
                    display_daily_pnl = float(trade_summary.get("net_total", 0.0) or 0.0)
                    fin = await _account_financial_breakdown_async(exchange)
                    payload["account"] = {
                        "equity": equity,
                        "daily_pnl": daily_pnl,
                        "display_daily_pnl": display_daily_pnl,
                        "display_daily_pnl_percent": (display_daily_pnl / float(risk_engine.initial_balance or equity) * 100.0)
                        if float(risk_engine.initial_balance or equity) > 0
                        else 0.0,
                        "session_start_balance": float(risk_engine.initial_balance or equity),
                        "positions": _normalize_positions_for_ui(positions),
                        "beta_neutral_hf_telemetry": beta_telemetry,
                        "total_equity": float(fin.get("total_equity", equity) or equity),
                        "margin_locked": float(fin.get("margin_locked", 0.0) or 0.0),
                        "available_balance": float(fin.get("available_balance", 0.0) or 0.0),
                        "total_fees_paid": float(fin.get("total_fees_paid", 0.0) or 0.0),
                        "total_unrealized_pnl": float(fin.get("total_unrealized_pnl", 0.0) or 0.0),
                        "session_realized_pnl_net": float(fin.get("session_realized_pnl_net", daily_pnl) or 0.0),
                        "account_financials": fin,
                    }

                    sm = bot_context.get_state_machine()
                    if sm:
                        payload["bot"] = {
                            "ui_mode": _system_state_to_ui_mode(sm.state),
                            "state": sm.state.value if isinstance(sm.state, SystemState) else str(sm.state),
                            "manual_trading_mode": sm.manual_trading_mode.value if sm.manual_trading_mode else None,
                            "beta_neutral_hf_telemetry": beta_telemetry,
                        }

                    from src.core.volume_radar import get_volume_radar_payload
                    from src.core.binance_leadlag import get_binance_leadlag_payload
                    from src.core.gate_hot_universe import get_gate_hot_universe_payload

                    payload["volume_radar"] = get_volume_radar_payload()
                    payload["binance_leadlag"] = get_binance_leadlag_payload()
                    payload["gate_hot_universe"] = get_gate_hot_universe_payload()

                    await ws_manager.broadcast(payload)

        except Exception as e:
            log.error(f"WS Push Loop Error: {e}")

        await asyncio.sleep(0.1)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(ws_push_loop())

# API Routes
@app.get("/api/fee_schedule")
async def get_fee_schedule(symbol: Optional[str] = Query(None)):
    """纸面/风控用 Taker·Maker 费率（名义×费率，与杠杆无关）。可选 symbol 返回该合约 Gate 同步后的解析费率。"""
    from src.core.paper_engine import paper_engine

    pe = config_manager.get_config().paper_engine
    base = {
        "taker_fee_rate": float(pe.taker_fee_rate),
        "maker_fee_rate": float(pe.maker_fee_rate),
        "taker_bps": float(pe.taker_fee_rate) * 1e4,
        "maker_bps": float(pe.maker_fee_rate) * 1e4,
        "note": "Fee = notional × rate; notional = contracts × contract_size × price. "
        "With symbol=, resolved_* uses Gate contract_specs_cache after sync.",
    }
    if symbol and str(symbol).strip():
        sym = str(symbol).strip()
        tk, mk = paper_engine._fee_rates_for_symbol(sym)
        base["symbol"] = sym
        base["resolved_taker_fee_rate"] = tk
        base["resolved_maker_fee_rate"] = mk
        base["resolved_taker_bps"] = tk * 1e4
        base["resolved_maker_bps"] = mk * 1e4
    return base


@app.get("/api/config")
async def get_config():
    """Get current configuration"""
    return config_manager.get_config().model_dump()

@app.post("/api/config/exchange")
async def update_exchange(
    api_key: str = Body(..., embed=True), 
    api_secret: str = Body(..., embed=True),
    sandbox_mode: bool = Body(False, embed=True)
):
    """Update Exchange API Keys"""
    try:
        config_manager.update_exchange_config(api_key, api_secret, sandbox_mode)
        
        # Trigger hot reload of exchange connection
        exchange = bot_context.get_exchange()
        if exchange:
            await exchange.reload_config()
            
        log.info("Exchange config updated via API")
        return {"status": "success", "message": "Exchange config updated and connection reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/risk")
async def update_risk(risk_settings: dict):
    """Update Risk Parameters"""
    try:
        config_manager.update_risk_config(**risk_settings)
        log.info("Risk config updated via API")
        return {"status": "success", "message": "Risk config updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/strategy")
async def update_strategy(strategy_settings: dict):
    """Update Strategy Parameters"""
    try:
        config_manager.update_strategy_config(**strategy_settings)
        log.info("Strategy config updated via API")
        return {"status": "success", "message": "Strategy config updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/license/upload")
async def upload_license(file: UploadFile = File(...)):
    """Upload new license file"""
    try:
        # Ensure directory exists
        os.makedirs("license", exist_ok=True)
        file_path = "license/license.key"
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        log.info("New license uploaded via API")
        # Here you might want to trigger a reload/re-validation
        return {"status": "success", "message": "License uploaded successfully. Please restart bot."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status")
async def get_status():
    """Get Bot Status"""
    sm = bot_context.get_state_machine()
    engine = bot_context.get_strategy_engine()
    
    if not sm or not engine:
        return {"status": "stopped", "state": "UNKNOWN", "ui_mode": "NEUTRAL"}

    return {
        "status": "running" if engine.is_running else "stopped",
        "state": sm.state.value if isinstance(sm.state, SystemState) else str(sm.state),
        "ui_mode": _system_state_to_ui_mode(sm.state),
        "manual_trading_mode": sm.manual_trading_mode.value if sm.manual_trading_mode else None,
    }

@app.post("/api/control")
async def control_bot(command: dict = Body(...)):
    """Start/Stop Bot"""
    engine = bot_context.get_strategy_engine()
    if not engine:
         raise HTTPException(status_code=503, detail="Bot not initialized")
         
    action = command.get("action")
    if action == "START":
        engine.resume()
        return {"status": "success", "message": "Bot Resumed"}
    elif action == "STOP":
        engine.pause()
        return {"status": "success", "message": "Bot Paused"}
    elif action == "SET_TRADING_MODE":
        sm = bot_context.get_state_machine()
        if not sm:
            raise HTTPException(status_code=503, detail="State machine not initialized")
        mode = command.get("mode", "")
        if not sm.set_trading_mode_from_ui(str(mode)):
            raise HTTPException(
                status_code=400,
                detail="Invalid mode (NEUTRAL, ATTACK, BERSERKER, AUTO) or system license-locked",
            )
        return {
            "status": "success",
            "message": f"Trading mode set to {mode}",
            "ui_mode": _system_state_to_ui_mode(sm.state),
        }
    elif action == "KILL_SWITCH":
        # Ultimate Kill Switch
        engine.pause()
        exchange = bot_context.get_exchange()
        if exchange and hasattr(exchange, 'close_all_positions'):
            try:
                await exchange.close_all_positions()
                log.critical("KILL SWITCH ACTIVATED: Bot paused and all positions closed.")
                return {"status": "success", "message": "KILL SWITCH ACTIVATED: Bot paused, positions closed."}
            except Exception as e:
                log.error(f"Error during Kill Switch close_all_positions: {e}")
                return {"status": "error", "message": f"Paused bot, but failed to close positions: {e}"}
        return {"status": "error", "message": "Bot paused, but exchange does not support close_all_positions"}
    
    return {"status": "error", "message": "Invalid action"}

@app.get("/api/contract_specs")
async def get_contract_specs():
    """Get micro-structure data like funding rate and spread"""
    exchange = bot_context.get_exchange()
    if not exchange:
        return {}
        
    symbols = config_manager.get_config().strategy.symbols
    specs_list = []
    
    # In production, we'd fetch all at once or cache them.
    # For now, we fetch them individually or mock for UI demo if too many
    for symbol in symbols:
        specs = await exchange.fetch_contract_specs(symbol)
        top = getattr(exchange, "latest_book_top", {}).get(symbol, {}) or {}
        tick = getattr(exchange, "latest_tick_by_symbol", {}).get(symbol, {}) or {}
        cache = getattr(exchange, "contract_specs_cache", {}).get(symbol, {}) or {}

        specs["spread"] = float(top.get("spread", 0) or 0)
        specs["last_price"] = float(tick.get("last", 0) or 0)
        if cache:
            specs["funding_rate"] = float(cache.get("funding_rate", specs.get("funding_rate", 0)) or 0)
            specs["mark_price"] = float(cache.get("mark_price", specs.get("mark_price", 0)) or 0)
            specs["index_price"] = float(cache.get("index_price", specs.get("index_price", 0)) or 0)
            v = cache.get("volume_24h", cache.get("24h_volume", 0))
            specs["volume_24h"] = float(v or 0)
            specs["change_24h_pct"] = float(
                cache.get("change_24h_pct", cache.get("change_percentage", specs.get("change_24h_pct", 0))) or 0
            )
        specs_list.append(specs)

    return {s["symbol"]: s for s in specs_list if "symbol" in s}


@app.get("/api/candles")
async def get_candles(
    symbol: str = Query(..., description="e.g. BTC/USDT"),
    interval: str = Query("1m"),
    limit: int = Query(200, ge=1, le=2000),
):
    exchange = bot_context.get_exchange()
    if not exchange or not hasattr(exchange, "fetch_candlesticks"):
        return []
    return await exchange.fetch_candlesticks(symbol, interval=interval, limit=limit)


@app.get("/api/footprint")
async def get_footprint(
    symbol: str = Query(..., description="e.g. BTC/USDT"),
    bars: int = Query(90, ge=5, le=200),
    levels: int = Query(40, ge=8, le=80),
):
    """
    L1 内存：分钟足迹柱 + 滚动 CVD。JSON schema: shark.footprint.v1。
    逐笔来自 Gate `futures.trades`；`price` 缺失时仅计入 CVD，不计入价位档位。
    """
    from src.core import l1_fast_loop

    return l1_fast_loop.footprint_snapshot(symbol, max_bars=bars, max_levels_per_bar=levels)


@app.get("/api/resonance_metrics")
async def get_resonance_metrics(
    symbol: str | None = Query(None, description="Dashboard active symbol, e.g. BTC/USDT"),
):
    """Get Real-time Resonance and Risk Metrics for the Dashboard"""
    engine = bot_context.get_strategy_engine()
    exchange = bot_context.get_exchange()
    
    # Defaults
    metrics = {
        "obi": 0.0,
        "tech_indicator": 0.0,
        "tech_signal": "neutral",
        "ai_score": 50.0,
        "ai_regime": "OSCILLATING",
        "ai_reason": "",
        "atr_pct": 0.02, # Mock default ATR
        "target_leverage": 1.0,
        "current_risk_exposure": 0.0,
        "ws_latency": 0,
        "ws_reconnects": 0,
        "kill_switch_progress": {
            "active": False,
            "executed_chunks": 0,
            "total_chunks": 5,
            "avg_slippage": 0.0
        },
        "adaptation": {
            "probe_mode": False,
            "adaptation_level": 0,
            "adaptation_label": "NORMAL",
            "window_trades": 0,
            "window_win_rate": 1.0,
            "consecutive_losses": 0,
            "recovery_win_rate": 1.0,
            "live_attack_ai_threshold": 0.0,
            "live_neutral_ai_threshold": 0.0,
            "live_funding_signal_weight": 0.0,
            "live_attack_align_bps": 0.0,
            "live_margin_cap_usdt": 0.0,
            "strongest_symbol": "",
            "strongest_symbol_win_rate": 0.0,
            "strongest_symbol_trades": 0,
            "weakest_symbol": "",
            "weakest_symbol_win_rate": 1.0,
            "weakest_symbol_trades": 0,
            "dominant_win_reason": "",
            "dominant_win_reason_count": 0,
            "dominant_loss_reason": "",
            "dominant_loss_reason_count": 0,
            "strongest_strategy": "",
            "strongest_strategy_win_rate": 0.0,
            "strongest_strategy_trades": 0,
            "strongest_scene": {},
            "weakest_scene": {},
            "scene_leaderboard": [],
            "weakest_strategy": "",
            "weakest_strategy_win_rate": 1.0,
            "weakest_strategy_trades": 0,
            "symbol_boosts": {},
        },
        "beta_neutral_hf": {
            "enabled": False,
            "anchor_symbol": "BTC/USDT",
            "tracked_symbols": [],
            "active_pairs": [],
            "candidate_pairs": [],
            "recent_closed": [],
            "anchor_target_contracts": 0.0,
            "anchor_actual_contracts": 0.0,
        },
    }
    
    if not engine or not exchange:
        return metrics

    sym_list = config_manager.get_config().strategy.symbols
    sym0 = symbol or (sym_list[0] if sym_list else "")

    # 1. Fetch OBI & Tech from CoreAttackStrategy if active
    attack_strategy = next((s for s in engine.strategies if s.name == "CoreAttack"), None)
    if attack_strategy:
        metrics["obi"] = getattr(attack_strategy, "latest_obi", 0.0)
        px_map = getattr(attack_strategy, "_prices_by_symbol", {}) or {}
        prices = list(px_map.get(sym0, [])) if sym0 else []
        if len(prices) > 0:
            current_price = prices[-1]
            sma = sum(prices) / len(prices)
            metrics["tech_indicator"] = (current_price / sma) - 1 if sma > 0 else 0.0
            if current_price > sma * 1.001:
                metrics["tech_signal"] = "bullish"
            elif current_price < sma * 0.999:
                metrics["tech_signal"] = "bearish"
                
    # 2. Fetch AI snapshot for active / primary symbol
    from src.ai.analyzer import ai_context

    ai_data = ai_context.get(sym0) if sym0 else {}
    metrics["ai_score"] = ai_data.get("score", 50.0)
    _ar = ai_data.get("regime", "OSCILLATING")
    metrics["ai_regime"] = _ar.value if hasattr(_ar, "value") else str(_ar)
    metrics["ai_reason"] = str(ai_data.get("reason", "") or "")
    
    # 3. Risk Engine Data
    total_equity = risk_engine.current_balance
    if total_equity > 0:
        # Mock calculation for total position margin (requires position data)
        # Assuming we fetch it or it's updated in a background task
        try:
            # We use a placeholder for now, or fetch from exchange if available
            positions = await _positions_for_ui(exchange)
            total_margin = sum([float(p.get("size", 0)) * float(p.get("entryPrice", 0)) / float(p.get("leverage", 1)) for p in positions])
            metrics["current_risk_exposure"] = (total_margin / total_equity) * 100
        except Exception:
            pass
            
    atr_live = float(risk_engine.symbol_atr_pct.get(sym0, metrics["atr_pct"]) or metrics["atr_pct"])
    metrics["atr_pct"] = atr_live
    pos_sizing = risk_engine.calculate_dynamic_position(
        win_rate=0.55, payoff_ratio=1.5, atr_pct=atr_live, max_leverage=config_manager.get_config().risk.grinder_leverage_max
    )
    metrics["target_leverage"] = pos_sizing["recommended_leverage"]
    metrics["berserker_max_leverage"] = berserker_max_leverage_for_symbol(sym0)
    
    # 4. Gateway Telemetry
    metrics["ws_reconnects"] = getattr(exchange, "reconnect_count", 0)
    # ws_latency could be ping/pong RTT if implemented in aiohttp ws
    metrics["adaptation"] = strategy_auto_tuner.runtime_status()
    beta_strategy = next((s for s in engine.strategies if s.name == "BetaNeutralHF"), None)
    if beta_strategy and hasattr(beta_strategy, "runtime_status"):
        try:
            metrics["beta_neutral_hf"] = beta_strategy.runtime_status()
        except Exception:
            pass
    
    return metrics

@app.get("/api/market_analysis")
async def get_market_analysis(
    symbol: str | None = Query(None, description="e.g. BTC/USDT; defaults to first configured symbol"),
):
    """Get Real-time AI Analysis"""
    exchange = bot_context.get_exchange()
    if not exchange:
         return {"symbol": "-", "regime": "OFFLINE", "ai_score": 0, "timestamp": 0, "reason": ""}

    syms = config_manager.get_config().strategy.symbols
    symbol = symbol or (syms[0] if syms else "BTC/USDT")
    ticker = await exchange.fetch_ticker(symbol)
    
    if not ticker:
         return {"symbol": symbol, "regime": "NO_DATA", "ai_score": 0, "timestamp": 0, "reason": ""}

    regime = regime_classifier.analyze(symbol)
    score = ai_scorer.score(symbol, ticker, "buy")
    from src.ai.analyzer import ai_context

    ai_data = ai_context.get(symbol)

    # Format score to 2 decimal places
    formatted_score = round(score, 2)
    
    return {
        "symbol": symbol,
        "regime": regime.value if hasattr(regime, "value") else str(regime),
        "ai_score": formatted_score,
        "timestamp": ticker.get('timestamp', 0),
        "reason": str(ai_data.get("reason", "") or ""),
    }

@app.get("/api/market_quotes")
async def get_market_quotes():
    """Get Real-time Market Quotes for Top 20 Pairs"""
    exchange = bot_context.get_exchange()
    if not exchange:
        return []

    # Top 20 Crypto Pairs (Futures usually)
    symbols = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
        "LTC/USDT", "TRX/USDT", "BCH/USDT", "UNI/USDT",
        "ATOM/USDT", "XLM/USDT", "ETC/USDT", "FIL/USDT", "NEAR/USDT"
    ]
    
    try:
        tickers = await exchange.fetch_tickers(symbols)
        # Convert dict to list for frontend
        quote_list = []
        for symbol, ticker in tickers.items():
            quote_list.append({
                "symbol": symbol,
                "price": ticker['last'],
                "change_pct": ticker['percentage'],
                "volume": ticker['quoteVolume']
            })
        
        # Sort by volume desc
        quote_list.sort(key=lambda x: x['volume'], reverse=True)
        return quote_list
    except Exception as e:
        log.error(f"Error fetching quotes: {e}")
        return []

@app.get("/api/recent_signals")
async def get_recent_signals():
    """Get Recent Signals Log"""
    # In real app, read from database or memory buffer
    return []


@app.get("/api/trade_history")
async def get_trade_history(limit: int = Query(200, ge=1, le=1000)):
    """
    历史仓位：读取 Darwin Protocol 平仓战报（paper_engine 写入的 JSON）。
    """
    try:
        items, summary, d = _trade_history_items_and_summary(limit=limit)
        return {"items": items, "source_dir": d, "summary": summary}
    except Exception as e:
        log.error(f"trade_history fatal: {e}")
        return {"items": [], "source_dir": "", "summary": {"total_count": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_total": 0.0}, "detail": f"Internal Server Error: {e}"}


@app.get("/api/account_info")
async def get_account_info():
    """Get Positions and Open Orders"""
    exchange = bot_context.get_exchange()
    if not exchange:
        return {"positions": [], "orders": [], "balance": 0.0, "daily_pnl": 0.0, "win_rate": 0.0}
        
    try:
        engine = bot_context.get_strategy_engine()
        # Fetch Real Data
        balance_data = await exchange.fetch_balance()
        # CCXT balance structure: {'total': {'USDT': 1000}, 'free': ...}
        total_balance = balance_data.get('total', {}).get('USDT', 0.0)
        
        # We need to implement fetch_positions in UnifiedExchange or access execution_exchange directly
        # For now, let's assume UnifiedExchange exposes it or we access the internal execution_exchange
        positions = []
        try:
            raw_pos = await _positions_for_ui(exchange)
            positions = _normalize_positions_for_ui(
                [p for p in raw_pos if float(p.get("size", 0)) != 0]
            )
        except Exception as e:
            log.error(f"Error processing positions: {e}")

        # Orders
        orders = []
        try:
            raw_orders = await exchange.fetch_open_orders()
            for o in raw_orders:
                orders.append({
                    "symbol": o['symbol'],
                    "side": o['side'],
                    "type": o['type'],
                    "price": o['price'],
                    "amount": o['amount'],
                    "status": o['status']
                })
        except Exception as e:
             log.error(f"Error processing orders: {e}")

        fin = await _account_financial_breakdown_async(exchange)
        return {
            "positions": positions,
            "orders": orders,
            "balance": total_balance,
            "daily_pnl": risk_engine.daily_pnl,
            "display_daily_pnl": float(_trade_history_items_and_summary(limit=None)[1].get("net_total", 0.0) or 0.0),
            "display_daily_pnl_percent": (
                float(_trade_history_items_and_summary(limit=None)[1].get("net_total", 0.0) or 0.0)
                / float(risk_engine.initial_balance or total_balance)
                * 100.0
            )
            if float(risk_engine.initial_balance or total_balance) > 0
            else 0.0,
            "session_start_balance": float(risk_engine.initial_balance or total_balance),
            "beta_neutral_hf_telemetry": _beta_neutral_hf_telemetry(engine),
            "win_rate": 0.0,
            "total_equity": float(fin.get("total_equity", total_balance) or total_balance),
            "margin_locked": float(fin.get("margin_locked", 0.0) or 0.0),
            "available_balance": float(fin.get("available_balance", 0.0) or 0.0),
            "total_fees_paid": float(fin.get("total_fees_paid", 0.0) or 0.0),
            "total_unrealized_pnl": float(fin.get("total_unrealized_pnl", 0.0) or 0.0),
            "session_realized_pnl_net": float(fin.get("session_realized_pnl_net", risk_engine.daily_pnl) or 0.0),
            "account_financials": fin,
        }
    except Exception as e:
        log.error(f"Error in account_info: {e}")
        return {"positions": [], "orders": [], "balance": 0.0, "daily_pnl": 0.0, "win_rate": 0.0}

@app.get("/api/logs")
async def get_logs():
    """Get System Logs"""
    # Use data_collector csv for strategy execution logs
    # or read the structured logs from loguru
    # Here we focus on 'bot operation logs' which might be the main log file
    
    try:
        log_dir = "logs"
        if not os.path.exists(log_dir):
            return {"logs": []}
            
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")]
        if not files:
             return {"logs": []}
             
        latest_file = max(files, key=os.path.getctime)
        with open(latest_file, "r") as f:
            lines = f.readlines()
            # Filter for meaningful operation logs if needed
            return {"logs": lines[-50:]} 
    except Exception as e:
        return {"logs": [f"Error reading logs: {e}"]}


# Serve Static Files (Frontend)
if os.path.exists("src/web"):
    app.mount("/", StaticFiles(directory="src/web", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
