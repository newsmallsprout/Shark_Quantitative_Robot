# 2026-05-13 Project Audit Issues

This local ledger tracks issues found during the full-project audit and repair pass.

## Status Legend

- `confirmed`: reproduced or verified by code inspection.
- `fixed`: repaired and covered by verification.
- `deferred`: intentionally left for a later pass with rationale.

## Baseline Verification

| ID | Status | Evidence | Acceptance Criteria |
| --- | --- | --- | --- |
| BASE-001 | fixed | `tests/` was missing; added Python unit tests. | Python tests run from a tracked `tests/` package. |
| BASE-002 | fixed | `executor/` was missing `go.sum`; refreshed module metadata. | Executor tests/builds run without dependency metadata errors. |
| BASE-003 | fixed | `matcher/` was missing `go.sum`; refreshed module metadata. | Matcher tests/builds run without dependency metadata errors. |
| BASE-004 | fixed | `rl/vendor` was incomplete for go-redis v9.19.0; refreshed vendor contents. | RL tests/builds run with valid vendor contents. |

## Safety And Correctness Issues

| ID | Status | Evidence | Acceptance Criteria |
| --- | --- | --- | --- |
| SAFE-001 | fixed | `execution/plan_gate.py` did not enforce single-direction bias, entry zone, or range checks for `bias=long/short`. | `PlanGate.can_open()` rejects wrong side and out-of-zone entries for all plan shapes. |
| SAFE-002 | fixed | `main.py` republished `shark:rl:action` as `shark:orders:new` without size, leverage, SL, TP, or plan gate checks. | RL actions are disabled by default and require complete validated size/leverage fields when enabled. |
| SAFE-003 | fixed | `executor/main.go` accepted any non-paper Redis command without schema validation or source authentication. | Executor validates required fields, mode, action, side, size, leverage, and optional command token before live execution. |
| SAFE-004 | fixed | `rl/webhook.go` exposed `/tv/alert`, `/rl/status`, and `/rl/patterns` without auth while compose publishes port `8081`. | Non-health RL endpoints require a configured token when `SHARK_API_TOKEN` or `RL_API_TOKEN` is set. |
| SAFE-005 | fixed | `rl/cmd/main.go` read `SHARK_REDIS_URL` then hardcoded `redis:6379`. | RL Redis client respects `SHARK_REDIS_URL`, including DB/password parsed by go-redis. |
| SAFE-006 | fixed | `rl/planning/planner.go` carried `LeverageCap` separately but did not clamp AI `Leverage`/`PositionSizePct`. | Stored plans clamp leverage and position sizing to bounded ranges before FastLoop consumes them. |

## Duplication And Drift

| ID | Status | Evidence | Acceptance Criteria |
| --- | --- | --- | --- |
| DRIFT-001 | fixed | `executor/main.go` defined unused `executePaper()` while `matcher/main.go` handles all paper commands. | Dead executor paper branch is removed. |
| DRIFT-002 | fixed | Order command structs and JSON creation were duplicated across Python, executor, and matcher. | Python creates orders through `execution/order_command.py`; executor and matcher validate their boundaries. |
| DRIFT-003 | fixed | `signal_engine.py` duplicated RangePlan direction logic that is currently inlined in `main.py`. | Deleted unused module and updated Docker/docs references. |
| DRIFT-004 | deferred | `oscillation.py` is documented as deprecated but remains active in `main.py` for averaging-down logic. | Deferred until the position-management path is split and tested; do not delete in this pass. |
| DRIFT-005 | fixed | ADR-0002 referenced `domain/trading`, but no `domain/` package exists. | ADR now documents `domain/trading` as a future target and points current code to `execution/order_command.py`. |
