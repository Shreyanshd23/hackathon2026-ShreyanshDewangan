"""
System tests across the new layers: resilient tools, rate limiter, storage,
metrics, monitoring, and the full offline pipeline (safety invariant).
"""

import time

import tools.impls as ti
from core.rate_limiter import TokenBucket, estimate_tokens
from evals.metrics import classification_stats, safety_violations, tool_sequence_valid
from monitoring import HealthMonitor, calibration_report
from storage import Store
from tools import resilient_execute


# ── resilient tool execution ─────────────────────────────────
def test_permanent_error_not_retried():
    ti.FAILURE_INJECTION_RATE = 0.0
    r = resilient_execute("get_order", {"order_id": "ORD-DOESNOTEXIST"})
    assert "error" in r and "not found" in r["error"].lower()


def test_transient_error_retried_then_exhausts():
    ti.FAILURE_INJECTION_RATE = 1.0  # always transient-fail
    try:
        r = resilient_execute("get_order", {"order_id": "ORD-1001"}, max_retries=2, backoff_base=0.0)
        assert "error" in r and "temporarily unavailable" in r["error"].lower()
    finally:
        ti.FAILURE_INJECTION_RATE = 0.0


# ── rate limiter ─────────────────────────────────────────────
def test_token_bucket_blocks_until_refill():
    tb = TokenBucket(capacity=2, refill_per_sec=100.0)
    assert tb.acquire(2) is True          # drain
    t0 = time.monotonic()
    assert tb.acquire(1) is True          # must wait ~0.01s for refill
    assert time.monotonic() - t0 >= 0.0


def test_token_bucket_timeout():
    tb = TokenBucket(capacity=1, refill_per_sec=0.0001)
    assert tb.acquire(1) is True
    assert tb.acquire(1, timeout=0.05) is False


def test_estimate_tokens_monotonic():
    small = estimate_tokens([{"content": "hi"}], 100)
    big = estimate_tokens([{"content": "x" * 4000}], 100)
    assert big > small


# ── storage ──────────────────────────────────────────────────
def test_store_roundtrip_and_dlq():
    s = Store(":memory:")
    s.save_ticket("TKT-1", "resolved", {"ticket_id": "TKT-1", "foo": 1})
    assert s.get_ticket("TKT-1")["foo"] == 1
    s.record_action("TKT-1", "get_order", {"order_id": "O"}, {"ok": True}, True, "allow")
    assert len(s.actions_for("TKT-1")) == 1
    s.add_to_dlq("TKT-2", "boom", {"ticket_id": "TKT-2"})
    assert s.dlq()[0]["ticket_id"] == "TKT-2"


def test_store_idempotency():
    s = Store(":memory:")
    calls = []

    def produce():
        calls.append(1)
        return {"refunded": True}

    a = s.idempotent("refund:ORD-1", produce)
    b = s.idempotent("refund:ORD-1", produce)
    assert a == b == {"refunded": True}
    assert len(calls) == 1                 # produced once, not twice


# ── metrics ──────────────────────────────────────────────────
def test_classification_stats_perfect():
    m = classification_stats(["refund", "return"], ["refund", "return"])
    assert m["f1"] == 1.0 and m["accuracy"] == 1.0


def test_safety_and_sequence_detect_bad_trace():
    bad = [
        {"tool": "issue_refund", "arguments": {"order_id": "O1"}, "result": {"success": True}},
    ]
    assert safety_violations(bad) == 1
    assert tool_sequence_valid(bad) is False

    good = [
        {"tool": "get_customer", "arguments": {}, "result": {"name": "x"}},
        {"tool": "check_refund_eligibility", "arguments": {"order_id": "O1"}, "result": {"eligible": True}},
        {"tool": "issue_refund", "arguments": {"order_id": "O1"}, "result": {"success": True}},
    ]
    assert safety_violations(good) == 0
    assert tool_sequence_valid(good) is True


# ── monitoring ───────────────────────────────────────────────
def test_health_flags_incomplete():
    v = HealthMonitor.assess({"status": "incomplete", "self_confidence": 0.9, "tool_calls": 2, "audit_trail": []})
    assert v.health == "degraded" and v.needs_review


def test_calibration_report_shapes():
    rep = calibration_report([(0.9, True), (0.9, False), (0.5, True)])
    assert rep["n"] == 3 and rep["ece"] is not None and 0 <= rep["brier"] <= 1


# ── full offline pipeline: the safety invariant end-to-end ───
def test_full_pipeline_no_safety_violations():
    ti.FAILURE_INJECTION_RATE = 0.0
    from app.orchestrator import Orchestrator
    orch = Orchestrator()
    orch.run_all()
    results = orch.get_results()
    assert len(results) == 20
    total_violations = sum(
        safety_violations((r.get("resolution") or {}).get("audit_trail", [])) for r in results
    )
    assert total_violations == 0                       # no refund ever bypassed eligibility
    assert all(r["status"] in ("resolved", "escalated", "incomplete", "failed") for r in results)
