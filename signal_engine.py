"""
Signal Engine — 方向信号决策层
从 tick() 抽取，负责 AI 委员会 + 多方兜底 + 方向确认
返回统一 SignalResult，策略层只消费结果
"""

from typing import Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class SignalResult:
    side: str = ""             # "long" / "short" / "" (skip)
    signal_src: str = ""       # "AI多维 信70" / "多方兜底 ..."
    ai_confidence: int = 0
    ai_use: bool = False       # 是否来自AI委员会
    learner_feat: list = field(default_factory=list)
    stop_mult: float = 2.0
    tp_mult: float = 3.0


class SignalEngine:
    """方向信号决策引擎：AI优先 → 多方兜底 → 多交易所确认"""

    def decide(self, runner, sym: str, px: float, funding: float,
               change: float, vol: float, cfg: dict, now: float,
               regime_cache: dict, _regime) -> SignalResult:
        """
        判定开仓方向，返回 SignalResult。
        无可用信号时返回 side=""。
        """
        from main import KLINE_ENABLED, MULTI_ENABLED
        from kline_cache import get_kline_cache
        from multi_exchange import get_multi_feed

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

        # ── 方向判定 ──
        side = ""
        signal_src = ""

        if ai_use:
            side = "long" if ai_dir_raw == "LONG" else "short"
            signal_src = f"AI多维 信{int(ai_confidence)}"
        else:
            # 多方信号兜底
            fb_votes = {"long": 0, "short": 0}
            fb_tags = []

            if abs(funding) > 0.0005:
                d = "short" if funding > 0 else "long"
                fb_votes[d] += 1; fb_tags.append(f"费率{funding*100:+.3f}%")

            try:
                kc = get_kline_cache() if KLINE_ENABLED else None
                if kc:
                    rsi_v = kc.rsi(sym, period=14, interval="5m")
                    if 0 < rsi_v < 35:
                        fb_votes["long"] += 1; fb_tags.append(f"RSI={rsi_v:.0f}超卖")
                    elif rsi_v > 65:
                        fb_votes["short"] += 1; fb_tags.append(f"RSI={rsi_v:.0f}超买")
            except Exception: pass

            if MULTI_ENABLED:
                try:
                    feed_m = get_multi_feed()
                    if feed_m:
                        ms = feed_m.direction_signal(sym)
                        if ms['divergence'] <= 0.5 and ms['bias'] != 'neutral':
                            fb_votes[ms['bias']] += 1; fb_tags.append(f"多所→{ms['bias']}")
                except Exception: pass

            try:
                kc = get_kline_cache() if KLINE_ENABLED else None
                if kc:
                    a = kc.adx(sym, period=14, interval="1m")
                    t = kc.ma_trend(sym, fast=9, slow=21, interval="1m")
                    if a > 20 and t in ("up", "down"):
                        d2 = "long" if t == "up" else "short"
                        fb_votes[d2] += 1; fb_tags.append(f"ADX={a:.0f}趋势{t}")
            except Exception: pass

            mv = cfg.get("min_volume", 500000)
            mc = cfg.get("min_change", 1.5)
            if vol > mv * 2 and abs(change) > mc * 2:
                d3 = "long" if change > 0 else "short"
                fb_votes[d3] += 1; fb_tags.append(f"量价{change:+.1f}%")

            best_dir = max(fb_votes, key=fb_votes.get)
            best_cnt = fb_votes[best_dir]
            non_fee = sum(1 for t in fb_tags if not t.startswith("费率"))

            if _is_stable_sym(sym):
                ok = non_fee >= 1 and best_dir in ("long", "short")
            else:
                ok = best_cnt >= 2 and non_fee >= 1 and best_dir in ("long", "short")

            if ok:
                side = best_dir
                signal_src = f"多方兜底 {'|'.join(fb_tags)} ✓{best_cnt}"

        if not side:
            return r

        # ── 多交易所方向确认 ──
        if MULTI_ENABLED:
            try:
                feed_m = get_multi_feed()
                if feed_m:
                    sig = feed_m.direction_signal(sym)
                    if sig['divergence'] > 0.5:
                        return r  # skip
                    if sig['bias'] != 'neutral' and sig['bias'] != side:
                        if ai_use:
                            if ai_confidence < 70:
                                return r
                        elif sig.get('confidence', 0) > 40:
                            return r
            except Exception:
                pass

        # ── 行情方向约束 ──
        _rc = regime_cache.get(sym, {}).get("cfg", {})
        _allowed = _rc.get("allowed_dir")
        if _allowed and _allowed != "both" and side != _allowed:
            return r  # 趋势不符

        r.side = side
        r.signal_src = signal_src
        r.stop_mult = _rc.get("stop_atr_mult", 2.0)
        r.tp_mult = _rc.get("tp_atr_mult", 3.0)
        return r


def _is_stable_sym(sym: str) -> bool:
    return sym in ("BTC/USDT", "ETH/USDT")
