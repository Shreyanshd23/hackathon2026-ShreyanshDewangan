# ShopWave — Autonomous, Policy-Governed Support Agent

An autonomous customer-support agent that ingests tickets, triages them, and
resolves them by chaining tool calls — with a **deterministic safety layer**
that governs every irreversible action, full **observability**, durable
**state**, and an offline **evaluation harness** wired into CI.

The design goal was not "a demo that works once." It was: *treat it like it's
going to production and let a hallucinating model loose on real money — and
have it be safe anyway.*

---

## Why this is not a typical agent demo

| Concern | What most demos do | What this does |
|---|---|---|
| Irreversible actions | Prompt says "check first" | **PolicyEngine enforces it in code** — a refund cannot execute without a prior successful eligibility check; verified by tests |
| Concurrency | `sleep()` between calls | Real thread pool throttled by a **token-bucket rate limiter** (RPM + TPM) |
| Failure handling | try/except, ticket lost | **Retry budget + backoff**, and a **dead-letter queue** so nothing vanishes |
| State | in-memory globals | **SQLite**: tickets, append-only audit, DLQ, idempotency |
| Observability | `print()` | **OpenTelemetry-style spans** + structured JSON logs; the audit log is a trace export |
| "Knows what it doesn't know" | a slogan | **Self-confidence** score + calibration (ECE/Brier) + confidence-gated human hand-off |
| Evaluation | scores itself vs the rubric | **Golden dataset + deterministic metrics**, gating CI |

---

## Architecture

```
Ingest → Orchestrator (ThreadPool, rate-limited) 
             │
             ▼
   Reader → Classifier(LLM) → Resolver (ReAct tool loop)
                                   │
                     ┌── PolicyEngine.guard() ──┐   ALLOW → run · DENY → block
                     │  deterministic invariants │   REQUIRE_HUMAN → HITL queue
                     └────────────┬──────────────┘
                                  ▼
                  resilient_execute() — retry + backoff
                                  │
   Storage (SQLite): tickets · actions · dead_letter · idempotency
   Cross-cutting: OpenTelemetry spans + structured logs
             │
   HealthMonitor → per-ticket health + needs_review
             ▼
   Offline evals/ (golden set + deterministic metrics + LLM judge) → CI gate
```

See [architecture.md](architecture.md) for the full component walkthrough and
the agent loop.

## Repository layout

```
app/         orchestrator (concurrent) + hardened Flask server
agents/      reader · classifier · resolver
tools/       impls · schemas (definitions) · resilient wrapper
policy/      engine · rules (invariants) · hitl queue        ← the safety story
monitoring/  health monitor · confidence calibration
core/        config · schemas (pydantic) · llm_client (+offline mock) · rate_limiter · telemetry
storage/     sqlite store (tickets/audit/DLQ/idempotency)
evals/       golden.json · metrics · runner (CI gate)
prompts/     versioned system prompts (markdown)
mcp_server/  optional MCP adapter (same policy-gated tools)
tests/       pytest suite (policy + system + pipeline)
docker/      Dockerfile · compose · gunicorn config
```

## Quickstart

```bash
pip install -r requirements.txt

# Fully offline — no API keys needed (deterministic mock LLM):
OFFLINE_MODE=1 python run.py --eval        # run 20 tickets + print scorecard
OFFLINE_MODE=1 python run.py               # web dashboard at http://127.0.0.1:5000

# With real models:
cp .env.example .env    # add GROQ_API_KEY (+ GEMINI_API_KEY for the judge)
python run.py
```

Entry points:
- `python run.py` — web dashboard (SSE live audit trail)
- `python run.py --headless` — process all 20 tickets once, write `audit_log.json`
- `python run.py --eval` — headless run + scored evaluation
- `python -m evals.runner --gate` — the CI evaluation gate
- `python -m mcp_server.server` — expose the tools over MCP (needs `pip install mcp`)

## Docker

```bash
docker compose -f docker/docker-compose.yaml up --build
```
Runs under gunicorn as a non-root user with a healthcheck; `debug` is off and
CORS is restricted to an explicit allow-list.

## Testing & CI

```bash
ruff check .          # lint
pytest -q             # unit + system tests (offline, deterministic)
python -m evals.runner --gate   # eval gate: safety, action-correctness, tool-sequence
```
GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs all
three on every push/PR. The eval gate fails the build on any safety violation,
a missing terminal action, an invalid tool sequence, or a classification-F1
regression.

## Security

- No secrets in the repo; keys come from the environment / `.env` only.
- Flask debug is **off** by default (the original had the Werkzeug debugger on
  → RCE); enable locally with `FLASK_DEBUG=1`.
- CORS is restricted to `ALLOWED_ORIGINS`, not a wildcard.
- Irreversible actions are gated by the PolicyEngine, not by trust in the model.

## Tech stack

Python 3.12 · Flask + gunicorn · Groq (Llama-3.3-70B) reasoning · Gemini judge
· Pydantic v2 · SQLite · OpenTelemetry (optional) · pytest · ruff · Docker.
