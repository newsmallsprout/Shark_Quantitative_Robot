"""
L2 指挥：全市场 USDT 永续 ticker 扫描 → 标的池排序 → 规则/LLM 生成 L1 运行时调参（供 ZMQ 下发）。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.core.config_manager import config_manager
from src.utils.logger import log

GATE_USDT_TICKERS = "https://api.gateio.ws/api/v4/futures/usdt/tickers"


def _contract_to_symbol(contract: str) -> str:
    c = (contract or "").strip()
    if not c:
        return ""
    return c.replace("_", "/")


async def fetch_gate_usdt_tickers(
    session: aiohttp.ClientSession,
) -> List[Dict[str, Any]]:
    async with session.get(GATE_USDT_TICKERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            text = await resp.text()
            log.warning(f"[L2] Gate tickers HTTP {resp.status}: {text[:200]}")
            return []
        data = await resp.json()
        return data if isinstance(data, list) else []


def rank_universe_symbols(
    rows: List[Dict[str, Any]],
    *,
    min_quote_vol: float,
    top_n: int,
    cap: int,
    anchors: List[str],
    change_pct_divisor: float = 25.0,
    change_multiplier_cap: float = 3.0,
    funding_scale: float = 800.0,
    funding_cap_mult: float = 1.5,
) -> Tuple[List[str], Dict[str, Any]]:
    """
    以 24h 计价成交额 + 涨跌幅异动 为启发式排序（无历史快照时的「突增」代理）。
    change_pct_divisor 越小越偏「高波动」；funding_* 调节资金费率项权重。
    返回 (symbols, debug_stats)。
    """
    scored: List[Tuple[float, str, Dict[str, Any]]] = []
    div = max(float(change_pct_divisor), 1e-6)
    ch_cap = max(float(change_multiplier_cap), 0.0)
    fscale = max(float(funding_scale), 0.0)
    fcap = max(float(funding_cap_mult), 0.0)
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = _contract_to_symbol(str(r.get("contract", "")))
        if not sym.endswith("/USDT"):
            continue
        try:
            vq = float(r.get("volume_24h_quote", 0) or 0)
        except (TypeError, ValueError):
            vq = 0.0
        if vq < min_quote_vol:
            continue
        try:
            chg = float(r.get("change_percentage", 0) or 0)
        except (TypeError, ValueError):
            chg = 0.0
        try:
            fr = float(r.get("funding_rate", 0) or 0)
        except (TypeError, ValueError):
            fr = 0.0
        # 高成交 + 波动更大者优先（参数可调，热门山寨可调小 divisor）
        score = vq * (1.0 + min(abs(chg) / div, ch_cap)) * (1.0 + min(abs(fr) * fscale, fcap))
        scored.append((score, sym, {"vq": vq, "chg": chg, "fr": fr}))

    scored.sort(key=lambda x: -x[0])
    picked_meta = scored[: max(1, top_n)]

    anchor_clean = [a.strip() for a in (anchors or []) if a and str(a).strip()]
    out: List[str] = []
    seen = set()
    for a in anchor_clean:
        if a not in seen:
            seen.add(a)
            out.append(a)
    for _sc, sym, _m in picked_meta:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= cap:
            break

    stats = {
        "candidates": len(scored),
        "picked": len(picked_meta),
        "symbols_out": len(out),
        "top_preview": [
            {"symbol": s, "vq": m["vq"], "chg_pct": m["chg"], "funding": m["fr"]}
            for _sc, s, m in picked_meta[:8]
        ],
    }
    return out, stats


def rules_l1_tuning(
    regime_counts: Counter,
    scores: List[float],
    l1: Any,
) -> Dict[str, Any]:
    """由本周期各币 AI regime/score 聚合出的保守规则（不依赖 LLM）。"""
    n = sum(regime_counts.values()) or 1
    chaotic_share = regime_counts.get("CHAOTIC", 0) / n
    osc_share = regime_counts.get("OSCILLATING", 0) / n
    avg_score = sum(scores) / len(scores) if scores else 50.0

    out: Dict[str, Any] = {}
    base_burst = float(l1.cvd_burst_mult)
    base_stop = float(l1.cvd_stop_mult)
    base_atr = float(l1.min_atr_bps)

    if chaotic_share >= 0.32:
        out["cvd_burst_mult"] = round(base_burst * 1.65, 4)
        out["cvd_stop_mult"] = round(base_stop * 1.12, 4)
        out["position_scale"] = 0.42
        out["min_atr_bps"] = round(max(base_atr, base_atr * 1.15), 4)
        out["halt_trading"] = False
    elif osc_share >= 0.62 and avg_score < 44.0:
        out["halt_trading"] = True
        out["min_atr_bps"] = round(max(120.0, base_atr * 4.0), 4)
        out["position_scale"] = 0.2
        out["cvd_burst_mult"] = round(base_burst * 1.25, 4)
        out["cvd_stop_mult"] = round(base_stop * 1.05, 4)
    else:
        out["halt_trading"] = False
        out["position_scale"] = 1.0
        out["cvd_burst_mult"] = base_burst
        out["cvd_stop_mult"] = base_stop
        out["min_atr_bps"] = base_atr

    return out


def merge_l1_tuning(
    rules: Dict[str, Any], llm: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """LLM 非空字段覆盖规则；null 表示不覆盖。"""
    merged = dict(rules)
    if not llm or not isinstance(llm, dict):
        return merged
    allowed = (
        "halt_trading",
        "cvd_burst_mult",
        "cvd_stop_mult",
        "min_atr_bps",
        "position_scale",
    )
    for k in allowed:
        if k not in llm:
            continue
        v = llm[k]
        if v is None:
            continue
        merged[k] = v
    return merged


def build_l1_tuning_prompt(
    regime_counts: Counter,
    scores: List[float],
    universe_stats: Dict[str, Any],
    l1: Any,
) -> str:
    snap = {
        "regime_counts": dict(regime_counts),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "l1_defaults": {
            "min_atr_bps": l1.min_atr_bps,
            "cvd_burst_mult": l1.cvd_burst_mult,
            "cvd_stop_mult": l1.cvd_stop_mult,
        },
        "universe": universe_stats,
    }
    return f"""L1_PARAM_TUNING
You are the slow tactical layer for a crypto futures sniper bot.
Given the JSON snapshot below, output ONE JSON object only (no markdown) with optional keys:
- "halt_trading": boolean — true if dead/choppy regime, no L1 entries
- "cvd_burst_mult": number or null — multiplier sensitivity for CVD burst (higher = stricter)
- "cvd_stop_mult": number or null — CVD reversal stop tightness
- "min_atr_bps": number or null — minimum 1m ATR in bps to allow trading
- "position_scale": number between 0 and 1 — scales L1 notional

Use null for any key you do not want to change. Favor safety in chaotic macro.

SNAPSHOT:
{json.dumps(snap, ensure_ascii=False)}
"""
