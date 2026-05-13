# RangePlan Execution Alignment Audit

## Removed Overrides

- Python order sizing now uses `RangePlan.position_size_pct` as the source of trading intent. Local config can cap plan risk lower with `max_plan_margin_pct`, but it no longer multiplies planned size upward via regime, volatility, stable-coin, or evolution factors.
- Python no longer rewrites planned entry into a local strategic entry before `PlanGate` checks. The market price is checked against the plan's entry zone, so Go planning remains the source of entry geometry.
- Redis order commands now preserve all take-profit levels in `take_profit_levels` while keeping the first level in `take_profit` for compatibility.
- Go executor now accepts order `source` and can split reduce-only take-profit orders across multiple plan targets.

## Planning Improvements

- Go planning now distinguishes breakout up/down, slow grind up/down, bleed down, choppy, and dead regimes in addition to existing trend and range regimes.
- Math fallback plans now emit explicit `position_size_pct`, `leverage`, and `cut_loss_pct` via regime playbooks, avoiding implicit minimum-risk defaults that made Python skip or distort plans.
- Regime playbooks now follow the core execution philosophy: micro margin (roughly 0.15%-0.5% balance), high leverage (about 65x-95x when tradable), quick first take-profit, and wider ATR-based stops to avoid noise stops immediately after entry.
- AI prompts now ask for small-margin high-leverage plans, fast first take-profit, and stop losses wider than the first target while forbidding loss-averaging pyramid points.
- Planner audit now validates both-side plans instead of only single long/short plans.
- Plan `state` is assigned before Redis serialization, so Python sees `LIVE` or `PAUSED` consistently.

## TradingView Learning

- Public TradingView ideas are parsed into structured `TVInsight` summaries with bias, support, resistance, targets, stop, timeframe, confidence, and count.
- The knowledge base now returns structured TradingView insight summaries to the planner instead of only coarse sentiment.
- Public scraping remains best-effort and degrades safely to local math/AI planning when data is unavailable.

## Remaining Safety Constraints

- `PlanGate` still rejects stale plans, paused plans, high news risk, invalid side, direction mismatches, and prices outside the relevant plan entry zone.
- Python still enforces balance, minimum contract size, total exposure, and bucket capital limits.
- Go executor still validates live mode, action, side, symbol, positive size, leverage range, and optional order token.

## Cleanup

- Removed unused `ai_position.py` and its Docker/import/documentation references. The live path no longer carries a second unused AI position state machine.
