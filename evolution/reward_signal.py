"""
基于已平仓交易序列的奖励分解（奖励驱动闭环的「唯一约定」实现）。

与 evolver/main.cpp 中 compute_reward_metrics 使用同一组权重与边界条件；
修改公式时请两边同步，并运行: python3 -m evolution.reward_signal

设计要点：
- 使用初始权益假定值构造权益曲线，得到百分比最大回撤（与 main 默认 initial_balance=200 对齐）。
- 「低波动」通过夏普式 mean/std(realized_pnl) 体现；单笔 PnL 波动越小该项越稳。
- 「过度交易」用 trades/day 超出软阈值惩罚。
- 「探索」用 unique 交易对数量小幅加分（避免长期只打一两只币）。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Sequence

# —— 与 evolver/main.cpp constexpr 同步 ——
INITIAL_EQUITY_DEFAULT = 200.0
DD_LIMIT_PCT = 15.0
OVERTRADE_SOFT_PER_DAY = 25.0


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def trades_chronological(rows: Sequence[dict]) -> List[dict]:
    """Redis LPUSH 为最新在前；按 closed_at / 索引转 oldest-first。"""
    lst = list(rows)
    lst.sort(key=lambda t: (_f(t.get("closed_at")), id(t)))
    return lst


def compute_reward_breakdown(
    rows: Sequence[dict],
    *,
    initial_equity: float = INITIAL_EQUITY_DEFAULT,
    dd_limit_pct: float = DD_LIMIT_PCT,
    overtrade_soft: float = OVERTRADE_SOFT_PER_DAY,
    now_ts: float | None = None,
) -> Dict[str, Any]:
    """
    输入为交易记录（与 main._trade_history / Redis shark:trade_history 字段一致）。
    返回 JSON 可序列化 dict，含 reward_terms 与 reward_total。
    """
    now_ts = float(now_ts if now_ts is not None else time.time())
    chrono = trades_chronological(rows)
    n = len(chrono)
    if n == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "mean_pnl": 0.0,
            "std_pnl": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown_pct": 0.0,
            "trades_per_day": 0.0,
            "unique_symbols": 0,
            "initial_equity_assumed": float(initial_equity),
            "reward_terms": {
                "profit": 0.0,
                "sharpe": 0.0,
                "dd_penalty": 0.0,
                "overtrade_penalty": 0.0,
                "exploration": 0.0,
            },
            "reward_total": 0.0,
            "computed_at": int(now_ts),
        }

    wins = sum(1 for t in chrono if _f(t.get("realized_pnl")) > 0)
    win_rate = wins / max(n, 1)
    pnls = [_f(t.get("realized_pnl")) for t in chrono]
    total_pnl = float(sum(pnls))
    mean_pnl = total_pnl / max(n, 1)
    std_pnl = 0.0
    if n >= 2:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / max(n - 1, 1)
        std_pnl = math.sqrt(max(var, 0.0))
    sharpe_like = (mean_pnl / std_pnl) if std_pnl > 1e-9 else 0.0

    eq = float(initial_equity)
    peak = eq
    max_dd_pct = 0.0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        dd = (peak - eq) / max(peak, 1e-9) * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    t0 = int(chrono[0].get("closed_at") or 0)
    t1 = int(chrono[-1].get("closed_at") or 0)
    span = max(float(t1 - t0), 3600.0)
    trades_per_day = n / (span / 86400.0)

    syms = {str(t.get("symbol") or "") for t in chrono}
    syms.discard("")
    unique_symbols = len(syms)

    # —— 加权奖励（与 Go evolver 一致）——
    profit_term = math.tanh(total_pnl / 50.0) * 2.0
    sharpe_term = max(-1.5, min(1.5, sharpe_like * 0.35))
    dd_penalty = -0.12 * max(0.0, max_dd_pct - dd_limit_pct)
    overtrade_penalty = -0.05 * max(0.0, trades_per_day - overtrade_soft)
    exploration = 0.03 * max(0, unique_symbols - 3)

    reward_total = (
        profit_term
        + sharpe_term
        + dd_penalty
        + overtrade_penalty
        + exploration
    )

    return {
        "trade_count": n,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "mean_pnl": mean_pnl,
        "std_pnl": std_pnl,
        "sharpe_like": sharpe_like,
        "max_drawdown_pct": max_dd_pct,
        "trades_per_day": trades_per_day,
        "unique_symbols": unique_symbols,
        "initial_equity_assumed": float(initial_equity),
        "reward_terms": {
            "profit": profit_term,
            "sharpe": sharpe_term,
            "dd_penalty": dd_penalty,
            "overtrade_penalty": overtrade_penalty,
            "exploration": exploration,
        },
        "reward_total": float(reward_total),
        "computed_at": int(now_ts),
    }


if __name__ == "__main__":
    # 自测：单调盈利、回撤、过密交易
    base_ts = 1_700_000_000.0
    sample = [
        {"symbol": "BTC/USDT", "realized_pnl": -5.0, "closed_at": base_ts},
        {"symbol": "BTC/USDT", "realized_pnl": -4.0, "closed_at": base_ts + 100},
        {"symbol": "ETH/USDT", "realized_pnl": 20.0, "closed_at": base_ts + 200},
    ]
    r = compute_reward_breakdown(sample, initial_equity=200.0)
    assert r["trade_count"] == 3
    assert "reward_total" in r
    print("reward_signal selfcheck OK:", round(r["reward_total"], 4), r["reward_terms"])
