"""
Orchestrator — concurrent ticket processing
──────────────────────────────────────────────
The honest-concurrency core. Tickets run in a real thread pool; there is NO
`time.sleep()` between submissions. Throughput is bounded by the token-bucket
rate limiter inside the LLM client, which is the actual constraint. So slow,
token-heavy tickets naturally hold back fast ones without a hardcoded delay.

Per ticket: Reader → Classifier → Resolver (policy-gated, resilient, traced) →
HealthMonitor. Results and the full audit trail are persisted to SQLite; a
ticket that crashes lands in the dead-letter queue instead of vanishing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from agents import ClassifierAgent, ReaderAgent, ResolverAgent
from core.config import settings
from core.schemas import TicketResult
from core.telemetry import log_event, tracer
from monitoring import HealthMonitor
from policy import PolicyEngine
from storage import Store


class Orchestrator:
    def __init__(self) -> None:
        self.engine = PolicyEngine()
        self.store = Store(settings.db_path)
        self._results: dict[str, dict] = {}
        self._subscribers: list = []
        self._lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._running = False

    # ── SSE pub/sub ──────────────────────────────────────────
    def subscribe(self, q) -> None:
        with self._sub_lock:
            self._subscribers.append(q)

    def unsubscribe(self, q) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    # ── accessors ────────────────────────────────────────────
    def is_running(self) -> bool:
        return self._running

    def get_results(self) -> list[dict]:
        with self._lock:
            return list(self._results.values())

    def _load_tickets(self) -> list[dict]:
        with open(os.path.join(settings.data_dir, "tickets.json"), encoding="utf-8") as f:
            return json.load(f)

    # ── single ticket ────────────────────────────────────────
    def _process_one(self, ticket: dict) -> dict:
        tid = ticket["ticket_id"]
        t0 = time.monotonic()
        with tracer.trace(trace_id=tid, ticket_id=tid):
            try:
                self.broadcast({"type": "agent_update", "ticket_id": tid, "agent": "reader", "status": "running"})
                with tracer.span("agent.reader", ticket_id=tid):
                    parsed = ReaderAgent.run(ticket)
                self.broadcast({"type": "agent_update", "ticket_id": tid, "agent": "reader", "status": "complete",
                                "data": {k: parsed[k] for k in ("extracted_order_ids", "has_threatening_language", "has_urgency_signals")}})

                self.broadcast({"type": "agent_update", "ticket_id": tid, "agent": "classifier", "status": "running"})
                classification = ClassifierAgent.run(parsed)
                self.broadcast({"type": "agent_update", "ticket_id": tid, "agent": "classifier", "status": "complete",
                                "data": classification.model_dump()})

                self.broadcast({"type": "agent_update", "ticket_id": tid, "agent": "resolver", "status": "running"})
                resolution = ResolverAgent.run(
                    parsed, classification, self.engine, store=self.store,
                    callback=lambda entry: self.broadcast({"type": "tool_call", "ticket_id": tid, **entry}),
                )
                verdict = HealthMonitor.assess(resolution.model_dump())
                resolution.health = verdict.health

                elapsed = round(time.monotonic() - t0, 2)
                result = TicketResult(
                    ticket_id=tid, subject=ticket.get("subject", ""),
                    customer_email=ticket.get("customer_email", ""), body=ticket.get("body", ""),
                    expected_action=ticket.get("expected_action", ""),
                    status=resolution.status, classification=classification, resolution=resolution,
                    elapsed_seconds=elapsed,
                ).model_dump()
                result["health"] = verdict.health
                result["needs_review"] = verdict.needs_review
                result["health_reasons"] = verdict.reasons

                self.store.save_ticket(tid, resolution.status, result)
                self.broadcast({"type": "ticket_done", "ticket_id": tid, "status": resolution.status,
                                "elapsed": elapsed, "classification": classification.model_dump(),
                                "tool_calls": resolution.tool_calls, "health": verdict.health})
                return result

            except Exception as exc:  # noqa: BLE001 — a crashed ticket must not vanish
                elapsed = round(time.monotonic() - t0, 2)
                tb = traceback.format_exc()
                log_event("ticket_failed", ticket_id=tid, error=str(exc))
                payload = {"ticket_id": tid, "subject": ticket.get("subject", ""),
                           "status": "failed", "error": str(exc), "traceback": tb, "elapsed_seconds": elapsed}
                self.store.add_to_dlq(tid, str(exc), payload)      # dead-letter, never lost
                self.store.save_ticket(tid, "failed", payload)
                self.broadcast({"type": "ticket_done", "ticket_id": tid, "status": "failed", "elapsed": elapsed, "error": str(exc)})
                return payload

    # ── run all ──────────────────────────────────────────────
    def run_all(self) -> dict:
        if self._running:
            return {"error": "Already running"}
        self._running = True
        with self._lock:
            self._results.clear()
        try:
            tickets = self._load_tickets()
            self.broadcast({"type": "start", "total": len(tickets)})
            # Real concurrency; the LLM rate limiter throttles — no sleep hack.
            with ThreadPoolExecutor(max_workers=settings.max_concurrent_tickets) as pool:
                futures = {pool.submit(self._process_one, t): t["ticket_id"] for t in tickets}
                for fut in as_completed(futures):
                    r = fut.result()
                    with self._lock:
                        self._results[r["ticket_id"]] = r
            stats = self._summary()
            self.broadcast({"type": "complete", "stats": stats})
            self._export_audit_log()
            return stats
        finally:
            self._running = False

    def _summary(self) -> dict:
        results = self.get_results()
        n = len(results)
        by = lambda s: sum(1 for r in results if r.get("status") == s)  # noqa: E731
        return {
            "total": n,
            "resolved": by("resolved"), "escalated": by("escalated"),
            "failed": by("failed"), "incomplete": by("incomplete"),
            "degraded": sum(1 for r in results if r.get("health") == "degraded"),
            "avg_time": round(sum(r.get("elapsed_seconds", 0) for r in results) / max(n, 1), 2),
            "total_tool_calls": sum((r.get("resolution") or {}).get("tool_calls", 0) for r in results),
            "dead_letter": len(self.store.dlq()),
        }

    def _export_audit_log(self) -> None:
        try:
            with open(os.path.join(settings.data_dir, "audit_log.json"), "w", encoding="utf-8") as f:
                json.dump(self.get_results(), f, indent=2, default=str)
            log_event("audit_log_saved", tickets=len(self._results))
        except Exception as exc:  # noqa: BLE001
            log_event("audit_log_save_failed", error=str(exc))


# module-level singleton used by the server
orchestrator = Orchestrator()
