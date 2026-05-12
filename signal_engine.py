"""
Signal Engine — 方向来自 RangePlan（默认）或 AI 委员会缓存。
无兜底量价投票。PlanGate 负责入场带/熔断。
"""

import os
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
    """方向：SHARK_SIGNAL_SOURCE=plan 读 Redis；=ai 读 _ai_signal_cache。"""

    def decide(self, runner, sym: str, px: float, funding: float,
               change: float, vol: float, cfg: dict, now: float,
               regime_cache: dict, _regime) -> SignalResult:
        """
        判定开仓方向，返回 SignalResult。
        SHARK_SIGNAL_SOURCE=plan：只认 Redis RangePlan.bias，不调 LLM。
        SHARK_SIGNAL_SOURCE=ai：依赖 _ai_signal_cache（DeepSeek 等预取）。
        无有效信号时 side=""。PlanGate 再做过期/入场带/方向二次校验。
        """
        r = SignalResult()
        src = os.environ.get("SHARK_SIGNAL_SOURCE", "plan").strip().lower()

        if src == "plan" and getattr(runner, "_plan_gate", None):
            pl = runner._plan_gate.get_plan(sym)
            if pl:
                b = (pl.get("bias") or "").lower()
                expired = pl.get("valid_until", 0) < now
                paused = pl.get("state") == "PAUSED" or pl.get("news_risk_level", 0) >= 2
                if not expired and not paused and b in ("long", "short"):
                    r.side = b
                    r.signal_src = "RangePlan"
                    r.ai_confidence = 50
                    r.ai_use = False
                    return r
            return r

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
