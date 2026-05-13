"""Validated Redis order command builder shared by Python trading paths."""

import json
import os
from typing import Any, Mapping, Optional


def _order_token() -> str:
    return os.environ.get("SHARK_ORDER_TOKEN", "").strip() or os.environ.get("SHARK_API_TOKEN", "").strip()


def _first_take_profit(value: Any) -> Optional[float]:
    if isinstance(value, list) and value:
        return float(value[0])
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_order_command(
    *,
    symbol: str,
    side: str,
    action: str,
    mode: str,
    size: int,
    leverage: int,
    stop_loss: Optional[float] = None,
    take_profit: Any = None,
    source: str = "strategy",
) -> str:
    symbol = str(symbol).strip()
    side = str(side).strip().lower()
    action = str(action).strip().lower()
    mode = str(mode).strip().lower()
    size = int(size)
    leverage = int(leverage)

    if "/" not in symbol:
        raise ValueError(f"invalid symbol: {symbol!r}")
    if side not in ("long", "short"):
        raise ValueError(f"invalid side: {side!r}")
    if action not in ("open", "close"):
        raise ValueError(f"invalid action: {action!r}")
    if mode not in ("paper", "live"):
        raise ValueError(f"invalid mode: {mode!r}")
    if size <= 0:
        raise ValueError("size must be positive")
    if leverage < 1 or leverage > 125:
        raise ValueError("leverage must be in [1, 125]")

    cmd = {
        "symbol": symbol,
        "side": side,
        "size": size,
        "leverage": leverage,
        "action": action,
        "mode": mode,
        "source": source,
    }
    if stop_loss:
        cmd["stop_loss"] = float(stop_loss)
    first_tp = _first_take_profit(take_profit)
    if first_tp:
        cmd["take_profit"] = first_tp
    if isinstance(take_profit, list) and take_profit:
        cmd["take_profit_levels"] = [float(tp) for tp in take_profit if tp]
    token = _order_token()
    if token:
        cmd["token"] = token
    return json.dumps(cmd, separators=(",", ":"), ensure_ascii=False)


def build_rl_order_command(action: Mapping[str, Any], *, mode: str) -> str:
    return build_order_command(
        symbol=action.get("symbol", ""),
        side=action.get("side", ""),
        action="open",
        mode=mode,
        size=int(action.get("size") or 0),
        leverage=int(action.get("leverage") or 0),
        stop_loss=action.get("stop_loss"),
        take_profit=action.get("take_profit"),
        source="rl-agent",
    )
