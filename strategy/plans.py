"""Plan 引擎：山寨计划构建、AI计划转换、入场区/方向判定。"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from strategy.dual import get_config, is_stable, is_high_vol_alt, trading_track

ALT_PLAN_TTL_SEC = 600


class PlanMixin:
    """Mixin: 需要 _alt_evo, _plan_gate, _price_replan_last, _alt_dynamic_leverage, _clamp_leverage_for_config, _safe_float, _ai_build_alt_plan 等。"""

    def _build_alt_attack_plan(self, sym: str, px: float, change_abs: float,
                               volume: float, funding: float) -> dict:
        cfg = get_config(sym)
        now = time.time()
        vol_band = max(0.025, min(0.070, abs(float(change_abs or 0)) / 100.0 * 0.9))
        entry_band = max(0.004, min(0.012, vol_band * 0.35))
        lev, lev_tag = self._alt_dynamic_leverage(sym, cfg, change_abs, funding)
        lev_cap = self._clamp_leverage_for_config(sym, int((cfg or {}).get("max_leverage") or 50), {"max_leverage": (cfg or {}).get("max_leverage", 50)})
        pos_pct = float((cfg or {}).get("min_plan_margin_pct") or (cfg or {}).get("margin_pct") or 0.02)
        loss_budget = 0.60
        lev_stop = loss_budget / max(float(lev), 1.0)
        vol_stop = vol_band * 0.20
        stop_move = max(0.008, min(vol_band * 0.45, lev_stop))
        carry_band = max(vol_band * 2.4, stop_move * 1.35)
        tp1 = max(0.015, vol_band * 1.0)
        tp2 = max(0.030, vol_band * 1.8)

        abs_funding = abs(float(funding or 0))
        is_one_sided = change_abs >= 12 and abs_funding >= 0.00020
        if is_one_sided:
            bias = "long" if funding <= 0 else "short"
            bias_tag = "纯单边拉盘" if bias == "long" else "纯单边砸盘"
        else:
            bias = "both"
            bias_tag = "双向区间"

        evo = self._alt_evo.setdefault(sym, {"gen": 0, "plans": 0, "wins": 0, "stops": 0,
                                              "atr_mult": 1.0, "stop_mult": 1.0, "tp_mult": 1.0})
        evo["plans"] += 1
        chg = abs(float(change_abs or 5.0))
        fuse_pct = max(3.0, min(12.0, 3.0 * (chg / 5.0)))
        base = {
            "symbol": sym, "generated_at": int(now), "valid_until": int(now + ALT_PLAN_TTL_SEC),
            "state": "LIVE", "regime": "hot_volatile_alt", "macro_regime": "hot_volatile_alt",
            "bias": bias, "range_low": px * (1 - carry_band), "range_high": px * (1 + carry_band),
            "plan_price": px, "position_size_pct": pos_pct, "leverage": lev,
            "leverage_cap": lev_cap, "cut_loss_pct": loss_budget, "ai_model": "alt_dynamic",
            "ai_confidence": 70, "ai_rationale": f"高波山寨({bias_tag}) {lev_tag} 成交额{volume:.0f}",
            "news_risk_level": 0, "risk_flags": [], "fuse_threshold_pct": round(fuse_pct, 2),
            "evo_gen": evo["gen"],
        }
        base["long_entry_low"] = px * (1 - entry_band)
        base["long_entry_high"] = px * (1 + entry_band * 0.4)
        base["long_stop_loss"] = px * (1 - stop_move)
        base["long_take_profit"] = [px * (1 + tp1), px * (1 + tp2)]
        base["short_entry_low"] = px * (1 - entry_band * 0.4)
        base["short_entry_high"] = px * (1 + entry_band)
        base["short_stop_loss"] = px * (1 + stop_move)
        base["short_take_profit"] = [px * (1 - tp1), px * (1 - tp2)]
        base["entry_zone_low"] = base["long_entry_low"]
        base["entry_zone_high"] = base["short_entry_high"]
        base["stop_loss"] = base["long_stop_loss"]
        base["take_profit"] = base["long_take_profit"]
        return base

    def _safe_float(self, val, default: float) -> float:
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _ai_to_alt_plan(self, sym: str, px: float, ai: dict,
                        change_abs: float, volume: float, funding: float) -> dict:
        now = time.time()
        bias = ai.get("bias", "both")
        chg = abs(float(change_abs or 5.0))
        fuse_pct = max(3.0, min(12.0, 3.0 * (chg / 5.0)))
        evo = self._alt_evo.setdefault(sym, {"gen": 0, "plans": 0, "wins": 0, "stops": 0,
                                              "atr_mult": 1.0, "stop_mult": 1.0, "tp_mult": 1.0})
        evo["plans"] += 1
        plan = {
            "symbol": sym, "generated_at": int(now), "valid_until": int(now + ALT_PLAN_TTL_SEC),
            "state": "LIVE", "regime": "hot_volatile_alt", "macro_regime": "hot_volatile_alt",
            "bias": bias, "plan_price": px, "ai_model": "deepseek",
            "ai_confidence": 75, "ai_rationale": str(ai.get("rationale", ""))[:30],
            "news_risk_level": 0, "risk_flags": [],
            "fuse_threshold_pct": round(fuse_pct, 2), "evo_gen": evo["gen"],
        }
        lev = ai.get("leverage", 25)
        plan["leverage"] = max(20, min(50, int(lev) if lev is not None else 25))
        plan["position_size_pct"] = 0.02
        plan["cut_loss_pct"] = 0.70
        plan["long_entry_low"] = self._safe_float(ai.get("long_entry_low"), px * 0.995)
        plan["long_entry_high"] = self._safe_float(ai.get("long_entry_high"), px * 1.005)
        plan["long_stop_loss"] = self._safe_float(ai.get("long_sl"), px * 0.98)
        plan["long_take_profit"] = [self._safe_float(ai.get("long_tp1"), px * 1.02), self._safe_float(ai.get("long_tp2"), px * 1.04)]
        plan["short_entry_low"] = self._safe_float(ai.get("short_entry_low"), px * 0.995)
        plan["short_entry_high"] = self._safe_float(ai.get("short_entry_high"), px * 1.005)
        plan["short_stop_loss"] = self._safe_float(ai.get("short_sl"), px * 1.02)
        plan["short_take_profit"] = [self._safe_float(ai.get("short_tp1"), px * 0.98), self._safe_float(ai.get("short_tp2"), px * 0.96)]
        plan["range_low"] = min(plan["long_entry_low"], plan["long_stop_loss"])
        plan["range_high"] = max(plan["short_entry_high"], plan["short_stop_loss"])
        plan["entry_zone_low"] = plan["long_entry_low"]
        plan["entry_zone_high"] = plan["short_entry_high"]
        plan["stop_loss"] = plan["long_stop_loss"]
        plan["take_profit"] = plan["long_take_profit"]
        return plan

    def _is_alt_dynamic_plan(self, plan: Optional[dict]) -> bool:
        return bool(plan and plan.get("ai_model") in ("alt_dynamic", "deepseek"))

    async def _ensure_alt_attack_plan(self, sym: str, px: float, change_abs: float,
                                       volume: float, funding: float, *,
                                       force: bool = False, reason: str = "") -> Optional[dict]:
        has_pos = hasattr(self, "positions") and sym in self.positions
        if is_stable(sym) or (not is_high_vol_alt(sym) and not has_pos) or px <= 0:
            return None
        if trading_track() == "stable":
            return None
        now = time.time()
        old = self._plan_gate.get_plan(sym) if self._plan_gate else None
        if old and self._is_alt_dynamic_plan(old) and not force:
            generated = float(old.get("generated_at") or 0)
            valid_until = float(old.get("valid_until") or 0)
            if now - generated < ALT_PLAN_TTL_SEC and valid_until > now:
                return old
        ai_plan = None
        if force or not old or now - float(old.get("generated_at", 0)) >= ALT_PLAN_TTL_SEC:
            ai_plan = await self._ai_build_alt_plan(sym, px, change_abs, volume, funding)
        if ai_plan and ai_plan.get("bias") in ("long", "short", "both"):
            plan = self._ai_to_alt_plan(sym, px, ai_plan, change_abs, volume, funding)
            plan["ai_model"] = "deepseek"
            plan["ai_confidence"] = 75
        else:
            plan = self._build_alt_attack_plan(sym, px, change_abs, volume, funding)
        if reason:
            plan["ai_rationale"] = f"{reason}；" + str(plan.get("ai_rationale", ""))
        try:
            import redis as _redis
            _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            _r.set(f"shark:plan:{sym}", json.dumps(plan, ensure_ascii=False), ex=ALT_PLAN_TTL_SEC + 30)
        except Exception:
            pass
        if self._plan_gate:
            self._plan_gate._plan_cache[sym] = plan
            self._plan_gate._last_fetch[sym] = now
        return plan

    async def _refresh_alt_plan_if_needed(self, sym: str, plan: dict, px: float,
                                           change_abs: float, volume: float,
                                           funding: float, now: float) -> tuple:
        if not self._is_alt_dynamic_plan(plan):
            return plan, False, ""
        generated = float(plan.get("generated_at") or 0)
        reason = ""
        if generated <= 0 or now - generated >= ALT_PLAN_TTL_SEC:
            reason = "山寨10分钟全量刷新"
            full_refresh = True
        else:
            reason = self._plan_replan_reason(plan, px)
            full_refresh = False
        if not reason:
            return plan, False, ""
        last = self._price_replan_last.get(sym, 0)
        if (not full_refresh) and now - last < 60:
            return plan, False, ""
        self._price_replan_last[sym] = now
        new_plan = await self._ensure_alt_attack_plan(
            sym, px, change_abs, volume, funding, force=True, reason=reason
        )
        return new_plan or plan, True, reason

    def _warmup_allows_open(self, *, has_kline: bool, has_detector: bool) -> bool:
        self._warmup_ticks += 1
        if self._warmup_done:
            return True
        self._warmup_done = True
        print(f"🔥 计划优先：跳过K线预热，立即允许开仓 (tick={self._warmup_ticks})")
        return True

    def _gross_pnl_usd(self, sym: str, pos: dict, px: float) -> float:
        q = self._quanto_for(sym)
        if pos["side"] == "long":
            return pos["size"] * q * (px - pos["entry"])
        return pos["size"] * q * (pos["entry"] - px)

    def _plan_entry_zone(self, plan: dict, side: str) -> tuple:
        if side == "long":
            return (plan.get("long_entry_low") or plan.get("entry_zone_low", 0),
                    plan.get("long_entry_high") or plan.get("entry_zone_high", 0))
        return (plan.get("short_entry_low") or plan.get("entry_zone_low", 0),
                plan.get("short_entry_high") or plan.get("entry_zone_high", 0))

    def _price_in_plan_entry_zone(self, plan: dict, side: str, px: float) -> bool:
        low, high = self._plan_entry_zone(plan, side)
        try:
            low, high = float(low or 0), float(high or 0)
        except (TypeError, ValueError):
            return False
        if low <= 0 or high <= 0:
            return False
        if low > high:
            low, high = high, low
        return low <= px <= high

    def _main_coin_entry_allowed(self, sym: str, plan: dict, side: str, px: float) -> bool:
        if not is_stable(sym):
            return True
        return self._price_in_plan_entry_zone(plan, side, px)

    def _plan_range(self, plan: dict) -> tuple:
        try:
            low, high = float(plan.get("range_low") or 0), float(plan.get("range_high") or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0
        if low > high:
            low, high = high, low
        return low, high

    def _plan_mid_price(self, plan: dict) -> float:
        low, high = self._plan_range(plan)
        return (low + high) / 2 if low > 0 and high > 0 else 0.0

    def _plan_reference_price(self, plan: dict) -> float:
        try:
            ref = float(plan.get("plan_price") or 0)
        except (TypeError, ValueError):
            ref = 0.0
        return ref if ref > 0 else self._plan_mid_price(plan)

    def _plan_replan_reason(self, plan: dict, px: float) -> str:
        low, high = self._plan_range(plan)
        if low > 0 and high > 0 and (px < low or px > high):
            return f"价格{px:.4f}跑出计划区间[{low:.4f},{high:.4f}]"
        ref = self._plan_reference_price(plan)
        if ref > 0:
            drift = abs(px - ref) / ref
            if drift >= 0.005:
                return f"价格偏离计划参考{drift*100:.2f}% px={px:.4f} ref={ref:.4f}"
        return ""

    def _should_replan_for_price_drift(self, sym: str, plan: dict, px: float, now: float) -> tuple:
        reason = self._plan_replan_reason(plan, px)
        if not reason:
            return False, ""
        last = self._price_replan_last.get(sym, 0)
        generated = float(plan.get("generated_at") or 0)
        if now - last < 60 or (generated > 0 and last >= generated):
            return False, ""
        self._price_replan_last[sym] = now
        return True, reason

    def _plan_price_debug(self, plan: dict, side: str, px: float) -> str:
        mid = self._plan_mid_price(plan)
        ref = self._plan_reference_price(plan)
        llo, lhi = self._plan_entry_zone(plan, "long")
        slo, shi = self._plan_entry_zone(plan, "short")
        parts = [f"点位=现价{px:.4f}"]
        if mid > 0: parts.append(f"中点{mid:.4f}")
        if ref > 0: parts.append(f"生成价{ref:.4f}")
        if llo and lhi: parts.append(f"多带[{float(llo):.4f},{float(lhi):.4f}]")
        if slo and shi: parts.append(f"空带[{float(slo):.4f},{float(shi):.4f}]")
        parts.append(f"开{side}")
        return " ".join(parts)

    def _plan_signature(self, plan: dict, side: str) -> tuple:
        low, high = self._plan_entry_zone(plan, side)
        return (plan.get("generated_at"), plan.get("macro_regime") or plan.get("regime"),
                side, low, high)

    def _side_from_plan(self, plan: dict, px: float) -> tuple:
        if not plan:
            return "", "", None, None
        bias = plan.get("bias", "")
        if bias in ("long", "short"):
            return bias, f"计划趋势 {bias}", plan.get("stop_loss"), plan.get("take_profit")
        if bias != "both":
            return "", "", None, None
        macro = str(plan.get("macro_regime") or plan.get("regime") or "").lower()
        long_ok = self._price_in_plan_entry_zone(plan, "long", px)
        short_ok = self._price_in_plan_entry_zone(plan, "short", px)
        if any(tok in macro for tok in ("up", "bull", "trend_up", "slow_grind_up", "breakout_up")):
            tag = "命中入场带" if long_ok else "激进区间开"
            return "long", f"计划顺势 {macro}→多({tag})", plan.get("long_stop_loss"), plan.get("long_take_profit")
        if any(tok in macro for tok in ("down", "bear", "trend_down", "slow_grind_down", "breakout_down", "bleed")):
            tag = "命中入场带" if short_ok else "激进区间开"
            return "short", f"计划顺势 {macro}→空({tag})", plan.get("short_stop_loss"), plan.get("short_take_profit")
        if long_ok and not short_ok:
            return "long", "计划震荡命中多头入场带", plan.get("long_stop_loss"), plan.get("long_take_profit")
        if short_ok and not long_ok:
            return "short", "计划震荡命中空头入场带", plan.get("short_stop_loss"), plan.get("short_take_profit")
        if long_ok and short_ok:
            mid = (plan.get("range_low", 0) + plan.get("range_high", 0)) / 2
            if px < mid:
                return "long", f"计划震荡重叠区 价{px:.0f}<中{mid:.0f}→多", plan.get("long_stop_loss"), plan.get("long_take_profit")
            return "short", f"计划震荡重叠区 价{px:.0f}>中{mid:.0f}→空", plan.get("short_stop_loss"), plan.get("short_take_profit")
        mid = (plan.get("range_low", 0) + plan.get("range_high", 0)) / 2
        if px < mid:
            return "long", f"计划震荡激进 价{px:.0f}<中{mid:.0f}→多", plan.get("long_stop_loss"), plan.get("long_take_profit")
        return "short", f"计划震荡激进 价{px:.0f}>中{mid:.0f}→空", plan.get("short_stop_loss"), plan.get("short_take_profit")

    def _request_symbol_replan(self, sym: str, reason: str) -> None:
        if trading_track() != "stable" and (not is_stable(sym)) and is_high_vol_alt(sym):
            try:
                import redis as _redis
                _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
                _r.delete(f"shark:plan:{sym}")
                if self._plan_gate:
                    self._plan_gate._plan_cache.pop(sym, None)
                    self._plan_gate._last_fetch.pop(sym, None)
                self._price_replan_last.pop(sym, None)
                print(f"[山寨重规划] {sym} {reason} → 清旧计划，下个tick本地重做进攻计划")
            except Exception:
                pass
            return
        payload = json.dumps({"symbol": sym, "reason": reason, "ts": time.time()}, ensure_ascii=False)
        try:
            import redis as _redis
            _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            _r.publish("shark:plan:replan", payload)
        except Exception:
            pass
