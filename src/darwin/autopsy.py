import time
from typing import Any, Dict, Optional


def _ai_bucket(score: float) -> str:
    if score >= 80.0:
        return "80+"
    if score >= 70.0:
        return "70-79"
    if score >= 60.0:
        return "60-69"
    if score >= 50.0:
        return "50-59"
    return "<50"


def _scene_key(symbol: str, side: str, regime: str, strategy: str, quadrant: str, ai_bucket: str) -> str:
    return "|".join(
        [
            str(regime or "UNKNOWN"),
            str(symbol or ""),
            str(side or "unknown"),
            str(strategy or "UNKNOWN"),
            str(quadrant or "NA"),
            str(ai_bucket or "<50"),
        ]
    )


def _enrich_entry_snapshot(entry_context: Dict[str, Any], symbol: str, side: str) -> Dict[str, Any]:
    ec = dict(entry_context or {})
    strategy = str(ec.get("strategy") or ec.get("strategy_name") or "UNKNOWN").strip() or "UNKNOWN"
    regime = str(ec.get("ai_regime") or ec.get("regime") or "UNKNOWN").strip() or "UNKNOWN"
    quadrant = str(
        ec.get("playbook_quadrant")
        or (ec.get("playbook_execution_plan") or {}).get("quadrant")
        or "NA"
    ).strip() or "NA"
    try:
        ai_score = float(ec.get("scene_adjusted_ai_score", ec.get("ai_score", 50.0)) or 50.0)
    except (TypeError, ValueError):
        ai_score = 50.0
    bucket = str(ec.get("ai_score_bucket") or _ai_bucket(ai_score))
    ec["strategy"] = strategy
    ec["regime"] = regime
    ec["ai_regime"] = regime
    ec["playbook_quadrant"] = quadrant
    ec["ai_score"] = ai_score
    ec["ai_score_bucket"] = bucket
    ec["scene_key"] = str(ec.get("scene_key") or _scene_key(symbol, side, regime, strategy, quadrant, bucket))
    return ec


def build_trade_autopsy(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    closed_size: float,
    contract_size: float,
    leverage: float,
    margin_mode: str,
    realized_pnl_gross: float,
    fees_on_trade: float,
    entry_context: Dict[str, Any],
    max_favorable_unrealized: float,
    max_adverse_unrealized: float,
    opened_at: float,
    exit_reason: str,
    trading_mode_at_exit: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full lifecycle JSON for Darwin (Trade Autopsy).

    Schema darwin.trade_autopsy.v2:
    - v1 字段全部保留
    - l1_at_signal: L1 开仓信号时刻的微观快照（若存在），供 L3 批反思归因
    """
    now = time.time()
    duration = max(0.0, now - opened_at) if opened_at else 0.0
    net = realized_pnl_gross - fees_on_trade

    ec = _enrich_entry_snapshot(entry_context, symbol, side)
    l1_at_signal = ec.pop("l1_signal_micro", None)
    base_qty = float(closed_size) * float(contract_size or 0.0)
    entry_notional = base_qty * float(entry_price or 0.0)
    exit_notional = base_qty * float(exit_price or 0.0)

    doc: Dict[str, Any] = {
        "schema": "darwin.trade_autopsy.v2",
        "closed_at": now,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "closed_size": closed_size,
        "contract_size": float(contract_size or 0.0),
        "base_qty": base_qty,
        "entry_notional_usdt": entry_notional,
        "exit_notional_usdt": exit_notional,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "duration_sec": round(duration, 3),
        "entry_snapshot": ec,
        "path_stats": {
            "max_favorable_unrealized": max_favorable_unrealized,
            "max_adverse_unrealized": max_adverse_unrealized,
        },
        "pnl": {
            "realized_gross": realized_pnl_gross,
            "fees_allocated": fees_on_trade,
            "realized_net": net,
        },
        "exit": {
            "reason": exit_reason,
            "trading_mode": trading_mode_at_exit,
        },
    }
    if l1_at_signal is not None:
        doc["l1_at_signal"] = l1_at_signal
    return doc
