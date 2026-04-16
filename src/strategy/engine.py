import asyncio
import math
from typing import Any, Dict, Optional, Tuple

from src.core.config_manager import MarketOracleConfig, config_manager
from src.config import SystemState
from src.utils.logger import log
from src.strategy.core_strategy import CoreNeutralStrategy, CoreAttackStrategy
from src.strategy.institutional_schemes import (
    MicroMakerStrategy,
    LiquidationSnipeStrategy,
    FundingSqueezeStrategy,
)
from src.strategy.user_loader import UserStrategyLoader
from src.strategy.l1_sniper import L1SniperStrategy
from src.strategy.slingshot import SlingshotStrategy
from src.strategy.micro_assassin import MicroAssassinStrategy
from src.strategy.shark_scalp import SharkScalpStrategy
from src.strategy.beta_neutral_hf import BetaNeutralHFScalpStrategy
from src.core.events import TickEvent, SignalEvent, OrderBookEvent
from src.core.risk_engine import risk_engine, RiskRejection
from src.core.paper_engine import paper_engine
from src.execution.order_types import OrderIntent, PairLegIntent, PairOrderIntent
from src.execution.order_manager import OrderManager
from src.ai.regime import regime_classifier, MarketRegime
from src.ai.analyzer import ai_context
from src.strategy.playbook import (
    PlaybookManager,
    PlaybookQuadrant,
    attach_playbook_to_signal,
    build_default_symbol_limits,
    select_quadrant,
)
from src.strategy.tuner import (
    apply_ai_confidence_discount_to_signal,
    apply_scene_learning_to_signal,
    feed_realized_net_from_exchange_result,
    strategy_auto_tuner,
)
from src.data.market_oracle import MarketOracle
from src.core.license_gate import assert_strategy_runtime_allowed

assert_strategy_runtime_allowed()


def _norm_symbol_key(s: str) -> str:
    return (s or "").replace(" ", "").upper()


def _market_oracle_veto_reason(
    signal: SignalEvent,
    snap: Dict[str, Any],
    oc: MarketOracleConfig,
) -> Optional[str]:
    """
    一票否决：拥挤做多、机构压盘（OBI）、大盘急跌时禁止中小币做多。
    数据缺失时不否决（仅打日志由上游快照可见）。
    """
    side = str(signal.side or "").lower()
    if side != "buy":
        return None
    sym = _norm_symbol_key(signal.symbol)
    anchor = _norm_symbol_key(oc.crash_anchor_symbol)

    ls = snap.get("ls_ratio")
    fr = snap.get("funding_rate")
    obi = snap.get("orderbook_imbalance")
    btc_ret = snap.get("anchor_return_pct")

    if ls is not None and fr is not None:
        if float(ls) > float(oc.crowded_ls_ratio) and float(fr) >= float(oc.crowded_funding_rate_min):
            return (
                f"crowded_long_veto(ls_ratio={float(ls):.3f}>{float(oc.crowded_ls_ratio):.3f}, "
                f"funding={float(fr):.6f}>={float(oc.crowded_funding_rate_min):.6f})"
            )
    if obi is not None and float(obi) < float(oc.long_obi_veto_max):
        return (
            f"smart_money_block_long(obi={float(obi):.3f}<{float(oc.long_obi_veto_max):.3f} "
            "heavy_asks_within_band)"
        )
    if sym != anchor and btc_ret is not None and float(btc_ret) <= float(oc.crash_max_anchor_return_pct):
        return (
            f"crash_avoidance_alt_long(anchor={oc.crash_anchor_symbol} "
            f"{int(oc.crash_lookback_minutes)}m_return={100.0 * float(btc_ret):.2f}% "
            f"<= {100.0 * float(oc.crash_max_anchor_return_pct):.2f}%)"
        )
    return None


def _calculate_dynamic_sizing(
    *,
    equity_usdt: float,
    win_rate: float,
    ref_price: float,
    atr: float,
    sl_atr_mult: float,
    tp_atr_mult: float,
    is_high_conviction: bool,
) -> Optional[dict]:
    """
    Kelly（分数）× ATR 止损距离 → 目标名义、合约张数、满足爆仓缓冲的杠杆与保证金。

    SL_Distance_Pct = (sl_atr_mult * atr) / ref_price
    Max_Loss = equity * max_account_risk_per_trade_pct
    Base_Notional = Max_Loss / SL_Distance_Pct
    Kelly 乘数：1 + kelly_fraction * max(0, (p*b - (1-p))/b)，p=win_rate, b=tp/sl；贪婪通道再乘 kelly_high_conviction_mult。
    杠杆：floor(1 / (SL_Distance_Pct * liquidation_safety_mult)) 并截断至 max_allowed_leverage。
    """
    ex_cfg = config_manager.get_config().execution
    if equity_usdt <= 0 or ref_price <= 0 or atr <= 0:
        return None
    sl_dist_pct = (float(sl_atr_mult) * float(atr)) / float(ref_price)
    if sl_dist_pct <= 1e-14:
        return None

    b = float(tp_atr_mult) / max(float(sl_atr_mult), 1e-12)
    p = float(win_rate)
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    f_star = max(0.0, (p * b - (1.0 - p)) / max(b, 1e-12))
    kelly_mult = 1.0 + float(ex_cfg.kelly_fraction) * min(f_star, 1.0)
    if is_high_conviction:
        kelly_mult *= float(ex_cfg.kelly_high_conviction_mult)
    kelly_mult = min(kelly_mult, float(ex_cfg.max_kelly_notional_mult))

    max_loss = float(equity_usdt) * float(ex_cfg.max_account_risk_per_trade_pct)
    base_notional = max_loss / sl_dist_pct
    notional = base_notional * kelly_mult

    sm = float(ex_cfg.liquidation_safety_mult)
    k_liq = sl_dist_pct * sm
    lev_cap = int(
        max(
            1,
            min(
                int(ex_cfg.max_allowed_leverage),
                math.floor((1.0 / k_liq) - 1e-9),
            ),
        )
    )
    margin = notional / float(lev_cap) if lev_cap > 0 else notional

    return {
        "sl_distance_pct": sl_dist_pct,
        "kelly_mult": kelly_mult,
        "notional_usdt": notional,
        "margin_usdt": margin,
        "leverage": lev_cap,
        "base_notional_usdt": base_notional,
    }


def _apply_playbook_execution_plan_to_intent(intent: OrderIntent, signal: SignalEvent) -> bool:
    """PlaybookManager 矩阵 + 币对约束 → 覆盖名义/张数/杠杆/保证金。"""
    if intent.reduce_only:
        return False
    ect_sig = dict(signal.entry_context or {})
    if bool(ect_sig.get("beta_bypass_playbook")):
        return False
    plan = ect_sig.get("playbook_execution_plan")
    if not plan:
        return False
    limits = ect_sig.get("symbol_limits") or {}
    plan = _apply_scene_learning_bias_to_plan(dict(plan), ect_sig, limits)
    plan = _apply_core_margin_floor_to_plan(plan, signal, limits)
    ect = dict(intent.entry_context or {})
    ref_px = float(intent.price or getattr(signal, "price", None) or ect.get("ref_price") or 0.0)
    if ref_px <= 0:
        return False
    notional = float(plan["notional_usdt"])
    lev = int(plan.get("dynamic_leverage", plan["leverage"]))
    margin = float(plan.get("margin_amount", plan["margin_usdt"]))
    cs = float(paper_engine._resolve_contract_size(intent.symbol))
    contracts = notional / max(ref_px * cs, 1e-12)
    if contracts <= 0:
        return False
    intent.amount = float(contracts)
    intent.leverage = lev
    intent.notional_size = notional
    intent.margin_amount = margin
    intent.entry_context = {
        **ect,
        **ect_sig,
        "dynamic_sizing": True,
        "playbook_matrix_sizing": True,
    }
    return True


def _apply_scene_learning_bias_to_plan(plan: Dict[str, Any], ect_sig: Dict[str, Any], limits: Dict[str, Any]) -> Dict[str, Any]:
    bias = dict(ect_sig.get("scene_learning") or {})
    mult = float(bias.get("margin_mult", 1.0) or 1.0)
    if abs(mult - 1.0) <= 1e-9:
        return plan

    lev = max(1.0, float(plan.get("dynamic_leverage", plan.get("leverage", 1)) or 1.0))
    min_nom = float(limits.get("min_notional_usdt") or 0.0)
    max_nom = float(limits.get("max_notional_usdt") or float("inf"))
    notional = max(0.0, float(plan.get("notional_usdt", 0.0) or 0.0) * mult)
    if max_nom < float("inf"):
        notional = min(notional, max_nom)
    if min_nom > 0:
        notional = max(notional, min_nom)
    margin = notional / lev if lev > 0 else 0.0
    out = dict(plan)
    out["notional_usdt"] = float(notional)
    out["margin_usdt"] = float(margin)
    out["margin_amount"] = float(margin)
    out["scene_learning_match"] = str(bias.get("match_level") or "none")
    out["scene_learning_margin_mult"] = float(mult)
    return out


def _apply_core_margin_floor_to_plan(plan: Dict[str, Any], signal: SignalEvent, limits: Dict[str, Any]) -> Dict[str, Any]:
    if getattr(signal, "strategy_name", "") not in {"CoreNeutral", "CoreAttack"}:
        return plan
    params = config_manager.get_config().strategy.params
    risk_cfg = config_manager.get_config().risk
    lev = max(1.0, float(plan.get("dynamic_leverage", plan.get("leverage", 1)) or 1.0))
    margin = max(0.0, float(plan.get("margin_usdt", 0.0) or 0.0))
    cap = max(0.0, float(getattr(risk_cfg, "max_margin_per_trade_usdt", 10.0) or 10.0))
    floor_abs = max(0.0, float(getattr(params, "core_margin_floor_usdt", 6.0) or 0.0))
    floor_frac = max(0.0, float(getattr(params, "core_margin_floor_cap_fraction", 0.60) or 0.0))
    floor = min(cap, max(floor_abs, cap * floor_frac))
    if floor <= 0 or margin >= floor:
        return plan

    min_nom = float(limits.get("min_notional_usdt") or 0.0)
    max_nom = float(limits.get("max_notional_usdt") or float("inf"))
    notional = max(float(plan.get("notional_usdt", 0.0) or 0.0), floor * lev)
    if max_nom < float("inf"):
        notional = min(notional, max_nom)
    if min_nom > 0:
        notional = max(notional, min_nom)
    new_margin = notional / lev if lev > 0 else margin
    out = dict(plan)
    out["notional_usdt"] = float(notional)
    out["margin_usdt"] = float(new_margin)
    out["margin_amount"] = float(new_margin)
    out["core_margin_floor_applied"] = True
    out["core_margin_floor_usdt"] = float(floor)
    return out


def _force_route_playbook_plan(intent: OrderIntent, signal: SignalEvent) -> bool:
    """
    Hard-route all opening intents through PlaybookManager.
    If async enrichment has not populated the plan yet, build a synchronous fallback plan
    from current equity + ATR% + symbol limits/default limits.
    """
    if intent.reduce_only:
        return False

    ect_sig = dict(signal.entry_context or {})
    if bool(ect_sig.get("beta_bypass_playbook")):
        return False
    if ect_sig.get("playbook_execution_plan"):
        return True

    pb = config_manager.get_config().playbook
    ref_px = float(intent.price or getattr(signal, "price", None) or ect_sig.get("ref_price") or 0.0)
    if ref_px <= 0:
        return False

    # RiskEngine currently persists ATR% snapshots, not raw ATR values.
    # Never touch a non-existent `symbol_atr` attribute here, or the signal processor dies.
    atr = float(getattr(signal, "atr_value", None) or ect_sig.get("atr_value") or 0.0)
    atr_pct = float(ect_sig.get("playbook_vol_pct") or 0.0)
    if atr_pct <= 0.0 and atr > 0 and ref_px > 0:
        atr_pct = atr / ref_px
    if atr_pct <= 0.0:
        atr_pct_map = getattr(risk_engine, "symbol_atr_pct", {}) or {}
        atr_pct = float(atr_pct_map.get(intent.symbol, 0.0) or 0.0)
    if atr_pct <= 0.0:
        atr_pct = float(pb.matrix_volatility_threshold_pct)

    q_raw = ect_sig.get("playbook_quadrant")
    quadrant: Optional[PlaybookQuadrant] = None
    if q_raw:
        try:
            quadrant = PlaybookQuadrant(str(q_raw))
        except ValueError:
            quadrant = None
    if quadrant is None:
        quadrant = select_quadrant(
            float(risk_engine.current_balance or 0.0),
            atr_pct,
            capital_threshold=pb.matrix_capital_threshold_usdt,
            volatility_threshold=pb.matrix_volatility_threshold_pct,
        ) or PlaybookQuadrant.B

    limits = ect_sig.get("symbol_limits") or build_default_symbol_limits(intent.symbol, ref_px)
    plan = PlaybookManager.get_execution_plan(
        total_equity=float(risk_engine.current_balance or 0.0),
        current_atr_pct=float(atr_pct),
        symbol_limits=limits,
        quadrant=quadrant,
    )
    plan = _apply_scene_learning_bias_to_plan(plan, ect_sig, limits)
    plan = _apply_core_margin_floor_to_plan(plan, signal, limits)
    ect_sig["playbook_quadrant"] = quadrant.value
    ect_sig["playbook_vol_pct"] = atr_pct
    ect_sig["symbol_limits"] = limits
    ect_sig["playbook_execution_plan"] = plan
    signal.entry_context = ect_sig
    return True


def _apply_guerrilla_sizing_to_intent(intent: OrderIntent, signal: SignalEvent) -> None:
    """无 execution_plan 时的回退：仅游击标签使用固定 guerrilla_margin_fraction。"""
    if intent.reduce_only:
        return
    ect = dict(intent.entry_context or {})
    if ect.get("playbook_execution_plan") or (signal.entry_context or {}).get("playbook_execution_plan"):
        return
    if not ect.get("playbook_guerrilla"):
        return
    pb = config_manager.get_config().playbook
    equity = float(risk_engine.current_balance or 0.0)
    if equity <= 0:
        return
    ref_px = float(intent.price or getattr(signal, "price", None) or ect.get("ref_price") or 0.0)
    if ref_px <= 0:
        return
    margin = equity * float(pb.guerrilla_margin_fraction)
    lev = max(1, int(pb.guerrilla_leverage))
    try:
        lev = min(lev, int(config_manager.get_config().risk.max_leverage))
    except Exception:
        pass
    ex_cfg = config_manager.get_config().execution
    lev = min(lev, int(ex_cfg.max_allowed_leverage))
    notional = margin * float(lev)
    cs = float(paper_engine._resolve_contract_size(intent.symbol))
    denom = max(ref_px * cs, 1e-12)
    contracts = notional / denom
    if contracts <= 0:
        return
    intent.amount = float(contracts)
    intent.leverage = int(lev)
    intent.notional_size = float(notional)
    intent.margin_amount = float(margin)
    intent.entry_context = {
        **ect,
        "dynamic_sizing": True,
        "playbook_guerrilla_sizing": True,
        "sizing_equity_usdt": equity,
        "intent_notional_usdt": intent.notional_size,
        "intent_margin_usdt": intent.margin_amount,
    }


def _apply_dynamic_sizing_to_intent(intent: OrderIntent, signal: SignalEvent) -> None:
    """当存在 ai_win_rate 且可解析 ATR/参考价时，覆写 intent.amount / leverage 并写入名义与保证金元数据。"""
    if intent.reduce_only:
        return
    ect = dict(intent.entry_context or {})
    if ect.get("playbook_execution_plan") or (signal.entry_context or {}).get("playbook_execution_plan"):
        return
    if ect.get("playbook_guerrilla"):
        return
    if getattr(signal, "ai_win_rate", None) is None:
        return

    ect = dict(intent.entry_context or {})
    ref_px = float(intent.price or getattr(signal, "price", None) or ect.get("ref_price") or 0.0)
    atr = float(
        getattr(signal, "atr_value", None)
        or ect.get("atr_value")
        or 0.0
    )
    ex_cfg = config_manager.get_config().execution
    equity = float(risk_engine.current_balance or 0.0)
    if equity <= 0:
        log.debug("[Sizing] risk_engine.current_balance<=0, skip dynamic sizing")
        return
    if ref_px <= 0 or atr <= 0:
        log.debug("[Sizing] missing ref_px or atr, skip dynamic sizing")
        return

    sz = _calculate_dynamic_sizing(
        equity_usdt=equity,
        win_rate=float(signal.ai_win_rate),
        ref_price=ref_px,
        atr=atr,
        sl_atr_mult=float(ex_cfg.sniper_atr_sl_mult),
        tp_atr_mult=float(ex_cfg.sniper_atr_tp_mult),
        is_high_conviction=bool(intent.is_high_conviction),
    )
    if not sz:
        return

    cs = float(paper_engine._resolve_contract_size(intent.symbol))
    denom = max(ref_px * cs, 1e-12)
    contracts = sz["notional_usdt"] / denom
    if contracts <= 0:
        return

    intent.amount = float(contracts)
    intent.leverage = int(sz["leverage"])
    intent.notional_size = float(sz["notional_usdt"])
    intent.margin_amount = float(sz["margin_usdt"])
    intent.entry_context = {
        **ect,
        "dynamic_sizing": True,
        "sl_distance_pct": sz["sl_distance_pct"],
        "kelly_mult_applied": sz["kelly_mult"],
        "base_notional_usdt": sz["base_notional_usdt"],
        "sizing_equity_usdt": equity,
        "intent_notional_usdt": intent.notional_size,
        "intent_margin_usdt": intent.margin_amount,
    }
    log.warning(
        f"[SIZING DEBUG] {intent.symbol} equity={equity:.4f} atr={atr:.8f} ref={ref_px:.8f} wr={float(signal.ai_win_rate):.4f} | "
        f"notional≈{intent.notional_size:.4f} margin≈{intent.margin_amount:.4f} lev={intent.leverage}x "
        f"contracts≈{intent.amount:.6f} | sl_dist_pct={sz['sl_distance_pct']:.6f} kelly_mult={sz['kelly_mult']:.4f}"
    )


def _apply_probe_micro_notional(intent: OrderIntent, signal: SignalEvent) -> None:
    """
    侦察模式：先压杠杆到 probe_leverage（与 risk / execution 上限对齐），再把名义对齐
    max(配置地板, 交易所 min_notional)，保证 margin = notional / lev 满足 API 最小约束。
    """
    if intent.reduce_only:
        return
    if dict(intent.entry_context or {}).get("playbook_matrix_sizing"):
        return
    try:
        c = config_manager.get_config().auto_tuner
        if not c.enabled or not strategy_auto_tuner.probe_mode:
            return
    except Exception:
        return
    ect = dict(intent.entry_context or {})
    sig = dict(signal.entry_context or {})
    limits = ect.get("symbol_limits") or sig.get("symbol_limits")
    min_ex = float((limits or {}).get("min_notional_usdt") or 0.0)
    floor = float(c.probe_notional_floor_usdt)
    target = max(floor, min_ex) if min_ex > 0 else floor
    ref_px = float(intent.price or getattr(signal, "price", None) or ect.get("ref_price") or 0.0)
    if ref_px <= 0:
        return
    risk_cfg = config_manager.get_config().risk
    ex_cfg = config_manager.get_config().execution
    probe_lev = max(1, int(c.probe_leverage))
    probe_lev = min(
        probe_lev,
        max(1, int(risk_cfg.max_leverage)),
        max(1, int(ex_cfg.max_allowed_leverage)),
    )
    intent.leverage = int(probe_lev)
    intent.notional_size = float(target)
    intent.margin_amount = float(target) / float(probe_lev)
    cs = float(paper_engine._resolve_contract_size(intent.symbol))
    intent.amount = float(target) / max(ref_px * cs, 1e-12)
    intent.entry_context = {
        **ect,
        "probe_micro_notional": True,
        "probe_target_notional_usdt": target,
        "probe_leverage": int(probe_lev),
    }


def _intent_with_sizing(intent: OrderIntent, signal: SignalEvent) -> Tuple[bool, OrderIntent]:
    _force_route_playbook_plan(intent, signal)
    if _apply_playbook_execution_plan_to_intent(intent, signal):
        return True, intent
    # Final fail-safe: if playbook plan still cannot be applied, fall back to dynamic sizing
    # instead of leaving amount/leverage in an undefined state.
    ect = dict(intent.entry_context or {})
    if ect.get("playbook_guerrilla"):
        _apply_guerrilla_sizing_to_intent(intent, signal)
    else:
        _apply_dynamic_sizing_to_intent(intent, signal)
    return True, intent


def _build_guerrilla_order_intent(
    signal: SignalEvent,
) -> Tuple[bool, Optional[OrderIntent]]:
    """小资金 + 低波动：限价 Post-Only Maker、无 ATR 宽幅 TP/SL、时间止损由 paper_engine 执行。"""
    pb = config_manager.get_config().playbook
    ex_cfg = config_manager.get_config().execution
    ttl_ms = int(pb.guerrilla_order_ttl_ms or ex_cfg.sniper_normal_ttl_ms)
    ect_base = dict(getattr(signal, "entry_context", None) or {})

    ref_px = float(getattr(signal, "price", None) or 0.0)
    if ref_px <= 0:
        ref_px = float(ect_base.get("ref_price") or 0.0)
    if ref_px <= 0:
        log.warning("[Playbook] Guerrilla intent skipped: missing ref price")
        return False, None

    intent = OrderIntent.from_signal(signal, ttl_ms=ttl_ms)
    intent.order_type = "limit"
    intent.post_only = True
    intent.price = ref_px
    intent.ttl_ms = ttl_ms
    intent.is_high_conviction = False
    intent.trailing_stop_activation_pct = 0.0
    intent.trailing_stop_callback_pct = 0.0
    intent.entry_context = {
        **ect_base,
        "resting_quote": True,
        "paper_shadow_limit": True,
        "playbook_guerrilla": True,
        "position_ttl_minutes": float(pb.position_ttl_minutes),
    }
    lev_cap = max(1, int(pb.guerrilla_leverage))
    try:
        lev_cap = min(lev_cap, int(config_manager.get_config().risk.max_leverage))
    except Exception:
        pass
    lev_cap = min(lev_cap, int(ex_cfg.max_allowed_leverage))
    intent.leverage = int(lev_cap)
    return _intent_with_sizing(intent, signal)


def _resolve_limit_ttl_ms(signal: SignalEvent, ex_cfg: Any) -> int:
    ect = dict(getattr(signal, "entry_context", None) or {})
    ttl = int(ect.get("core_limit_ttl_ms") or 0)
    if ttl > 0 and str(getattr(signal, "order_type", "")).lower() == "limit":
        return ttl
    if str(getattr(signal, "order_type", "")).lower() == "limit":
        return int(ex_cfg.default_order_ttl_ms)
    return 0


def _build_order_intent_with_sniper_pipeline(
    signal: SignalEvent,
) -> Tuple[bool, Optional[OrderIntent]]:
    """
    狙击手过滤 + 双通道路由 → OrderIntent。
    若 ai_win_rate 未设置：走旧逻辑（限价可带 paper_shadow_limit）。
    返回 (False, None)：硬性拦截丢弃；(True, intent)：执行。
    """
    ex_cfg = config_manager.get_config().execution
    ect_base = dict(getattr(signal, "entry_context", None) or {})

    if ect_base.get("playbook_guerrilla") and not getattr(signal, "reduce_only", False):
        return _build_guerrilla_order_intent(signal)

    if getattr(signal, "reduce_only", False):
        ttl_ms = _resolve_limit_ttl_ms(signal, ex_cfg)
        intent = OrderIntent.from_signal(signal, ttl_ms=ttl_ms)
        if str(intent.order_type).lower() == "limit":
            intent.entry_context = {**dict(intent.entry_context), "paper_shadow_limit": True}
        return _intent_with_sizing(intent, signal)

    wr_raw = getattr(signal, "ai_win_rate", None)
    if wr_raw is None:
        ttl_ms = _resolve_limit_ttl_ms(signal, ex_cfg)
        intent = OrderIntent.from_signal(signal, ttl_ms=ttl_ms)
        if str(intent.order_type).lower() == "limit":
            extra = {}
            if getattr(signal, "strategy_name", "") in {"CoreNeutral", "CoreAttack"} and not getattr(signal, "reduce_only", False):
                extra = {"resting_quote": True, "paper_shadow_limit": True}
                intent.post_only = True
            else:
                extra = {"paper_shadow_limit": True}
            intent.entry_context = {**dict(intent.entry_context), **extra}
        return _intent_with_sizing(intent, signal)

    wr = float(wr_raw)
    floor = float(ex_cfg.sniper_win_rate_floor)
    if wr < floor:
        log.info(f"[Sniper] Discarded (ai_win_rate={wr:.3f} < {floor})")
        return False, None

    atr_v = getattr(signal, "atr_value", None)
    if atr_v is None or float(atr_v or 0) <= 0:
        atr_v = float(ect_base.get("atr_value") or 0.0)
    atr_f = float(atr_v)
    ref_px = float(getattr(signal, "price", 0) or 0)
    side = str(signal.side).lower()
    hi = float(ex_cfg.high_conviction_win_rate_floor)

    if wr >= hi:
        act = float(ex_cfg.high_conviction_trailing_activation_pct)
        cb = float(ex_cfg.high_conviction_trailing_callback_pct)
        intent = OrderIntent.from_signal(signal, ttl_ms=0)
        intent.order_type = "market"
        intent.price = None
        intent.post_only = False
        intent.ttl_ms = 0
        intent.is_high_conviction = True
        intent.trailing_stop_activation_pct = act
        intent.trailing_stop_callback_pct = cb
        intent.entry_context = {
            **dict(intent.entry_context),
            **ect_base,
            "high_conviction_trailing": True,
            "trailing_stop_activation_pct": act,
            "trailing_stop_callback_pct": cb,
        }
        log.info(f"[Sniper] High-conviction channel (wr={wr:.3f}) → market + trailing stop")
        return _intent_with_sizing(intent, signal)

    if ref_px <= 0 or atr_f <= 0:
        log.warning(
            f"[Sniper] Normal channel missing ref price or ATR (px={ref_px}, atr={atr_f}); "
            "fallback limit+shadow"
        )
        ttl_ms = int(ex_cfg.sniper_normal_ttl_ms)
        intent = OrderIntent.from_signal(signal, ttl_ms=ttl_ms)
        intent.order_type = "limit"
        intent.ttl_ms = ttl_ms
        intent.is_high_conviction = False
        intent.trailing_stop_activation_pct = 0.0
        intent.trailing_stop_callback_pct = 0.0
        if intent.price is None and ref_px > 0:
            intent.price = ref_px
        intent.entry_context = {**dict(intent.entry_context), **ect_base, "paper_shadow_limit": True}
        return _intent_with_sizing(intent, signal)

    sl_m = float(ex_cfg.sniper_atr_sl_mult)
    tp_m = float(ex_cfg.sniper_atr_tp_mult)
    if side == "buy":
        sl = ref_px - sl_m * atr_f
        tp = ref_px + tp_m * atr_f
    else:
        sl = ref_px + sl_m * atr_f
        tp = ref_px - tp_m * atr_f

    ttl_ms = int(ex_cfg.sniper_normal_ttl_ms)
    intent = OrderIntent.from_signal(signal, ttl_ms=ttl_ms)
    intent.order_type = "limit"
    intent.price = ref_px
    intent.post_only = True
    intent.ttl_ms = ttl_ms
    intent.is_high_conviction = False
    intent.trailing_stop_activation_pct = 0.0
    intent.trailing_stop_callback_pct = 0.0
    intent.entry_context = {
        **dict(intent.entry_context),
        **ect_base,
        "resting_quote": True,
        "take_profit_limit_price": tp,
        "stop_loss_limit_price": sl,
    }
    sl_dist_abs = abs(ref_px - sl)
    tp_dist_abs = abs(tp - ref_px)
    sl_bps = (sl_dist_abs / ref_px) * 1e4 if ref_px > 0 else -1.0
    tp_bps = (tp_dist_abs / ref_px) * 1e4 if ref_px > 0 else -1.0
    log.warning(
        f"[SNIPER DEBUG ATR/TP-SL] symbol pending wr={wr:.4f} atr={atr_f:.8f} ref_px={ref_px:.8f} | "
        f"SL_px={sl:.8f} TP_px={tp:.8f} | dist SL={sl_dist_abs:.8f} ({sl_bps:.2f}bps) "
        f"TP={tp_dist_abs:.8f} ({tp_bps:.2f}bps) mults sl×ATR={sl_m} tp×ATR={tp_m}"
    )
    log.info(
        f"[Sniper] Normal channel (wr={wr:.3f}) → post-only limit {ttl_ms}ms, "
        f"TP/SL from ATR mult sl={sl_m} tp={tp_m}"
    )
    return _intent_with_sizing(intent, signal)


class StrategyEngine:
    """
    Event-driven Trading Engine & Mode Switcher.
    Responsible for tick generation, strategy dispatch, risk interception, and execution.
    """
    def __init__(self, exchange, state_machine):
        self.exchange = exchange
        self.state_machine = state_machine
        self.running = False
        self.paused = False
        self.order_manager: Optional[OrderManager] = None
        self._market_oracle = MarketOracle(exchange)
        self.strategies = []
        self._load_strategies()
        self.signal_queue = asyncio.Queue()

    @property
    def is_running(self):
        return self.running and not self.paused

    def pause(self):
        self.paused = True
        log.info("Strategy Engine Paused by User")

    def resume(self):
        self.paused = False
        log.info("Strategy Engine Resumed by User")

    def _load_strategies(self):
        # 1. Load Core Strategies
        self.strategies.append(CoreNeutralStrategy())
        self.strategies.append(CoreAttackStrategy())
        self.strategies.append(MicroMakerStrategy())
        self.strategies.append(LiquidationSnipeStrategy())
        self.strategies.append(FundingSqueezeStrategy())
        self.strategies.append(L1SniperStrategy())
        self.strategies.append(SlingshotStrategy())
        self.strategies.append(MicroAssassinStrategy())
        self.strategies.append(BetaNeutralHFScalpStrategy())

        # 2. Load User Strategies
        user_loader = UserStrategyLoader()
        user_strategies = user_loader.load_strategies()
        self.strategies.extend(user_strategies)
        
        log.info(f"Strategy Engine loaded {len(self.strategies)} strategies.")

    @staticmethod
    def _position_abs_size(symbol: str) -> float:
        pos = paper_engine.positions.get(symbol)
        if not pos:
            return 0.0
        return abs(float(pos.get("size", 0.0) or 0.0))

    @staticmethod
    def _pair_leg_status_snapshot(res: Any) -> str:
        if isinstance(res, Exception):
            return f"exception:{type(res).__name__}:{res!s}"
        if isinstance(res, dict):
            st = res.get("status", "?")
            rsn = res.get("reason") or res.get("label") or res.get("message")
            oid = res.get("id", "")
            return f"status={st!r} reason={rsn!r} id={oid!r}"
        return repr(res)

    def _log_fatal_pair_panic(self, pair_id: str, alt_res: Any, anchor_res: Any) -> None:
        log.critical(
            f"[FATAL] Panic Close Triggered for {pair_id}! ALT status: {self._pair_leg_status_snapshot(alt_res)}, "
            f"BTC status: {self._pair_leg_status_snapshot(anchor_res)}. Find out why the hedge leg failed!"
        )

    def _filled_amount_from_result(
        self,
        leg: PairLegIntent,
        res: Any,
        before_size: float,
        after_size: Optional[float] = None,
    ) -> float:
        if isinstance(res, dict):
            for key in ("filled", "left", "amount"):
                if key == "filled":
                    try:
                        v = abs(float(res.get(key, 0.0) or 0.0))
                        if v > 0:
                            return min(v, abs(float(leg.amount)))
                    except Exception:
                        pass
        after = self._position_abs_size(leg.symbol) if after_size is None else float(after_size)
        if leg.reduce_only:
            delta = max(0.0, float(before_size) - float(after))
        else:
            delta = max(0.0, float(after) - float(before_size))
        return min(delta, abs(float(leg.amount)))

    async def _panic_close_leg(self, leg: PairLegIntent, opened_res: Dict[str, Any], before_size: float = 0.0) -> None:
        try:
            filled_amount = self._filled_amount_from_result(leg, opened_res, before_size)
            if filled_amount <= 1e-12:
                return
            close_side = "sell" if str(leg.side).lower() == "buy" else "buy"
            await self.exchange.create_order(
                symbol=leg.symbol,
                side=close_side,
                amount=float(filled_amount),
                price=None,
                reduce_only=True,
                leverage=max(int(leg.leverage), 1),
                margin_mode="isolated",
                berserker=bool(leg.berserker),
                post_only=False,
                entry_context={
                    **dict(leg.entry_context or {}),
                    "panic_close": True,
                    "exit_reason": "pair_atomic_panic_close",
                },
                exit_reason="pair_atomic_panic_close",
                order_text=f"panic-{(opened_res or {}).get('id', '')}"[:28],
            )
            log.warning(f"[PairAtomic] Panic close sent for {leg.symbol} filled={filled_amount:.8f}")
        except Exception as e:
            log.error(f"[PairAtomic] Panic close failed for {leg.symbol}: {e}")

    @staticmethod
    def _pair_leg_filled(res: Any) -> bool:
        if not isinstance(res, dict):
            return False
        st = str(res.get("status", "")).lower()
        return st in {"closed", "filled", "partially_filled"}

    async def _submit_leg(self, leg: PairLegIntent) -> Any:
        if abs(float(leg.amount)) <= 1e-12:
            return {"status": "skipped", "filled": 0.0, "symbol": leg.symbol, "side": leg.side}
        return await self.exchange.create_order(
            symbol=leg.symbol,
            side=leg.side,
            amount=leg.amount,
            price=leg.price if str(leg.order_type).lower() == "limit" else None,
            reduce_only=leg.reduce_only,
            leverage=int(leg.leverage),
            margin_mode="isolated",
            berserker=bool(leg.berserker),
            post_only=bool(leg.post_only),
            entry_context=leg.entry_context if leg.entry_context else None,
            exit_reason=(leg.entry_context or {}).get("exit_reason"),
            order_text=(leg.entry_context or {}).get("client_oid"),
        )

    async def _submit_pair_legs(self, alt_leg: PairLegIntent, anchor_leg: PairLegIntent) -> tuple[Any, Any]:
        results = await asyncio.gather(
            self._submit_leg(alt_leg),
            self._submit_leg(anchor_leg),
            return_exceptions=True,
        )
        return results[0], results[1]

    @staticmethod
    def _pair_leg_submit_ok(res: Any) -> bool:
        if isinstance(res, Exception):
            return False
        if not isinstance(res, dict):
            return False
        st = str(res.get("status", "")).lower()
        return st not in {"rejected", "error", "failed"}

    @staticmethod
    def _pair_leg_filled_enough_for_realized(res: Any) -> bool:
        if not isinstance(res, dict):
            return False
        st = str(res.get("status", "")).lower()
        return st in {"closed", "filled", "partially_filled"}

    async def _chase_resting_leg(self, leg: PairLegIntent, before_size: float, maker_grace_ms: int, retries: int = 2) -> Any:
        working = PairLegIntent(**leg.__dict__)
        last_res: Any = {"status": "rejected", "reason": "maker_not_attempted"}
        for _ in range(max(retries, 0)):
            await asyncio.sleep(max(maker_grace_ms, 1) / 1000.0)
            current_after = self._position_abs_size(leg.symbol)
            if self._filled_amount_from_result(leg, last_res, before_size, current_after) > 0:
                return {"status": "filled_via_resting", "filled": current_after - before_size, "price": leg.price}
            if isinstance(last_res, dict) and last_res.get("id"):
                try:
                    await self.exchange.cancel_order(str(last_res["id"]), leg.symbol)
                except Exception:
                    pass
            bb, ba = paper_engine._best_bid_ask(leg.symbol)
            if str(leg.side).lower() == "buy":
                px = bb if bb > 0 else (ba if ba > 0 else float(leg.price or 0.0))
            else:
                px = ba if ba > 0 else (bb if bb > 0 else float(leg.price or 0.0))
            working.price = float(px)
            working.entry_context = {
                **dict(leg.entry_context or {}),
                "maker_chase_attempt": int((working.entry_context or {}).get("maker_chase_attempt", 0)) + 1,
                "maker_filled": True,
            }
            last_res = await self._submit_leg(working)
            if self._pair_leg_filled(last_res):
                return last_res
        taker_leg = PairLegIntent(
            symbol=leg.symbol,
            side=leg.side,
            order_type="market",
            amount=float(leg.amount),
            price=None,
            leverage=int(leg.leverage),
            margin_mode=str(leg.margin_mode),
            reduce_only=bool(leg.reduce_only),
            post_only=False,
            berserker=bool(leg.berserker),
            entry_context={**dict(leg.entry_context or {}), "maker_fallback_to_taker": True},
        )
        return await self._submit_leg(taker_leg)

    async def _execute_pair_order_intent(self, pair: PairOrderIntent) -> None:
        legs = [pair.alt_leg, pair.anchor_leg]
        orders = []
        for leg in legs:
            orders.append(
                {
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "type": leg.order_type,
                    "amount": leg.amount,
                    "price": leg.price,
                    "leverage": leg.leverage,
                    "reduce_only": leg.reduce_only,
                    "berserker": bool(leg.berserker),
                    "entry_context": dict(leg.entry_context or {}),
                }
            )
        for order in orders:
            risk_engine.check_order(order)
        for leg, order in zip(legs, orders):
            leg.amount = float(order.get("amount", leg.amount))
            leg.leverage = int(float(order.get("leverage", leg.leverage)))
            if isinstance(order.get("entry_context"), dict):
                leg.entry_context = dict(order["entry_context"])

        log.info(
            f"[PairAtomic] Executing {pair.pair_id} ALT={pair.alt_leg.symbol}:{pair.alt_leg.side} "
            f"ANCHOR={pair.anchor_leg.symbol}:{pair.anchor_leg.side}"
        )
        alt_before = self._position_abs_size(pair.alt_leg.symbol)
        anchor_before = self._position_abs_size(pair.anchor_leg.symbol)

        # Sequential open: ALT first, then anchor. Parallel submit allowed anchor (BTC) to fill
        # while ALT rejected/resting, producing naked-BTC churn and pair_atomic_panic_close fee bleed.
        alt_res = await self._submit_leg(pair.alt_leg)
        if isinstance(alt_res, Exception):
            raise RiskRejection(f"Atomic pair alt leg exception pair_id={pair.pair_id}: {alt_res!r}")
        if not self._pair_leg_submit_ok(alt_res):
            raise RiskRejection(
                f"Atomic pair alt submit failed pair_id={pair.pair_id} alt_res={alt_res!r}"
            )
        alt_resting = isinstance(alt_res, dict) and str(alt_res.get("status", "")).lower() == "resting"
        if alt_resting:
            alt_res = await self._chase_resting_leg(pair.alt_leg, alt_before, pair.maker_grace_ms, retries=2)
        alt_ok = self._filled_amount_from_result(pair.alt_leg, alt_res, alt_before) > 0
        if not alt_ok:
            raise RiskRejection(
                f"Atomic pair alt not filled pair_id={pair.pair_id} alt_res={alt_res!r}"
            )

        anchor_zero = abs(float(pair.anchor_leg.amount)) <= 1e-12
        if anchor_zero:
            log.info(f"[PairAtomic] anchor leg skipped (BTC radar-only) pair_id={pair.pair_id}")
            feed_realized_net_from_exchange_result(alt_res)
            return

        anchor_res = await self._submit_leg(pair.anchor_leg)
        if isinstance(anchor_res, Exception):
            if pair.panic_close_on_partial:
                self._log_fatal_pair_panic(pair.pair_id, alt_res, anchor_res)
                await self._panic_close_leg(pair.alt_leg, alt_res if isinstance(alt_res, dict) else {}, alt_before)
            raise RiskRejection(
                f"Atomic pair anchor leg exception pair_id={pair.pair_id}: {anchor_res!r}"
            )
        if not self._pair_leg_submit_ok(anchor_res):
            if pair.panic_close_on_partial:
                self._log_fatal_pair_panic(pair.pair_id, alt_res, anchor_res)
                await self._panic_close_leg(pair.alt_leg, alt_res if isinstance(alt_res, dict) else {}, alt_before)
            raise RiskRejection(
                f"Atomic pair anchor submit failed pair_id={pair.pair_id} anchor_res={anchor_res!r}"
            )
        anchor_resting = isinstance(anchor_res, dict) and str(anchor_res.get("status", "")).lower() == "resting"
        if anchor_resting:
            anchor_res = await self._chase_resting_leg(pair.anchor_leg, anchor_before, pair.maker_grace_ms, retries=2)
        anchor_ok = self._filled_amount_from_result(pair.anchor_leg, anchor_res, anchor_before) > 0
        if not anchor_ok:
            if pair.panic_close_on_partial:
                self._log_fatal_pair_panic(pair.pair_id, alt_res, anchor_res)
                await self._panic_close_leg(pair.alt_leg, alt_res if isinstance(alt_res, dict) else {}, alt_before)
            raise RiskRejection(
                f"Atomic pair anchor not filled pair_id={pair.pair_id} anchor_res={anchor_res!r}"
            )

        feed_realized_net_from_exchange_result(alt_res)
        feed_realized_net_from_exchange_result(anchor_res)

    async def start(self):
        self.running = True
        log.info("Strategy Engine Started")

        cfg = config_manager.get_config()
        need_core_limit_manager = bool(getattr(cfg.strategy.params, "core_entry_limit_enabled", False))
        if cfg.execution.use_order_manager or need_core_limit_manager:
            self.order_manager = OrderManager(self.exchange)
            await self.order_manager.start()
            log.info(
                "OrderManager enabled "
                f"(use_order_manager={bool(cfg.execution.use_order_manager)} "
                f"core_limit_manager={need_core_limit_manager})"
            )

        # Start the signal processor task
        processor_task = asyncio.create_task(self.process_signals())
        
        # Start background balance updater
        balance_task = asyncio.create_task(self._periodic_balance_update())
        
        await asyncio.gather(processor_task, balance_task)

    async def start_replay_workers(self) -> None:
        """
        事件回放：启动与实盘相同的 OrderManager（若启用）+ 信号处理 + 余额同步，
        但不阻塞调用方（由 event_replay 驱动 tick 泵送）。
        """
        self.running = True
        log.info("Strategy Engine replay workers starting…")
        cfg = config_manager.get_config()
        need_core_limit_manager = bool(getattr(cfg.strategy.params, "core_entry_limit_enabled", False))
        if cfg.execution.use_order_manager or need_core_limit_manager:
            self.order_manager = OrderManager(self.exchange)
            await self.order_manager.start()
            log.info(
                "OrderManager enabled for replay "
                f"(use_order_manager={bool(cfg.execution.use_order_manager)} "
                f"core_limit_manager={need_core_limit_manager})"
            )
        self._replay_processor_task = asyncio.create_task(self.process_signals())
        self._replay_balance_task = asyncio.create_task(self._periodic_balance_update())

    async def stop_replay_workers(self) -> None:
        self.running = False
        for attr in ("_replay_processor_task", "_replay_balance_task"):
            t = getattr(self, attr, None)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        await self.stop()

    async def _periodic_balance_update(self):
        """Fetch balance periodically since WS doesn't push it in standard ticker."""
        while self.running:
            try:
                if hasattr(self.exchange, 'fetch_balance'):
                    balance_data = await self.exchange.fetch_balance()
                    wallet_balance = 0.0
                    unrealized_pnl = 0.0
                    accumulated_fee = 0.0
                    accumulated_funding_fee = 0.0
                    if isinstance(balance_data, dict):
                        wallet_balance = float(
                            (balance_data.get('wallet_balance') or {}).get('USDT')
                            or (balance_data.get('free') or {}).get('USDT')
                            or (balance_data.get('total') or {}).get('USDT')
                            or 0.0
                        )
                        unrealized_pnl = float((balance_data.get('unrealized_pnl') or {}).get('USDT') or 0.0)
                        accumulated_fee = float((balance_data.get('accumulated_fee_paid') or {}).get('USDT') or 0.0)
                        accumulated_funding_fee = float((balance_data.get('accumulated_funding_fee') or {}).get('USDT') or 0.0)
                    if hasattr(self.exchange, 'fetch_positions'):
                        positions = await self.exchange.fetch_positions()
                        unrealized_sum = 0.0
                        exch_pos_sizes = {}
                        for p in positions or []:
                            try:
                                sym = str(p.get('symbol') or '')
                                sz = abs(float(p.get('size', 0.0) or 0.0))
                                upnl = float(p.get('unrealizedPnl', 0.0) or 0.0)
                                if sym and sz > 1e-12:
                                    exch_pos_sizes[sym] = sz
                                unrealized_sum += upnl
                            except Exception:
                                continue
                        if abs(unrealized_sum) > 1e-12:
                            unrealized_pnl = unrealized_sum
                        local_pos_sizes = {
                            sym: abs(float((pos or {}).get('size', 0.0) or 0.0))
                            for sym, pos in paper_engine.positions.items()
                            if abs(float((pos or {}).get('size', 0.0) or 0.0)) > 1e-12
                        }
                        reconciliation_gaps = []
                        for sym, exch_sz in exch_pos_sizes.items():
                            local_sz = float(local_pos_sizes.get(sym, 0.0) or 0.0)
                            if abs(exch_sz - local_sz) > 1e-9:
                                reconciliation_gaps.append(f"{sym}: exchange={exch_sz:.8f}, local={local_sz:.8f}")
                        if reconciliation_gaps:
                            log.critical("ORPHAN POSITION ALERT: " + " | ".join(reconciliation_gaps))
                    risk_engine.note_fees(
                        fee_usdt=accumulated_fee - float(risk_engine.accumulated_fee or 0.0),
                        funding_fee_usdt=accumulated_funding_fee - float(risk_engine.accumulated_funding_fee or 0.0),
                    )
                    risk_engine.update_account_snapshot(wallet_balance=wallet_balance, unrealized_pnl=unrealized_pnl)
            except Exception:
                pass
            await asyncio.sleep(10)

    async def stop(self):
        self.running = False
        log.info("Strategy Engine Stopping...")
        if self.order_manager:
            await self.order_manager.stop()
            self.order_manager = None

    async def process_ws_tick(self, symbol: str, ticker: dict):
        """
        Called by Gateway when a new WS tick arrives.
        """
        if self.paused or not self.running:
            return

        global_config = config_manager.get_config()
        
        # We rely on periodic_balance_update for risk_engine balance
        total_equity = risk_engine.current_balance
        
        # Update Legacy State Machine
        self.state_machine.update(total_equity, {symbol: ticker})
        current_state = self.state_machine.state
        
        # Execute based on Global State
        if current_state == SystemState.LICENSE_LOCKED:
            await self.exchange.close_all_positions()
            return

        if current_state == SystemState.COOL_DOWN:
            return

        # Mode switcher from AI regime — disabled while dashboard has locked NEUTRAL/ATTACK
        # 仅用锚定合约评估，避免每个 tick 上不同标的 regime 冲突导致全局状态抖动 + 反复 kill_switch 全平
        if self.state_machine.manual_trading_mode is None:
            anchor = (global_config.strategy.regime_switch_anchor_symbol or "BTC/USDT").strip()
            if symbol == anchor:
                regime = regime_classifier.analyze(symbol)
                if regime == MarketRegime.STABLE and current_state != SystemState.NEUTRAL:
                    log.info(f"Mode Switcher: Regime is {regime.name}, switching to NEUTRAL mode")
                    self.state_machine.state = SystemState.NEUTRAL
                    current_state = SystemState.NEUTRAL
                    if global_config.strategy.regime_switch_close_positions:
                        log.info("Mode Switcher: Closing all positions (regime_switch_close_positions=true).")
                        await self.exchange.close_all_positions()

                elif regime == MarketRegime.VOLATILE and current_state != SystemState.ATTACK:
                    log.info(f"Mode Switcher: Regime is {regime.name}, switching to ATTACK mode")
                    self.state_machine.state = SystemState.ATTACK
                    current_state = SystemState.ATTACK
                    if global_config.strategy.regime_switch_close_positions:
                        log.info("Mode Switcher: Closing all positions before attack (regime_switch_close_positions=true).")
                        await self.exchange.close_all_positions()
        
        # Dispatch TickEvent to Strategies
        tick_event = TickEvent(symbol=symbol, ticker=ticker)
        active_names = global_config.strategy.active_strategies
        
        for strategy in self.strategies:
            should_run = True
            
            # Check config enablement
            if strategy.name == "CoreNeutral" and "core_neutral" not in active_names:
                should_run = False
            if strategy.name == "CoreAttack" and "core_attack" not in active_names:
                should_run = False

            isc = global_config.institutional_schemes
            if strategy.name == "MicroMaker" and (
                "micro_maker" not in active_names
                or not isc.micro_maker.enabled
                or current_state != SystemState.NEUTRAL
            ):
                should_run = False
            if strategy.name == "LiquidationSnipe" and (
                "liquidation_snipe" not in active_names
                or not isc.liquidation_snipe.enabled
                or current_state not in (SystemState.NEUTRAL, SystemState.ATTACK)
            ):
                should_run = False
            if strategy.name == "FundingSqueeze" and (
                "funding_squeeze" not in active_names
                or not isc.funding_squeeze.enabled
                or current_state not in (SystemState.NEUTRAL, SystemState.ATTACK)
            ):
                should_run = False

            if strategy.name == "L1Sniper":
                should_run = False
            if strategy.name == "MicroAssassin":
                should_run = False
            if strategy.name == "BetaNeutralHF":
                if (
                    "beta_neutral_hf" not in active_names
                    and not bool(global_config.beta_neutral_hf.enabled)
                ):
                    should_run = False

            scfg = global_config.slingshot
            if strategy.name == "Slingshot":
                if "slingshot" not in active_names or not scfg.enabled:
                    should_run = False
                elif current_state == SystemState.LICENSE_LOCKED:
                    should_run = False
                elif current_state == SystemState.COOL_DOWN:
                    should_run = False
                elif scfg.require_attack_mode and current_state not in (
                    SystemState.ATTACK,
                    SystemState.BERSERKER,
                ):
                    should_run = False

            # Respect System State (Mode)
            if strategy.name == "CoreNeutral" and current_state != SystemState.NEUTRAL:
                should_run = False
            if strategy.name == "CoreAttack" and current_state not in (
                SystemState.ATTACK,
                SystemState.BERSERKER,
            ):
                should_run = False

            if should_run:
                # Process tick
                await strategy.on_tick(tick_event)
                
                # Collect emitted signals and put them into the execution queue
                while strategy.events_queue:
                    signal = strategy.events_queue.pop(0)
                    await self.signal_queue.put(signal)

    async def process_ws_orderbook(self, symbol: str, ob: dict):
        """
        Called by Gateway when a new L2 Orderbook arrives.
        Strategies can use this for micro-structure analysis if needed.
        """
        if self.paused or not self.running:
            return
            
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        
        # Calculate OBI (Order Book Imbalance) from top N levels
        # Level data is [price, size]
        top_n = 5
        bid_vol = sum([size for price, size in bids[:top_n]])
        ask_vol = sum([size for price, size in asks[:top_n]])
        
        obi = 0.0
        if (bid_vol + ask_vol) > 0:
            obi = (bid_vol - ask_vol) / (bid_vol + ask_vol)
            
        ob_event = OrderBookEvent(symbol=symbol, bids=bids, asks=asks, obi=obi)
        
        global_config = config_manager.get_config()
        active_names = global_config.strategy.active_strategies
        current_state = self.state_machine.state
        
        for strategy in self.strategies:
            should_run = True
            if strategy.name == "CoreNeutral" and ("core_neutral" not in active_names or current_state != SystemState.NEUTRAL):
                should_run = False
            if strategy.name == "CoreAttack" and (
                "core_attack" not in active_names
                or current_state not in (SystemState.ATTACK, SystemState.BERSERKER)
            ):
                should_run = False
            if strategy.name == "BetaNeutralHF":
                if (
                    "beta_neutral_hf" not in active_names
                    and not bool(global_config.beta_neutral_hf.enabled)
                ):
                    should_run = False

            isc = global_config.institutional_schemes
            if strategy.name == "MicroMaker" and (
                "micro_maker" not in active_names
                or not isc.micro_maker.enabled
                or current_state != SystemState.NEUTRAL
            ):
                should_run = False
            if strategy.name == "LiquidationSnipe" and (
                "liquidation_snipe" not in active_names
                or not isc.liquidation_snipe.enabled
                or current_state not in (SystemState.NEUTRAL, SystemState.ATTACK)
            ):
                should_run = False
            if strategy.name == "FundingSqueeze" and (
                "funding_squeeze" not in active_names
                or not isc.funding_squeeze.enabled
                or current_state not in (SystemState.NEUTRAL, SystemState.ATTACK)
            ):
                should_run = False

            lc = global_config.l1_fast_loop
            if strategy.name == "L1Sniper":
                if "l1_sniper" not in active_names or not lc.enabled:
                    should_run = False
                elif current_state == SystemState.COOL_DOWN:
                    should_run = False
                elif lc.require_attack_mode and current_state not in (
                    SystemState.ATTACK,
                    SystemState.BERSERKER,
                ):
                    should_run = False

            sc_shark = global_config.shark_scalp
            if strategy.name == "SharkScalp":
                if "shark_scalp" not in active_names or not sc_shark.enabled:
                    should_run = False
                elif current_state == SystemState.COOL_DOWN:
                    should_run = False
                elif sc_shark.require_attack_mode and current_state not in (
                    SystemState.ATTACK,
                    SystemState.BERSERKER,
                ):
                    should_run = False

            if should_run and hasattr(strategy, 'on_orderbook'):
                await strategy.on_orderbook(ob_event)
                
                # Collect emitted signals and put them into the execution queue
                while strategy.events_queue:
                    signal = strategy.events_queue.pop(0)
                    await self.signal_queue.put(signal)

    async def process_ws_trade(self, symbol: str, trades: list):
        """futures.trades：驱动 L1Sniper、MicroAssassin、SharkScalp（CVD / 剥头皮扳机），无 HTTP。"""
        if self.paused or not self.running:
            return

        rows = trades if isinstance(trades, list) else []

        global_config = config_manager.get_config()
        active_names = global_config.strategy.active_strategies
        l1_on = global_config.l1_fast_loop.enabled and "l1_sniper" in active_names
        as_on = global_config.assassin_micro.enabled and "micro_assassin" in active_names
        shark_on = global_config.shark_scalp.enabled and "shark_scalp" in active_names
        if not l1_on and not as_on and not shark_on:
            return

        current_state = self.state_machine.state
        if current_state == SystemState.LICENSE_LOCKED:
            return
        if current_state == SystemState.COOL_DOWN:
            return

        for strategy in self.strategies:
            if strategy.name == "L1Sniper":
                if not l1_on:
                    continue
            elif strategy.name == "MicroAssassin":
                if not as_on:
                    continue
            elif strategy.name == "SharkScalp":
                if not shark_on:
                    continue
            else:
                continue
            if strategy.name == "SharkScalp":
                if hasattr(strategy, "ingest_trades_and_maybe_fire"):
                    strategy.ingest_trades_and_maybe_fire(symbol, rows, self.state_machine)
            elif hasattr(strategy, "on_ws_trades"):
                await strategy.on_ws_trades(symbol, self.state_machine)
            while strategy.events_queue:
                signal = strategy.events_queue.pop(0)
                await self.signal_queue.put(signal)

    async def _enrich_playbook_limits_and_plan(self, signal: SignalEvent) -> None:
        """侦察：拉取币对交易所约束 + PlaybookManager 执行计划（异步，带缓存）。"""
        if bool(getattr(signal, "reduce_only", False)):
            return
        pb = config_manager.get_config().playbook
        if not pb.enabled:
            return
        ect = dict(signal.entry_context or {})
        if bool(ect.get("beta_bypass_playbook")):
            return
        q_raw = ect.get("playbook_quadrant")
        if not q_raw:
            return
        vol = ect.get("playbook_vol_pct")
        if vol is None:
            return
        equity = float(risk_engine.current_balance or 0.0)
        ref_px = float(getattr(signal, "price", None) or 0.0) or float(ect.get("ref_price") or 0.0)
        limits: Optional[dict] = None
        try:
            ex = self.exchange
            if ex is not None and hasattr(ex, "get_symbol_limits"):
                limits = await ex.get_symbol_limits(signal.symbol, ref_price=ref_px or None)
        except Exception as e:
            log.warning(f"[Playbook] get_symbol_limits failed: {e}")
        if not limits:
            limits = build_default_symbol_limits(signal.symbol, ref_px)
        try:
            quadrant = PlaybookQuadrant(str(q_raw))
        except ValueError:
            return
        plan = PlaybookManager.get_execution_plan(
            total_equity=equity,
            current_atr_pct=float(vol),
            symbol_limits=limits,
            quadrant=quadrant,
        )
        ect["symbol_limits"] = limits
        ect["playbook_execution_plan"] = plan
        signal.entry_context = ect

    async def _apply_market_oracle_gate(self, signal: SignalEvent) -> Optional[str]:
        """
        拉取战术雷达快照写入 entry_context['market_oracle']；
        若返回 str 则为防绞杀一票否决原因（调用方应丢弃信号）。
        """
        oc = config_manager.get_config().market_oracle
        if not oc.enabled:
            return None
        if getattr(signal, "reduce_only", False):
            return None
        ect = dict(signal.entry_context or {})
        if bool(ect.get("beta_neutral_hf")):
            return None
        self._market_oracle.set_cache_ttl_sec(float(oc.cache_ttl_sec))
        snap = await self._market_oracle.build_context_snapshot(
            signal.symbol,
            depth_pct=float(oc.orderbook_depth_pct),
            anchor_symbol=str(oc.crash_anchor_symbol),
            anchor_lookback_min=int(oc.crash_lookback_minutes),
        )
        ect["market_oracle"] = snap
        signal.entry_context = ect
        log.info(
            f"[MarketOracle] snapshot sym={signal.symbol} side={signal.side} "
            f"ls_ratio={snap.get('ls_ratio')} funding={snap.get('funding_rate')} "
            f"obi={snap.get('orderbook_imbalance')} "
            f"anchor_ret={snap.get('anchor_return_pct')} ({snap.get('anchor_symbol')})"
        )
        return _market_oracle_veto_reason(signal, snap, oc)

    async def process_signals(self):
        """
        Consume SignalEvents from the queue and execute them through RiskEngine.
        """
        while self.running:
            try:
                first_signal = await self.signal_queue.get()
                batch = [first_signal]
                while True:
                    try:
                        batch.append(self.signal_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if self.paused:
                    for _ in batch:
                        self.signal_queue.task_done()
                    continue

                for sig in batch:
                    attach_playbook_to_signal(sig)
                    apply_ai_confidence_discount_to_signal(sig)
                    apply_scene_learning_to_signal(sig)

                batch.sort(
                    key=lambda sig: (
                        float(((sig.entry_context or {}).get("scene_learning") or {}).get("priority_score", 0.0) or 0.0),
                        float(((sig.entry_context or {}).get("scene_adjusted_ai_score") or (sig.entry_context or {}).get("ai_score") or 0.0) or 0.0),
                    ),
                    reverse=True,
                )

                for signal in batch:
                    try:
                        pair_payload = dict((signal.entry_context or {}).get("pair_order_intent") or {})
                        if pair_payload:
                            await self._execute_pair_order_intent(PairOrderIntent.from_dict(pair_payload))
                            continue
                        if len(batch) > 1:
                            sc = dict((signal.entry_context or {}).get("scene_learning") or {})
                            log.info(
                                f"[ScenePriority] {signal.symbol} {signal.side} "
                                f"score={float(sc.get('priority_score', 0.0) or 0.0):.2f} "
                                f"match={sc.get('match_level', 'none')} key={sc.get('scene_key', '')}"
                            )

                        await self._enrich_playbook_limits_and_plan(signal)
                        apply_scene_learning_to_signal(signal)
                        veto = await self._apply_market_oracle_gate(signal)
                        if veto:
                            log.warning(
                                f"[MarketOracle] VETO discard signal — {signal.symbol} {signal.side} "
                                f"strategy={getattr(signal, 'strategy_name', '')} reason={veto}"
                            )
                            continue
                        ok, routed_intent = _build_order_intent_with_sniper_pipeline(signal)
                        if not ok:
                            continue
                        assert routed_intent is not None
                        intent = routed_intent
                        intent.margin_mode = "isolated"

                        ect = dict(intent.entry_context or {})

                        order = {
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "type": intent.order_type,
                            "amount": intent.amount,
                            "price": intent.price,
                            "leverage": intent.leverage,
                            "reduce_only": intent.reduce_only,
                            "berserker": bool(intent.berserker),
                            "entry_context": ect,
                        }

                        try:
                            if (
                                getattr(signal, "strategy_name", "") == "MicroAssassin"
                                and not order.get("reduce_only", False)
                            ):
                                from src.core.assassin_gate import assassin_entry_blocked

                                if assassin_entry_blocked(signal.symbol):
                                    log.warning(
                                        f"[Engine] MicroAssassin skipped (entry mutex): {signal.symbol}"
                                    )
                                    continue

                            # 1. Risk Check
                            if risk_engine.check_order(order):
                                # RiskEngine may compress exposure in-place (amount/notional/margin)
                                intent.amount = float(order.get("amount", intent.amount))
                                intent.leverage = int(float(order.get("leverage", intent.leverage)))
                                intent.notional_size = float(order.get("notional_size", intent.notional_size or 0.0))
                                intent.margin_amount = float(order.get("margin_amount", intent.margin_amount or 0.0))
                                if isinstance(order.get("entry_context"), dict):
                                    intent.entry_context = dict(order["entry_context"])

                                # 2. Execution
                                log.info(
                                    f"Executing Signal: {intent.side} {intent.amount} {intent.symbol} "
                                    f"@ {intent.price} (type={intent.order_type})"
                                )

                                ex_cfg = config_manager.get_config().execution
                                if str(intent.order_type).lower() == "limit" and not ect.get("resting_quote"):
                                    intent.entry_context = {**ect, "paper_shadow_limit": True}
                                    ect = dict(intent.entry_context or {})

                                use_manager = bool(ex_cfg.use_order_manager)
                                if (
                                    not use_manager
                                    and self.order_manager
                                    and bool((intent.entry_context or {}).get("core_limit_requote_enabled"))
                                    and str(intent.order_type).lower() == "limit"
                                    and not bool(intent.reduce_only)
                                ):
                                    use_manager = True

                                if use_manager and self.order_manager:
                                    await self.order_manager.submit_intent(intent)
                                else:
                                    ex_res = await self.exchange.create_order(
                                        symbol=intent.symbol,
                                        side=intent.side,
                                        amount=intent.amount,
                                        price=intent.price if intent.order_type == "limit" else None,
                                        reduce_only=intent.reduce_only,
                                        leverage=int(intent.leverage),
                                        margin_mode=str(intent.margin_mode),
                                        berserker=bool(intent.berserker),
                                        post_only=bool(intent.post_only),
                                        entry_context=intent.entry_context if intent.entry_context else None,
                                        exit_reason=(intent.entry_context or {}).get("exit_reason"),
                                        order_text=(intent.entry_context or {}).get("client_oid"),
                                    )
                                    if isinstance(ex_res, dict) and str(ex_res.get("status", "")).lower() == "rejected":
                                        log.warning(f"Order Rejected by Exchange/Paper: {ex_res.get('reason', 'unknown')}")
                                    feed_realized_net_from_exchange_result(ex_res)
                        except RiskRejection as e:
                            log.warning(f"Order Rejected by Risk Engine: {e}")
                        except Exception as e:
                            log.error(f"Error executing order: {e}")
                    finally:
                        self.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in signal processor: {e}")
