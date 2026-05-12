"""
Signal Engine — 方向信号决策层（v2 纯AI版）
从 tick() 抽取，只负责 AI 委员会信号判定。
已移除所有兜底逻辑（费率/RSI/多交易所/ADX/量价投票）。
无AI信号 = 跳过开仓。FastLoop 门禁由 PlanGate 负责。
"""

from typing import Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class SignalResult:
    side: str = ""             # "long" / "short" / "" (skip)
    signal_src: str = ""       # "AI多维 信70"
    ai_confidence: int = 0
    ai_use: bool = False       # 是否来自AI委员会
    learner_feat: list = field(default_factory=list)
    stop_mult: float = 2.0
    tp_mult: float = 3.0


class SignalEngine:
    """方向信号决策引擎：纯AI委员会"""

    def decide(self, runner, sym: str, px: float, funding: float,
               change: float, vol: float, cfg: dict, now: float,
               regime_cache: dict, _regime) -> SignalResult:
        """
        判定开仓方向，返回 SignalResult。
        无AI信号时返回 side="" — 上游 PlanGate 决定是否放行。
        """
        r = SignalResult()

        # ── AI 信号缓存 ──
        ai_cache = runner._ai_signal_cache.get(sym)
        ai_dir_raw = ""
        ai_confidence = 0
        if ai_cache and now - ai_cache.get("ts", 0) < 180:
            ai_plan = ai_cache.get("plan", {})
            ai_dir_raw = (ai_plan.get("direction") or "").strip().upper()
            ai_confidence = float(ai_plan.get("confidence", 0) or 0)

        _ai_conf_min = 45 + (runner._reflector.ai_boost if runner._reflector else 0)
        _learner_feat = []

        # 在线学习器信任调整
        if runner._learner and _regime:
            try:
                diag = regime_cache.get(sym, {}).get("diag", {})
                feat = runner._learner.extractor.extract(
                    sym, px, diag, ai_cache, funding, change, vol,
                    _is_stable_sym(sym),
                    {"position_count": len(runner.positions),
                     "exposure": sum(p["margin"] for p in runner.positions.values()) / max(runner.balance, 1),
                     "win_rate": runner.wins / max(runner.closed_trades, 1),
                     "consecutive_losses": sum(1 for t in reversed(runner._trade_history[-10:])
                                               if t["realized_pnl"] <= 0) if runner._trade_history else 0}
                )
                trust = runner._learner.get_trust(feat)
                _ai_conf_min -= int(trust * 10)
                _learner_feat = feat
            except Exception:
                pass

        ai_use = ai_dir_raw in ("LONG", "SHORT") and ai_confidence >= _ai_conf_min
        r.ai_confidence = ai_confidence
        r.ai_use = ai_use
        r.learner_feat = _learner_feat

        # ── 方向判定：纯AI，无兜底 ──
        if ai_use:
            r.side = "long" if ai_dir_raw == "LONG" else "short"
            r.signal_src = f"AI多维 信{int(ai_confidence)}"

        # 无AI信号 = side="" → 跳过
        return r


def _is_stable_sym(sym: str) -> bool:
    return sym in ("BTC/USDT", "ETH/USDT")
