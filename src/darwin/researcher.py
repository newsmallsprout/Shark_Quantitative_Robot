import json
from typing import Any, Dict

from src.utils.logger import log
from src.core.config_manager import StrategyParams, config_manager
from src.ai.llm_factory import LLMFactory
from src.darwin.experience_store import tail_experience_text


def _load_macro_context(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()[:12000]
    except OSError:
        return "(macro context file missing — inject ETF flows, funding regime, or research notes here)"


class DarwinResearcher:
    """
    Independent reflection step: consumes trade autopsy + external RAG snippet, emits JSON patches.
    """

    async def run(self, autopsy: Dict[str, Any]) -> None:
        cfg = config_manager.get_config().darwin
        macro = _load_macro_context(cfg.macro_context_path)
        prompt = self._build_prompt(autopsy, macro)

        llm = LLMFactory.create_from_darwin_config()
        try:
            result = await llm.analyze(prompt)
        except Exception as e:
            log.error(f"[Darwin] LLM reflection failed: {e}")
            return

        if not isinstance(result, dict):
            log.warning("[Darwin] LLM returned non-dict; skipping apply.")
            return

        log.info(f"[Darwin] Reflection: {result.get('reflection', '')[:200]}")

        if not cfg.autopilot:
            log.info("[Darwin] autopilot=false — not merging LLM patches.")
            return
        if not cfg.apply_llm_patches:
            log.info("[Darwin] apply_llm_patches=false — not writing settings.yaml.")
            return

        applied = config_manager.apply_darwin_llm_result(result)
        if applied:
            log.info("[Darwin] Applied researcher patches; config reloaded on disk.")
        else:
            log.info("[Darwin] No applicable patches in researcher output.")

    def _build_prompt(self, autopsy: Dict[str, Any], macro: str) -> str:
        autopsy_json = json.dumps(autopsy, indent=2, default=str)
        exp_block = tail_experience_text()
        if not exp_block.strip():
            exp_block = "(empty — no prior experience lines yet)"
        sp_keys = ", ".join(sorted(StrategyParams.model_fields.keys()))
        return f"""Darwin Protocol reflection task.

You are the quantitative researcher. A closed trade autopsy (JSON), a rolling EXPERIENCE BANK (recent opens/closes), and external context are below.

Goals:
1) Attribute what worked or failed (liquidity, mode, OBI, leverage, fees/slippage, bracket exits).
2) Compare this trade to recent experience — avoid repeating loss patterns (e.g. same symbol + exit_reason cluster).
3) Propose small, justified parameter nudges; omit patches if evidence is weak.

Respond with a single JSON object (no markdown) of this exact shape:
{{
  "reflection": "short causal analysis string",
  "patches": {{
    "risk": {{ optional keys among RiskConfig fields, e.g. max_single_risk, max_orders_per_second, max_leverage, grinder_leverage_min, grinder_leverage_max, berserker_obi_threshold }},
    "strategy": {{ optional: single_open_per_symbol, regime_switch_anchor_symbol, allocations }},
    "strategy_params": {{ optional: any subset of valid strategy.params field names listed below }},
    "shark_scalp": {{ optional SharkScalp fields except enabled — e.g. signal_cooldown_sec, max_equity_fraction_per_shot }},
    "symbols": {{
      "SYMBOL/USDT": {{ "berserker_obi_threshold": 0.9, "max_leverage": 30 }}
    }}
  }}
}}

Valid strategy_params field names (use exact keys only): {sp_keys}

Rules:
- Omit "patches" keys you do not want to change.
- Symbol keys use slash form e.g. PEPE/USDT (not PEPE_USDT).
- "max_leverage" under symbols caps berserker tier leverage from above (cannot exceed exchange tier).
- Nudge at most a few numeric fields per reflection; do not wipe risk controls.

--- TRADE AUTOPSY ---
{autopsy_json}

--- EXPERIENCE BANK (recent JSONL, newest at bottom) ---
{exp_block}

--- EXTERNAL CONTEXT (RAG / macro) ---
{macro}

--- END ---
"""

