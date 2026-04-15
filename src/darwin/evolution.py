"""
L3 达尔文批进化：积累 N 笔 trade_autopsy.v2 → 单次 LLM 归因 → darwin.evolution.v1 补丁。

输出契约（与 apply_darwin_llm_result 对齐，并扩展 l1_fast_loop / l1_runtime）：
{
  "schema": "darwin.evolution.v1",
  "reflection": "string",
  "batch_stats": { "n": int, "win_rate": float, "mean_net_pnl": float, ... },
  "patches": {
    "risk": { ... },
    "strategy": {},
    "strategy_params": { ... },
    "shark_scalp": { "signal_cooldown_sec": 0.35 },
    "symbols": { "X/USDT": { "max_leverage": 15 } },
    "l1_fast_loop": { "cvd_burst_mult": 3.0, "min_atr_bps": 12.0, ... },
    "l1_runtime": { "halt_trading": false, "cvd_burst_mult": 3.2, "position_scale": 0.7 }
  }
}

说明：
- l1_fast_loop: 写入 settings.yaml（不含 enabled，由安全策略忽略）
- l1_runtime: 仅内存，经 apply_l1_tuning 影响当前 L1 进程
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from src.utils.logger import log
from src.core.config_manager import config_manager
from src.ai.llm_factory import LLMFactory
from src.darwin.researcher import _load_macro_context
from src.darwin.experience_store import tail_experience_text


def summarize_batch(autopsies: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(autopsies)
    nets = [float(a.get("pnl", {}).get("realized_net", 0) or 0) for a in autopsies]
    wins = sum(1 for x in nets if x > 0)
    reasons: Dict[str, int] = {}
    sym_pnls: Dict[str, List[float]] = {}
    for a in autopsies:
        r = str(a.get("exit", {}).get("reason", "unknown"))
        reasons[r] = reasons.get(r, 0) + 1
        sym = str(a.get("symbol", ""))
        sym_pnls.setdefault(sym, []).append(float(a.get("pnl", {}).get("realized_net", 0) or 0))

    worst_syms = sorted(
        ((s, sum(v) / max(len(v), 1)) for s, v in sym_pnls.items()),
        key=lambda x: x[1],
    )[:6]

    l1_rows = [a for a in autopsies if a.get("l1_at_signal")]
    return {
        "n": n,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "mean_net_pnl": round(sum(nets) / n, 6) if n else 0.0,
        "sum_net_pnl": round(sum(nets), 6),
        "exit_reason_counts": reasons,
        "worst_symbols_by_avg_net": [{"symbol": s, "avg_net": v} for s, v in worst_syms],
        "l1_trades_in_batch": len(l1_rows),
    }


def build_batch_evolution_prompt(autopsies: List[Dict[str, Any]], macro: str) -> str:
    stats = summarize_batch(autopsies)
    # 控制 token：全文序列化；必要时可再截断
    body = json.dumps(autopsies, ensure_ascii=False, default=str)
    stats_j = json.dumps(stats, ensure_ascii=False, default=str)
    exp_block = tail_experience_text()
    if not exp_block.strip():
        exp_block = "(empty)"
    return f"""DARWIN_BATCH_EVOLUTION

You are the evolution/research layer. Below is BATCH_STATS and then an array of {len(autopsies)} closed-trade autopsies (schema darwin.trade_autopsy.v2).
Find loss clusters (symbol, exit_reason, l1_at_signal vs pnl). Propose CONSERVATIVE patches.

Output ONE JSON object (no markdown) with this exact shape:
{{
  "schema": "darwin.evolution.v1",
  "reflection": "short string — main causal themes",
  "batch_stats": (copy or refine the provided BATCH_STATS object as you see fit),
  "patches": {{
    "risk": {{ optional: max_single_risk, max_orders_per_second, max_leverage, grinder_leverage_min, grinder_leverage_max, berserker_obi_threshold, drawdown_cool_down_enabled, drawdown_cool_down_sec, drawdown_halt_trading, daily_drawdown_limit, hard_drawdown_limit }},
    "strategy": {{ optional: single_open_per_symbol, regime_switch_anchor_symbol, allocations }},
    "strategy_params": {{ optional: neutral_rsi_buy, neutral_rsi_sell, neutral_ai_threshold, neutral_ai_trend_relax_enabled, neutral_ai_trend_run_bps, neutral_ai_trend_relax_points, neutral_ai_trend_relax_floor, neutral_block_if_window_run_bps, attack_ai_threshold, attack_slow_sma_trend_guard_bps, attack_sma_align_max_adverse_bps, attack_sma_fast_ticks, funding_signal_weight }},
    "shark_scalp": {{ optional: signal_cooldown_sec, max_equity_fraction_per_shot, bid_ask_size_ratio_min — do NOT set enabled }},
    "symbols": {{ "PEPE/USDT": {{ "max_leverage": 12, "berserker_obi_threshold": 0.9 }} }},
    "l1_fast_loop": {{
      optional keys (persisted to yaml, do NOT set "enabled"):
      min_atr_bps, cvd_burst_mult, cvd_stop_mult, max_obi_opposition_long,
      tp_bps, trade_notional_usd, leverage, signal_cooldown_sec, require_attack_mode
    }},
    "l1_runtime": {{
      optional keys (in-memory hot tuning only):
      halt_trading, cvd_burst_mult, cvd_stop_mult, min_atr_bps, position_scale
    }}
  }}
}}

Rules:
- Omit any patch key you do not want to change.
- Never set l1_fast_loop.enabled.
- Prefer small nudges; do not zero out risk limits.
- If evidence weak, return empty patches {{}}.

BATCH_STATS:
{stats_j}

AUTOPSIES_JSON:
{body}
---
EXPERIENCE_BANK_TAIL:
{exp_block}
---
EXTERNAL_CONTEXT:
{macro}
---
END
"""


class DarwinBatchEvolution:
    async def run(self, autopsies: List[Dict[str, Any]]) -> None:
        cfg = config_manager.get_config().darwin
        macro = _load_macro_context(cfg.macro_context_path)
        prompt = build_batch_evolution_prompt(autopsies, macro)
        llm = LLMFactory.create_from_darwin_config()
        try:
            result = await llm.analyze(prompt)
        except Exception as e:
            log.error(f"[Darwin/L3] Batch LLM failed: {e}")
            return

        if not isinstance(result, dict):
            log.warning("[Darwin/L3] Batch LLM returned non-dict.")
            return

        log.info(f"[Darwin/L3] Batch reflection: {str(result.get('reflection', ''))[:240]}")

        if not cfg.autopilot:
            log.info("[Darwin/L3] autopilot=false — not merging patches.")
            return
        if not cfg.apply_llm_patches:
            log.info("[Darwin/L3] apply_llm_patches=false — not merging patches.")
            return

        applied = config_manager.apply_darwin_llm_result(result)
        if applied:
            log.info("[Darwin/L3] Batch patches applied (yaml and/or L1 runtime).")
        else:
            log.info("[Darwin/L3] No applicable batch patches.")
