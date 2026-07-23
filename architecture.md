# Architecture

## Agent loop, tool design, state management

```
                         ┌─────────────────────────────────────────────┐
                         │            Flask server (SSE)                │
                         │  /api/process /stream /results /hitl /dlq    │
                         └───────────────────┬─────────────────────────┘
                                             │ start (background thread)
                         ┌───────────────────▼─────────────────────────┐
                         │  Orchestrator                                │
                         │  ThreadPoolExecutor(max_concurrent_tickets)  │
                         │  NO sleep(); throughput bounded by the       │
                         │  LLM token-bucket rate limiter (RPM + TPM)   │
                         └───────────────────┬─────────────────────────┘
             per ticket, concurrently        │
        ┌────────────────────────────────────▼─────────────────────────────────┐
        │  trace(ticket_id)  ── one OpenTelemetry trace per ticket               │
        │                                                                        │
        │  ① ReaderAgent      regex + word-sets (no LLM): order IDs, threat/     │
        │                     urgency signals                                    │
        │  ② ClassifierAgent  one LLM call → Pydantic Classification             │
        │                     {category, priority, confidence, ...}              │
        │  ③ ResolverAgent    ReAct loop (≤12 iters) over 8 tools                │
        │        for each proposed tool call:                                    │
        │           PolicyEngine.guard(tool, args, TicketState, executor)        │
        │             ├─ ALLOW         → resilient_execute() (retry+backoff)     │
        │             ├─ DENY          → blocked; policy-error fed back to LLM    │
        │             └─ REQUIRE_HUMAN → HITL queue; not executed                 │
        │        finishes on send_reply (resolve) or escalate (hand off)         │
        │  ④ HealthMonitor    degraded? needs_review? (self-confidence, errors)  │
        └────────────────────────────────────┬─────────────────────────────────┘
                                             │
                    Storage (SQLite): tickets · actions(audit) · dead_letter · idempotency
                                             │
                    audit_log.json  ◄── exported from the recorded spans/actions
```

## Why these choices

**Deterministic pipeline, agentic core.** Reader→Classifier→Resolver is a fixed
DAG (predictable, cheap, explainable). Only stage ③ is a true open-ended
tool-calling loop, which is where autonomy actually adds value.

**Policy in code, not in the prompt.** `TicketState` accumulates every tool
call. Before any tool runs, `PolicyEngine.evaluate()` reasons over that state.
The refund invariant — *a refund cannot execute unless a successful
`check_refund_eligibility` for that order already happened, the amount is within
the eligible figure, and it is ≤ the $200 auto-approval limit* — is therefore a
property of the code, provable by unit tests, not a hope about the model.
Verdict precedence is `DENY > REQUIRE_HUMAN > ALLOW`.

**Honest concurrency.** Tickets run in a real thread pool. The only throttle is
a token-bucket rate limiter modeling the provider's true limits (requests/min
and tokens/min), so fast tickets aren't blocked behind a hardcoded delay.

**State that survives.** SQLite holds ticket results, an append-only audit of
every tool call (with the policy verdict), a dead-letter queue for tickets that
crash, and an idempotency table so a retried irreversible action isn't applied
twice.

**Observability as the source of truth.** Every stage/tool/LLM call is a span
with a trace id, duration, attributes, and status. `audit_log.json` is a derived
export — the trace is the primary record.

**Self-awareness that acts.** The resolver emits a grounded `self_confidence`;
low confidence on an irreversible action is *routed to a human by policy*, and
the calibration report (ECE/Brier) measures whether those confidences mean
anything.

## Tool catalog

| Tool | Type | Governed by policy? |
|---|---|---|
| get_order, get_customer, get_product, search_knowledge_base | read | no (no side effects) |
| check_refund_eligibility | read | no (but its result is the precondition for refunds) |
| issue_refund | **write, irreversible** | **yes** — eligibility, amount, limit, confidence gates |
| send_reply | write | passes through (low risk) |
| escalate | write | passes through (the hand-off itself) |
