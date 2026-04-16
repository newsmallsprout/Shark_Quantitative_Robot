from typing import Dict, Any, List, Optional
from collections import deque
import math
import time
from src.core.config_manager import config_manager
from src.utils.logger import log


def _berserker_tier_cap(symbol: str) -> int:
    u = symbol.upper()
    if "BTC" in u or "ETH" in u:
        return 200
    if any(x in u for x in ("PEPE", "WIF", "DOGE", "SHIB", "FLOKI", "BONK", "MEME")):
        return 50
    return 100


def berserker_max_leverage_for_symbol(symbol: str) -> int:
    """Hard cap by asset class; Darwin per-symbol patch may lower cap (never raise above tier)."""
    tier = _berserker_tier_cap(symbol)
    p = config_manager.get_config().darwin.symbol_patches.get(symbol)
    if p and p.max_leverage is not None:
        return max(1, min(tier, int(p.max_leverage)))
    return tier


def berserker_obi_threshold_for(symbol: str) -> float:
    cfg = config_manager.get_config()
    base = float(cfg.risk.berserker_obi_threshold)
    patch = cfg.darwin.symbol_patches.get(symbol)
    if patch and patch.berserker_obi_threshold is not None:
        return float(patch.berserker_obi_threshold)
    return base

class RiskRejection(Exception):
    """Exception raised when an order is rejected by the risk engine."""
    pass

class RiskEngine:
    """
    Independent Risk Control Engine.
    All orders must pass through check_order() before execution.
    """
    def __init__(self):
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.wallet_balance = 0.0
        self.unrealized_pnl = 0.0
        self.total_equity = 0.0
        self.daily_high = 0.0
        self.daily_drawdown = 0.0
        self.realized_pnl = 0.0
        self.realized_high = 0.0
        self.realized_drawdown = 0.0
        self.realized_drawdown_usdt = 0.0
        self.peak_to_trough_drawdown = 0.0
        self.peak_to_trough_drawdown_usdt = 0.0
        self.accumulated_fee = 0.0
        self.accumulated_funding_fee = 0.0
        self.last_reconciliation_ts = 0.0
        self.orphan_position_alerts: Dict[str, float] = {}
        
        # Frequency Control (cap from config_manager.get_config().risk.max_orders_per_second)
        self.order_timestamps: List[float] = []

        # State
        self.is_halted = False
        self.halt_reason = ""
        # Last ATR% estimate per symbol (from strategy price window), for grinder target leverage UI
        self.symbol_atr_pct: Dict[str, float] = {}
        # Last-600s ticks for 10m range% → grinder leverage tiering (fed from gateway ticker WS)
        self._ten_min_ticks: Dict[str, deque] = {}
        self.symbol_10m_range_pct: Dict[str, float] = {}

    def _max_orders_per_second(self) -> int:
        try:
            v = int(config_manager.get_config().risk.max_orders_per_second)
            return max(1, v)
        except (TypeError, ValueError):
            return 80

    def entry_mutex_reason(
        self,
        symbol: str,
        side: str,
        reduce_only: bool,
        entry_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """非 reduce_only 开仓/加仓前的互斥：已有净头寸（纸面会话）。"""
        if reduce_only or not symbol:
            return None
        ect = dict(entry_context or {})
        if bool(ect.get("beta_hedge_anchor_adjust")):
            return None
        if bool(ect.get("beta_neutral_hf")) and str(ect.get("pair_role", "")).lower() in (
            "anchor",
            "anchor_net",
        ):
            return None
        sc = config_manager.get_config().strategy
        if not sc.single_open_per_symbol:
            return None
        try:
            from src.core.paper_engine import paper_engine

            pos = paper_engine.positions.get(symbol)
            if pos and float(pos.get("size", 0) or 0) > 1e-12:
                pos_side = str(pos.get("side", "")).lower()
                same = (pos_side == "long" and str(side).lower() == "buy") or (pos_side == "short" and str(side).lower() == "sell")
                if not same:
                    return "open_position_mutex"
                try:
                    max_layers = max(1, int(getattr(config_manager.get_config().paper_engine, "max_pyramid_layers", 3) or 3))
                except Exception:
                    max_layers = 3
                cur_layers = int(float(pos.get("pyramid_layers", 1) or 1))
                if cur_layers >= max_layers:
                    return "pyramid_layers_mutex"
            rest = paper_engine._maker_resting.get(symbol) or []
            for o in rest:
                if bool(o.get("reduce_only", False)):
                    continue
                o_side = str(o.get("side", "")).lower()
                if o_side == str(side).lower():
                    continue
                return "resting_entry_mutex"
        except Exception:
            pass
        return None

    @property
    def daily_pnl(self) -> float:
        """Realized session PnL for dashboard/accounting."""
        return float(self.realized_pnl)

    @property
    def equity_pnl(self) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return float(self.total_equity - self.initial_balance)

    def record_realized_pnl(self, realized_net_usdt: float) -> None:
        try:
            realized = float(realized_net_usdt or 0.0)
        except (TypeError, ValueError):
            return
        self.realized_pnl += realized
        self.realized_high = max(self.realized_high, self.realized_pnl)
        self.realized_drawdown_usdt = max(0.0, self.realized_high - self.realized_pnl)
        base = max(self.initial_balance, 1e-9)
        self.realized_drawdown = self.realized_drawdown_usdt / base

    def note_fees(self, fee_usdt: float = 0.0, funding_fee_usdt: float = 0.0) -> None:
        try:
            self.accumulated_fee += float(fee_usdt or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            self.accumulated_funding_fee += float(funding_fee_usdt or 0.0)
        except (TypeError, ValueError):
            pass

    def update_account_snapshot(self, wallet_balance: float, unrealized_pnl: float = 0.0) -> None:
        try:
            wallet = float(wallet_balance or 0.0)
            unreal = float(unrealized_pnl or 0.0)
        except (TypeError, ValueError):
            return
        equity = wallet + unreal
        if wallet <= 0 and equity <= 0:
            return

        if self.initial_balance == 0:
            self.initial_balance = wallet if wallet > 0 else equity
            self.daily_high = equity
            self.realized_high = 0.0

        self.wallet_balance = wallet
        self.unrealized_pnl = unreal
        self.total_equity = equity
        self.current_balance = equity
        self.daily_high = max(self.daily_high, equity)
        if self.daily_high > 0:
            self.peak_to_trough_drawdown_usdt = max(0.0, self.daily_high - self.total_equity)
            self.peak_to_trough_drawdown = self.peak_to_trough_drawdown_usdt / self.daily_high
            self.daily_drawdown = self.peak_to_trough_drawdown

        self._check_drawdown()

    def update_balance(self, balance: float):
        """Backward-compatible legacy wrapper."""
        self.update_account_snapshot(balance, 0.0)

    def reset_for_event_replay(self, wallet_usdt: float) -> None:
        """Risk 状态清零，与纸面钱包对齐。"""
        w = float(wallet_usdt)
        self.initial_balance = w
        self.wallet_balance = w
        self.unrealized_pnl = 0.0
        self.total_equity = w
        self.current_balance = w
        self.daily_high = w
        self.realized_pnl = 0.0
        self.realized_high = 0.0
        self.realized_drawdown_usdt = 0.0
        self.realized_drawdown = 0.0
        self.peak_to_trough_drawdown = 0.0
        self.peak_to_trough_drawdown_usdt = 0.0
        self.accumulated_fee = 0.0
        self.accumulated_funding_fee = 0.0
        self.order_timestamps.clear()
        self.is_halted = False
        self.halt_reason = ""
        self.orphan_position_alerts.clear()
        self.symbol_atr_pct.clear()
        self._ten_min_ticks.clear()
        self.symbol_10m_range_pct.clear()

    def _check_drawdown(self):
        """Internal check for drawdown limits.
        Drawdown is telemetry only; neither daily nor hard drawdown halts trading.
        """
        risk_config = config_manager.get_config().risk
        # Clear any historical drawdown halt left from older code/config versions.
        if self.is_halted and "DRAWDOWN LIMIT REACHED" in (self.halt_reason or "").upper():
            self.is_halted = False
            self.halt_reason = ""

        if self.peak_to_trough_drawdown >= risk_config.hard_drawdown_limit:
            log.critical(
                f"HARD DRAWDOWN WARNING: equity_dd={self.peak_to_trough_drawdown_usdt:.4f}USDT "
                f"({self.peak_to_trough_drawdown*100:.2f}%) >= {risk_config.hard_drawdown_limit*100:.2f}% | "
                f"realized_dd={self.realized_drawdown_usdt:.4f}USDT ({self.realized_drawdown*100:.2f}%) | "
                f"wallet={self.wallet_balance:.4f} unrealized={self.unrealized_pnl:.4f} equity={self.total_equity:.4f} "
                f"fees={self.accumulated_fee:.4f} funding={self.accumulated_funding_fee:.4f}"
            )
        elif self.peak_to_trough_drawdown >= risk_config.daily_drawdown_limit:
            log.warning(
                f"DAILY DRAWDOWN WARNING: equity_dd={self.peak_to_trough_drawdown_usdt:.4f}USDT "
                f"({self.peak_to_trough_drawdown*100:.2f}%) >= {risk_config.daily_drawdown_limit*100:.2f}% | "
                f"realized_dd={self.realized_drawdown_usdt:.4f}USDT ({self.realized_drawdown*100:.2f}%) | "
                f"wallet={self.wallet_balance:.4f} unrealized={self.unrealized_pnl:.4f} equity={self.total_equity:.4f} "
                f"fees={self.accumulated_fee:.4f} funding={self.accumulated_funding_fee:.4f}"
            )

    def check_order(self, order: Dict[str, Any]) -> bool:
        """
        Validate a single order against all risk rules.
        Expected order dict format:
        {
            'symbol': 'BTC/USDT',
            'side': 'buy' or 'sell',
            'type': 'market' or 'limit',
            'amount': float,
            'price': float (optional for market, required for risk calc if possible),
            'leverage': int (optional, defaults to 1),
            'reduce_only': bool (optional)
        }
        
        Raises RiskRejection if the order violates any rule.
        Returns True if the order is safe to execute.
        """
        # 1. Global Halt Check
        if self.is_halted:
            # Allow reduce-only orders (closing positions) even when halted
            if not order.get('reduce_only', False):
                raise RiskRejection(f"System halted. Order rejected. Reason: {self.halt_reason}")

        sym = str(order.get("symbol", "") or "")
        ect0 = order.get("entry_context") or {}
        if not isinstance(ect0, dict):
            ect0 = {}

        # BetaNeutralHF anchor netting orders must bypass single-position mutex; the whole point is to
        # adjust hedge on the anchor symbol even when a BTC position already exists.
        if bool(ect0.get("beta_hedge_anchor_adjust")):
            return True

        em = self.entry_mutex_reason(sym, str(order.get("side", "") or ""), bool(order.get("reduce_only", False)), ect0)
        if em:
            raise RiskRejection(f"Single-position mutex: already open on {sym}.")

        # 幽灵 silo：SNIPER_LEADLAG 单独单笔风险与杠杆（与 grinder / berserker 无关）
        if (
            ect0.get("position_silo") == "SNIPER_LEADLAG"
            and not order.get("reduce_only", False)
        ):
            risk_config = config_manager.get_config().risk
            now = time.time()
            self.order_timestamps = [t for t in self.order_timestamps if now - t <= 1.0]
            mps = self._max_orders_per_second()
            if len(self.order_timestamps) >= mps:
                raise RiskRejection(
                    f"Order frequency exceeded {mps} ops/sec."
                )
            self.order_timestamps.append(now)

            lev = float(order.get("leverage", 1))
            cap_ll = float(risk_config.sniper_leadlag_max_leverage)
            if lev > cap_ll:
                raise RiskRejection(
                    f"LeadLag silo leverage {lev}x exceeds cap {cap_ll:g}x."
                )

            if self.current_balance > 0:
                from src.core.paper_engine import paper_engine as _pe

                amount = float(order.get("amount", 0))
                price = float(order.get("price") or 0)
                if price > 0:
                    cs = _pe.contract_size_for_symbol(sym)
                    notional_value = amount * price * cs
                    margin_required = notional_value / max(lev, 1.0)
                    max_allowed_margin = self.current_balance * float(
                        risk_config.sniper_leadlag_max_single_risk
                    )
                    if margin_required > max_allowed_margin:
                        raise RiskRejection(
                            f"LeadLag silo margin ({margin_required:.2f}) exceeds "
                            f"silo limit ({max_allowed_margin:.2f})."
                        )
            return True

        # 狂鲨剥头皮：固定名义 + 绝对 USDT 括号；单独占用保证金比例与杠杆上限
        if (
            ect0.get("position_silo") == "SHARK_SCALP"
            and not order.get("reduce_only", False)
        ):
            sc = config_manager.get_config().shark_scalp
            now = time.time()
            self.order_timestamps = [t for t in self.order_timestamps if now - t <= 1.0]
            mps = self._max_orders_per_second()
            if len(self.order_timestamps) >= mps:
                raise RiskRejection(
                    f"Order frequency exceeded {mps} ops/sec."
                )
            self.order_timestamps.append(now)

            lev = float(order.get("leverage", 1))
            cap = max(1, int(sc.max_leverage))
            if lev > cap:
                raise RiskRejection(f"SharkScalp leverage {lev}x exceeds cap {cap}x.")

            if self.current_balance > 0:
                from src.core.paper_engine import paper_engine as _pe

                amount = float(order.get("amount", 0))
                price = float(order.get("price") or 0)
                if price > 0:
                    cs = _pe.contract_size_for_symbol(sym)
                    notional_value = amount * price * cs
                    margin_required = notional_value / max(lev, 1.0)
                    risk_cfg = config_manager.get_config().risk
                    if getattr(risk_cfg, "use_equity_tier_margin", True):
                        from src.core.equity_sizing import margin_cap_fraction

                        tier_cap = margin_cap_fraction(self.current_balance)
                        eff_frac = min(float(sc.max_equity_fraction_per_shot), tier_cap)
                    else:
                        eff_frac = max(float(sc.max_equity_fraction_per_shot), 1e-6)
                    max_allowed = self.current_balance * max(eff_frac, 1e-6)
                    if margin_required > max_allowed:
                        raise RiskRejection(
                            f"SharkScalp margin ({margin_required:.2f}) exceeds "
                            f"fraction cap ({max_allowed:.2f})."
                        )
            return True

        # Berserker: isolated wall — no 5% cap; cap leverage by symbol tier only
        if order.get("berserker") and not order.get("reduce_only", False):
            now = time.time()
            self.order_timestamps = [t for t in self.order_timestamps if now - t <= 1.0]
            mps = self._max_orders_per_second()
            if len(self.order_timestamps) >= mps:
                raise RiskRejection(f"Order frequency exceeded {mps} ops/sec.")
            self.order_timestamps.append(now)

            lev = float(order.get("leverage", 1))
            cap = berserker_max_leverage_for_symbol(sym)
            if lev > cap:
                raise RiskRejection(f"Berserker leverage {lev}x exceeds tier cap {cap}x for {sym}.")
            return True

        # 2. Frequency Check (Rate Limiting)
        now = time.time()
        # Keep only timestamps within the last 1 second
        self.order_timestamps = [t for t in self.order_timestamps if now - t <= 1.0]
        mps = self._max_orders_per_second()
        if len(self.order_timestamps) >= mps:
            raise RiskRejection(f"Order frequency exceeded {mps} ops/sec.")

        self.order_timestamps.append(now)

        # 3. Single Order Risk Check
        # Only check opening orders for risk capacity
        if not order.get('reduce_only', False) and self.current_balance > 0:
            risk_config = config_manager.get_config().risk
            
            # Estimate order notional value
            amount = float(order.get('amount', 0))
            # 市价单常见 price=None；get("price",0) 在键存在时仍返回 None → float(None) 崩溃
            price = float(order.get("price") or 0)
            
            # If market order and no price provided, we might need a fallback or current ticker.
            # For strict risk, price should be provided (e.g., last ticker price)
            if price <= 0:
                log.warning(f"Order {order.get('symbol')} missing price. Risk calc may be inaccurate.")
                # Can't accurately calculate risk without price, pass for now or reject based on strictness.
                # In HFT, we usually have the latest mid-price.
                pass 
            else:
                from src.core.paper_engine import paper_engine as _pe

                cs = _pe.contract_size_for_symbol(sym)
                ect = order.get("entry_context") or {}
                if not isinstance(ect, dict):
                    ect = {}
                leverage = max(float(order.get('leverage', 1) or 1.0), 1.0)
                if bool(ect.get("beta_neutral_hf")):
                    try:
                        from src.ai.regime import regime_classifier

                        ai = regime_classifier.snapshot(sym)
                        ai_cap = max(10.0, float(ai.get("suggested_leverage_cap", 10) or 10))
                        leverage = min(leverage, ai_cap)
                    except Exception:
                        leverage = min(leverage, 10.0)
                notional_value = float(
                    ect.get("intent_notional_usdt")
                    or order.get("notional_size")
                    or (amount * price * cs)
                    or 0.0
                )
                margin_required = float(
                    ect.get("intent_margin_usdt")
                    or order.get("margin_amount")
                    or (notional_value / leverage)
                    or 0.0
                )
                leverage = max(float(order.get('leverage', 1) or 1.0), 1.0)

                # Max margin vs equity：分档上限或 max_single_risk
                if getattr(risk_config, "use_equity_tier_margin", True):
                    from src.core.equity_sizing import margin_cap_fraction

                    cap_frac = margin_cap_fraction(self.current_balance)
                else:
                    cap_frac = float(risk_config.max_single_risk)
                max_allowed_margin = min(
                    self.current_balance * cap_frac,
                    float(getattr(risk_config, "max_margin_per_trade_usdt", 10.0) or 10.0),
                )
                max_allowed_notional = float(
                    getattr(risk_config, "max_notional_per_trade_usdt", 2000.0) or 2000.0
                )

                # Exposure-first risk model:
                # 1) If margin too large, compress notional by shrinking margin while keeping leverage unchanged.
                if margin_required > max_allowed_margin + 1e-12:
                    margin_required = max_allowed_margin
                    notional_value = margin_required * leverage

                # 2) If notional still exceeds absolute cap, compress margin again; do not reduce leverage.
                if notional_value > max_allowed_notional + 1e-12:
                    notional_value = max_allowed_notional
                    margin_required = notional_value / leverage

                # 3) Exchange minimum notional is the only legitimate micro-size veto.
                limits = ect.get("symbol_limits") or {}
                min_notional = float(limits.get("min_notional_usdt") or 0.0)
                if min_notional > 0 and notional_value + 1e-12 < min_notional:
                    raise RiskRejection(
                        f"VETO: Calculated notional {notional_value:.4f} USDT is below "
                        f"exchange minimum {min_notional:.4f} USDT."
                    )

                # 4) If exposure was compressed, rewrite the order in-place for downstream execution.
                implied_amount = notional_value / max(price * cs, 1e-12)
                if implied_amount <= 0:
                    raise RiskRejection("VETO: Compressed order amount became non-positive.")

                specs = _pe._exchange_physics_specs(sym) or {}
                if not bool(specs.get("enable_decimal", False)):
                    implied_amount = float(math.floor(float(implied_amount) + 1e-12))
                else:
                    o_min = float(specs.get("order_size_min") or 0.0)
                    if o_min > 0:
                        implied_amount = math.floor((float(implied_amount) + 1e-12) / o_min) * o_min
                if implied_amount <= 0:
                    raise RiskRejection("VETO: Compressed integer order amount became non-positive.")

                order["amount"] = float(implied_amount)
                order["notional_size"] = float(notional_value)
                order["margin_amount"] = float(margin_required)
                order["leverage"] = float(leverage)
                order["entry_context"] = {
                    **ect,
                    "intent_notional_usdt": float(notional_value),
                    "intent_margin_usdt": float(margin_required),
                    "risk_engine_exposure_model": True,
                    "risk_engine_margin_cap_usdt": float(max_allowed_margin),
                    "risk_engine_notional_cap_usdt": float(max_allowed_notional),
                }

        return True

    def calculate_dynamic_position(self, win_rate: float, payoff_ratio: float, atr_pct: float, max_leverage: int = 10) -> Dict[str, float]:
        """
        Calculate Dynamic Position Size and Leverage using Kelly Criterion and ATR Volatility Scaling.
        
        :param win_rate: Historical win rate (0.0 to 1.0)
        :param payoff_ratio: Average win / Average loss
        :param atr_pct: Current Average True Range as a percentage of price (e.g., 0.02 for 2%)
        :return: Dict containing 'recommended_risk_pct' and 'recommended_leverage'
        """
        # 1. Kelly Criterion for Optimal Risk Fraction (Half-Kelly for safety)
        if win_rate <= 0 or payoff_ratio <= 0:
            kelly_pct = 0.0
        else:
            kelly_pct = win_rate - ((1 - win_rate) / payoff_ratio)
            
        # Use Half-Kelly
        half_kelly = max(0.0, kelly_pct / 2.0)
        
        # Cap maximum risk：权益分档中点或 max_single_risk
        risk_config = config_manager.get_config().risk
        if self.current_balance > 0 and getattr(risk_config, "use_equity_tier_margin", True):
            from src.core.equity_sizing import margin_target_fraction

            max_single_risk = margin_target_fraction(self.current_balance)
        else:
            max_single_risk = risk_config.max_single_risk
        recommended_risk_pct = min(half_kelly, max_single_risk)
        
        # 2. Volatility Scaling for Leverage
        # High ATR = High Volatility -> Reduce Leverage
        # Assuming we want the position value to swing no more than our risk per day
        if atr_pct > 0:
            target_leverage = recommended_risk_pct / atr_pct
        else:
            target_leverage = 1.0

        cap = min(max_leverage, risk_config.grinder_leverage_max, risk_config.max_leverage)
        recommended_leverage = max(risk_config.grinder_leverage_min, min(int(target_leverage), cap))

        return {
            "recommended_risk_pct": recommended_risk_pct,
            "recommended_leverage": recommended_leverage,
        }

    TEN_MIN_SEC = 600.0
    MIN_SPAN_FOR_TIER_SEC = 600.0  # 窗口内从最早到最新价跨越满 10 分钟再用该窗的高低价算波动分档

    def record_ticker_for_10m_volatility(self, symbol: str, ts: float, price: float) -> None:
        """由行情 WS 逐笔推送；维护滚动 600s 窗口，计算 (高-低)/中价 作为波动率。"""
        if not symbol or price <= 0 or not (ts and float(ts) > 0):
            return
        dq = self._ten_min_ticks.setdefault(symbol, deque(maxlen=5000))
        tsf = float(ts)
        dq.append((tsf, float(price)))
        cutoff = tsf - self.TEN_MIN_SEC
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if len(dq) < 2:
            self.symbol_10m_range_pct[symbol] = 0.0
            return
        prices = [p for _, p in dq]
        hi, lo = max(prices), min(prices)
        mid = (hi + lo) / 2.0
        if mid <= 0:
            return
        rng = (hi - lo) / mid
        self.symbol_10m_range_pct[symbol] = min(float(rng), 1.0)

    def ten_min_range_pct(self, symbol: str) -> float:
        return float(self.symbol_10m_range_pct.get(symbol, 0.0) or 0.0)

    def _ten_min_window_ready(self, symbol: str) -> bool:
        dq = self._ten_min_ticks.get(symbol)
        if not dq or len(dq) < 2:
            return False
        return (dq[-1][0] - dq[0][0]) >= self.MIN_SPAN_FOR_TIER_SEC

    def _leverage_from_10m_range_pct(self, r: float) -> int:
        """
        10 分钟窗内价格波动比例 r（小数，3% = 0.03）：
        r ≤ 3% → 约 75x→50x（低波动区间仍放大，但避免默认顶到 100x）；
        3%~5% → 50x→20x；>5% → 20x→10x。
        """
        risk_config = config_manager.get_config().risk
        lo = max(1, int(risk_config.grinder_leverage_min))
        hi = max(lo, int(risk_config.grinder_leverage_max))
        if r <= 0.03:
            base = 75.0 - (r / 0.03) * 25.0
        elif r <= 0.05:
            base = 50.0 - (r - 0.03) / 0.02 * 30.0
        else:
            x = min(max((r - 0.05) / 0.10, 0.0), 1.0)
            base = 20.0 - x * 10.0
        lev = int(round(base))
        return max(lo, min(lev, hi))

    def record_symbol_atr_pct(self, symbol: str, atr_pct: float) -> None:
        if atr_pct > 0:
            # 防止窗口噪声把 ATR 推到 >100%，导致 Kelly 杠杆被压到地板上只剩 min 档
            self.symbol_atr_pct[symbol] = min(float(atr_pct), 0.5)

    def recommended_grinder_leverage(self, symbol: str) -> int:
        """优先按近 10 分钟价格波动分档；数据不足时回退 Kelly + 短窗 ATR。"""
        risk_config = config_manager.get_config().risk
        if self._ten_min_window_ready(symbol):
            lev = self._leverage_from_10m_range_pct(self.ten_min_range_pct(symbol))
            # 10 分钟振幅不大但短窗已在抖：压杠杆，减少「高倍 +  equity 括号仍偏紧」的连环止损
            ap = float(self.symbol_atr_pct.get(symbol, 0.0) or 0.0)
            thr = float(
                getattr(config_manager.get_config().strategy.params, "core_high_atr_threshold", 0.01)
                or 0.01
            )
            if thr > 0 and ap >= thr:
                cap = int(getattr(risk_config, "grinder_choppy_atr_cap_leverage", 35) or 35)
                lev = min(int(lev), max(1, cap))
            return int(lev)
        atr_pct = max(self.symbol_atr_pct.get(symbol, 0.02), 1e-6)
        out = self.calculate_dynamic_position(
            0.55,
            1.5,
            atr_pct,
            max_leverage=risk_config.grinder_leverage_max,
        )
        return int(out["recommended_leverage"])

    def reset_daily_high(self):
        """Called at daily rollover (e.g., 00:00 UTC) to reset the high water mark."""
        self.daily_high = self.total_equity or self.current_balance
        self.daily_drawdown = 0.0
        self.peak_to_trough_drawdown = 0.0
        self.peak_to_trough_drawdown_usdt = 0.0
        self.realized_high = self.realized_pnl
        self.realized_drawdown = 0.0
        self.realized_drawdown_usdt = 0.0
        self.is_halted = False
        self.halt_reason = ""
        log.info("Risk Engine daily metrics reset.")

# Global instance for easy access across the app
risk_engine = RiskEngine()
