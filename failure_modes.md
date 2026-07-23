# Failure Modes & Resilience

Each scenario below is backed by code and, where noted, a test — not just a
description.

## 1. The model tries to move money it shouldn't
**Scenario:** the LLM hallucinates and calls `issue_refund` without checking
eligibility, on an ineligible order, for the wrong amount, or above the
approval limit.
**Handling:** the `PolicyEngine` ([policy/rules.py](policy/rules.py)) blocks it
*in code* before execution:
- `refund_requires_eligibility` — DENY if no prior successful eligibility check.
- `refund_must_be_eligible` — DENY if the check returned `eligible=false`.
- `refund_amount_within_eligible` — DENY if amount > eligible amount.
- `large_refund_needs_human` — refunds > $200 → human-in-the-loop queue.
The blocked call returns a `BLOCKED_BY_POLICY[...]` message to the model so it
can recover (run the missing step). **Tested:** `tests/test_policy.py`,
`tests/test_system.py::test_full_pipeline_no_safety_violations` (asserts zero
violations across all 20 tickets).

## 2. Transient tool failures (timeouts / "temporarily unavailable")
**Scenario:** a backend call fails intermittently (simulated by
`FAILURE_INJECTION_RATE`).
**Handling:** `resilient_execute` ([tools/resilient.py](tools/resilient.py))
retries only *transient* errors with exponential backoff, up to a budget.
Permanent errors ("not found", "already refunded") are returned immediately —
retrying them would be pointless. If the budget is exhausted, a clean error is
returned and the agent escalates. **Tested:**
`tests/test_system.py::test_transient_error_retried_then_exhausts`.

## 3. A ticket crashes entirely
**Scenario:** an unexpected exception kills processing for one ticket.
**Handling:** the orchestrator catches it per-ticket, writes the ticket to the
**dead-letter queue** in SQLite, marks it `failed`, and continues the rest.
Failed tickets are queryable at `/api/dlq` — they are never silently lost.
**Tested:** `tests/test_system.py::test_store_roundtrip_and_dlq`.

## 4. Provider rate limits (429s) under concurrency
**Scenario:** many tickets in flight exceed the provider's RPM/TPM.
**Handling:** a token-bucket rate limiter ([core/rate_limiter.py](core/rate_limiter.py))
reserves request + estimated-token capacity before each call, so concurrent
workers self-throttle below the ceiling instead of racing into 429s. The LLM
client also retries with backoff on transient API errors. **Tested:**
`tests/test_system.py::test_token_bucket_blocks_until_refill`.

## 5. Reasoning loops
**Scenario:** the agent repeats tool calls and never finishes.
**Handling:** a hard `MAX_ITER` cap (12). If hit, the ticket ends `incomplete`,
the `HealthMonitor` flags it `degraded` + `needs_review`, and it surfaces for a
human rather than burning tokens forever.

## 6. Low-confidence or ambiguous tickets
**Scenario:** triage confidence is low, or the ticket is genuinely ambiguous.
**Handling:** confidence is a bounded, validated field; the policy layer routes
*irreversible* actions to a human when confidence is below threshold, and the
calibration report ([monitoring/calibration.py](monitoring/calibration.py))
measures whether the confidences are trustworthy (ECE/Brier) instead of assuming
they are.

## 7. Malformed LLM output
**Scenario:** the classifier returns non-JSON, or tool arguments are garbage.
**Handling:** classifier output is coerced through a Pydantic model (falling
back to safe defaults); tool arguments that don't match a tool's signature
return a clean "bad arguments" error rather than crashing the loop.
