"""
纸面撮合引擎 — 审计清单（与实盘对齐时重点核对）

清算对齐（Gate / 通用 USDT 本位永续）：
- 名义 Notional(USDT) = 合约张数 Qty × ContractSize(每张标的数量) × Price。
- 手续费 Fee = Notional × Rate（与杠杆无关）。纸面 Taker/Maker 费率硬编码为模块常量 TAKER_FEE_RATE / MAKER_FEE_RATE，不从网关或 YAML 读取。
- 毛盈亏（多）PnL_gross = Qty × ContractSize × (P_mark − P_entry)；杠杆不放大该绝对值。
- 起始保证金（占用）≈ Notional / Leverage；仅影响占用与强平语义，不改变单笔毛利公式。
- ContractSize 来自 Gate REST quanto_multiplier（无缓存时用 paper_engine.default_contract_size）。

1) 悲观滑点乘数
   - 有 futures.order_book 缓存时：市价单走「吃盘 VWAP」，不在 best ask/bid 上再乘固定 spread_buffer。
   - 历史上仅「无盘口」时用 last * 1.001 / 0.999（10bps×2 观感）过于像惩罚；已改为可配置
     `paper_engine.fallback_slippage_bps`（默认 2bps）。
   - 可选 `paper_engine.taker_extra_bps`：在 VWAP 之后再叠加（默认 0 = 不加人为冲击）。

2) 固定点差陷阱
   - 非 last*固定比例；gate_gateway 在双档齐全时调用 update_orderbook，撮合用真实 bids/asks 档位。

3) 执行延迟
   - execute_order 同步立即成交，无 50–100ms 人为 sleep；与 WS tick 到达顺序由网关线程决定。

4) 强平
   - 本模块不实现交易所 MMR 强平；仅维护 unrealized_pnl / available_balance。
     不会出现「一碰强平价整账户归零」的模拟；若需强平语义需单独模块 + 缓冲/减仓规则。

Maker：触价成交 — 市价可立即匹配侧（ask<=买限价 / bid>=卖限价）或 last 穿过限价时成交。

开仓限价止盈/止损（可选 paper_engine.require_entry_tp_sl_limits）：
- entry_context 须含 take_profit_limit_price、stop_loss_limit_price；成交后挂 OCO（maker_limit 止盈 + maker_limit 止损限价），并设 fixed_tp_sl_protocol 跳过四维 ATR 追踪。
"""

import math
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.utils.logger import log
from src.darwin.autopsy import build_trade_autopsy
from src.darwin.pipeline import schedule_trade_autopsy
from src.core.assassin_cost import (
    assassin_hurdle_rate,
    long_hard_tp_price,
    short_hard_tp_price,
)

# Gate.io USDT 永续基准（与杠杆无关）：Fee = Notional×Rate，Notional = |张|×ContractSize×成交价。
# 标准档参考：Taker 万分之五、Maker 万分之一点五（不从 CCXT 覆盖）。
TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.00015


class SimulatedAPIError(Exception):
    """纸面预检失败：语义对齐 Gate HTTP 400 / INVALID_REQUEST。"""

    __slots__ = ("http_status", "label")

    def __init__(self, message: str, *, http_status: int = 400, label: str = "INVALID_REQUEST"):
        super().__init__(message)
        self.http_status = int(http_status)
        self.label = str(label)


def _paper_engine_settings() -> Tuple[float, float]:
    """(fallback_slippage_bps, taker_extra_bps)"""
    try:
        from src.core.config_manager import config_manager

        pe = config_manager.get_config().paper_engine
        return (float(pe.fallback_slippage_bps), float(pe.taker_extra_bps))
    except Exception:
        return (2.0, 0.0)


def _global_paper_initial_balance_usdt() -> float:
    """全局 paper_engine 起始资金：来自 settings.yaml paper_engine.initial_balance_usdt。"""
    try:
        from src.core.config_manager import config_manager

        v = float(config_manager.get_config().paper_engine.initial_balance_usdt)
        return max(1.0, v)
    except Exception:
        return 100.0


def _l1_fast_loop_config() -> Any:
    try:
        from src.core.config_manager import config_manager

        return config_manager.get_config().l1_fast_loop
    except Exception:
        return None


def _slingshot_config() -> Any:
    try:
        from src.core.config_manager import config_manager

        return config_manager.get_config().slingshot
    except Exception:
        return None


def _assassin_micro_config() -> Any:
    try:
        from src.core.config_manager import config_manager

        return config_manager.get_config().assassin_micro
    except Exception:
        return None


def _current_trading_mode() -> Optional[str]:
    try:
        from src.core.globals import bot_context

        sm = bot_context.get_state_machine()
        if sm and sm.state:
            return sm.state.value
    except Exception:
        pass
    return None


def _attach_exit_bracket(pos: Dict[str, Any], entry_px: float) -> None:
    """四维出场监控用的每仓状态（由 PositionExitMonitor 在 WS 回调里驱动）。"""
    pos["exit"] = {
        "atr": None,
        "initial_sl": None,
        "active_sl": None,
        "extreme": float(entry_px),
        "breakeven_done": False,
        "obi_adverse_since": None,
        "closing": False,
        "trailing_armed": False,
    }


class PaperTradingEngine:
    """
    Local Virtual Matching Engine for Paper Trading.
    Intercepts orders and simulates execution with realistic slippage and fees based on live orderbook data.
    """

    def __init__(self, initial_balance: float = 10000.0, taker_fee: float = 0.0005, maker_fee: float = 0.00015):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.available_balance = initial_balance
        self.accumulated_fee_paid = 0.0
        self.accumulated_funding_fee = 0.0
        self.taker_fee = float(TAKER_FEE_RATE)
        self.maker_fee = float(MAKER_FEE_RATE)

        self.positions: Dict[str, Dict[str, Any]] = {}
        self.orderbooks_cache: Dict[str, Dict[str, List]] = {}
        self.latest_prices: Dict[str, float] = {}
        # 最近一次平仓的已实现净盈亏（供 create_order 回包 → OrderManager 绩效闭环）
        self._pending_realized_net_usdt: Optional[float] = None
        self._pending_fee_audit: Optional[Dict[str, Any]] = None
        # Post-only 挂单模拟：entry_context.resting_quote=True 时先入队，成交价触达限价再撮合
        self._maker_resting: Dict[str, List[Dict[str, Any]]] = {}
        # OrderManager / 影子限价：虚拟排队 + 部分成交 + TTL 撤单（非「一口价全成」）
        self._shadow_orders: Dict[str, Dict[str, Any]] = {}
        # futures.trades 聚合：用于影子单可撮合张数上界（吞吐）
        self._last_tick_volume: Dict[str, float] = {}

    def note_trade_volume(self, symbol: str, base_volume: float) -> None:
        """网关 futures.trades 回调写入；用于影子限价按盘口成交量分批撮合。"""
        if not symbol:
            return
        try:
            v = float(base_volume)
        except (TypeError, ValueError):
            return
        if v <= 0:
            return
        self._last_tick_volume[symbol] = v

    def note_funding_fee(self, amount_usdt: float, symbol: Optional[str] = None) -> None:
        try:
            amt = float(amount_usdt or 0.0)
        except (TypeError, ValueError):
            return
        if abs(amt) <= 1e-12:
            return
        self.initial_balance -= amt
        self.accumulated_funding_fee += amt
        log.info(f"[Paper] FUNDING symbol={symbol or 'N/A'} amount={amt:.8f} accumulated={self.accumulated_funding_fee:.8f}")

    def list_open_orders_for_gateway(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """供 Gate 网关 fetch_open_orders（纸面）：影子挂单 + maker 队列。"""
        out: List[Dict[str, Any]] = []
        for oid, o in self._shadow_orders.items():
            if symbol and o.get("symbol") != symbol:
                continue
            amt = float(o.get("amount") or 0)
            filled = float(o.get("filled") or 0)
            rem = max(amt - filled, 0.0)
            st = "open" if filled <= 1e-12 else "partially_filled"
            out.append(
                {
                    "id": oid,
                    "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "type": "limit",
                    "price": float(o.get("price") or 0),
                    "amount": amt,
                    "filled": filled,
                    "remaining": rem,
                    "status": st,
                }
            )
        for sym, lst in self._maker_resting.items():
            if symbol and sym != symbol:
                continue
            for m in lst:
                out.append(
                    {
                        "id": m.get("id"),
                        "symbol": sym,
                        "side": m.get("side"),
                        "type": "limit",
                        "price": float(m.get("price") or 0),
                        "amount": float(m.get("amount") or 0),
                        "filled": 0.0,
                        "remaining": float(m.get("amount") or 0),
                        "status": "open",
                    }
                )
        return out

    def cancel_local_order(self, order_id: str) -> Dict[str, Any]:
        """响应 OrderManager TTL / 用户撤单：影子单与 maker 队列均可撤。"""
        oid = str(order_id or "").strip()
        if not oid:
            return {"status": "rejected", "reason": "empty order id"}
        if oid in self._shadow_orders:
            self._shadow_orders.pop(oid, None)
            return {"status": "canceled", "id": oid}
        for sym, lst in list(self._maker_resting.items()):
            new = [x for x in lst if str(x.get("id")) != oid]
            if len(new) != len(lst):
                self._maker_resting[sym] = new
                return {"status": "canceled", "id": oid}
        return {"status": "not_found", "id": oid}

    @staticmethod
    def _shadow_touch_executable(side: str, limit_px: float, touch: float) -> bool:
        s = str(side).lower()
        if s == "buy":
            return float(touch) <= float(limit_px) + 1e-12
        return float(touch) >= float(limit_px) - 1e-12

    def _shadow_fill_chunk(self, symbol: str, remaining: float) -> float:
        try:
            from src.core.config_manager import config_manager

            frac = float(config_manager.get_config().execution.shadow_fill_fraction_per_tick)
        except Exception:
            frac = 0.18
        frac = max(0.02, min(0.95, float(frac)))
        remaining = float(remaining)
        vol = float(self._last_tick_volume.get(symbol, 0.0) or 0.0)
        o_min = 1.0
        specs = self._exchange_physics_specs(symbol)
        if specs:
            om = float(specs.get("order_size_min") or 0.0)
            if om > 0:
                o_min = om
            if not bool(specs.get("enable_decimal", False)):
                rem_int = float(math.floor(remaining + 1e-12))
                if rem_int <= 0:
                    return 0.0
                base_int = math.floor(min(rem_int, max(o_min, rem_int * frac)) + 1e-12)
                if vol > 0:
                    base_int = math.floor(
                        min(rem_int, max(o_min, min(rem_int * frac, vol * 0.03))) + 1e-12
                    )
                return float(min(rem_int, max(base_int, 1.0 if rem_int >= 1.0 else rem_int)))
        base = min(remaining, max(o_min, remaining * frac))
        if vol > 0:
            base = min(remaining, max(o_min, min(remaining * frac, vol * 0.03)))
        return float(min(remaining, max(base, o_min if remaining >= o_min else remaining)))

    @staticmethod
    def _sanitize_fee_rate_decimal(rate: float) -> float:
        """
        Gate/CCXT 偶发把费率标成 bps 整数或百分数；与「名义×小数费率」对齐，严禁 >2% 的荒谬值进入 Fee。
        """
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return 0.0005
        if not math.isfinite(r):
            return 0.0005
        if abs(r) <= 1e-18:
            return r
        sign = 1.0 if r >= 0 else -1.0
        ar = abs(r)
        while ar > 0.02:
            ar /= 100.0
        v = sign * ar
        return max(-0.01, min(v, 0.25))

    def _process_shadow_orders(self, symbol: str, touch_price: float) -> None:
        if not self._shadow_orders:
            return
        tp = float(touch_price)
        if tp <= 0:
            return
        pos0 = self.positions.get(symbol)
        sz0 = float((pos0 or {}).get("size", 0) or 0.0)
        if sz0 <= 1e-12:
            for oid, o in list(self._shadow_orders.items()):
                if o and o.get("symbol") == symbol:
                    self._shadow_orders.pop(oid, None)
            return
        for oid in list(self._shadow_orders.keys()):
            o = self._shadow_orders.get(oid)
            if not o or o.get("symbol") != symbol:
                continue
            rem = float(o["amount"]) - float(o.get("filled") or 0.0)
            if rem <= 1e-12:
                self._shadow_orders.pop(oid, None)
                continue
            if not self._shadow_touch_executable(str(o["side"]), float(o["price"]), tp):
                continue
            chunk = self._shadow_fill_chunk(symbol, rem)
            if chunk <= 0:
                continue
            ect = dict(o.get("entry_context") or {})
            ect.pop("paper_shadow_limit", None)
            ect["paper_slice"] = True
            ect["shadow_parent_order_id"] = oid
            res = self.execute_order(
                symbol,
                str(o["side"]),
                float(chunk),
                float(o["price"]),
                bool(o.get("reduce_only")),
                leverage=int(o.get("leverage", 10)),
                margin_mode=str(o.get("margin_mode", "isolated")),
                berserker=bool(o.get("berserker")),
                post_only=False,
                entry_context=ect,
                exit_reason=ect.get("exit_reason"),
                order_text=o.get("order_text"),
            )
            if res.get("status") == "rejected":
                reason = str(res.get("reason") or "")
                if (
                    "max_pyramid_layers_reached" in reason
                    or "pyramid_layers_mutex" in reason
                    or "integer contracts for this market" in reason
                    or "INVALID_SIZE_STEP" in reason
                    or "Reduce only order on same side" in reason
                    or "No position to reduce" in reason
                ):
                    self._shadow_orders.pop(oid, None)
                    log.info(f"[Paper] Shadow order dropped {oid}: {reason}")
                    continue
                log.warning(f"[Paper] Shadow slice rejected {oid}: {reason}")
                self._shadow_orders.pop(oid, None)
                continue
            o["filled"] = float(o.get("filled") or 0.0) + float(chunk)
            if o["filled"] + 1e-9 >= float(o["amount"]):
                tot = float(o["amount"])
                self._shadow_orders.pop(oid, None)
                log.info(f"[Paper] SHADOW_FILLED {oid} {symbol} total={tot}")

    def _enqueue_shadow_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        limit_price: float,
        reduce_only: bool,
        leverage: int,
        margin_mode: str,
        berserker: bool,
        post_only: bool,
        ctx0: Dict[str, Any],
        exit_reason: Optional[str],
        order_text: Optional[str],
    ) -> dict:
        """限价进入本地影子队列（挂起），由行情驱动部分成交。"""
        exec_price = float(limit_price)
        pos = self.positions.get(symbol)
        is_fresh_open = (not pos or float(pos.get("size") or 0) <= 0) and not reduce_only
        if is_fresh_open:
            has_ts = self._has_explicit_tp_sl(ctx0)
            req = self._paper_require_entry_tp_sl() and not self._entry_tp_sl_exempt(ctx0)
            if req and not has_ts:
                return {
                    "status": "rejected",
                    "reason": "Missing take_profit_limit_price / stop_loss_limit_price (require_entry_tp_sl_limits)",
                }
            if has_ts:
                tpv = float(ctx0["take_profit_limit_price"])
                slv = float(ctx0["stop_loss_limit_price"])
                verr = self._validate_tp_sl_vs_entry(side, exec_price, tpv, slv)
                if verr:
                    try:
                        from src.core.config_manager import config_manager

                        prm = config_manager.get_config().strategy.params
                        tb = float(getattr(prm, "core_entry_tp_bps", 55.0) or 55.0)
                        sb = float(getattr(prm, "core_entry_sl_bps", 50.0) or 50.0)
                        if str(side).lower() == "buy":
                            ctx0["take_profit_limit_price"] = exec_price * (1.0 + tb / 1e4)
                            ctx0["stop_loss_limit_price"] = exec_price * (1.0 - sb / 1e4)
                        else:
                            ctx0["take_profit_limit_price"] = exec_price * (1.0 - tb / 1e4)
                            ctx0["stop_loss_limit_price"] = exec_price * (1.0 + sb / 1e4)
                        verr = self._validate_tp_sl_vs_entry(
                            side,
                            exec_price,
                            float(ctx0["take_profit_limit_price"]),
                            float(ctx0["stop_loss_limit_price"]),
                        )
                    except Exception:
                        pass
                if verr:
                    return {"status": "rejected", "reason": verr}

        if not pos or float(pos.get("size") or 0) <= 0:
            if reduce_only:
                return {"status": "rejected", "reason": "No position to reduce"}
        oid = str(uuid.uuid4())
        if order_text:
            ctx0 = dict(ctx0)
            ctx0.setdefault("client_oid", str(order_text)[:28])
        self._shadow_orders[oid] = {
            "id": oid,
            "symbol": symbol,
            "side": side,
            "amount": float(amount),
            "filled": 0.0,
            "price": float(limit_price),
            "reduce_only": bool(reduce_only),
            "leverage": int(leverage),
            "margin_mode": margin_mode,
            "berserker": bool(berserker),
            "post_only": bool(post_only),
            "entry_context": dict(ctx0),
            "order_text": str(order_text)[:28] if order_text else None,
            "created_ts": time.time(),
        }
        log.info(f"[Paper] SHADOW_ENQUEUE {side.upper()} {amount} {symbol} @ {float(limit_price):.6f} id={oid}")
        return {
            "status": "open",
            "id": oid,
            "filled": 0.0,
            "amount": float(amount),
            "symbol": symbol,
            "side": side,
            "price": float(limit_price),
        }

    def cancel_open_makers(self, symbol: str) -> int:
        lst = self._maker_resting.pop(symbol, [])
        return len(lst)

    def get_display_tp_sl(self, symbol: str) -> Dict[str, Optional[float]]:
        """
        星舰持仓表：括号 OCO 的限价止盈、模拟止损价；无挂单时用四维出场 active_sl 作止损参考。
        """
        tp: Optional[float] = None
        sl: Optional[float] = None
        for o in self._maker_resting.get(symbol, []):
            if o.get("bracket_role") != "tp":
                continue
            try:
                p = float(o.get("price") or 0.0)
            except (TypeError, ValueError):
                p = 0.0
            if p > 0.0:
                tp = p
                break
        for o in self._maker_resting.get(symbol, []):
            if o.get("bracket_role") == "sl" or o.get("order_kind") == "stop_reduce":
                try:
                    p = float(o.get("stop_price") or o.get("price") or 0.0)
                except (TypeError, ValueError):
                    p = 0.0
                if p > 0.0:
                    sl = p
                    break
        if sl is None or sl <= 0:
            pos = self.positions.get(symbol)
            ex = (pos or {}).get("exit") if pos else None
            if ex and ex.get("active_sl") is not None:
                try:
                    av = float(ex.get("active_sl") or 0.0)
                except (TypeError, ValueError):
                    av = 0.0
                if av > 0.0:
                    sl = av
        if (tp is None or tp <= 0) or (sl is None or sl <= 0):
            pos2 = self.positions.get(symbol)
            if pos2:
                ect = dict(pos2.get("entry_context") or {})
                if bool(ect.get("beta_neutral_hf")):
                    if tp is None or tp <= 0:
                        try:
                            vtp = float(ect.get("beta_display_take_profit_price") or ect.get("take_profit_limit_price") or 0.0)
                        except (TypeError, ValueError):
                            vtp = 0.0
                        if vtp > 0.0:
                            tp = vtp
                    if sl is None or sl <= 0:
                        try:
                            vsl = float(ect.get("beta_display_stop_loss_price") or ect.get("stop_loss_limit_price") or 0.0)
                        except (TypeError, ValueError):
                            vsl = 0.0
                        if vsl > 0.0:
                            sl = vsl
        return {"take_profit": tp, "stop_loss": sl}

    def sync_beta_hf_display_tp_sl(self, symbol: str, take_profit_px: float, stop_loss_px: float) -> None:
        """BetaNeutralHF：把动态止盈/止损价写回持仓 entry_context，供 get_display_tp_sl 与前端展示。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 1e-12:
            return
        ect = dict(pos.get("entry_context") or {})
        if not bool(ect.get("beta_neutral_hf")):
            return
        if float(take_profit_px) > 0:
            ect["beta_display_take_profit_price"] = float(take_profit_px)
            ect["take_profit_limit_price"] = float(take_profit_px)
        if float(stop_loss_px) > 0:
            ect["beta_display_stop_loss_price"] = float(stop_loss_px)
            ect["stop_loss_limit_price"] = float(stop_loss_px)
        else:
            ect.pop("beta_display_stop_loss_price", None)
            ect.pop("stop_loss_limit_price", None)
            ect.pop("dynamic_stop_loss_price", None)
        pos["entry_context"] = ect

    def clear_beta_hf_symbol_tp_sl(self, symbol: str) -> None:
        """BetaNeutralHF 对冲腿：剥离独立 TP/SL，避免单腿 OCO 误触。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 1e-12:
            return
        ect = dict(pos.get("entry_context") or {})
        if not bool(ect.get("beta_neutral_hf")):
            return
        for k in (
            "take_profit_limit_price",
            "stop_loss_limit_price",
            "dynamic_stop_loss_price",
            "beta_display_take_profit_price",
            "beta_display_stop_loss_price",
        ):
            ect.pop(k, None)
        pos["entry_context"] = ect

    def _paper_require_entry_tp_sl(self) -> bool:
        try:
            from src.core.config_manager import config_manager

            return bool(config_manager.get_config().paper_engine.require_entry_tp_sl_limits)
        except Exception:
            return False

    @staticmethod
    def _entry_tp_sl_exempt(ctx: Dict[str, Any]) -> bool:
        sch = ctx.get("scheme")
        if sch in ("micro_maker", "liquidation_snipe", "funding_squeeze"):
            return True
        return bool(
            ctx.get("infinite_matrix_ultra")
            or ctx.get("beta_neutral_hf")
            or ctx.get("beta_hedge_anchor_adjust")
            or ctx.get("l1_managed")
            or ctx.get("slingshot_managed")
            or ctx.get("slingshot_maker_entry")
            or ctx.get("assassin_managed")
            or ctx.get("leadlag_managed")
            or ctx.get("high_conviction_trailing")
            or ctx.get("playbook_guerrilla")
        )

    @staticmethod
    def _has_explicit_tp_sl(ctx: Dict[str, Any]) -> bool:
        try:
            tp = float(ctx.get("take_profit_limit_price") or 0.0)
            sl = float(ctx.get("stop_loss_limit_price") or 0.0)
            return tp > 0.0 and sl > 0.0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _validate_tp_sl_vs_entry(
        side: str, entry_px: float, tp_px: float, sl_px: float
    ) -> Optional[str]:
        if entry_px <= 0:
            return "Invalid entry price for TP/SL validation"
        s = str(side).lower()
        if s == "buy":
            if not (sl_px < entry_px < tp_px):
                return (
                    "TP/SL invalid for long: need stop < entry < take_profit "
                    f"({sl_px:.8f} < {entry_px:.8f} < {tp_px:.8f})"
                )
        elif s == "sell":
            if not (tp_px < entry_px < sl_px):
                return (
                    "TP/SL invalid for short: need take_profit < entry < stop "
                    f"({tp_px:.8f} < {entry_px:.8f} < {sl_px:.8f})"
                )
        else:
            return "Unknown order side for TP/SL"
        return None

    def _default_contract_size(self) -> float:
        try:
            from src.core.config_manager import config_manager

            v = float(config_manager.get_config().paper_engine.default_contract_size)
            return v if v > 0 else 1.0
        except Exception:
            return 1.0

    def _resolve_contract_size(self, symbol: str) -> float:
        """每张合约对应标的资产数量（Gate: quanto_multiplier）。"""
        try:
            from src.core.globals import bot_context

            ex = bot_context.get_exchange()
            if ex and getattr(ex, "contract_specs_cache", None):
                spec = ex.contract_specs_cache.get(symbol) or {}
                qm = float(spec.get("quanto_multiplier") or 0.0)
                if qm > 0:
                    return qm
        except Exception:
            pass
        return self._default_contract_size()

    def contract_size_for_symbol(self, symbol: str) -> float:
        """每张合约对应标的数量（Gate quanto_multiplier）；用于策略把目标 USDT 名义换成张数。"""
        return max(float(self._resolve_contract_size(symbol)), 1e-18)

    def contracts_for_target_usdt_notional(
        self, symbol: str, ref_price_usdt: float, usdt_notional: float
    ) -> float:
        """张数 = 目标名义USDT / (价格 × 每张面值)，与 execute_order 的 amount=张数一致。"""
        px = float(ref_price_usdt)
        if px <= 0 or usdt_notional <= 0:
            return 0.0
        denom = px * self.contract_size_for_symbol(symbol)
        if denom <= 1e-24:
            return 0.0
        return max(float(usdt_notional) / denom, 0.0)

    def _position_contract_size(self, pos: Optional[Dict[str, Any]], symbol: str) -> float:
        if pos:
            cs = float(pos.get("contract_size") or 0.0)
            if cs > 0:
                return cs
        return self._resolve_contract_size(symbol)

    @staticmethod
    def _paper_mmr_rate(specs: Optional[Dict[str, Any]]) -> float:
        if specs:
            for key in (
                "maintenance_margin_rate",
                "maintenance_rate",
                "mmr",
                "mmr_rate",
            ):
                try:
                    v = float(specs.get(key, 0.0) or 0.0)
                    if v > 0:
                        return v
                except Exception:
                    pass
        return 0.005

    @staticmethod
    def _liq_price_from_margin_fraction(entry_price: float, side: str, margin_frac: float, mmr_rate: float) -> float:
        ep = float(entry_price or 0.0)
        if ep <= 0:
            return 0.0
        room = max(float(margin_frac) - float(mmr_rate), 1e-6)
        if str(side).lower() in ("buy", "long"):
            return ep * (1.0 - room)
        return ep * (1.0 + room)

    def _extract_stop_loss_price(self, entry_context: Optional[Dict[str, Any]]) -> float:
        ctx = dict(entry_context or {})
        for key in (
            "stop_loss_limit_price",
            "dynamic_stop_loss_price",
            "beta_stop_loss_price",
        ):
            try:
                px = float(ctx.get(key, 0.0) or 0.0)
                if px > 0:
                    return px
            except Exception:
                pass
        return 0.0

    @staticmethod
    def _max_pyramid_layers() -> int:
        try:
            from src.core.config_manager import config_manager

            return max(1, int(getattr(config_manager.get_config().paper_engine, "max_pyramid_layers", 3) or 3))
        except Exception:
            return 3

    @staticmethod
    def _liq_safety_buffer_pct() -> float:
        return 0.001

    @staticmethod
    def _pyramid_layer_count(pos: Optional[Dict[str, Any]]) -> int:
        try:
            return max(1, int(float((pos or {}).get("pyramid_layers", 1) or 1)))
        except Exception:
            return 1

    def _compose_position_risk(
        self,
        *,
        symbol: str,
        side: str,
        total_contracts: float,
        vwap_entry: float,
        margin_used: float,
        leverage_hint: int,
        contract_size: float,
        specs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        total_contracts = float(total_contracts or 0.0)
        vwap_entry = float(vwap_entry or 0.0)
        contract_size = float(contract_size or 0.0)
        notional_entry = self._notional_from(total_contracts, vwap_entry, contract_size)
        if notional_entry <= 0 or margin_used <= 0:
            lev = max(int(leverage_hint or 1), 1)
            margin_used = max(notional_entry / lev, 0.0)
        margin_frac = margin_used / max(notional_entry, 1e-12)
        mmr_rate = self._paper_mmr_rate(specs)
        liquidation_price = self._liq_price_from_margin_fraction(vwap_entry, side, margin_frac, mmr_rate)
        effective_leverage = max(1.0, notional_entry / max(margin_used, 1e-12))
        return {
            "entry_notional_usdt": float(notional_entry),
            "margin_used": float(margin_used),
            "margin_fraction": float(margin_frac),
            "mmr_rate": float(mmr_rate),
            "liquidation_price": float(liquidation_price),
            "effective_leverage": float(effective_leverage),
        }

    def _validate_liquidation_guard(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        leverage: int,
        specs: Optional[Dict[str, Any]],
        entry_context: Optional[Dict[str, Any]],
    ) -> None:
        ect = dict(entry_context or {})
        # Beta 中性：SL 仅为限价预检占位，实际退出靠微利双腿平 / 断臂 / 保证金熔断，不参与真实强平缓冲校验。
        if bool(ect.get("beta_neutral_hf")):
            return
        sl_px = self._extract_stop_loss_price(entry_context)
        if sl_px <= 0:
            return
        mmr_rate = self._paper_mmr_rate(specs)
        liq_px = self._liq_price_from_margin_fraction(entry_price, side, 1.0 / max(int(leverage), 1), mmr_rate)
        cushion = float(entry_price) * self._liq_safety_buffer_pct()
        if str(side).lower() == "buy":
            if sl_px <= liq_px + cushion:
                raise SimulatedAPIError(
                    f"API Error 400: Stop loss too close to liquidation for {symbol}. "
                    f"SL={sl_px:.8f}, LIQ={liq_px:.8f}, buffer={cushion:.8f}",
                    label="LIQUIDATION_BUFFER_TOO_SMALL",
                )
        else:
            if sl_px >= liq_px - cushion:
                raise SimulatedAPIError(
                    f"API Error 400: Stop loss too close to liquidation for {symbol}. "
                    f"SL={sl_px:.8f}, LIQ={liq_px:.8f}, buffer={cushion:.8f}",
                    label="LIQUIDATION_BUFFER_TOO_SMALL",
                )

    @staticmethod
    def _notional_from(
        contracts: float, price: float, contract_size: float
    ) -> float:
        return float(contracts) * float(contract_size) * float(price)

    def apply_config_fees(self) -> None:
        """费率硬编码 TAKER_FEE_RATE / MAKER_FEE_RATE；忽略 YAML 与 CCXT。"""
        self.taker_fee = float(TAKER_FEE_RATE)
        self.maker_fee = float(MAKER_FEE_RATE)

    def _fee_rates_for_symbol(self, symbol: str) -> Tuple[float, float]:
        return float(TAKER_FEE_RATE), float(MAKER_FEE_RATE)

    @staticmethod
    def break_even_exit_price(
        side: str, entry: float, fee_rate_open: float, fee_rate_close: float
    ) -> float:
        """
        平仓价达到该值时（理想化），毛利约等于开+平手续费（不含滑点）。
        多: entry*(1+r_open)/(1-r_close) ；空对称。
        """
        if entry <= 0:
            return 0.0
        ro, rc = float(fee_rate_open), float(fee_rate_close)
        s = str(side).lower()
        if s in ("long", "buy"):
            return entry * (1.0 + ro) / max(1e-12, (1.0 - rc))
        return entry * (1.0 - ro) / max(1e-12, (1.0 + rc))

    def round_trip_edge_usdt(
        self,
        symbol: str,
        contracts: float,
        entry_price: float,
        exit_price: float,
        *,
        position_side: str,
        entry_is_taker: bool,
        exit_is_maker: bool,
        include_spread_penalty: bool = True,
    ) -> Dict[str, Any]:
        """
        预估一轮开平的净值（USDT）：毛利 − 开仓费 − 平仓费 − 可选点差惩罚。
        position_side: 最终持仓方向 long | short（与「买开多」一致）。
        """
        cs = self._resolve_contract_size(symbol)
        base = float(contracts) * cs
        tk, mk = self._fee_rates_for_symbol(symbol)
        r_in = float(tk if entry_is_taker else mk)
        r_out = float(mk if exit_is_maker else tk)
        n_in = self._notional_from(contracts, entry_price, cs)
        n_out = self._notional_from(contracts, exit_price, cs)
        fee_in = n_in * r_in
        fee_out = n_out * r_out
        spread_pen = 0.0
        if include_spread_penalty:
            sf = self._leadlag_spread_mid_frac(symbol)
            spread_pen = 0.5 * sf * (n_in + n_out)
        ps = str(position_side).lower()
        if ps == "long":
            gross = base * (float(exit_price) - float(entry_price))
        else:
            gross = base * (float(entry_price) - float(exit_price))
        net = gross - fee_in - fee_out - spread_pen
        return {
            "gross_usdt": gross,
            "fee_open_usdt": fee_in,
            "fee_close_usdt": fee_out,
            "spread_penalty_usdt": spread_pen,
            "expected_net_usdt": net,
            "contract_size": cs,
            "notional_open_usdt": n_in,
            "notional_close_usdt": n_out,
            "fee_rate_open": r_in,
            "fee_rate_close": r_out,
        }

    def cancel_slingshot_entry_orders(self, symbol: str) -> int:
        """撤掉未成交的弹弓 Post-Only 进场挂单（不含已开仓的括号腿）。"""
        rest = self._maker_resting.get(symbol)
        if not rest:
            return 0
        kept: List[Dict[str, Any]] = []
        n = 0
        for o in rest:
            if (o.get("entry_context") or {}).get("slingshot_maker_entry"):
                n += 1
                continue
            kept.append(o)
        if n:
            self._maker_resting[symbol] = kept
        return n

    def _clear_bracket_resting(self, symbol: str, oco_id: Optional[str]) -> None:
        if not oco_id:
            return
        rest = self._maker_resting.get(symbol)
        if not rest:
            return
        self._maker_resting[symbol] = [o for o in rest if o.get("bracket_oco_id") != oco_id]

    def _best_bid_ask(self, symbol: str) -> Tuple[float, float]:
        ob = self.orderbooks_cache.get(symbol)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return (0.0, 0.0)
        try:
            return (float(ob["bids"][0][0]), float(ob["asks"][0][0]))
        except (TypeError, ValueError, IndexError):
            return (0.0, 0.0)

    @staticmethod
    def _maker_limit_touch(o: Dict[str, Any], last: float, bb: float, ba: float) -> bool:
        lim = float(o["price"])
        if o["side"] == "buy":
            if ba > 0 and ba <= lim:
                return True
            if last > 0 and last <= lim:
                return True
        else:
            if bb > 0 and bb >= lim:
                return True
            if last > 0 and last >= lim:
                return True
        return False

    @staticmethod
    def _bracket_sl_limit_touch(o: Dict[str, Any], last: float, bb: float, ba: float) -> bool:
        """
        括号「止损」减仓限价触价（与止盈方向相反，不能用 _maker_limit_touch）。
        多仓止损：卖限价在现价下方 → 须 last/bid 跌至挂单价及以下才成交。
        空仓止损：买限价在现价上方 → 须 last/ask 涨至挂单价及以上才成交。
        """
        lim = float(o.get("price") or 0)
        if lim <= 0 or last <= 0:
            return False
        side = str(o.get("side") or "")
        if side == "sell":
            if bb > 0 and bb <= lim:
                return True
            return last <= lim
        if side == "buy":
            if ba > 0 and ba >= lim:
                return True
            return last >= lim
        return False

    def _leadlag_spread_mid_frac(self, symbol: str) -> float:
        bb, ba = self._best_bid_ask(symbol)
        if bb <= 0 or ba <= 0 or ba < bb:
            return 0.0
        mid = 0.5 * (bb + ba)
        return (ba - bb) / mid if mid > 0 else 0.0

    @staticmethod
    def _post_only_limit_touch(o: Dict[str, Any], last: float, bb: float, ba: float) -> bool:
        """Post-Only 止盈：卖价须高于买一、买价须低于卖一；成交仅当价格穿过挂单价（Maker 侧）。"""
        lim = float(o.get("price") or 0)
        if lim <= 0 or last <= 0:
            return False
        side = str(o.get("side") or "")
        if side == "sell":
            if bb > 0 and lim <= bb:
                return False
            return last >= lim
        if side == "buy":
            if ba > 0 and lim >= ba:
                return False
            return last <= lim
        return False

    def _leadlag_breakeven_trail(self, symbol: str, last: float) -> None:
        """价格越过盈亏平衡后，将深水止损抬至 进场价±开仓 taker（保本结界）。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        ect = pos.get("entry_context") or {}
        if not ect.get("leadlag_bracket_protocol"):
            return
        if pos.get("_leadlag_be_lifted"):
            return
        try:
            from src.core.config_manager import config_manager

            ll = config_manager.get_config().binance_leadlag
            arm = float(ll.breakeven_arm_frac)
        except Exception:
            arm = 0.0002
        taker = float(self.taker_fee)
        entry = float(pos["entry_price"])
        oco = ect.get("bracket_oco_id")
        if not oco or entry <= 0:
            return

        if pos["side"] == "long":
            if last < entry * (1.0 + taker + arm):
                return
            new_sp = entry * (1.0 + taker)
        else:
            if last > entry * (1.0 - taker - arm):
                return
            new_sp = entry * (1.0 - taker)

        rest = self._maker_resting.get(symbol)
        if not rest:
            return
        touched = False
        for o in rest:
            if o.get("order_kind") != "stop_reduce":
                continue
            if o.get("bracket_oco_id") != oco:
                continue
            ps = str(o.get("position_side") or "")
            if pos["side"] == "long" and ps != "long":
                continue
            if pos["side"] == "short" and ps != "short":
                continue
            old = float(o.get("stop_price") or 0)
            if pos["side"] == "long":
                o["stop_price"] = max(old, new_sp) if old > 0 else new_sp
            else:
                o["stop_price"] = min(old, new_sp) if old > 0 else new_sp
            o["price"] = o["stop_price"]
            touched = True
        if touched:
            pos["_leadlag_be_lifted"] = True
            log.info(
                f"[Paper] LeadLag BE lift stop → {new_sp:.6f} {symbol} (last={last:.6f})"
            )

    def _core_fixed_bracket_breakeven_lift(self, symbol: str, last: float) -> None:
        """Core 显式 TP/SL：达到首段浮盈后，将止损抬到保本附近。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        ect = pos.get("entry_context") or {}
        if not ect.get("fixed_tp_sl_protocol"):
            return
        if pos.get("_core_be_lifted"):
            return

        oco = ect.get("bracket_oco_id")
        entry = float(pos.get("entry_price") or 0.0)
        tp = float(ect.get("take_profit_limit_price") or 0.0)
        sl = float(ect.get("stop_loss_limit_price") or 0.0)
        if not oco or entry <= 0 or tp <= 0 or sl <= 0 or last <= 0:
            return

        try:
            from src.core.config_manager import config_manager

            prm = config_manager.get_config().strategy.params
            arm_r = float(getattr(prm, "core_breakeven_arm_r", 0.40) or 0.40)
            fee_buf_bps = float(getattr(prm, "core_breakeven_fee_buffer_bps", 6.0) or 6.0)
        except Exception:
            arm_r = 0.40
            fee_buf_bps = 6.0

        side = str(pos.get("side") or "")
        risk_dist = abs(entry - sl)
        if risk_dist <= 0:
            return
        taker = float(self.taker_fee)
        fee_buf = entry * (fee_buf_bps / 10000.0) + entry * 2.0 * taker
        if side == "long":
            if last < entry + (risk_dist * arm_r):
                return
            new_stop = entry + fee_buf
        else:
            if last > entry - (risk_dist * arm_r):
                return
            new_stop = entry - fee_buf

        rest = self._maker_resting.get(symbol)
        if not rest:
            return
        touched = False
        for o in rest:
            if o.get("bracket_oco_id") != oco or o.get("bracket_role") != "sl":
                continue
            old = float(o.get("price") or 0.0)
            if side == "long":
                if old > 0 and new_stop <= old:
                    continue
            else:
                if old > 0 and new_stop >= old:
                    continue
            o["price"] = float(new_stop)
            octx = dict(o.get("entry_context") or {})
            octx["stop_loss_limit_price"] = float(new_stop)
            octx["core_breakeven_lifted"] = True
            o["entry_context"] = octx
            touched = True
        if touched:
            ect["stop_loss_limit_price"] = float(new_stop)
            ect["core_breakeven_lifted"] = True
            pos["entry_context"] = ect
            pos["_core_be_lifted"] = True
            log.info(
                f"[Paper] Core BE lift stop → {new_stop:.6f} {symbol} (last={last:.6f})"
            )

    @staticmethod
    def _bracket_stop_triggered(o: Dict[str, Any], last: float, bb: float, ba: float) -> bool:
        sp = float(o.get("stop_price") or o.get("price") or 0)
        if sp <= 0 or last <= 0:
            return False
        pside = str(o.get("position_side") or "")
        if pside == "long":
            if bb > 0 and bb <= sp:
                return True
            return last <= sp
        if pside == "short":
            if ba > 0 and ba >= sp:
                return True
            return last >= sp
        return False

    def _maybe_bracket_tp_decay(self, symbol: str) -> None:
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        ctx = pos.get("entry_context") or {}
        if not ctx.get("l1_bracket_protocol"):
            return
        lc = _l1_fast_loop_config()
        if not lc:
            return
        decay_sec = float(lc.bracket_tp_decay_sec)
        decay_bps = float(lc.bracket_tp_decay_bps)
        entry = float(pos["entry_price"])
        side = str(pos["side"])
        rest = self._maker_resting.get(symbol)
        if not rest:
            return
        now = time.time()
        for o in rest:
            if o.get("order_kind") == "stop_reduce":
                continue
            if not (o.get("bracket_role") == "tp" or o.get("l1_tp_limit")):
                continue
            if not o.get("bracket_decay_armed"):
                continue
            since = float(o.get("bracket_tp_since") or 0)
            if since <= 0 or now - since < decay_sec:
                continue
            o["bracket_decay_armed"] = False
            if side == "long":
                o["price"] = entry * (1.0 + decay_bps / 1e4)
            else:
                o["price"] = entry * (1.0 - decay_bps / 1e4)
            log.info(
                f"[Paper] L1 bracket TP decay -> {decay_bps:g}bps from entry {symbol} @ {float(o['price']):.6f}"
            )

    def _queue_l1_brackets(
        self,
        symbol: str,
        exec_price: float,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
    ) -> None:
        lc = _l1_fast_loop_config()
        if lc and bool(lc.bracket_protocol):
            taker_bps = float(lc.bracket_taker_fee_bps)
            net_bps = float(lc.bracket_net_target_bps)
            tp_total_bps = taker_bps + net_bps
            atr_bps = float(ctx.get("l1_atr_bps") or 0)
            if atr_bps <= 0:
                micro = ctx.get("l1_signal_micro") or {}
                atr_bps = float(micro.get("atr_1m_bps") or 0)
            if atr_bps <= 0:
                atr_bps = float(lc.min_atr_bps)
            sl_frac = max(
                float(lc.bracket_sl_floor_bps) / 1e4,
                (atr_bps / 1e4) * float(lc.bracket_sl_atr_mult),
            )
            oco = str(uuid.uuid4())
            now = time.time()
            base_ctx = {**ctx, "l1_bracket_protocol": True, "bracket_oco_id": oco}
            if open_side == "buy":
                tp_px = exec_price * (1.0 + tp_total_bps / 1e4)
                sl_px = exec_price * (1.0 - sl_frac)
                tp_ctx = {
                    **base_ctx,
                    "resting_quote": True,
                    "l1_tp_limit": True,
                    "l1_managed": True,
                }
                self._maker_resting.setdefault(symbol, []).append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "sell",
                        "amount": float(amount),
                        "price": tp_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": tp_ctx,
                        "order_kind": "maker_limit",
                        "bracket_oco_id": oco,
                        "bracket_role": "tp",
                        "bracket_tp_since": now,
                        "bracket_decay_armed": True,
                    }
                )
                sl_ctx = {**base_ctx, "l1_managed": True}
                self._maker_resting[symbol].append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "sell",
                        "amount": float(amount),
                        "price": sl_px,
                        "stop_price": sl_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": sl_ctx,
                        "order_kind": "stop_reduce",
                        "bracket_oco_id": oco,
                        "bracket_role": "sl",
                        "position_side": "long",
                    }
                )
            else:
                tp_px = exec_price * (1.0 - tp_total_bps / 1e4)
                sl_px = exec_price * (1.0 + sl_frac)
                tp_ctx = {
                    **base_ctx,
                    "resting_quote": True,
                    "l1_tp_limit": True,
                    "l1_managed": True,
                }
                self._maker_resting.setdefault(symbol, []).append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "buy",
                        "amount": float(amount),
                        "price": tp_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": tp_ctx,
                        "order_kind": "maker_limit",
                        "bracket_oco_id": oco,
                        "bracket_role": "tp",
                        "bracket_tp_since": now,
                        "bracket_decay_armed": True,
                    }
                )
                sl_ctx = {**base_ctx, "l1_managed": True}
                self._maker_resting[symbol].append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "buy",
                        "amount": float(amount),
                        "price": sl_px,
                        "stop_price": sl_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": sl_ctx,
                        "order_kind": "stop_reduce",
                        "bracket_oco_id": oco,
                        "bracket_role": "sl",
                        "position_side": "short",
                    }
                )
            self.positions[symbol]["entry_context"] = base_ctx
            self.positions[symbol]["bracket_oco_id"] = oco
            log.info(
                f"[Paper] L1 bracket OCO={oco[:8]}… TP {tp_px:.6f} SL_stop {sl_px:.6f} "
                f"(tp={tp_total_bps:g}bps sl_frac={sl_frac*100:.3f}%)"
            )
            return
        if open_side == "buy":
            tp_bps = float(ctx.get("l1_tp_bps", 30.0))
            tp_px = exec_price * (1.0 + tp_bps / 10000.0)
            oid_tp = str(uuid.uuid4())
            tp_ctx = {
                **ctx,
                "resting_quote": True,
                "l1_tp_limit": True,
                "l1_managed": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": oid_tp,
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                }
            )
            log.info(f"[Paper] L1 TP maker queued SELL {amount} {symbol} @ {tp_px:.6f} (+{tp_bps}bps)")
        else:
            tp_bps = float(ctx.get("l1_tp_bps", 30.0))
            tp_px = exec_price * (1.0 - tp_bps / 10000.0)
            oid_tp = str(uuid.uuid4())
            tp_ctx = {
                **ctx,
                "resting_quote": True,
                "l1_tp_limit": True,
                "l1_managed": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": oid_tp,
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                }
            )
            log.info(f"[Paper] L1 TP maker queued BUY {amount} {symbol} @ {tp_px:.6f} (-{tp_bps}bps)")

    def _queue_explicit_tp_sl_brackets(
        self,
        symbol: str,
        exec_price: float,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
        tp_px: float,
        sl_px: float,
    ) -> None:
        """开仓即挂 OCO：止盈/止损均为限价 maker 单（成交价=挂单价），不再用 stop_reduce 市价甩单。"""
        ctx["fixed_tp_sl_protocol"] = True
        oco = str(uuid.uuid4())
        base_ctx = {**ctx, "bracket_oco_id": oco}
        if open_side == "buy":
            tp_ctx = {**base_ctx, "resting_quote": True, "l1_tp_limit": True}
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": float(tp_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": float(sl_px),
                    "stop_price": float(sl_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "long",
                }
            )
        else:
            tp_ctx = {**base_ctx, "resting_quote": True, "l1_tp_limit": True}
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": float(tp_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": float(sl_px),
                    "stop_price": float(sl_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "short",
                }
            )
        if symbol in self.positions:
            self.positions[symbol]["entry_context"] = ctx
            self.positions[symbol]["bracket_oco_id"] = oco
        log.info(
            f"[Paper] Entry TP/SL OCO={oco[:8]}… TP {float(tp_px):.6f} SL {float(sl_px):.6f} {symbol}"
        )

    def _queue_slingshot_brackets(
        self,
        symbol: str,
        exec_price: float,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
    ) -> None:
        sc = _slingshot_config()
        if not sc:
            return
        tp_bps = float(sc.tp_bps)
        sl_bps = float(sc.sl_bps)
        sl_frac = sl_bps / 1e4
        tp_frac = tp_bps / 1e4
        oco = str(uuid.uuid4())
        base_ctx = {**ctx, "slingshot_managed": True, "bracket_oco_id": oco}
        if open_side == "buy":
            tp_px = exec_price * (1.0 + tp_frac)
            sl_px = exec_price * (1.0 - sl_frac)
            tp_ctx = {
                **base_ctx,
                "resting_quote": True,
                "l1_tp_limit": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "long",
                }
            )
        else:
            tp_px = exec_price * (1.0 - tp_frac)
            sl_px = exec_price * (1.0 + sl_frac)
            tp_ctx = {
                **base_ctx,
                "resting_quote": True,
                "l1_tp_limit": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "short",
                }
            )
        self.positions[symbol]["entry_context"] = base_ctx
        self.positions[symbol]["bracket_oco_id"] = oco
        log.info(
            f"[Paper] Slingshot OCO={oco[:8]}… TP {tp_px:.6f} SL_stop {sl_px:.6f} "
            f"(tp={tp_bps:g}bps sl={sl_bps:g}bps)"
        )

    def _queue_leadlag_bracket_protocol(
        self,
        symbol: str,
        exec_price: float,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
    ) -> None:
        """
        Taker 进场成交后立刻：Post-Only 限价止盈 + 本地 stop OCO。
        P_tp = P_fill * (1 ± (fee_taker + spread_mid + margin_net))，并受 bracket_min_tp_bps 下限约束。
        """
        try:
            from src.core.config_manager import config_manager

            ll = config_manager.get_config().binance_leadlag
        except Exception:
            ll = None

        taker = float(self.taker_fee)
        spread_pen = float(self._leadlag_spread_mid_frac(symbol))
        margin_net = float(
            ctx.get(
                "leadlag_target_net_frac",
                getattr(ll, "bracket_target_net_frac", 0.0015) if ll else 0.0015,
            )
        )
        min_tp_bps = float(
            ctx.get("leadlag_min_tp_bps", getattr(ll, "bracket_min_tp_bps", 15.0) if ll else 15.0)
        )
        init_sl_bps = float(
            ctx.get("leadlag_initial_sl_bps", getattr(ll, "initial_sl_bps", 100.0) if ll else 100.0)
        )

        total_frac = taker + spread_pen + max(0.0, margin_net)
        min_frac = max(0.0, min_tp_bps / 1e4)
        if total_frac < min_frac:
            total_frac = min_frac

        sl_frac = init_sl_bps / 1e4
        oco = str(uuid.uuid4())
        base_ctx = {
            **ctx,
            "leadlag_managed": True,
            "leadlag_bracket_protocol": True,
            "bracket_oco_id": oco,
        }
        bb, ba = self._best_bid_ask(symbol)
        nudge = 1e-5

        if open_side == "buy":
            tp_px = exec_price * (1.0 + total_frac)
            if bb > 0 and tp_px <= bb:
                tp_px = bb * (1.0 + nudge)
            if tp_px < exec_price * (1.0 + min_frac):
                tp_px = exec_price * (1.0 + min_frac)
            sl_px = exec_price * (1.0 - sl_frac)
            tp_ctx = {
                **base_ctx,
                "resting_quote": True,
                "leadlag_tp_limit": True,
                "post_only": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": float(tp_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "leadlag_post_only": True,
                }
            )
            sl_ctx = {**base_ctx}
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "long",
                }
            )
        else:
            tp_px = exec_price * (1.0 - total_frac)
            if ba > 0 and tp_px >= ba:
                tp_px = ba * (1.0 - nudge)
            if tp_px > exec_price * (1.0 - min_frac):
                tp_px = exec_price * (1.0 - min_frac)
            sl_px = exec_price * (1.0 + sl_frac)
            tp_ctx = {
                **base_ctx,
                "resting_quote": True,
                "leadlag_tp_limit": True,
                "post_only": True,
            }
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": float(tp_px),
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "leadlag_post_only": True,
                }
            )
            sl_ctx = {**base_ctx}
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "short",
                }
            )

        if symbol in self.positions:
            self.positions[symbol]["entry_context"] = base_ctx
            self.positions[symbol]["bracket_oco_id"] = oco
        log.info(
            f"[Paper] LeadLag PO+OCO={oco[:8]}… TP {tp_px:.6f} (post-only) SL_stop {sl_px:.6f} "
            f"edge={total_frac*100:.4f}% spread={spread_pen*10000:.2f}bps"
        )

    def _assassin_unwind_invalid(
        self,
        symbol: str,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
        reason: str,
    ) -> None:
        close_side = "sell" if open_side == "buy" else "buy"
        ect = dict(ctx)
        log.warning(f"[Paper] Assassin unwind ({reason}) {symbol}")
        self.execute_order(
            symbol,
            close_side,
            float(amount),
            None,
            reduce_only=True,
            leverage=int(leverage),
            margin_mode=str(mm),
            berserker=berserker,
            post_only=False,
            entry_context=ect,
            exit_reason="assassin_invalid_tp_unwind",
        )

    def _queue_assassin_brackets(
        self,
        symbol: str,
        exec_price: float,
        amount: float,
        leverage: int,
        mm: str,
        berserker: bool,
        ctx: Dict[str, Any],
        open_side: str,
    ) -> None:
        ac = _assassin_micro_config()
        vwap = float(ctx.get("assassin_target_vwap") or 0.0)
        frac = float(ctx.get("assassin_tp_path_fraction", (ac.tp_path_fraction if ac else 0.8)))
        sl_bps = float(ctx.get("assassin_sl_bps", (ac.sl_bps if ac else 25.0)))
        min_tp_bps = float(ac.min_tp_bps if ac else 5.0)
        sl_frac = sl_bps / 1e4
        min_tp_frac = min_tp_bps / 1e4
        oco = str(uuid.uuid4())
        base_ctx = {**ctx, "assassin_managed": True, "bracket_oco_id": oco}
        cost_aware = bool(ctx.get("assassin_cost_aware", False))

        if cost_aware:
            hurdle = assassin_hurdle_rate(symbol, self)
            tn = float(ctx.get("assassin_target_net_frac", getattr(ac, "target_net_frac", 5e-4)))
            if open_side == "buy":
                tp_px = long_hard_tp_price(exec_price, hurdle, tn)
                if vwap > 0 and tp_px >= vwap:
                    self._assassin_unwind_invalid(
                        symbol, amount, leverage, mm, berserker, base_ctx, "buy", "tp_ge_vwap"
                    )
                    return
                if tp_px < exec_price * (1.0 + min_tp_frac):
                    tp_px = exec_price * (1.0 + min_tp_frac)
                    if vwap > 0 and tp_px >= vwap:
                        self._assassin_unwind_invalid(
                            symbol, amount, leverage, mm, berserker, base_ctx, "buy", "tp_ge_vwap_after_min_tp"
                        )
                        return
            else:
                tp_px = short_hard_tp_price(exec_price, hurdle, tn)
                if vwap > 0 and tp_px <= vwap:
                    self._assassin_unwind_invalid(
                        symbol, amount, leverage, mm, berserker, base_ctx, "sell", "tp_le_vwap"
                    )
                    return
                if tp_px > exec_price * (1.0 - min_tp_frac):
                    tp_px = exec_price * (1.0 - min_tp_frac)
                    if vwap > 0 and tp_px <= vwap:
                        self._assassin_unwind_invalid(
                            symbol, amount, leverage, mm, berserker, base_ctx, "sell", "tp_le_vwap_after_min_tp"
                        )
                        return
            sl_px = (
                exec_price * (1.0 - sl_frac)
                if open_side == "buy"
                else exec_price * (1.0 + sl_frac)
            )
            tp_ctx = {**base_ctx, "resting_quote": True, "l1_tp_limit": True}
            if open_side == "buy":
                self._maker_resting.setdefault(symbol, []).append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "sell",
                        "amount": float(amount),
                        "price": tp_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": tp_ctx,
                        "order_kind": "maker_limit",
                        "bracket_oco_id": oco,
                        "bracket_role": "tp",
                        "bracket_decay_armed": False,
                    }
                )
                sl_ctx = dict(base_ctx)
                self._maker_resting[symbol].append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "sell",
                        "amount": float(amount),
                        "price": sl_px,
                        "stop_price": sl_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": sl_ctx,
                        "order_kind": "stop_reduce",
                        "bracket_oco_id": oco,
                        "bracket_role": "sl",
                        "position_side": "long",
                    }
                )
            else:
                self._maker_resting.setdefault(symbol, []).append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "buy",
                        "amount": float(amount),
                        "price": tp_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": tp_ctx,
                        "order_kind": "maker_limit",
                        "bracket_oco_id": oco,
                        "bracket_role": "tp",
                        "bracket_decay_armed": False,
                    }
                )
                sl_ctx = dict(base_ctx)
                self._maker_resting[symbol].append(
                    {
                        "id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "side": "buy",
                        "amount": float(amount),
                        "price": sl_px,
                        "stop_price": sl_px,
                        "leverage": int(leverage),
                        "margin_mode": mm,
                        "berserker": berserker,
                        "reduce_only": True,
                        "entry_context": sl_ctx,
                        "order_kind": "stop_reduce",
                        "bracket_oco_id": oco,
                        "bracket_role": "sl",
                        "position_side": "short",
                    }
                )
            self.positions[symbol]["entry_context"] = base_ctx
            self.positions[symbol]["bracket_oco_id"] = oco
            log.info(
                f"[Paper] Assassin(cost) OCO={oco[:8]}… TP {tp_px:.6f} SL {sl_px:.6f} "
                f"vwap={vwap:.6f} hurdle={hurdle*10000:.2f}bps net={tn*10000:.2f}bps"
            )
            return

        if open_side == "buy":
            if vwap > exec_price:
                tp_px = exec_price + frac * (vwap - exec_price)
            else:
                tp_px = exec_price * (1.0 + min_tp_frac)
            if tp_px <= exec_price:
                tp_px = exec_price * (1.0 + min_tp_frac)
            sl_px = exec_price * (1.0 - sl_frac)
            tp_ctx = {**base_ctx, "resting_quote": True, "l1_tp_limit": True}
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "sell",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "long",
                }
            )
        else:
            if vwap < exec_price and vwap > 0:
                tp_px = exec_price - frac * (exec_price - vwap)
            else:
                tp_px = exec_price * (1.0 - min_tp_frac)
            if tp_px >= exec_price:
                tp_px = exec_price * (1.0 - min_tp_frac)
            sl_px = exec_price * (1.0 + sl_frac)
            tp_ctx = {**base_ctx, "resting_quote": True, "l1_tp_limit": True}
            self._maker_resting.setdefault(symbol, []).append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": tp_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": tp_ctx,
                    "order_kind": "maker_limit",
                    "bracket_oco_id": oco,
                    "bracket_role": "tp",
                    "bracket_decay_armed": False,
                }
            )
            sl_ctx = dict(base_ctx)
            self._maker_resting[symbol].append(
                {
                    "id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "side": "buy",
                    "amount": float(amount),
                    "price": sl_px,
                    "stop_price": sl_px,
                    "leverage": int(leverage),
                    "margin_mode": mm,
                    "berserker": berserker,
                    "reduce_only": True,
                    "entry_context": sl_ctx,
                    "order_kind": "stop_reduce",
                    "bracket_oco_id": oco,
                    "bracket_role": "sl",
                    "position_side": "short",
                }
            )
        self.positions[symbol]["entry_context"] = base_ctx
        self.positions[symbol]["bracket_oco_id"] = oco
        log.info(
            f"[Paper] Assassin OCO={oco[:8]}… TP {tp_px:.6f} SL_stop {sl_px:.6f} vwap={vwap:.6f} path={frac}"
        )

    def _process_maker_fills(self, symbol: str) -> None:
        rest = self._maker_resting.get(symbol)
        if not rest:
            return
        last = float(self.latest_prices.get(symbol, 0) or 0)
        bb, ba = self._best_bid_ask(symbol)

        stop_oco: Optional[str] = None
        for o in rest:
            if o.get("order_kind") != "stop_reduce":
                continue
            if self._bracket_stop_triggered(o, last, bb, ba):
                stop_oco = o.get("bracket_oco_id")
                break

        if stop_oco:
            rest = [o for o in rest if o.get("bracket_oco_id") != stop_oco]
            self._maker_resting[symbol] = rest
            pos = self.positions.get(symbol)
            if pos and float(pos.get("size", 0) or 0) > 0:
                side_close = "sell" if pos["side"] == "long" else "buy"
                ectx = dict(pos.get("entry_context") or {})
                if ectx.get("slingshot_managed"):
                    er_stop = "slingshot_bracket_stop"
                elif ectx.get("assassin_managed"):
                    er_stop = "assassin_bracket_stop"
                elif ectx.get("leadlag_bracket_protocol"):
                    er_stop = "leadlag_bracket_stop"
                elif ectx.get("fixed_tp_sl_protocol"):
                    er_stop = "core_bracket_stop"
                else:
                    er_stop = "l1_bracket_stop"
                self.execute_order(
                    symbol,
                    side_close,
                    float(pos["size"]),
                    None,
                    reduce_only=True,
                    leverage=int(pos.get("leverage", 10)),
                    margin_mode=str(pos.get("margin_mode", "isolated")),
                    berserker=False,
                    post_only=False,
                    entry_context=ectx,
                    exit_reason=er_stop,
                )
            return

        purge_ocos = set()
        kept: List[Dict[str, Any]] = []
        for o in rest:
            oco = o.get("bracket_oco_id")
            if oco and oco in purge_ocos:
                continue
            kind = o.get("order_kind") or "maker_limit"
            if kind == "stop_reduce":
                if oco and oco in purge_ocos:
                    continue
                kept.append(o)
                continue
            if o.get("bracket_role") == "sl":
                touch = self._bracket_sl_limit_touch(o, last, bb, ba)
            elif o.get("leadlag_post_only") and o.get("bracket_role") == "tp":
                touch = self._post_only_limit_touch(o, last, bb, ba)
            else:
                touch = self._maker_limit_touch(o, last, bb, ba)
            if not touch:
                kept.append(o)
                continue
            ro = bool(o.get("reduce_only"))
            if oco:
                purge_ocos.add(oco)
                kept = [x for x in kept if x.get("bracket_oco_id") != oco]
            fill_ctx = dict(o.get("entry_context") or {})
            if fill_ctx.get("slingshot_maker_entry"):
                fill_ctx["slingshot_managed"] = True
                fill_ctx.pop("slingshot_maker_entry", None)
            fill_ctx.pop("resting_quote", None)
            fill_ctx.pop("l1_tp_limit", None)
            fill_ctx.pop("leadlag_tp_limit", None)
            fill_ctx["maker_filled"] = True
            er_tp = None
            if oco:
                if fill_ctx.get("slingshot_managed"):
                    er_tp = "slingshot_bracket_tp"
                elif fill_ctx.get("assassin_managed"):
                    er_tp = "assassin_bracket_tp"
                elif fill_ctx.get("leadlag_bracket_protocol"):
                    er_tp = "leadlag_bracket_tp"
                elif fill_ctx.get("fixed_tp_sl_protocol"):
                    er_tp = (
                        "core_bracket_sl"
                        if o.get("bracket_role") == "sl"
                        else "core_bracket_tp"
                    )
                else:
                    er_tp = "l1_bracket_tp"
            self.execute_order(
                o["symbol"],
                o["side"],
                float(o["amount"]),
                float(o["price"]),
                reduce_only=ro,
                leverage=int(o.get("leverage", 10)),
                margin_mode=str(o.get("margin_mode", "isolated")),
                berserker=bool(o.get("berserker", False)),
                post_only=False,
                entry_context=fill_ctx,
                exit_reason=er_tp,
            )

        kept = [x for x in kept if x.get("bracket_oco_id") not in purge_ocos]
        self._maker_resting[symbol] = kept

    def update_orderbook(self, symbol: str, bids: list, asks: list):
        self.orderbooks_cache[symbol] = {
            "bids": sorted(bids, key=lambda x: float(x[0]), reverse=True),
            "asks": sorted(asks, key=lambda x: float(x[0])),
        }
        self._maybe_bracket_tp_decay(symbol)
        last = float(self.latest_prices.get(symbol, 0) or 0)
        if last > 0:
            self._leadlag_breakeven_trail(symbol, last)
            self._core_fixed_bracket_breakeven_lift(symbol, last)
            self._process_shadow_orders(symbol, last)
        self._process_maker_fills(symbol)
        last2 = float(self.latest_prices.get(symbol, 0) or 0)
        if last2 > 0:
            self._calculate_pnl()
            self._maybe_high_conviction_trailing_exit(symbol, last2)
            self._check_time_stops(symbol)

    def update_price(self, symbol: str, price: float):
        self.latest_prices[symbol] = price
        self._maybe_bracket_tp_decay(symbol)
        self._leadlag_breakeven_trail(symbol, float(price))
        self._core_fixed_bracket_breakeven_lift(symbol, float(price))
        self._process_shadow_orders(symbol, float(price))
        self._process_maker_fills(symbol)
        self._calculate_pnl()
        self._maybe_high_conviction_trailing_exit(symbol, float(price))
        self._check_time_stops(symbol)

    def _maybe_high_conviction_trailing_exit(self, symbol: str, mark: float) -> None:
        """高置信度仓：ROE 达阈值后，按最有利极价回撤 callback 触发市价全平。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        if not pos.get("high_conviction_trailing"):
            return
        mp = float(mark)
        if mp <= 0:
            return
        try:
            from src.core.config_manager import config_manager

            exe = config_manager.get_config().execution
            min_hold = float(exe.high_conviction_trailing_min_hold_sec or 0.0)
            grace_after_arm = float(exe.trailing_callback_grace_after_arm_sec or 0.0)
        except Exception:
            min_hold = 2.0
            grace_after_arm = 1.0

        now = time.time()
        opened = float(pos.get("opened_at", 0) or 0)
        if min_hold > 0 and opened > 0 and (now - opened) < min_hold:
            return

        contracts = float(pos["size"])
        entry = float(pos.get("entry_price", 0) or 0)
        cs = self._position_contract_size(pos, symbol)
        lev = max(int(pos.get("leverage", 1) or 1), 1)
        if str(pos.get("side", "long")).lower() == "long":
            pnl = contracts * cs * (mp - entry)
        else:
            pnl = contracts * cs * (entry - mp)
        notional = self._notional_from(contracts, mp, cs)
        margin = notional / float(lev) if lev > 0 else notional
        roe = (pnl / margin) if margin > 1e-12 else 0.0

        act = float(pos.get("trailing_stop_activation_pct") or 0.02)
        cb = float(pos.get("trailing_stop_callback_pct") or 0.005)

        hi = max(float(pos.get("highest_unrealized_pnl_pct") or 0.0), roe)
        pos["highest_unrealized_pnl_pct"] = hi

        if not pos.get("trailing_armed"):
            if roe >= act - 1e-12:
                pos["trailing_armed"] = True
                pos["trailing_favorable_extreme"] = mp
                pos["trailing_armed_ts"] = now
                log.warning(
                    f"[TRAIL DEBUG] {symbol} ARM roe={roe:.6f}>={act:.6f} mark={mp:.8f} entry={entry:.8f} "
                    f"pnl={pnl:.6f} margin={margin:.6f} notional={notional:.6f} lev={lev} | "
                    f"callback exit suppressed for {grace_after_arm:.2f}s after arm"
                )
            return

        if grace_after_arm > 0:
            ts_arm = float(pos.get("trailing_armed_ts", 0) or 0)
            if ts_arm > 0 and (now - ts_arm) < grace_after_arm:
                return

        ext_raw = pos.get("trailing_favorable_extreme")
        if ext_raw is None:
            pos["trailing_favorable_extreme"] = mp
            ext_raw = mp
        ext = float(ext_raw)
        ps = str(pos.get("side", "long")).lower()
        if ps == "long":
            ext = max(ext, mp)
            pos["trailing_favorable_extreme"] = ext
            dd = (ext - mp) / ext if ext > 1e-12 else 0.0
        else:
            ext = min(ext, mp)
            pos["trailing_favorable_extreme"] = ext
            dd = (mp - ext) / ext if ext > 1e-12 else 0.0

        if dd < cb - 1e-12:
            return
        close_side = "sell" if ps == "long" else "buy"
        ect = dict(pos.get("entry_context") or {})
        ect["exit_reason"] = "high_conviction_trailing"
        log.warning(
            f"[TRAIL EXIT] {symbol} ROE={roe:.6f} dd_price={dd:.6f} vs cb={cb:.6f} "
            f"mark={mp:.8f} extreme={ext:.8f} entry={entry:.8f} margin={margin:.6f}"
        )
        self.execute_order(
            symbol,
            close_side,
            contracts,
            None,
            reduce_only=True,
            leverage=int(pos.get("leverage", 10)),
            margin_mode=str(pos.get("margin_mode", "isolated")),
            berserker=False,
            post_only=False,
            entry_context=ect,
            exit_reason="high_conviction_trailing",
        )

    def attach_high_conviction_trailing(
        self,
        symbol: str,
        activation_roe: float,
        callback_roe: float,
        extra_ctx: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """为已有仓位挂载 ROE 追踪止盈（Beta 断臂后主腿顺势奔跑）。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return False
        if pos.get("high_conviction_trailing"):
            return True
        pos["high_conviction_trailing"] = True
        pos["trailing_stop_activation_pct"] = max(1e-6, float(activation_roe))
        pos["trailing_stop_callback_pct"] = max(1e-6, float(callback_roe))
        pos["highest_unrealized_pnl_pct"] = 0.0
        pos["trailing_armed"] = False
        pos["trailing_favorable_extreme"] = None
        ect = dict(pos.get("entry_context") or {})
        ect.update(dict(extra_ctx or {}))
        ect["high_conviction_trailing"] = True
        ect.setdefault("beta_hf_ride_trailing", True)
        pos["entry_context"] = ect
        return True

    def clear_high_conviction_trailing(self, symbol: str) -> None:
        """矩阵模式回到 STABLE 时卸掉顺势追踪，避免与 1U 刷单逻辑冲突。"""
        pos = self.positions.get(symbol)
        if not pos:
            return
        for k in (
            "high_conviction_trailing",
            "trailing_stop_activation_pct",
            "trailing_stop_callback_pct",
            "highest_unrealized_pnl_pct",
            "trailing_armed",
            "trailing_armed_ts",
            "trailing_favorable_extreme",
        ):
            pos.pop(k, None)
        ect = dict(pos.get("entry_context") or {})
        ect.pop("high_conviction_trailing", None)
        ect.pop("beta_hf_ride_trailing", None)
        ect.pop("infinite_matrix_trend_ride", None)
        pos["entry_context"] = ect

    def _check_time_stops(self, symbol: str) -> None:
        """Playbook 游击仓：持仓超过 position_ttl_minutes 则市价平。"""
        pos = self.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        if bool((pos.get("entry_context") or {}).get("beta_neutral_hf")):
            return
        dl = float(pos.get("time_stop_deadline_ts") or 0.0)
        if dl <= 0:
            return
        if time.time() < dl:
            return
        contracts = float(pos["size"])
        ps = str(pos.get("side", "long")).lower()
        close_side = "sell" if ps == "long" else "buy"
        ect = dict(pos.get("entry_context") or {})
        ect["exit_reason"] = "time_stop"
        mp = float(self.latest_prices.get(symbol, 0) or pos.get("entry_price") or 0)
        log.warning(
            f"[TIME STOP] {symbol} deadline reached (mark≈{mp:.6f}) — reducing position"
        )
        self.execute_order(
            symbol,
            close_side,
            contracts,
            None,
            reduce_only=True,
            leverage=int(pos.get("leverage", 10)),
            margin_mode=str(pos.get("margin_mode", "isolated")),
            berserker=False,
            post_only=False,
            entry_context=ect,
            exit_reason="time_stop",
        )

    def estimate_flat_net_pnl(
        self, symbol: str, pos: Dict[str, Any], exit_price: float
    ) -> float:
        """按当前价市价平仓的预估净利：毛利 − 已累计手续费 − 预估本笔 taker 费（均按名义）。"""
        entry = float(pos.get("entry_price", 0) or 0)
        contracts = float(pos.get("size", 0) or 0)
        cs = float(pos.get("contract_size") or 0.0)
        if cs <= 0:
            cs = 1.0
        if entry <= 0 or contracts <= 0 or exit_price <= 0:
            return -1e30
        acc = float(pos.get("accumulated_fees", 0) or 0)
        side = str(pos.get("side", "long"))
        base = contracts * cs
        if side == "long":
            gross = (exit_price - entry) * base
        else:
            gross = (entry - exit_price) * base
        tk, _ = self._fee_rates_for_symbol(symbol)
        fee_close_n = self._notional_from(contracts, exit_price, cs) * float(tk)
        return gross - acc - fee_close_n

    def _calculate_pnl(self):
        total_unrealized = 0.0
        used_margin = 0.0

        for symbol, pos in self.positions.items():
            if pos["size"] == 0:
                continue

            current_price = float(
                self.latest_prices.get(symbol, pos["entry_price"]) or 0
            )
            contracts = float(pos["size"])
            entry = float(pos["entry_price"])
            cs = self._position_contract_size(pos, symbol)

            if pos["side"] == "long":
                pnl = contracts * cs * (current_price - entry)
            else:
                pnl = contracts * cs * (entry - current_price)

            pos["unrealized_pnl"] = pnl
            total_unrealized += pnl
            notional_mark = self._notional_from(contracts, current_price, cs)
            lev = max(int(pos.get("leverage", 1) or 1), 1)
            used_margin += float(pos.get("margin_used", 0.0) or 0.0) or (notional_mark / lev)

            if "max_unrealized" not in pos:
                pos["max_unrealized"] = pos["min_unrealized"] = pnl
            else:
                pos["max_unrealized"] = max(float(pos["max_unrealized"]), pnl)
                pos["min_unrealized"] = min(float(pos["min_unrealized"]), pnl)

        self.balance = self.initial_balance + total_unrealized
        self.available_balance = self.balance - used_margin

    def _calculate_slippage_execution(self, symbol: str, side: str, amount: float) -> float:
        ob = self.orderbooks_cache.get(symbol)
        fb_bps, extra_bps = _paper_engine_settings()

        if not ob or not ob["bids"] or not ob["asks"]:
            current_price = self.latest_prices.get(symbol, 0)
            if current_price == 0:
                raise ValueError(f"No price or orderbook available for {symbol}")
            bps = max(0.0, fb_bps)
            adj = bps / 10_000.0
            log.warning(
                f"[Paper] No OB for {symbol}. Using last with fallback_slippage_bps={bps:g} (config paper_engine)."
            )
            if side == "buy":
                return float(current_price) * (1.0 + adj)
            return float(current_price) * (1.0 - adj)

        cs = self._resolve_contract_size(symbol)
        remaining_amount = amount
        total_notional = 0.0
        levels = ob["asks"] if side == "buy" else ob["bids"]

        for price_str, size_str in levels:
            price = float(price_str)
            sz = float(size_str)

            if sz >= remaining_amount:
                total_notional += price * remaining_amount * cs
                remaining_amount = 0
                break
            total_notional += price * sz * cs
            remaining_amount -= sz

        if remaining_amount > 0:
            log.warning(f"[Paper] Orderbook depth insufficient for {amount} {symbol}. Slipping remaining to last level.")
            last_price = float(levels[-1][0])
            total_notional += last_price * remaining_amount * cs

        denom = amount * cs
        vwap = total_notional / denom if denom > 0 else 0.0
        extra = max(0.0, extra_bps) / 10_000.0
        if extra <= 0:
            return vwap
        if side == "buy":
            return vwap * (1.0 + extra)
        return vwap * (1.0 - extra)

    def _enforce_exchange_physics(self) -> bool:
        try:
            from src.core.config_manager import config_manager

            return bool(config_manager.get_config().paper_engine.enforce_exchange_physics)
        except Exception:
            return False

    def _exchange_physics_specs(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            from src.core.globals import bot_context

            ex = bot_context.get_exchange()
            if not ex or not getattr(ex, "contract_specs_cache", None):
                return None
            sp = ex.contract_specs_cache.get(symbol)
            return sp if isinstance(sp, dict) and sp else None
        except Exception:
            return None

    @staticmethod
    def _effective_order_size_cap(specs: Dict[str, Any], is_market: bool) -> float:
        mx = float(specs.get("order_size_max") or 0.0)
        if is_market:
            mm = float(specs.get("market_order_size_max") or 0.0)
            if mm > 0:
                mx = min(mx, mm) if mx > 0 else mm
        return mx

    def _tier_max_leverage_for_exposure_usdt(
        self, symbol: str, exposure_usdt: float, spec: Dict[str, Any]
    ) -> float:
        cap = float(spec.get("leverage_max") or 0.0) or 125.0
        try:
            from src.core.globals import bot_context

            ex = bot_context.get_exchange()
            tiers = getattr(ex, "risk_limit_tiers_cache", None) if ex else None
            lst = tiers.get(symbol) if isinstance(tiers, dict) else None
            if not lst:
                return cap
            ordered = sorted(lst, key=lambda t: float(t.get("risk_limit") or 0.0))
            tmax: Optional[float] = None
            for t in ordered:
                lim = float(t.get("risk_limit") or 0.0)
                if exposure_usdt <= lim + 1e-6:
                    tmax = float(t.get("leverage_max") or cap)
                    break
            if tmax is None:
                tmax = float(ordered[-1].get("leverage_max") or cap)
            return min(cap, tmax)
        except Exception:
            return cap

    def _net_contracts_after_order(
        self, symbol: str, side: str, amount: float, reduce_only: bool
    ) -> float:
        """成交后净持仓张数（绝对值，单向）。仅用于风控阶梯名义估算。"""
        am = float(amount)
        pos = self.positions.get(symbol)
        S = float(pos.get("size", 0) or 0) if pos else 0.0
        ps = str(pos.get("side", "long")).lower() if pos and S > 1e-12 else ""

        if reduce_only:
            if S <= 0:
                return 0.0
            close_am = min(am, S)
            return max(S - close_am, 0.0)

        if S <= 0:
            return am

        same = (ps == "long" and side == "buy") or (ps == "short" and side == "sell")
        if same:
            return S + am

        close_am = min(am, S)
        rem = am - close_am
        new_s = S - close_am
        if new_s > 1e-12:
            return new_s
        return max(rem, 0.0)

    def _opening_notional_usdt_for_margin(
        self,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool,
        ref_px: float,
        cs: float,
    ) -> float:
        if reduce_only:
            return 0.0
        am = float(amount)
        pos = self.positions.get(symbol)
        S = float(pos.get("size", 0) or 0) if pos else 0.0
        ps = str(pos.get("side", "long")).lower() if pos and S > 1e-12 else ""
        full = am * float(cs) * float(ref_px)
        if S <= 0:
            return full
        same = (ps == "long" and side == "buy") or (ps == "short" and side == "sell")
        if same:
            return full
        close_am = min(am, S)
        open_am = am - close_am
        return max(open_am, 0.0) * float(cs) * float(ref_px)

    def _preflight_order_physics(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float],
        reduce_only: bool,
        leverage: int,
        will_rest: bool,
        entry_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._enforce_exchange_physics():
            return
        specs = self._exchange_physics_specs(symbol)
        if not specs:
            raise SimulatedAPIError(
                f"API Error 400: Unknown contract or specs not synced: {symbol}",
                label="CONTRACT_NOT_FOUND",
            )

        is_market = (not will_rest) and (price is None or float(price) <= 0)
        try:
            if will_rest or not is_market:
                ref_px = float(price or 0.0)
                if ref_px <= 0:
                    raise ValueError(f"Invalid reference price for {symbol}")
            else:
                ref_px = float(self._calculate_slippage_execution(symbol, side, amount))
        except ValueError as e:
            raise SimulatedAPIError(str(e), label="INVALID_PRICE") from e

        qm = float(specs.get("quanto_multiplier") or 0.0)
        cs = qm if qm > 0 else self._resolve_contract_size(symbol)

        o_min = float(specs.get("order_size_min") or 0.0)
        o_cap = self._effective_order_size_cap(specs, is_market)
        if o_min > 0 and amount + 1e-12 < o_min:
            raise SimulatedAPIError(
                f"API Error 400: Order size less than min limit {o_min}",
                label="ORDER_SIZE_TOO_SMALL",
            )
        if o_cap > 0 and amount - 1e-12 > o_cap:
            raise SimulatedAPIError(
                f"API Error 400: Order size exceeds max limit {o_cap}",
                label="ORDER_SIZE_TOO_BIG",
            )

        if not bool(specs.get("enable_decimal", False)):
            if abs(amount - round(amount)) > 1e-6:
                raise SimulatedAPIError(
                    "API Error 400: Order size must be integer contracts for this market",
                    label="INVALID_SIZE_STEP",
                )

        lev = max(int(leverage), 1)
        lev_min = int(float(specs.get("leverage_min") or 1))
        lev_cap_spec = float(specs.get("leverage_max") or 0.0) or 125.0

        if reduce_only:
            return

        if lev < lev_min or lev > lev_cap_spec + 1e-9:
            raise SimulatedAPIError(
                f"API Error 400: Leverage {lev}x outside allowed [{lev_min}, {int(lev_cap_spec)}]x for {symbol}",
                label="INVALID_LEVERAGE",
            )

        self._validate_liquidation_guard(
            symbol=symbol,
            side=side,
            entry_price=ref_px,
            leverage=lev,
            specs=specs,
            entry_context=entry_context,
        )

        net_after = self._net_contracts_after_order(symbol, side, amount, reduce_only=False)
        exposure_usdt = net_after * cs * ref_px
        max_lev = self._tier_max_leverage_for_exposure_usdt(symbol, exposure_usdt, specs)
        if lev > max_lev + 1e-9:
            raise SimulatedAPIError(
                f"API Error 400: Leverage {lev}x exceeds max {max_lev:.2f}x for exposure {exposure_usdt:.2f} USDT",
                label="RISK_LIMIT_TIER",
            )

        open_nom = self._opening_notional_usdt_for_margin(
            symbol, side, amount, False, ref_px, cs
        )
        if open_nom > 0 and lev > 0:
            required_margin = open_nom / float(lev)
            if required_margin > float(self.available_balance) + 1e-6:
                raise SimulatedAPIError(
                    "API Error 400: Insufficient margin. (资金不足)",
                    label="INSUFFICIENT_MARGIN",
                )

    def _schedule_autopsy(
        self,
        symbol: str,
        pos_snapshot: Dict[str, Any],
        closed_size: float,
        exit_price: float,
        realized_gross: float,
        exit_reason: str,
    ) -> None:
        try:
            fees_total = float(pos_snapshot.get("accumulated_fees", 0) or 0)
            snap = build_trade_autopsy(
                symbol=symbol,
                side=str(pos_snapshot.get("side", "long")),
                entry_price=float(pos_snapshot.get("entry_price", 0) or 0),
                exit_price=exit_price,
                closed_size=closed_size,
                contract_size=float(pos_snapshot.get("contract_size", 0) or 0) or self._resolve_contract_size(symbol),
                leverage=float(pos_snapshot.get("leverage", 1) or 1),
                margin_mode=str(pos_snapshot.get("margin_mode", "isolated")),
                realized_pnl_gross=realized_gross,
                fees_on_trade=fees_total,
                entry_context=dict(pos_snapshot.get("entry_context") or {}),
                max_favorable_unrealized=float(pos_snapshot.get("max_unrealized", 0) or 0),
                max_adverse_unrealized=float(pos_snapshot.get("min_unrealized", 0) or 0),
                opened_at=float(pos_snapshot.get("opened_at", 0) or 0),
                exit_reason=exit_reason,
                trading_mode_at_exit=_current_trading_mode(),
            )
            schedule_trade_autopsy(snap)
        except Exception as e:
            log.error(f"[Darwin] Failed to schedule autopsy: {e}")

    def _set_pending_realized_with_fee_audit(
        self, snap: Dict[str, Any], gross_pnl: float, fee_close_leg: float
    ) -> None:
        """
        单笔平仓审计：Order_Net = Gross − 本笔 leg 手续费 only。
        开仓费已在各笔成交时从余额扣除；accumulated_fee_paid 仅做全局 +=，不得再拿全量累计费递归进本条净盈亏。
        """
        try:
            fee_c = float(fee_close_leg)
            fees_total_on_snap = float(snap.get("accumulated_fees", 0) or 0)
            fee_open_total = max(0.0, fees_total_on_snap - fee_c)
            g = float(gross_pnl)
            final_net = g - fee_c
            self._pending_realized_net_usdt = final_net
            self._pending_fee_audit = {
                "gross_realized_pnl_usdt": g,
                "fee_open_total_usdt": fee_open_total,
                "fee_close_leg_usdt": fee_c,
                "final_trade_pnl_net_usdt": final_net,
            }
        except Exception:
            self._pending_realized_net_usdt = None
            self._pending_fee_audit = None

    def _log_close_autopsy(
        self,
        symbol: str,
        snap: Dict[str, Any],
        close_amt: float,
        exec_price: float,
        realized_price_pnl: float,
        fee_close_leg: float,
        exit_reason: str,
    ) -> None:
        """
        平仓尸检：入场/TP-SL、平仓时间价、原因、手续费与价格盈亏（price pnl 不含历史已扣费毛估）。
        """
        ect = dict(snap.get("entry_context") or {})
        oa = float(snap.get("opened_at", 0) or 0)
        entry_px = float(snap.get("entry_price", 0) or 0)
        side = str(snap.get("side", "long"))
        lev = int(snap.get("leverage", 1) or 1)
        acc_f = float(snap.get("accumulated_fees", 0) or 0)
        cs = float(snap.get("contract_size") or 0) or self._resolve_contract_size(symbol)
        try:
            tp = ect.get("take_profit_limit_price")
            sl = ect.get("stop_loss_limit_price")
            tp_f = float(tp) if tp is not None else None
            sl_f = float(sl) if sl is not None else None
        except (TypeError, ValueError):
            tp_f, sl_f = None, None
        slip_note = ""
        if entry_px > 0 and exec_price > 0:
            if side == "long":
                move_bps = (exec_price - entry_px) / entry_px * 1e4
            else:
                move_bps = (entry_px - exec_price) / entry_px * 1e4
            slip_note = f"move_vs_entry_bps={move_bps:.2f}"
        notional_leg = self._notional_from(float(close_amt), float(exec_price), cs)
        tp_s = f"{tp_f:.8f}" if tp_f is not None else "None"
        sl_s = f"{sl_f:.8f}" if sl_f is not None else "None"
        log.warning(
            f"[AUTOPSY CLOSE] {symbol} | side={side} contracts={close_amt:.8f} lev={lev}x | "
            f"ENTRY opened_at_ts={oa:.3f} entry_px={entry_px:.8f} | TP={tp_s} SL={sl_s} | "
            f"EXIT reason={exit_reason} exit_px={exec_price:.8f} fee_close_leg={fee_close_leg:.8f} "
            f"notional_leg~{notional_leg:.4f}USDT | realized_PRICE_PNL={realized_price_pnl:.8f} "
            f"(不含单独展示的历史进仓费; accumulated_fees_incl_close={acc_f:.8f}) | "
            f"{slip_note} | ect_keys={list(ect.keys())[:24]}"
        )

    def execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float = None,
        reduce_only: bool = False,
        leverage: int = 10,
        margin_mode: str = "isolated",
        berserker: bool = False,
        post_only: bool = False,
        entry_context: Optional[Dict[str, Any]] = None,
        exit_reason: Optional[str] = None,
        order_text: Optional[str] = None,
    ) -> dict:
        specs0 = self._exchange_physics_specs(symbol) or {}
        if not bool(specs0.get("enable_decimal", False)):
            nearest = round(float(amount))
            if abs(float(amount) - float(nearest)) <= 1e-3:
                amount = float(nearest)
        if amount <= 0:
            return {"status": "rejected", "reason": "Amount must be > 0"}

        self._calculate_pnl()

        try:
            self._pending_realized_net_usdt = None
            self._pending_fee_audit = None
            ctx0 = dict(entry_context or {})
            margin_mode = "isolated"
            if order_text:
                ctx0.setdefault("client_oid", str(order_text)[:28])
            resting_quote = bool(ctx0.get("resting_quote"))
            l1_tp = bool(ctx0.get("l1_tp_limit"))
            will_rest = bool(
                post_only
                and price
                and float(price) > 0
                and resting_quote
                and (not reduce_only or l1_tp)
            )
            self._preflight_order_physics(
                symbol,
                side,
                float(amount),
                price,
                reduce_only,
                int(leverage),
                will_rest,
                ctx0,
            )
            if (
                post_only
                and price
                and float(price) > 0
                and resting_quote
                and (not reduce_only or l1_tp)
            ):
                if bool(ctx0.get("sniper_pair_immediate_maker")) and not reduce_only:
                    exec_price = float(price)
                else:
                    oid = str(uuid.uuid4())
                    self._maker_resting.setdefault(symbol, []).append(
                        {
                            "id": oid,
                            "symbol": symbol,
                            "side": side,
                            "amount": float(amount),
                            "price": float(price),
                            "leverage": int(leverage),
                            "margin_mode": margin_mode,
                            "berserker": berserker,
                            "reduce_only": bool(reduce_only),
                            "entry_context": ctx0,
                        }
                    )
                    log.info(f"[Paper] MAKER_QUEUE {side.upper()} {amount} {symbol} @ {float(price):.6f}")
                    return {"status": "resting", "id": oid}

            if (
                bool(ctx0.get("paper_shadow_limit"))
                and not bool(ctx0.get("paper_slice"))
                and price is not None
                and float(price) > 0
            ):
                return self._enqueue_shadow_limit_order(
                    symbol,
                    side,
                    float(amount),
                    float(price),
                    reduce_only,
                    int(leverage),
                    margin_mode,
                    berserker,
                    post_only,
                    ctx0,
                    exit_reason,
                    order_text,
                )

            if price is None or price <= 0:
                exec_price = self._calculate_slippage_execution(symbol, side, amount)
                fee_rate = float(TAKER_FEE_RATE)
            else:
                exec_price = price
                if post_only or bool(ctx0.get("maker_filled")):
                    fee_rate = float(MAKER_FEE_RATE)
                else:
                    fee_rate = float(TAKER_FEE_RATE)

            pos = self.positions.get(symbol)
            if pos and float(pos.get("size") or 0) > 0:
                ps = float(pos["size"])
                opposite_to_pos = (pos["side"] == "long" and str(side).lower() == "sell") or (
                    pos["side"] == "short" and str(side).lower() == "buy"
                )
                if (
                    opposite_to_pos
                    and not reduce_only
                    and bool(ctx0.get("beta_neutral_hf"))
                    and float(amount) > ps + 1e-12
                ):
                    amount = ps
                    reduce_only = True
            is_fresh_open = (not pos or float(pos.get("size") or 0) <= 0) and not reduce_only
            if is_fresh_open:
                has_ts = self._has_explicit_tp_sl(ctx0)
                req = self._paper_require_entry_tp_sl() and not self._entry_tp_sl_exempt(
                    ctx0
                )
                if req and not has_ts:
                    return {
                        "status": "rejected",
                        "reason": "Missing take_profit_limit_price / stop_loss_limit_price (require_entry_tp_sl_limits)",
                    }
                if has_ts:
                    ep = float(exec_price)
                    tpv = float(ctx0["take_profit_limit_price"])
                    slv = float(ctx0["stop_loss_limit_price"])
                    verr = self._validate_tp_sl_vs_entry(side, ep, tpv, slv)
                    if verr:
                        try:
                            from src.core.config_manager import config_manager

                            prm = config_manager.get_config().strategy.params
                            tb = float(getattr(prm, "core_entry_tp_bps", 55.0) or 55.0)
                            sb = float(getattr(prm, "core_entry_sl_bps", 50.0) or 50.0)
                            if str(side).lower() == "buy":
                                ctx0["take_profit_limit_price"] = ep * (1.0 + tb / 1e4)
                                ctx0["stop_loss_limit_price"] = ep * (1.0 - sb / 1e4)
                            else:
                                ctx0["take_profit_limit_price"] = ep * (1.0 - tb / 1e4)
                                ctx0["stop_loss_limit_price"] = ep * (1.0 + sb / 1e4)
                            verr = self._validate_tp_sl_vs_entry(
                                side,
                                ep,
                                float(ctx0["take_profit_limit_price"]),
                                float(ctx0["stop_loss_limit_price"]),
                            )
                        except Exception:
                            pass
                    if verr:
                        return {"status": "rejected", "reason": verr}

            cs_open = self._position_contract_size(pos, symbol) if pos and float(pos.get("size") or 0) > 0 else self._resolve_contract_size(symbol)
            # Fee = |张|×cs×价×rate（硬编码 TAKER/MAKER，禁止变量污染）
            notional_value = self._notional_from(abs(float(amount)), float(exec_price), float(cs_open))
            fee_amount = notional_value * float(fee_rate)
            self.initial_balance -= fee_amount
            self.accumulated_fee_paid += fee_amount

            if not pos or pos["size"] == 0:
                if reduce_only:
                    return {"status": "rejected", "reason": "No position to reduce"}

                mm = "isolated"
                ctx = dict(ctx0)
                cs_snap = self._resolve_contract_size(symbol)
                opened_ts = time.time()
                risk_snap = self._compose_position_risk(
                    symbol=symbol,
                    side="buy" if side == "buy" else "sell",
                    total_contracts=float(amount),
                    vwap_entry=float(exec_price),
                    margin_used=float(notional_value) / max(int(ctx.get("effective_leverage") or leverage), 1),
                    leverage_hint=int(ctx.get("effective_leverage") or leverage),
                    contract_size=cs_snap,
                    specs=self._exchange_physics_specs(symbol),
                )
                self.positions[symbol] = {
                    "side": "long" if side == "buy" else "short",
                    "size": amount,
                    "entry_price": exec_price,
                    "contract_size": cs_snap,
                    "unrealized_pnl": 0.0,
                    "leverage": int(max(1, round(float(risk_snap["effective_leverage"])))),
                    "margin_mode": mm,
                    "opened_at": opened_ts,
                    "open_timestamp": opened_ts,
                    "entry_context": ctx,
                    "accumulated_fees": fee_amount,
                    "pyramid_layers": 1,
                    "margin_used": float(risk_snap["margin_used"]),
                    "mmr_rate": float(risk_snap["mmr_rate"]),
                    "liquidation_price": float(risk_snap["liquidation_price"]),
                }
                ttl_min = float(ctx.get("position_ttl_minutes") or 0.0)
                if bool(ctx.get("playbook_guerrilla")) and ttl_min > 0:
                    self.positions[symbol]["time_stop_deadline_ts"] = opened_ts + ttl_min * 60.0
                if bool(ctx.get("high_conviction_trailing")):
                    self.positions[symbol]["high_conviction_trailing"] = True
                    self.positions[symbol]["trailing_stop_activation_pct"] = float(
                        ctx.get("trailing_stop_activation_pct") or 0.02
                    )
                    self.positions[symbol]["trailing_stop_callback_pct"] = float(
                        ctx.get("trailing_stop_callback_pct") or 0.005
                    )
                    self.positions[symbol]["highest_unrealized_pnl_pct"] = 0.0
                    self.positions[symbol]["trailing_armed"] = False
                    self.positions[symbol]["trailing_favorable_extreme"] = None
                managed_brackets = (
                    ctx.get("beta_neutral_hf")
                    or ctx.get("l1_managed")
                    or ctx.get("slingshot_managed")
                    or ctx.get("assassin_managed")
                    or ctx.get("leadlag_managed")
                    or ctx.get("playbook_guerrilla")
                )
                if self._has_explicit_tp_sl(ctx) and not managed_brackets:
                    self._queue_explicit_tp_sl_brackets(
                        symbol,
                        float(exec_price),
                        float(amount),
                        int(leverage),
                        mm,
                        berserker,
                        ctx,
                        "buy" if side == "buy" else "sell",
                        float(ctx["take_profit_limit_price"]),
                        float(ctx["stop_loss_limit_price"]),
                    )
                elif not managed_brackets:
                    _attach_exit_bracket(self.positions[symbol], exec_price)
                lev_i = max(int(leverage), 1)
                est_margin = float(notional_value) / float(lev_i) if lev_i > 0 else float(notional_value)
                log.warning(
                    f"[FEE/MARGIN DEBUG] OPEN {symbol} | notional_exec={notional_value:.8f} USDT (contracts×cs×px) | "
                    f"fee_rate={fee_rate:.6f} fee_deduct={fee_amount:.8f} (名义×费率, 不再×杠杆) | "
                    f"leverage={lev_i}x est_margin≈{est_margin:.8f} USDT | "
                    f"intent_dynamic={ctx.get('dynamic_sizing')}"
                )
                log.info(f"[Paper] OPEN {side.upper()} {amount} {symbol} @ {exec_price:.4f} (Fee: {fee_amount:.2f})")
                try:
                    from src.darwin.experience_store import append_order_open_event

                    append_order_open_event(
                        symbol=symbol,
                        side=str(self.positions[symbol]["side"]),
                        price=float(exec_price),
                        contracts=float(amount),
                        leverage=int(leverage),
                        margin_mode=str(mm),
                        entry_context=ctx,
                    )
                except Exception:
                    pass
                if ctx.get("l1_managed") and side == "buy":
                    self._queue_l1_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "buy"
                    )
                elif ctx.get("l1_managed") and side == "sell":
                    self._queue_l1_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "sell"
                    )
                elif ctx.get("slingshot_managed") and side == "buy":
                    self._queue_slingshot_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "buy"
                    )
                elif ctx.get("slingshot_managed") and side == "sell":
                    self._queue_slingshot_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "sell"
                    )
                elif ctx.get("assassin_managed") and side == "buy":
                    self._queue_assassin_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "buy"
                    )
                elif ctx.get("assassin_managed") and side == "sell":
                    self._queue_assassin_brackets(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "sell"
                    )
                elif ctx.get("leadlag_managed") and side == "buy":
                    self._queue_leadlag_bracket_protocol(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "buy"
                    )
                elif ctx.get("leadlag_managed") and side == "sell":
                    self._queue_leadlag_bracket_protocol(
                        symbol, exec_price, float(amount), leverage, mm, berserker, ctx, "sell"
                    )

            else:
                is_same_side = (pos["side"] == "long" and side == "buy") or (pos["side"] == "short" and side == "sell")

                if is_same_side:
                    if reduce_only:
                        return {"status": "rejected", "reason": "Reduce only order on same side"}
                    if bool(ctx0.get("beta_neutral_hf")) and not reduce_only:
                        return {"status": "rejected", "reason": "beta_pair_no_same_side_add"}
                    bypass_pyramid = bool(ctx0.get("beta_hedge_anchor_adjust")) or (
                        bool(ctx0.get("beta_neutral_hf")) and str(ctx0.get("pair_role", "")).lower() == "anchor"
                    )
                    cur_layers = self._pyramid_layer_count(pos)
                    max_layers = self._max_pyramid_layers()
                    if (not bypass_pyramid) and cur_layers >= max_layers:
                        return {"status": "rejected", "reason": f"max_pyramid_layers_reached:{max_layers}"}

                    pos["accumulated_fees"] = pos.get("accumulated_fees", 0.0) + fee_amount
                    cs_p = self._position_contract_size(pos, symbol)
                    new_size = pos["size"] + amount
                    new_entry = ((pos["size"] * pos["entry_price"]) + (amount * exec_price)) / new_size
                    prev_margin = float(pos.get("margin_used", 0.0) or 0.0)
                    add_margin = float(notional_value) / max(int(leverage), 1)
                    risk_snap = self._compose_position_risk(
                        symbol=symbol,
                        side="buy" if pos["side"] == "long" else "sell",
                        total_contracts=float(new_size),
                        vwap_entry=float(new_entry),
                        margin_used=prev_margin + add_margin,
                        leverage_hint=int(leverage),
                        contract_size=cs_p,
                        specs=self._exchange_physics_specs(symbol),
                    )
                    pos["size"] = new_size
                    pos["entry_price"] = new_entry
                    pos["contract_size"] = cs_p
                    pos["pyramid_layers"] = cur_layers if bypass_pyramid else (cur_layers + 1)
                    pos["margin_used"] = float(risk_snap["margin_used"])
                    pos["mmr_rate"] = float(risk_snap["mmr_rate"])
                    pos["liquidation_price"] = float(risk_snap["liquidation_price"])
                    pos["leverage"] = int(max(1, round(float(risk_snap["effective_leverage"]))))
                    ect = pos.get("entry_context") or {}
                    if (
                        not ect.get("l1_managed")
                        and not ect.get("slingshot_managed")
                        and not ect.get("assassin_managed")
                        and not ect.get("leadlag_managed")
                        and not ect.get("beta_neutral_hf")
                    ):
                        _attach_exit_bracket(pos, new_entry)
                    log.info(
                        f"[Paper] ADD {side.upper()} {amount} {symbol} @ {exec_price:.4f} "
                        f"(New Avg: {new_entry:.4f}, Layers: {pos['pyramid_layers']}, Liq: {pos['liquidation_price']:.4f})"
                    )

                else:
                    cs_x = self._position_contract_size(pos, symbol)
                    if amount <= pos["size"]:
                        close_amt = min(amount, pos["size"])
                        pos["accumulated_fees"] = pos.get("accumulated_fees", 0.0) + fee_amount
                        snap = {k: pos[k] for k in pos if k != "unrealized_pnl"}
                        snap["accumulated_fees"] = pos["accumulated_fees"]

                        if pos["side"] == "long":
                            realized_pnl = (exec_price - pos["entry_price"]) * close_amt * cs_x
                        else:
                            realized_pnl = (pos["entry_price"] - exec_price) * close_amt * cs_x

                        self.initial_balance += realized_pnl
                        pos["size"] -= close_amt
                        if float(snap.get("size", 0.0) or 0.0) > 1e-12:
                            remain_ratio = max(float(pos["size"]), 0.0) / max(float(snap.get("size", 0.0) or 0.0), 1e-12)
                            pos["margin_used"] = float(snap.get("margin_used", 0.0) or 0.0) * remain_ratio
                            pos["pyramid_layers"] = max(1, int(math.ceil(float(snap.get("pyramid_layers", 1) or 1) * remain_ratio)))
                        log.info(
                            f"[Paper] CLOSE {close_amt} {symbol} @ {exec_price:.4f} (PnL: {realized_pnl:.2f}, Fee: {fee_amount:.2f})"
                        )

                        if pos["size"] <= 1e-8:
                            reason = exit_reason or ("reduce_only" if reduce_only else "opposite_fill")
                            oco_clr = snap.get("bracket_oco_id") or (
                                (snap.get("entry_context") or {}).get("bracket_oco_id")
                            )
                            self._clear_bracket_resting(symbol, oco_clr)
                            self._log_close_autopsy(
                                symbol,
                                snap,
                                close_amt,
                                float(exec_price),
                                float(realized_pnl),
                                float(fee_amount),
                                str(reason),
                            )
                            self._set_pending_realized_with_fee_audit(snap, float(realized_pnl), float(fee_amount))
                            self._schedule_autopsy(symbol, snap, close_amt, exec_price, realized_pnl, reason)
                            pos["size"] = 0
                            pos["entry_price"] = 0
                            pos["margin_used"] = 0.0
                            pos["liquidation_price"] = 0.0
                    else:
                        if reduce_only:
                            close_amt = pos["size"]
                            pos["accumulated_fees"] = pos.get("accumulated_fees", 0.0) + fee_amount
                            snap = {k: pos[k] for k in pos if k != "unrealized_pnl"}
                            snap["accumulated_fees"] = pos["accumulated_fees"]
                            realized_pnl = (
                                (exec_price - pos["entry_price"]) * close_amt * cs_x
                                if pos["side"] == "long"
                                else (pos["entry_price"] - exec_price) * close_amt * cs_x
                            )
                            self.initial_balance += realized_pnl
                            pos["size"] = 0
                            log.info(
                                f"[Paper] REDUCE-ONLY CLOSE {close_amt} {symbol} @ {exec_price:.4f} (PnL: {realized_pnl:.2f})"
                            )
                            oco_clr = snap.get("bracket_oco_id") or (
                                (snap.get("entry_context") or {}).get("bracket_oco_id")
                            )
                            self._clear_bracket_resting(symbol, oco_clr)
                            self._log_close_autopsy(
                                symbol,
                                snap,
                                close_amt,
                                float(exec_price),
                                float(realized_pnl),
                                float(fee_amount),
                                str(exit_reason or "reduce_only"),
                            )
                            self._set_pending_realized_with_fee_audit(snap, float(realized_pnl), float(fee_amount))
                            self._schedule_autopsy(
                                symbol,
                                snap,
                                close_amt,
                                exec_price,
                                realized_pnl,
                                exit_reason or "reduce_only",
                            )
                            pos["entry_price"] = 0
                            pos["margin_used"] = 0.0
                            pos["liquidation_price"] = 0.0
                            pos["margin_used"] = 0.0
                            pos["liquidation_price"] = 0.0
                        else:
                            close_amount = pos["size"]
                            open_amount = amount - close_amount
                            pos["accumulated_fees"] = pos.get("accumulated_fees", 0.0) + fee_amount
                            snap = {k: pos[k] for k in pos if k != "unrealized_pnl"}
                            snap["accumulated_fees"] = pos["accumulated_fees"]

                            realized_pnl = (
                                (exec_price - pos["entry_price"]) * close_amount * cs_x
                                if pos["side"] == "long"
                                else (pos["entry_price"] - exec_price) * close_amount * cs_x
                            )
                            self.initial_balance += realized_pnl
                            oco_clr = snap.get("bracket_oco_id") or (
                                (snap.get("entry_context") or {}).get("bracket_oco_id")
                            )
                            self._clear_bracket_resting(symbol, oco_clr)
                            self._log_close_autopsy(
                                symbol,
                                snap,
                                close_amount,
                                float(exec_price),
                                float(realized_pnl),
                                float(fee_amount),
                                str(exit_reason or "reverse_open_opposite"),
                            )
                            self._set_pending_realized_with_fee_audit(snap, float(realized_pnl), float(fee_amount))
                            self._schedule_autopsy(
                                symbol,
                                snap,
                                close_amount,
                                exec_price,
                                realized_pnl,
                                exit_reason or "reverse_open_opposite",
                            )

                            pos["side"] = "long" if side == "buy" else "short"
                            pos["size"] = open_amount
                            pos["entry_price"] = exec_price
                            pos["contract_size"] = self._resolve_contract_size(symbol)
                            risk_snap = self._compose_position_risk(
                                symbol=symbol,
                                side="buy" if side == "buy" else "sell",
                                total_contracts=float(open_amount),
                                vwap_entry=float(exec_price),
                                margin_used=self._notional_from(float(open_amount), float(exec_price), pos["contract_size"]) / max(int(leverage), 1),
                                leverage_hint=int(leverage),
                                contract_size=pos["contract_size"],
                                specs=self._exchange_physics_specs(symbol),
                            )
                            pos["leverage"] = int(max(1, round(float(risk_snap["effective_leverage"]))))
                            pos["margin_mode"] = "isolated"
                            pos["opened_at"] = time.time()
                            pos["entry_context"] = dict(entry_context or {})
                            pos["accumulated_fees"] = 0.0
                            pos["pyramid_layers"] = 1
                            pos["margin_used"] = float(risk_snap["margin_used"])
                            pos["mmr_rate"] = float(risk_snap["mmr_rate"])
                            pos["liquidation_price"] = float(risk_snap["liquidation_price"])
                            pos.pop("max_unrealized", None)
                            pos.pop("min_unrealized", None)
                            ect = pos.get("entry_context") or {}
                            rev_managed = (
                                ect.get("l1_managed")
                                or ect.get("slingshot_managed")
                                or ect.get("assassin_managed")
                                or ect.get("leadlag_managed")
                                or ect.get("beta_neutral_hf")
                            )
                            if self._has_explicit_tp_sl(ect) and not rev_managed:
                                self._queue_explicit_tp_sl_brackets(
                                    symbol,
                                    float(exec_price),
                                    float(open_amount),
                                    int(leverage),
                                    pos["margin_mode"],
                                    berserker,
                                    ect,
                                    "buy" if side == "buy" else "sell",
                                    float(ect["take_profit_limit_price"]),
                                    float(ect["stop_loss_limit_price"]),
                                )
                            elif not rev_managed:
                                _attach_exit_bracket(pos, exec_price)
                            log.info(
                                f"[Paper] REVERSE to {pos['side'].upper()} {open_amount} {symbol} @ {exec_price:.4f} (Realized: {realized_pnl:.2f})"
                            )

            self._calculate_pnl()

            if ctx0.get("shadow_parent_order_id"):
                oid_out = str(ctx0["shadow_parent_order_id"])
            else:
                oid_out = str(uuid.uuid4())
            if ctx0.get("paper_slice"):
                st = "partially_filled"
            else:
                st = "closed" if not price or float(price) <= 0 else "open"
            out: Dict[str, Any] = {
                "status": st,
                "id": oid_out,
                "price": exec_price,
                "amount": amount,
                "filled": float(amount),
                "avg_price": float(exec_price),
                "symbol": symbol,
                "side": side,
                "post_only": bool(post_only),
            }
            if self._pending_realized_net_usdt is not None:
                out["realized_net_usdt"] = float(self._pending_realized_net_usdt)
                self._pending_realized_net_usdt = None
            if self._pending_fee_audit:
                out.update(self._pending_fee_audit)
                self._pending_fee_audit = None
            return out

        except SimulatedAPIError as e:
            log.warning(f"[Paper] Preflight rejected: {e}")
            return {
                "status": "rejected",
                "reason": str(e),
                "label": e.label,
                "http_status": e.http_status,
            }
        except Exception as e:
            log.error(f"[Paper Engine] Execution Error: {e}")
            return {"status": "rejected", "reason": str(e)}

    def financial_snapshot(self) -> Dict[str, Any]:
        """
        面板/WS 用资金明细（纸面会话）。Total_Fees_Paid = 历史成交累计扣费（不含 funding 调整 initial 的部分）。
        """
        self._calculate_pnl()
        margin_locked = 0.0
        unreal = 0.0
        for pos in (self.positions or {}).values():
            if float(pos.get("size", 0) or 0) <= 1e-12:
                continue
            margin_locked += float(pos.get("margin_used", 0) or 0) or 0.0
            unreal += float(pos.get("unrealized_pnl", 0) or 0)
        try:
            from src.core.risk_engine import risk_engine

            session_net = float(risk_engine.realized_pnl or 0.0)
        except Exception:
            session_net = 0.0
        return {
            "total_equity": float(self.balance),
            "margin_locked": float(margin_locked),
            "available_balance": float(self.available_balance),
            "total_fees_paid": float(self.accumulated_fee_paid),
            "total_unrealized_pnl": float(unreal),
            "session_realized_pnl_net": float(session_net),
            "total_funding_paid_usdt": float(self.accumulated_funding_fee),
            "wallet_cash_ledger_usdt": float(self.initial_balance),
        }

    def get_balance(self):
        self._calculate_pnl()
        unrealized = float(self.balance - self.initial_balance)
        return {
            "total": {"USDT": self.balance},
            "free": {"USDT": self.available_balance},
            "wallet_balance": {"USDT": self.initial_balance},
            "unrealized_pnl": {"USDT": unrealized},
            "accumulated_fee_paid": {"USDT": float(self.accumulated_fee_paid)},
            "accumulated_funding_fee": {"USDT": float(self.accumulated_funding_fee)},
        }

    def get_positions(self):
        self._calculate_pnl()
        pos_list = []
        for symbol, p in self.positions.items():
            if p["size"] > 0:
                pos_list.append(
                    {
                        "symbol": symbol,
                        "side": p["side"],
                        "size": p["size"],
                        "entryPrice": p["entry_price"],
                        "contractSize": float(p.get("contract_size") or 0) or self._resolve_contract_size(symbol),
                        "unrealizedPnl": p["unrealized_pnl"],
                        "leverage": p.get("leverage", 1),
                        "liquidationPrice": float(p.get("liquidation_price", 0.0) or 0.0),
                        "marginUsed": float(p.get("margin_used", 0.0) or 0.0),
                        "pyramidLayers": int(p.get("pyramid_layers", 1) or 1),
                        "margin_mode": p.get("margin_mode", "isolated"),
                    }
                )
        return pos_list


paper_engine = PaperTradingEngine(initial_balance=_global_paper_initial_balance_usdt())
