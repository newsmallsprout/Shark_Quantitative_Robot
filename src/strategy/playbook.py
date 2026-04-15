"""
PlaybookManager — 资金×波动 2×2 战术矩阵 + 交易所币对约束裁剪。

象限 A：小资金 + 低波动（游击/高频刮痧）
象限 B：小资金 + 高波动（防守反击）
象限 C：大资金 + 低波动（重装阵地）
象限 D：大资金 + 高波动（控场收割）

开仓前由网关 `get_symbol_limits` 提供 max_leverage、min/max_notional_usdt；执行计划内随机杠杆并向下裁剪名义。
"""
from __future__ import annotations

import random
from enum import Enum
from typing import Any, Dict, Optional, TYPE_CHECKING

from src.core.config_manager import config_manager
from src.core.events import SignalEvent
from src.core.risk_engine import risk_engine
from src.utils.logger import log

if TYPE_CHECKING:
    from src.core.config_manager import PlaybookConfig


class PlaybookQuadrant(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def volatility_pct_from_signal(signal: SignalEvent) -> Optional[float]:
    """current_atr / ref_price，无量则无法判定。"""
    ect = dict(getattr(signal, "entry_context", None) or {})
    atr = getattr(signal, "atr_value", None)
    if atr is None or float(atr) <= 0:
        atr = ect.get("atr_value")
    try:
        atr_f = float(atr or 0.0)
    except (TypeError, ValueError):
        atr_f = 0.0
    px = float(getattr(signal, "price", None) or 0.0) or float(ect.get("ref_price") or 0.0)
    if atr_f <= 0 or px <= 0:
        return None
    return atr_f / px


def build_default_symbol_limits(symbol: str, ref_price: float) -> Dict[str, Any]:
    """网关不可用时保守默认值（仍参与裁剪）。"""
    from src.core.paper_engine import paper_engine

    # Fallback should not become an accidental leverage bottleneck.
    # Exchange-side limits still clip the final leverage when available.
    ml = 200
    qm = float(paper_engine._resolve_contract_size(symbol))
    px = max(float(ref_price or 1.0), 1e-12)
    return {
        "symbol": symbol,
        "max_leverage": ml,
        "min_leverage": 1,
        "min_notional_usdt": max(5.0, 0.0),
        "max_notional_usdt": float("inf"),
        "quanto_multiplier": qm,
        "order_size_min": 0.0,
        "order_size_max": 0.0,
    }


def _rand_int_leverage(lo: int, hi: int, cap: int) -> int:
    lo = max(1, min(lo, cap))
    hi = max(lo, min(hi, cap))
    return random.randint(lo, hi)


def _volatility_sizing_adjustment(current_atr_pct: float, threshold: float) -> Dict[str, Any]:
    """
    Continuous volatility scaling for position sizing.
    High volatility shrinks margin allocation; calm conditions allow a modest increase.
    """
    atr = max(0.0, float(current_atr_pct or 0.0))
    thr = max(1e-9, float(threshold or 0.0))
    if atr <= 0.0 or thr <= 0.0:
        return {"profile": "unknown", "sizing_mult": 1.0, "atr_ratio": 0.0}

    ratio = atr / thr
    if ratio >= 2.5:
        return {"profile": "extreme", "sizing_mult": 0.50, "atr_ratio": ratio}
    if ratio >= 1.8:
        return {"profile": "high", "sizing_mult": 0.62, "atr_ratio": ratio}
    if ratio >= 1.2:
        return {"profile": "elevated", "sizing_mult": 0.78, "atr_ratio": ratio}
    if ratio <= 0.6:
        return {"profile": "calm", "sizing_mult": 1.10, "atr_ratio": ratio}
    if ratio <= 0.85:
        return {"profile": "stable", "sizing_mult": 1.04, "atr_ratio": ratio}
    return {"profile": "normal", "sizing_mult": 1.0, "atr_ratio": ratio}


class PlaybookManager:
    """战术执行计划：随机杠杆 + 保证金占比 → 名义 = margin×lev → 按币对约束裁剪。"""

    @staticmethod
    def get_execution_plan(
        *,
        total_equity: float,
        current_atr_pct: float,
        symbol_limits: Dict[str, Any],
        quadrant: PlaybookQuadrant,
        cfg: Optional["PlaybookConfig"] = None,
    ) -> Dict[str, Any]:
        """
        返回字段供引擎写入 OrderIntent：leverage, margin_usdt, notional_usdt, clipped 原因等。
        current_atr_pct 为 ATR/价（小数），用于审计日志。
        """
        pb = cfg or config_manager.get_config().playbook
        eq = max(0.0, float(total_equity))
        cap_lev = max(1, int(symbol_limits.get("max_leverage") or 200))
        min_lev = max(1, int(symbol_limits.get("min_leverage") or 1))
        min_nom = float(symbol_limits.get("min_notional_usdt") or 0.0)
        max_nom_raw = symbol_limits.get("max_notional_usdt")
        max_nom = float("inf")
        if max_nom_raw is not None:
            try:
                mx = float(max_nom_raw)
                if mx > 0 and mx < float("inf"):
                    max_nom = mx
            except (TypeError, ValueError):
                pass

        if quadrant == PlaybookQuadrant.A:
            lo, hi = 50, min(150, 200)
            margin_frac = float(pb.matrix_margin_fraction_a)
        elif quadrant == PlaybookQuadrant.B:
            lo, hi = 10, min(25, 200)
            margin_frac = float(pb.matrix_margin_fraction_b)
        elif quadrant == PlaybookQuadrant.C:
            lo, hi = 25, min(75, 200)
            margin_frac = float(pb.matrix_margin_fraction_c)
        else:
            lo, hi = 75, min(200, 200)
            margin_frac = float(pb.matrix_margin_fraction_d)

        if hi < lo:
            lo, hi = min_lev, max(min_lev, cap_lev)
        random_leverage = _rand_int_leverage(lo, hi, 200)
        final_leverage = max(min_lev, min(int(random_leverage), cap_lev))

        vol_adj = _volatility_sizing_adjustment(
            float(current_atr_pct),
            float(getattr(pb, "matrix_volatility_threshold_pct", 0.01) or 0.01),
        )
        margin_frac *= float(vol_adj["sizing_mult"])
        margin = eq * margin_frac
        notional = margin * float(final_leverage)
        clipped_max = False
        clipped_min = False

        if max_nom < float("inf") and notional > max_nom + 1e-9:
            notional = max_nom
            clipped_max = True
            # Keep leverage intact; compress margin instead.
            margin = notional / float(final_leverage) if final_leverage > 0 else 0.0

        if min_nom > 0 and notional < min_nom - 1e-9:
            notional = min_nom
            clipped_min = True
            margin = notional / float(final_leverage) if final_leverage > 0 else min_nom

        if max_nom < float("inf") and notional > max_nom + 1e-9:
            notional = max_nom
            clipped_max = True
            margin = notional / float(final_leverage) if final_leverage > 0 else margin

        plan: Dict[str, Any] = {
            "quadrant": quadrant.value,
            "leverage": int(final_leverage),
            "dynamic_leverage": int(final_leverage),
            "margin_usdt": float(margin),
            "margin_amount": float(margin),
            "notional_usdt": float(notional),
            "margin_fraction_target": float(margin_frac),
            "volatility_sizing_mult": float(vol_adj["sizing_mult"]),
            "volatility_profile": str(vol_adj["profile"]),
            "volatility_atr_ratio": float(vol_adj["atr_ratio"]),
            "max_leverage_exchange": cap_lev,
            "random_leverage_pre_clip": int(random_leverage),
            "current_atr_pct_logged": float(current_atr_pct),
            "equity_usdt": eq,
            "clipped_to_max_notional": clipped_max,
            "clipped_to_min_notional": clipped_min,
            "min_notional_usdt": min_nom,
            "max_notional_usdt": max_nom,
        }
        log.info(
            f"[PlaybookManager] Q{quadrant.value} lev={final_leverage}x rand={random_leverage} "
            f"margin≈{margin:.4f} "
            f"notional≈{notional:.4f} USDT atr_pct={current_atr_pct:.6f} "
            f"vol_profile={vol_adj['profile']} vol_mult={float(vol_adj['sizing_mult']):.2f} "
            f"clip_max={clipped_max} clip_min={clipped_min} cap_lev={cap_lev}"
        )
        return plan


def select_quadrant(
    equity_usdt: float,
    vol_pct: Optional[float],
    *,
    capital_threshold: float,
    volatility_threshold: float,
) -> Optional[PlaybookQuadrant]:
    if vol_pct is None:
        return None
    small = float(equity_usdt) < float(capital_threshold)
    low = float(vol_pct) < float(volatility_threshold)
    if small and low:
        return PlaybookQuadrant.A
    if small and not low:
        return PlaybookQuadrant.B
    if not small and low:
        return PlaybookQuadrant.C
    return PlaybookQuadrant.D


def attach_playbook_to_signal(signal: SignalEvent) -> None:
    """
    第一步：写入象限与波动/权益快照；不调用网关（网关异步在 StrategyEngine 中补 limits + execution_plan）。
    """
    if bool(getattr(signal, "reduce_only", False)):
        return

    pb = config_manager.get_config().playbook
    if not pb.enabled:
        return

    equity = float(risk_engine.current_balance or 0.0)
    vol_pct = volatility_pct_from_signal(signal)

    ect: Dict[str, Any] = dict(getattr(signal, "entry_context", None) or {})
    ect["playbook_equity_usdt"] = equity
    ect["playbook_vol_pct"] = vol_pct

    if vol_pct is None:
        ect.pop("playbook_quadrant", None)
        ect.pop("playbook_guerrilla", None)
        signal.entry_context = ect
        return

    q = select_quadrant(
        equity,
        vol_pct,
        capital_threshold=pb.matrix_capital_threshold_usdt,
        volatility_threshold=pb.matrix_volatility_threshold_pct,
    )
    if q is None:
        signal.entry_context = ect
        return

    ect["playbook_quadrant"] = q.value
    ect["playbook"] = q.value
    if q == PlaybookQuadrant.A:
        ect["playbook_guerrilla"] = True
        ect["position_ttl_minutes"] = float(pb.position_ttl_minutes)
    else:
        ect.pop("playbook_guerrilla", None)

    signal.entry_context = ect
