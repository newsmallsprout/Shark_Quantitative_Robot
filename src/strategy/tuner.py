"""
Strategy Auto-Tuner — 平仓已实现净盈亏 → 滑动窗口绩效 → 侦察模式（置信度打折 + 安全微仓）。

盈亏反馈必须由执行层（OrderManager / 网关 create_order 返回）注入，不得依赖 paper_engine 尸检路径。
"""
from __future__ import annotations

import copy
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

from src.core.config_manager import DarwinSymbolPatch, config_manager
from src.core.events import SignalEvent
from src.utils.logger import log


@dataclass
class _TradeClose:
    net_usdt: float
    ts: float


class PerformanceMonitor:
    def __init__(self, maxlen: int) -> None:
        self._closes: Deque[_TradeClose] = deque(maxlen=max(3, maxlen))

    def record(self, realized_net_usdt: float) -> None:
        self._closes.append(_TradeClose(float(realized_net_usdt), time.time()))

    def consecutive_losses(self) -> int:
        n = 0
        for t in reversed(self._closes):
            if t.net_usdt < 0:
                n += 1
            else:
                break
        return n

    def realized_win_rate(self) -> float:
        if not self._closes:
            return 1.0
        wins = sum(1 for t in self._closes if t.net_usdt > 0)
        return wins / len(self._closes)

    def last_n_win_rate(self, n: int) -> float:
        lst = list(self._closes)[-max(1, n) :]
        if not lst:
            return 1.0
        wins = sum(1 for t in lst if t.net_usdt > 0)
        return wins / len(lst)


class StrategyAutoTuner:
    def __init__(self) -> None:
        try:
            w = int(config_manager.get_config().auto_tuner.window_trades)
        except Exception:
            w = 10
        self._monitor = PerformanceMonitor(maxlen=w)
        self._bootstrapped = False
        self.probe_mode: bool = False
        self.adaptation_level: int = 0
        self._base_runtime: Dict[str, float] = {}
        self._base_symbol_patches: Dict[str, Dict[str, Optional[float]]] = {}
        self._scene_priority_map: Dict[str, float] = {}
        self._coarse_scene_priority_map: Dict[str, float] = {}
        self._symbol_side_priority_map: Dict[str, float] = {}
        self._last_targeted_state: Dict[str, Any] = {
            "strongest_symbol": "",
            "strongest_symbol_win_rate": 0.0,
            "strongest_symbol_trades": 0,
            "weakest_symbol": "",
            "weakest_symbol_win_rate": 1.0,
            "weakest_symbol_trades": 0,
            "dominant_win_reason": "",
            "dominant_win_reason_count": 0,
            "dominant_loss_reason": "",
            "dominant_loss_reason_count": 0,
            "strongest_strategy": "",
            "strongest_strategy_win_rate": 0.0,
            "strongest_strategy_trades": 0,
            "strongest_scene": {},
            "weakest_scene": {},
            "scene_leaderboard": [],
            "weakest_strategy": "",
            "weakest_strategy_win_rate": 1.0,
            "weakest_strategy_trades": 0,
            "symbol_boosts": {},
        }
        self._capture_runtime_baseline()

    @staticmethod
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

    @staticmethod
    def _coerce_side(v: Any) -> str:
        x = str(v or "").strip().lower()
        if x in {"buy", "long"}:
            return "buy"
        if x in {"sell", "short"}:
            return "sell"
        return x or "unknown"

    def _scene_features_from_autopsy(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        snap = raw.get("entry_snapshot") or {}
        score = float(snap.get("ai_score", 50.0) or 50.0)
        quadrant = str(
            snap.get("playbook_quadrant")
            or (snap.get("playbook_execution_plan") or {}).get("quadrant")
            or "NA"
        ).strip() or "NA"
        regime = str(snap.get("ai_regime") or snap.get("regime") or "UNKNOWN").strip() or "UNKNOWN"
        strategy = str(snap.get("strategy") or snap.get("strategy_name") or "UNKNOWN").strip() or "UNKNOWN"
        symbol = str(raw.get("symbol", "") or "").strip()
        side = self._coerce_side(raw.get("side"))
        return {
            "regime": regime,
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "quadrant": quadrant,
            "ai_score": score,
            "ai_bucket": self._ai_bucket(score),
        }

    def _scene_features_from_signal(self, signal: SignalEvent) -> Dict[str, Any]:
        ect = dict(getattr(signal, "entry_context", None) or {})
        score = float(ect.get("ai_score", 50.0) or 50.0)
        quadrant = str(ect.get("playbook_quadrant") or "NA").strip() or "NA"
        regime = str(ect.get("ai_regime") or ect.get("regime") or "UNKNOWN").strip() or "UNKNOWN"
        strategy = str(getattr(signal, "strategy_name", None) or ect.get("strategy") or "UNKNOWN").strip() or "UNKNOWN"
        symbol = str(getattr(signal, "symbol", "") or "").strip()
        side = self._coerce_side(getattr(signal, "side", ""))
        return {
            "regime": regime,
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "quadrant": quadrant,
            "ai_score": score,
            "ai_bucket": self._ai_bucket(score),
        }

    @staticmethod
    def _scene_key(feat: Dict[str, Any], *, include_bucket: bool = True, include_quadrant: bool = True) -> str:
        parts = [
            str(feat.get("regime") or "UNKNOWN"),
            str(feat.get("symbol") or ""),
            str(feat.get("side") or "unknown"),
            str(feat.get("strategy") or "UNKNOWN"),
        ]
        if include_quadrant:
            parts.append(str(feat.get("quadrant") or "NA"))
        if include_bucket:
            parts.append(str(feat.get("ai_bucket") or "<50"))
        return "|".join(parts)

    def _capture_runtime_baseline(self) -> None:
        cfg = config_manager.get_config()
        p = cfg.strategy.params
        pb = cfg.playbook
        rk = cfg.risk
        self._base_runtime = {
            "attack_ai_threshold": float(getattr(p, "attack_ai_threshold", 60) or 60),
            "neutral_ai_threshold": float(getattr(p, "neutral_ai_threshold", 40) or 40),
            "funding_signal_weight": float(getattr(p, "funding_signal_weight", 1.0) or 1.0),
            "attack_sma_align_max_adverse_bps": float(getattr(p, "attack_sma_align_max_adverse_bps", 12.0) or 12.0),
            "attack_slow_sma_trend_guard_bps": float(getattr(p, "attack_slow_sma_trend_guard_bps", 0.0) or 0.0),
            "matrix_margin_fraction_a": float(getattr(pb, "matrix_margin_fraction_a", 0.02) or 0.02),
            "matrix_margin_fraction_b": float(getattr(pb, "matrix_margin_fraction_b", 0.03) or 0.03),
            "matrix_margin_fraction_c": float(getattr(pb, "matrix_margin_fraction_c", 0.15) or 0.15),
            "matrix_margin_fraction_d": float(getattr(pb, "matrix_margin_fraction_d", 0.05) or 0.05),
            "max_margin_per_trade_usdt": float(getattr(rk, "max_margin_per_trade_usdt", 10.0) or 10.0),
        }
        syms = set(cfg.strategy.symbols) | set(cfg.darwin.symbol_patches.keys())
        self._base_symbol_patches = {}
        for sym in syms:
            patch = cfg.darwin.symbol_patches.get(sym)
            self._base_symbol_patches[sym] = {
                "max_leverage": float(patch.max_leverage) if patch and patch.max_leverage is not None else None,
                "berserker_obi_threshold": (
                    float(patch.berserker_obi_threshold)
                    if patch and patch.berserker_obi_threshold is not None
                    else None
                ),
            }

    def _cfg(self):
        return config_manager.get_config().auto_tuner

    def _seed_from_autopsies_once(self) -> None:
        if self._bootstrapped:
            return
        self._bootstrapped = True
        try:
            cfg = config_manager.get_config().darwin
            d = str(getattr(cfg, "autopsy_dir", "") or "").strip()
            if not d or not os.path.isdir(d):
                return
            files = [
                os.path.join(d, name)
                for name in os.listdir(d)
                if name.endswith(".json")
            ]
            if not files:
                return
            files.sort(key=os.path.getmtime, reverse=True)
            sample = list(reversed(files[: max(8, self._monitor._closes.maxlen)]))
            for path in sample:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    pnl = raw.get("pnl") or {}
                    net = float(pnl.get("realized_net", 0.0) or 0.0)
                    ts = float(raw.get("closed_at", time.time()) or time.time())
                    self._monitor._closes.append(_TradeClose(net, ts))
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"[AutoTuner] bootstrap from autopsies failed: {e}")

    def _load_recent_autopsies(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        cfg = config_manager.get_config().darwin
        d = str(getattr(cfg, "autopsy_dir", "") or "").strip()
        if not d or not os.path.isdir(d):
            return []
        try:
            files = [
                os.path.join(d, name)
                for name in os.listdir(d)
                if name.endswith(".json")
            ]
            if not files:
                return []
            cap = min(50, int(limit or max(20, self._monitor._closes.maxlen * 3)))
            files.sort(key=os.path.getmtime, reverse=True)
            out: List[Dict[str, Any]] = []
            for path in files[:cap]:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        out.append(raw)
                except Exception:
                    continue
            return list(reversed(out))
        except Exception as e:
            log.warning(f"[AutoTuner] recent autopsies load failed: {e}")
            return []

    def _refresh_targeted_state(self) -> None:
        autopsies = self._load_recent_autopsies()
        active_symbols = set(config_manager.get_config().strategy.symbols)
        state: Dict[str, Any] = {
            "strongest_symbol": "",
            "strongest_symbol_win_rate": 0.0,
            "strongest_symbol_trades": 0,
            "weakest_symbol": "",
            "weakest_symbol_win_rate": 1.0,
            "weakest_symbol_trades": 0,
            "dominant_win_reason": "",
            "dominant_win_reason_count": 0,
            "dominant_loss_reason": "",
            "dominant_loss_reason_count": 0,
            "strongest_strategy": "",
            "strongest_strategy_win_rate": 0.0,
            "strongest_strategy_trades": 0,
            "strongest_scene": {},
            "weakest_scene": {},
            "scene_leaderboard": [],
            "weakest_strategy": "",
            "weakest_strategy_win_rate": 1.0,
            "weakest_strategy_trades": 0,
            "symbol_boosts": {},
        }
        if not autopsies:
            self._last_targeted_state = state
            return

        symbol_stats: Dict[str, Dict[str, float]] = {}
        reason_win_counts: Dict[str, int] = {}
        reason_loss_counts: Dict[str, int] = {}
        strategy_stats: Dict[str, Dict[str, float]] = {}
        scene_stats: Dict[str, Dict[str, Any]] = {}

        for raw in autopsies:
            pnl = raw.get("pnl") or {}
            net = float(pnl.get("realized_net", 0.0) or 0.0)
            sym = str(raw.get("symbol", "") or "").strip()
            if active_symbols and sym and sym not in active_symbols:
                continue
            exit_meta = raw.get("exit") or {}
            reason = str(exit_meta.get("reason", "") or "").strip()
            snap = raw.get("entry_snapshot") or {}
            strat = str(snap.get("strategy") or snap.get("strategy_name") or "UNKNOWN").strip() or "UNKNOWN"

            if sym:
                ss = symbol_stats.setdefault(sym, {"n": 0.0, "wins": 0.0, "net": 0.0})
                ss["n"] += 1.0
                ss["net"] += net
                if net > 0:
                    ss["wins"] += 1.0
            st = strategy_stats.setdefault(strat, {"n": 0.0, "wins": 0.0, "net": 0.0})
            st["n"] += 1.0
            st["net"] += net
            if net > 0:
                st["wins"] += 1.0

            if net < 0:
                if reason:
                    reason_loss_counts[reason] = reason_loss_counts.get(reason, 0) + 1
            elif net > 0:
                if reason:
                    reason_win_counts[reason] = reason_win_counts.get(reason, 0) + 1

            feat = self._scene_features_from_autopsy(raw)
            scene_key = self._scene_key(feat)
            sc = scene_stats.setdefault(
                scene_key,
                {
                    **feat,
                    "n": 0.0,
                    "wins": 0.0,
                    "net": 0.0,
                },
            )
            sc["n"] += 1.0
            sc["net"] += net
            if net > 0:
                sc["wins"] += 1.0

        strongest_symbol = None
        for sym, ss in symbol_stats.items():
            n = int(ss["n"])
            if n < 3:
                continue
            wr = ss["wins"] / max(ss["n"], 1.0)
            if strongest_symbol is None or wr > strongest_symbol[1] or (wr == strongest_symbol[1] and ss["net"] > strongest_symbol[2]):
                strongest_symbol = (sym, wr, ss["net"], n)

        weakest_symbol = None
        for sym, ss in symbol_stats.items():
            n = int(ss["n"])
            if n < 3:
                continue
            wr = ss["wins"] / max(ss["n"], 1.0)
            if weakest_symbol is None or wr < weakest_symbol[1] or (wr == weakest_symbol[1] and ss["net"] < weakest_symbol[2]):
                weakest_symbol = (sym, wr, ss["net"], n)

        strongest_strategy = None
        for strat, ss in strategy_stats.items():
            if not strat or strat == "UNKNOWN":
                continue
            n = int(ss["n"])
            if n < 4:
                continue
            wr = ss["wins"] / max(ss["n"], 1.0)
            if strongest_strategy is None or wr > strongest_strategy[1] or (wr == strongest_strategy[1] and ss["net"] > strongest_strategy[2]):
                strongest_strategy = (strat, wr, ss["net"], n)

        weakest_strategy = None
        for strat, ss in strategy_stats.items():
            if not strat or strat == "UNKNOWN":
                continue
            n = int(ss["n"])
            if n < 4:
                continue
            wr = ss["wins"] / max(ss["n"], 1.0)
            if weakest_strategy is None or wr < weakest_strategy[1] or (wr == weakest_strategy[1] and ss["net"] < weakest_strategy[2]):
                weakest_strategy = (strat, wr, ss["net"], n)

        if strongest_symbol:
            state["strongest_symbol"] = strongest_symbol[0]
            state["strongest_symbol_win_rate"] = float(strongest_symbol[1])
            state["strongest_symbol_trades"] = int(strongest_symbol[3])

        if weakest_symbol:
            state["weakest_symbol"] = weakest_symbol[0]
            state["weakest_symbol_win_rate"] = float(weakest_symbol[1])
            state["weakest_symbol_trades"] = int(weakest_symbol[3])

        if strongest_strategy:
            state["strongest_strategy"] = strongest_strategy[0]
            state["strongest_strategy_win_rate"] = float(strongest_strategy[1])
            state["strongest_strategy_trades"] = int(strongest_strategy[3])

        strongest_scene = None
        weakest_scene = None
        for _, sc in scene_stats.items():
            n = int(sc["n"])
            if n < 3:
                continue
            wr = float(sc["wins"]) / max(float(sc["n"]), 1.0)
            payload = {
                "wr": wr,
                "net": float(sc["net"]),
                "n": n,
                "regime": str(sc["regime"]),
                "symbol": str(sc["symbol"]),
                "side": str(sc["side"]),
                "strategy": str(sc["strategy"]),
                "quadrant": str(sc["quadrant"]),
                "ai_bucket": str(sc["ai_bucket"]),
            }
            if strongest_scene is None or wr > strongest_scene["wr"] or (wr == strongest_scene["wr"] and float(sc["net"]) > strongest_scene["net"]):
                strongest_scene = payload
            if weakest_scene is None or wr < weakest_scene["wr"] or (wr == weakest_scene["wr"] and float(sc["net"]) < weakest_scene["net"]):
                weakest_scene = payload
        if strongest_scene:
            state["strongest_scene"] = strongest_scene
        if weakest_scene:
            state["weakest_scene"] = weakest_scene

        ranked_scenes: List[Dict[str, Any]] = []
        self._scene_priority_map = {}
        self._coarse_scene_priority_map = {}
        self._symbol_side_priority_map = {}
        for scene_key, sc in scene_stats.items():
            n = int(sc["n"])
            if n < 2:
                continue
            wr = float(sc["wins"]) / max(float(sc["n"]), 1.0)
            net = float(sc["net"])
            priority_score = float((net * 100.0) + (wr * 20.0) + min(n, 10))
            item = {
                "scene_key": scene_key,
                "priority_score": priority_score,
                "wr": wr,
                "net": net,
                "n": n,
                "regime": str(sc["regime"]),
                "symbol": str(sc["symbol"]),
                "side": str(sc["side"]),
                "strategy": str(sc["strategy"]),
                "quadrant": str(sc["quadrant"]),
                "ai_bucket": str(sc["ai_bucket"]),
            }
            ranked_scenes.append(item)
            self._scene_priority_map[scene_key] = priority_score
            coarse_key = self._scene_key(sc, include_bucket=False, include_quadrant=False)
            self._coarse_scene_priority_map[coarse_key] = max(
                priority_score, self._coarse_scene_priority_map.get(coarse_key, float("-inf"))
            )
            ss_key = f"{item['symbol']}|{item['side']}"
            self._symbol_side_priority_map[ss_key] = max(
                priority_score, self._symbol_side_priority_map.get(ss_key, float("-inf"))
            )
        ranked_scenes.sort(
            key=lambda x: (x["priority_score"], x["net"], x["wr"], x["n"]),
            reverse=True,
        )
        state["scene_leaderboard"] = ranked_scenes[:5]

        if weakest_strategy:
            state["weakest_strategy"] = weakest_strategy[0]
            state["weakest_strategy_win_rate"] = float(weakest_strategy[1])
            state["weakest_strategy_trades"] = int(weakest_strategy[3])

        if reason_win_counts:
            dom_reason, dom_count = max(reason_win_counts.items(), key=lambda kv: kv[1])
            state["dominant_win_reason"] = dom_reason
            state["dominant_win_reason_count"] = int(dom_count)

        if reason_loss_counts:
            dom_reason, dom_count = max(reason_loss_counts.items(), key=lambda kv: kv[1])
            state["dominant_loss_reason"] = dom_reason
            state["dominant_loss_reason_count"] = int(dom_count)

        boosts: Dict[str, Dict[str, float]] = {}
        for sym, ss in symbol_stats.items():
            n = int(ss["n"])
            if n < 3:
                continue
            wr = ss["wins"] / max(ss["n"], 1.0)
            if wr < 0.55 or ss["net"] <= 0:
                continue
            base_patch = self._base_symbol_patches.get(sym, {})
            base_lev = base_patch.get("max_leverage")
            if base_lev is None:
                base_lev = float(config_manager.get_config().risk.max_leverage)
            risk_cap = float(config_manager.get_config().risk.max_leverage or base_lev)
            strength = 1.08 if wr < 0.70 else 1.15
            lev = max(4.0, min(risk_cap, round(float(base_lev) * strength)))
            obi_base = base_patch.get("berserker_obi_threshold")
            if obi_base is None:
                obi_base = 0.85
            obi = max(0.72, float(obi_base) - (0.03 if wr < 0.70 else 0.06))
            boosts[sym] = {
                "max_leverage": float(lev),
                "berserker_obi_threshold": float(obi),
                "win_rate": float(wr),
                "trades": float(n),
                "net": float(ss["net"]),
            }

        state["symbol_boosts"] = boosts
        self._last_targeted_state = state

    def _apply_runtime_tactics(self, level: int) -> None:
        cfg = config_manager.get_config()
        p = cfg.strategy.params
        pb = cfg.playbook
        rk = cfg.risk
        b = self._base_runtime
        if not b:
            self._capture_runtime_baseline()
            b = self._base_runtime

        if level <= 0:
            atk_add = 0.0
            neu_add = 0.0
            funding_mult = 1.0
            align_mult = 1.0
            trend_mult = 1.0
            margin_mult = 1.0
            playbook_mult = 1.0
        elif level == 1:
            atk_add = 6.0
            neu_add = 4.0
            funding_mult = 0.80
            align_mult = 0.75
            trend_mult = 1.15
            margin_mult = 0.75
            playbook_mult = 0.80
        else:
            atk_add = 12.0
            neu_add = 8.0
            funding_mult = 0.60
            align_mult = 0.55
            trend_mult = 1.35
            margin_mult = 0.55
            playbook_mult = 0.60

        p.attack_ai_threshold = int(round(b["attack_ai_threshold"] + atk_add))
        p.neutral_ai_threshold = int(round(b["neutral_ai_threshold"] + neu_add))
        p.funding_signal_weight = max(0.05, b["funding_signal_weight"] * funding_mult)
        p.attack_sma_align_max_adverse_bps = max(8.0, b["attack_sma_align_max_adverse_bps"] * align_mult)
        p.attack_slow_sma_trend_guard_bps = max(0.0, b["attack_slow_sma_trend_guard_bps"] * trend_mult)

        pb.matrix_margin_fraction_a = max(0.002, b["matrix_margin_fraction_a"] * playbook_mult)
        pb.matrix_margin_fraction_b = max(0.003, b["matrix_margin_fraction_b"] * playbook_mult)
        pb.matrix_margin_fraction_c = max(0.01, b["matrix_margin_fraction_c"] * playbook_mult)
        pb.matrix_margin_fraction_d = max(0.005, b["matrix_margin_fraction_d"] * playbook_mult)
        rk.max_margin_per_trade_usdt = max(2.0, b["max_margin_per_trade_usdt"] * margin_mult)

        targeted = self._last_targeted_state
        strong_strategy = str(targeted.get("strongest_strategy") or "")
        strong_strategy_wr = float(targeted.get("strongest_strategy_win_rate", 0.0) or 0.0)
        weak_strategy = str(targeted.get("weakest_strategy") or "")
        weak_strategy_wr = float(targeted.get("weakest_strategy_win_rate", 1.0) or 1.0)
        win_reason = str(targeted.get("dominant_win_reason") or "")
        win_reason_count = int(targeted.get("dominant_win_reason_count", 0) or 0)
        dom_reason = str(targeted.get("dominant_loss_reason") or "")
        dom_reason_count = int(targeted.get("dominant_loss_reason_count", 0) or 0)

        if strong_strategy == "CoreAttack" and strong_strategy_wr >= 0.55:
            p.attack_ai_threshold = max(int(round(b["attack_ai_threshold"])), int(round(p.attack_ai_threshold - 4)))
            pb.matrix_margin_fraction_a = min(b["matrix_margin_fraction_a"] * 1.15, pb.matrix_margin_fraction_a * 1.10)
            pb.matrix_margin_fraction_b = min(b["matrix_margin_fraction_b"] * 1.10, pb.matrix_margin_fraction_b * 1.05)
        elif strong_strategy == "CoreNeutral" and strong_strategy_wr >= 0.55:
            p.neutral_ai_threshold = max(int(round(b["neutral_ai_threshold"])), int(round(p.neutral_ai_threshold - 4)))

        if weak_strategy == "CoreAttack" and weak_strategy_wr <= 0.40:
            p.attack_ai_threshold = int(round(p.attack_ai_threshold + 5))
            p.attack_sma_align_max_adverse_bps = max(8.0, p.attack_sma_align_max_adverse_bps * 0.8)
            p.attack_slow_sma_trend_guard_bps = max(p.attack_slow_sma_trend_guard_bps, b["attack_slow_sma_trend_guard_bps"] * 1.2)
        elif weak_strategy == "CoreNeutral" and weak_strategy_wr <= 0.40:
            p.neutral_ai_threshold = int(round(p.neutral_ai_threshold + 5))

        stop_like = {
            "core_bracket_sl",
            "exit_atr_initial",
            "l1_cvd_stop",
            "l1_bracket_stop",
            "time_stop",
            "assassin_invalid_tp_unwind",
            "high_conviction_trailing",
        }
        whipsaw_like = {"reverse_open_opposite", "opposite_fill", "reduce_only"}
        trend_capture_like = {"core_bracket_tp", "take_profit", "exit_chandelier_trail", "high_conviction_take_profit"}
        if win_reason_count >= 3 and win_reason in trend_capture_like:
            rk.max_margin_per_trade_usdt = min(b["max_margin_per_trade_usdt"], rk.max_margin_per_trade_usdt * 1.08)
            pb.matrix_margin_fraction_a = min(b["matrix_margin_fraction_a"] * 1.2, pb.matrix_margin_fraction_a * 1.12)
            pb.matrix_margin_fraction_b = min(b["matrix_margin_fraction_b"] * 1.15, pb.matrix_margin_fraction_b * 1.08)
            p.funding_signal_weight = min(b["funding_signal_weight"], p.funding_signal_weight * 1.10)
        if dom_reason_count >= 3 and dom_reason in stop_like:
            p.attack_ai_threshold = int(round(p.attack_ai_threshold + 3))
            p.neutral_ai_threshold = int(round(p.neutral_ai_threshold + 2))
            rk.max_margin_per_trade_usdt = max(2.0, rk.max_margin_per_trade_usdt * 0.85)
            pb.matrix_margin_fraction_a = max(0.002, pb.matrix_margin_fraction_a * 0.9)
            pb.matrix_margin_fraction_b = max(0.003, pb.matrix_margin_fraction_b * 0.9)
        elif dom_reason_count >= 3 and dom_reason in whipsaw_like:
            p.attack_slow_sma_trend_guard_bps = max(
                p.attack_slow_sma_trend_guard_bps,
                b["attack_slow_sma_trend_guard_bps"] * 1.35,
            )
            p.funding_signal_weight = max(0.05, p.funding_signal_weight * 0.85)

        for sym, base_patch in self._base_symbol_patches.items():
            prev = cfg.darwin.symbol_patches.get(sym, DarwinSymbolPatch())
            merged = {
                "max_leverage": base_patch.get("max_leverage"),
                "berserker_obi_threshold": base_patch.get("berserker_obi_threshold"),
            }
            boost = (targeted.get("symbol_boosts") or {}).get(sym)
            if isinstance(boost, dict):
                merged["max_leverage"] = float(boost.get("max_leverage")) if boost.get("max_leverage") is not None else merged["max_leverage"]
                merged["berserker_obi_threshold"] = (
                    float(boost.get("berserker_obi_threshold"))
                    if boost.get("berserker_obi_threshold") is not None
                    else merged["berserker_obi_threshold"]
                )
            payload = {k: v for k, v in merged.items() if v is not None}
            if payload:
                cfg.darwin.symbol_patches[sym] = DarwinSymbolPatch(**payload)
            elif sym in cfg.darwin.symbol_patches and not prev.model_dump(exclude_none=True):
                cfg.darwin.symbol_patches.pop(sym, None)

    def _compute_adaptation_level(self) -> int:
        c = self._cfg()
        mon = self._monitor
        n = len(mon._closes)
        if n <= 0:
            return 0
        wr = mon.realized_win_rate()
        cons = mon.consecutive_losses()
        min_n = int(c.min_trades_for_enter_wr)
        hard_wr = max(0.20, float(c.enter_win_rate_max))
        soft_wr = max(0.45, hard_wr + 0.12)
        if cons >= int(c.enter_consecutive_losses) + 2 or (n >= max(8, min_n) and wr <= hard_wr):
            return 2
        if cons >= int(c.enter_consecutive_losses) or (n >= min_n and wr <= soft_wr):
            return 1
        return 0

    def record_realized_net(self, net_usdt: float) -> None:
        c = self._cfg()
        if not c.enabled:
            return
        self._seed_from_autopsies_once()
        self._monitor.record(net_usdt)
        self._sync_probe_state()

    def _sync_probe_state(self) -> None:
        c = self._cfg()
        mon = self._monitor
        self._seed_from_autopsies_once()
        self._refresh_targeted_state()
        n = len(mon._closes)
        wr = mon.realized_win_rate()
        cons = mon.consecutive_losses()

        rec_n = int(c.recovery_trades)
        rec_wr = mon.last_n_win_rate(rec_n)
        can_recover = n >= rec_n and rec_wr >= float(c.recovery_win_rate_min)

        enter_wr = n >= int(c.min_trades_for_enter_wr) and wr < float(c.enter_win_rate_max)
        enter_cons = cons >= int(c.enter_consecutive_losses)
        enter = enter_wr or enter_cons
        level = self._compute_adaptation_level()

        if rec_wr >= float(c.recovery_win_rate_min) and cons == 0 and level > 0:
            level = max(0, level - 1)

        if self.probe_mode and can_recover:
            self.probe_mode = False
            log.info(
                f"[AutoTuner] Recovery: probe OFF — last_{rec_n} win_rate={rec_wr:.2f} "
                f">= {c.recovery_win_rate_min}"
            )
        elif not self.probe_mode and enter:
            self.probe_mode = True
            log.warning(
                f"[AutoTuner] Probe ON — consecutive_losses={cons} window_win_rate={wr:.2f}"
            )

        if level != self.adaptation_level:
            self.adaptation_level = level
            self._apply_runtime_tactics(level)
            label = {0: "NORMAL", 1: "DEFENSIVE", 2: "SURVIVAL"}.get(level, "NORMAL")
            log.warning(
                f"[AutoTuner] Tactics -> {label} | wr={wr:.2f} cons_losses={cons} "
                f"attack_ai={config_manager.get_config().strategy.params.attack_ai_threshold} "
                f"neutral_ai={config_manager.get_config().strategy.params.neutral_ai_threshold} "
                f"margin_cap={config_manager.get_config().risk.max_margin_per_trade_usdt:.2f} "
                f"strong_symbol={self._last_targeted_state.get('strongest_symbol') or '-'} "
                f"win_reason={self._last_targeted_state.get('dominant_win_reason') or '-'} "
                f"loss_reason={self._last_targeted_state.get('dominant_loss_reason') or '-'} "
                f"strong_strategy={self._last_targeted_state.get('strongest_strategy') or '-'} "
                f"weak_strategy={self._last_targeted_state.get('weakest_strategy') or '-'}"
            )

    def runtime_status(self) -> Dict[str, Any]:
        self._sync_probe_state()
        mon = self._monitor
        cfg = config_manager.get_config()
        p = cfg.strategy.params
        ts = self._last_targeted_state
        return {
            "probe_mode": bool(self.probe_mode),
            "adaptation_level": int(self.adaptation_level),
            "adaptation_label": {0: "NORMAL", 1: "DEFENSIVE", 2: "SURVIVAL"}.get(self.adaptation_level, "NORMAL"),
            "window_trades": len(mon._closes),
            "window_win_rate": float(mon.realized_win_rate()),
            "consecutive_losses": int(mon.consecutive_losses()),
            "recovery_win_rate": float(mon.last_n_win_rate(int(self._cfg().recovery_trades))),
            "live_attack_ai_threshold": float(getattr(p, "attack_ai_threshold", 0) or 0),
            "live_neutral_ai_threshold": float(getattr(p, "neutral_ai_threshold", 0) or 0),
            "live_funding_signal_weight": float(getattr(p, "funding_signal_weight", 0) or 0),
            "live_attack_align_bps": float(getattr(p, "attack_sma_align_max_adverse_bps", 0) or 0),
            "live_margin_cap_usdt": float(getattr(cfg.risk, "max_margin_per_trade_usdt", 0) or 0),
            "strongest_symbol": str(ts.get("strongest_symbol") or ""),
            "strongest_symbol_win_rate": float(0.0 if ts.get("strongest_symbol_win_rate") is None else ts.get("strongest_symbol_win_rate")),
            "strongest_symbol_trades": int(0 if ts.get("strongest_symbol_trades") is None else ts.get("strongest_symbol_trades")),
            "weakest_symbol": str(ts.get("weakest_symbol") or ""),
            "weakest_symbol_win_rate": float(1.0 if ts.get("weakest_symbol_win_rate") is None else ts.get("weakest_symbol_win_rate")),
            "weakest_symbol_trades": int(0 if ts.get("weakest_symbol_trades") is None else ts.get("weakest_symbol_trades")),
            "dominant_win_reason": str(ts.get("dominant_win_reason") or ""),
            "dominant_win_reason_count": int(0 if ts.get("dominant_win_reason_count") is None else ts.get("dominant_win_reason_count")),
            "dominant_loss_reason": str(ts.get("dominant_loss_reason") or ""),
            "dominant_loss_reason_count": int(0 if ts.get("dominant_loss_reason_count") is None else ts.get("dominant_loss_reason_count")),
            "strongest_strategy": str(ts.get("strongest_strategy") or ""),
            "strongest_strategy_win_rate": float(0.0 if ts.get("strongest_strategy_win_rate") is None else ts.get("strongest_strategy_win_rate")),
            "strongest_strategy_trades": int(0 if ts.get("strongest_strategy_trades") is None else ts.get("strongest_strategy_trades")),
            "strongest_scene": copy.deepcopy(ts.get("strongest_scene") or {}),
            "weakest_scene": copy.deepcopy(ts.get("weakest_scene") or {}),
            "scene_leaderboard": copy.deepcopy(ts.get("scene_leaderboard") or []),
            "weakest_strategy": str(ts.get("weakest_strategy") or ""),
            "weakest_strategy_win_rate": float(1.0 if ts.get("weakest_strategy_win_rate") is None else ts.get("weakest_strategy_win_rate")),
            "weakest_strategy_trades": int(0 if ts.get("weakest_strategy_trades") is None else ts.get("weakest_strategy_trades")),
            "symbol_boosts": copy.deepcopy(ts.get("symbol_boosts") or {}),
        }

    def scene_bias_for_signal(self, signal: SignalEvent) -> Dict[str, Any]:
        self._seed_from_autopsies_once()
        self._refresh_targeted_state()
        feat = self._scene_features_from_signal(signal)
        return self.scene_bias_for_features(feat)

    def scene_bias_for_features(self, feat: Dict[str, Any]) -> Dict[str, Any]:
        self._seed_from_autopsies_once()
        self._refresh_targeted_state()
        strong = self._last_targeted_state.get("strongest_scene") or {}
        weak = self._last_targeted_state.get("weakest_scene") or {}
        exact_key = self._scene_key(feat)
        coarse_key = self._scene_key(feat, include_bucket=False, include_quadrant=False)
        symbol_side_key = f"{feat['symbol']}|{feat['side']}"
        ranking_score = self._scene_priority_map.get(
            exact_key,
            self._coarse_scene_priority_map.get(
                coarse_key,
                self._symbol_side_priority_map.get(symbol_side_key, 0.0),
            ),
        )
        if not strong and not weak:
            return {
                "match_level": "none",
                "margin_mult": 1.0,
                "notional_mult": 1.0,
                "ai_score_bonus": 0.0,
                "threshold_delta": 0.0,
                "scene_key": exact_key,
                "priority_score": float(ranking_score),
            }

        def _match_level(scene: Dict[str, Any]) -> str:
            if not scene:
                return "none"
            exact = (
                feat["regime"] == scene.get("regime")
                and feat["symbol"] == scene.get("symbol")
                and feat["side"] == scene.get("side")
                and feat["strategy"] == scene.get("strategy")
                and feat["quadrant"] == scene.get("quadrant")
                and feat["ai_bucket"] == scene.get("ai_bucket")
            )
            coarse = (
                feat["regime"] == scene.get("regime")
                and feat["symbol"] == scene.get("symbol")
                and feat["side"] == scene.get("side")
                and feat["strategy"] == scene.get("strategy")
            )
            symbol_side = feat["symbol"] == scene.get("symbol") and feat["side"] == scene.get("side")
            if exact:
                return "exact"
            if coarse:
                return "coarse"
            if symbol_side:
                return "symbol_side"
            return "none"

        strong_level = _match_level(strong)
        weak_level = _match_level(weak)
        if strong_level == "exact":
            return {
                "match_level": "strong_exact",
                "margin_mult": 1.18,
                "notional_mult": 1.18,
                "ai_score_bonus": 6.0,
                "threshold_delta": -8.0,
                "scene_key": exact_key,
                "priority_score": float(ranking_score),
            }
        if strong_level == "coarse":
            return {
                "match_level": "strong_coarse",
                "margin_mult": 1.10,
                "notional_mult": 1.10,
                "ai_score_bonus": 3.0,
                "threshold_delta": -4.0,
                "scene_key": coarse_key,
                "priority_score": float(ranking_score),
            }
        if strong_level == "symbol_side":
            return {
                "match_level": "strong_symbol_side",
                "margin_mult": 1.05,
                "notional_mult": 1.05,
                "ai_score_bonus": 1.5,
                "threshold_delta": -2.0,
                "scene_key": symbol_side_key,
                "priority_score": float(ranking_score),
            }
        if weak_level == "exact":
            return {
                "match_level": "weak_exact",
                "margin_mult": 0.92,
                "notional_mult": 0.92,
                "ai_score_bonus": -4.0,
                "threshold_delta": 8.0,
                "scene_key": exact_key,
                "priority_score": float(ranking_score),
            }
        if weak_level == "coarse":
            return {
                "match_level": "weak_coarse",
                "margin_mult": 0.96,
                "notional_mult": 0.96,
                "ai_score_bonus": -2.0,
                "threshold_delta": 4.0,
                "scene_key": coarse_key,
                "priority_score": float(ranking_score),
            }
        if weak_level == "symbol_side":
            return {
                "match_level": "weak_symbol_side",
                "margin_mult": 0.98,
                "notional_mult": 0.98,
                "ai_score_bonus": -1.0,
                "threshold_delta": 2.0,
                "scene_key": symbol_side_key,
                "priority_score": float(ranking_score),
            }
        return {
            "match_level": "none",
            "margin_mult": 1.0,
            "notional_mult": 1.0,
            "ai_score_bonus": 0.0,
            "threshold_delta": 0.0,
            "scene_key": exact_key,
            "priority_score": float(ranking_score),
        }


strategy_auto_tuner = StrategyAutoTuner()


def feed_realized_net_from_exchange_result(result: Any) -> None:
    """OrderManager / 引擎在 create_order 返回后调用（纸面带 realized_net_usdt；实盘可后续扩展）。"""
    if not isinstance(result, dict):
        return
    if result.get("status") == "rejected":
        return
    net = result.get("realized_net_usdt")
    if net is None:
        return
    try:
        try:
            from src.core.risk_engine import risk_engine

            risk_engine.record_realized_pnl(float(net))
        except Exception:
            pass
        strategy_auto_tuner.record_realized_net(float(net))
    except (TypeError, ValueError):
        pass


def apply_ai_confidence_discount_to_signal(signal: SignalEvent) -> None:
    """侦察模式：对 AI 预测胜率做乘法打折（不改 settings 里的狙击手 floor）。"""
    raw = getattr(signal, "ai_win_rate", None)
    if raw is None:
        return
    try:
        c = config_manager.get_config().auto_tuner
        if not c.enabled or not strategy_auto_tuner.probe_mode:
            return
        x = float(raw)
        m = float(c.confidence_penalty_multiplier)
        signal.ai_win_rate = max(0.0, min(1.0, x * m))
    except (TypeError, ValueError):
        pass


def apply_scene_learning_to_signal(signal: SignalEvent) -> None:
    ect = dict(getattr(signal, "entry_context", None) or {})
    bias = strategy_auto_tuner.scene_bias_for_signal(signal)
    ect["scene_learning"] = bias
    try:
        base_score = float(ect.get("ai_score", 50.0) or 50.0)
    except (TypeError, ValueError):
        base_score = 50.0
    ect["ai_score_bucket"] = str(ect.get("ai_score_bucket") or strategy_auto_tuner._ai_bucket(base_score))
    ect["scene_key"] = str(ect.get("scene_key") or bias.get("scene_key") or "")
    try:
        ai_score = float(ect.get("ai_score", 0.0) or 0.0)
        ect["scene_adjusted_ai_score"] = ai_score + float(bias.get("ai_score_bonus", 0.0) or 0.0)
    except (TypeError, ValueError):
        pass
    signal.entry_context = ect
