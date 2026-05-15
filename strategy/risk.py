"""风控引擎：杠杆、保证金、入场风险调整。"""

from __future__ import annotations

from typing import Tuple


class RiskMixin:
    """Mixin: 需要宿主类提供 balance, positions, _contract_specs, _initial_capital, _loss_replay_guard 等。"""

    def _clamp_leverage_for_config(self, sym: str, lev: int, cfg: dict) -> int:
        try:
            out = int(lev)
        except Exception:
            out = 0
        try:
            max_lev = int((cfg or {}).get("max_leverage") or 0)
        except Exception:
            max_lev = 0
        try:
            min_lev = int((cfg or {}).get("min_leverage") or 0)
        except Exception:
            min_lev = 0
        if max_lev > 0:
            out = min(out, max_lev)
        spec = self._contract_specs.get(sym) if hasattr(self, "_contract_specs") else None
        try:
            exchange_max = int(getattr(spec, "leverage_max", 0) or 0)
        except Exception:
            exchange_max = 0
        if exchange_max > 0:
            out = min(out, exchange_max)
        if min_lev > 0:
            out = max(out, min(min_lev, exchange_max) if exchange_max > 0 else min_lev)
        return max(1, out)

    def _alt_dynamic_leverage(self, sym: str, cfg: dict, change_pct: float, funding: float) -> Tuple[int, str]:
        change_abs = abs(float(change_pct or 0))
        funding_abs = abs(float(funding or 0))
        if change_abs >= 12 and funding_abs >= 0.00025:
            raw, tag = 50, "强单边放大"
        elif change_abs >= 8 and funding_abs >= 0.00015:
            raw, tag = 45, "单边进攻"
        elif change_abs >= 10:
            raw, tag = 35, "高波动"
        elif change_abs >= 5:
            raw, tag = 30, "中波动"
        else:
            raw, tag = 25, "低波动"
        if change_abs >= 15 and funding_abs < 0.00015:
            raw, tag = min(raw, 30), "巨震低确认"
        lev_cfg = {"min_leverage": (cfg or {}).get("min_leverage", 20),
                    "max_leverage": (cfg or {}).get("max_leverage", 50)}
        return self._clamp_leverage_for_config(sym, raw, lev_cfg), tag

    def _margin_from_plan(self, plan: dict, cfg: dict, regime_cfg: dict, change_abs: float, *, strict_plan: bool = False) -> float:
        try:
            pct = float((plan or {}).get("position_size_pct") or 0)
        except Exception:
            pct = 0.0
        try:
            min_pct = float((cfg or {}).get("min_plan_margin_pct") or 0)
        except Exception:
            min_pct = 0.0
        if min_pct > 0 and not strict_plan:
            pct = max(pct, min_pct)
        if pct <= 0:
            return 0.0
        sizing_base = max(
            float(getattr(self, "static_equity", 0) or 0),
            float(getattr(self, "equity", 0) or 0),
            float(getattr(self, "_initial_capital", 0) or 0),
            float(self.balance or 0),
        )
        margin = sizing_base * pct
        try:
            cap_pct = float((cfg or {}).get("max_plan_margin_pct") or 0)
        except Exception:
            cap_pct = 0.0
        if cap_pct > 0 and not strict_plan:
            margin = min(margin, sizing_base * cap_pct)
        return max(0.0, margin)

    def _entry_risk_adjustment(self, sym: str, plan: dict, side: str, px: float) -> tuple:
        from strategy.dual import get_config as _get_config
        cfg = _get_config(sym)
        if (cfg or {}).get("disable_aggressive_entry"):
            lev_cap = int((cfg or {}).get("max_leverage") or 35)
            return 1.0, lev_cap, "中长线重仓"
        if not plan or plan.get("bias") != "both":
            return 1.0, 125, "标准"
        range_low, range_high = self._plan_range(plan)
        if range_low <= 0 or range_high <= 0 or px < range_low or px > range_high:
            return 0.0, 0, "区间外"
        if self._price_in_plan_entry_zone(plan, side, px):
            margin_mult, lev_cap, tag = 1.0, 125, "入场带"
        else:
            low, high = self._plan_entry_zone(plan, side)
            opp = "short" if side == "long" else "long"
            opp_low, opp_high = self._plan_entry_zone(plan, opp)
            try:
                low, high = float(low or 0), float(high or 0)
                opp_low, opp_high = float(opp_low or 0), float(opp_high or 0)
            except (TypeError, ValueError):
                low = high = opp_low = opp_high = 0.0
            if low > high:
                low, high = high, low
            if opp_low > opp_high:
                opp_low, opp_high = opp_high, opp_low
            in_opp_zone = opp_low > 0 and opp_high > 0 and opp_low <= px <= opp_high
            past_opp_zone = (side == "long" and opp_high > 0 and px > opp_high) or (side == "short" and opp_low > 0 and px < opp_low)
            if in_opp_zone or past_opp_zone:
                margin_mult, lev_cap, tag = 0.28, 45, "反向区探单"
            elif side == "long" and high > 0 and px > high:
                margin_mult, lev_cap, tag = 0.55, 80, "追多降档"
            elif side == "short" and low > 0 and px < low:
                margin_mult, lev_cap, tag = 0.55, 80, "追空降档"
            else:
                margin_mult, lev_cap, tag = 0.70, 90, "偏离入场带"
        guard = self._loss_replay_guard.get(sym)
        if guard and guard.get("signature") == self._plan_signature(plan, side):
            margin_mult = min(margin_mult, 0.35)
            lev_cap = min(lev_cap, 50)
            tag += "+连损探单"
        return margin_mult, lev_cap, tag
