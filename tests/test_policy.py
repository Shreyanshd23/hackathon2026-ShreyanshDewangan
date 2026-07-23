"""
Tests for the deterministic policy / guardrail layer.

These are the tests that prove the core safety claim: an irreversible refund
cannot be executed unless the invariants hold in code — regardless of what
the LLM proposes. Run with:  pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import TicketState, Verdict
from policy.engine import PolicyEngine
from policy.rules import LOW_CONFIDENCE_THRESHOLD, REFUND_HITL_THRESHOLD


# ── helpers ──────────────────────────────────────────────────
def fresh_state(confidence: float = 1.0) -> TicketState:
    return TicketState(ticket_id="TKT-TEST", classification_confidence=confidence)


def add_eligibility(state: TicketState, order_id: str, eligible: bool, amount: float | None = None):
    result = {"eligible": eligible, "order_id": order_id, "reason": "test"}
    if amount is not None:
        result["refundable_amount"] = amount
    state.record("check_refund_eligibility", {"order_id": order_id}, result)


# ── read-only tools are always allowed ───────────────────────
@pytest.mark.parametrize("tool", ["get_order", "get_customer", "get_product", "search_knowledge_base"])
def test_read_tools_allowed(tool):
    engine = PolicyEngine()
    d = engine.evaluate(tool, {"order_id": "ORD-1001"}, fresh_state())
    assert d.verdict == Verdict.ALLOW


# ── the headline invariant: no refund without an eligibility check ──
def test_refund_without_eligibility_is_denied():
    engine = PolicyEngine()
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": 50}, fresh_state())
    assert d.verdict == Verdict.DENY
    assert d.rule == "refund_requires_eligibility"


def test_refund_after_eligible_check_is_allowed():
    engine = PolicyEngine()
    state = fresh_state()
    add_eligibility(state, "ORD-1001", eligible=True, amount=100.0)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": 100.0}, state)
    assert d.verdict == Verdict.ALLOW


def test_refund_when_ineligible_is_denied():
    engine = PolicyEngine()
    state = fresh_state()
    add_eligibility(state, "ORD-1001", eligible=False)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": 50.0}, state)
    assert d.verdict == Verdict.DENY
    assert d.rule == "refund_must_be_eligible"


def test_refund_over_eligible_amount_is_denied():
    engine = PolicyEngine()
    state = fresh_state()
    add_eligibility(state, "ORD-1001", eligible=True, amount=100.0)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": 150.0}, state)
    assert d.verdict == Verdict.DENY
    assert d.rule == "refund_amount_within_eligible"


@pytest.mark.parametrize("amount", [0, -10, "not-a-number", None])
def test_invalid_refund_amount_is_denied(amount):
    engine = PolicyEngine()
    state = fresh_state()
    add_eligibility(state, "ORD-1001", eligible=True, amount=100.0)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": amount}, state)
    assert d.verdict == Verdict.DENY


# ── human-in-the-loop gates ──────────────────────────────────
def test_large_refund_requires_human():
    engine = PolicyEngine()
    state = fresh_state()
    big = REFUND_HITL_THRESHOLD + 50
    add_eligibility(state, "ORD-1001", eligible=True, amount=big)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": big}, state)
    assert d.verdict == Verdict.REQUIRE_HUMAN
    assert d.rule == "large_refund_needs_human"


def test_deny_beats_require_human():
    # Large refund (REQUIRE_HUMAN) but NO eligibility check (DENY) -> DENY wins.
    engine = PolicyEngine()
    d = engine.evaluate("issue_refund", {"order_id": "ORD-9", "amount": 999}, fresh_state())
    assert d.verdict == Verdict.DENY


def test_low_confidence_routes_irreversible_to_human():
    engine = PolicyEngine()
    state = fresh_state(confidence=LOW_CONFIDENCE_THRESHOLD - 0.1)
    add_eligibility(state, "ORD-1001", eligible=True, amount=50.0)
    d = engine.evaluate("issue_refund", {"order_id": "ORD-1001", "amount": 50.0}, state)
    assert d.verdict == Verdict.REQUIRE_HUMAN
    assert d.rule == "low_confidence_blocks_irreversible"


# ── guarded execution end-to-end ─────────────────────────────
def test_guard_allows_and_executes():
    engine = PolicyEngine()
    state = fresh_state()
    add_eligibility(state, "ORD-1001", eligible=True, amount=100.0)
    calls = []

    def executor(tool, args):
        calls.append((tool, args))
        return {"success": True}

    result, decision = engine.guard("issue_refund", {"order_id": "ORD-1001", "amount": 100.0}, state, executor)
    assert decision.verdict == Verdict.ALLOW
    assert result == {"success": True}
    assert calls == [("issue_refund", {"order_id": "ORD-1001", "amount": 100.0})]


def test_guard_denies_and_does_not_execute():
    engine = PolicyEngine()
    state = fresh_state()
    executed = []

    def executor(tool, args):
        executed.append(tool)
        return {"success": True}

    result, decision = engine.guard("issue_refund", {"order_id": "ORD-1", "amount": 50}, state, executor)
    assert decision.verdict == Verdict.DENY
    assert executed == []                      # the money-moving tool never ran
    assert "BLOCKED_BY_POLICY" in result["error"]


def test_guard_require_human_enqueues_and_skips_execution():
    engine = PolicyEngine()
    state = fresh_state()
    big = REFUND_HITL_THRESHOLD + 100
    add_eligibility(state, "ORD-1001", eligible=True, amount=big)
    executed = []

    def executor(tool, args):
        executed.append(tool)
        return {"success": True}

    result, decision = engine.guard("issue_refund", {"order_id": "ORD-1001", "amount": big}, state, executor)
    assert decision.verdict == Verdict.REQUIRE_HUMAN
    assert executed == []
    assert result["status"] == "pending_human_review"
    assert len(engine.hitl.pending()) == 1
    assert engine.hitl.pending()[0]["ticket_id"] == "TKT-TEST"
