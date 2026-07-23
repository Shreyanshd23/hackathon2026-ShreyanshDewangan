"""
Policy rules — the deterministic invariants
────────────────────────────────────────────
Each rule is a pure function: (tool, args, state) -> PolicyDecision | None.
  • Return None  → the rule doesn't apply / raises no objection.
  • Return a DENY / REQUIRE_HUMAN decision → the rule objects.

These encode business + safety invariants *in code*, so they hold no matter
what the LLM decides. This is the fix for the core flaw in the original
system, where "check eligibility before refunding" lived only in the prompt.

Read-only tools (get_order, get_customer, get_product, search_knowledge_base)
have no side effects and are intentionally governed by no rule → ALLOW.
"""

from __future__ import annotations

from typing import Any

from core.schemas import PolicyDecision, RiskLevel, TicketState, Verdict

# ── Tunable thresholds (kept here so policy is auditable in one place) ──
REFUND_HITL_THRESHOLD = 200.0     # refunds above this always go to a human
LOW_CONFIDENCE_THRESHOLD = 0.6    # below this, irreversible actions need a human
AMOUNT_TOLERANCE = 0.01           # float slack when comparing money

IRREVERSIBLE_TOOLS = {"issue_refund"}


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
#  Refund invariants (the high-stakes ones)
# ─────────────────────────────────────────────────────────────
def refund_requires_eligibility(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """issue_refund MUST be preceded by a successful eligibility check for the
    same order. Without it, we have no verified basis to move money."""
    if tool != "issue_refund":
        return None
    order_id = args.get("order_id")
    if not order_id or state.eligibility_result(order_id) is None:
        return PolicyDecision(
            verdict=Verdict.DENY,
            rule="refund_requires_eligibility",
            reason=(
                f"issue_refund for '{order_id}' blocked: no prior successful "
                "check_refund_eligibility for this order. Run the eligibility "
                "check first."
            ),
            risk=RiskLevel.CRITICAL,
        )
    return None


def refund_must_be_eligible(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """The eligibility check that ran must have returned eligible=True."""
    if tool != "issue_refund":
        return None
    order_id = args.get("order_id")
    elig = state.eligibility_result(order_id) if order_id else None
    if elig is not None and not elig.get("eligible", False):
        return PolicyDecision(
            verdict=Verdict.DENY,
            rule="refund_must_be_eligible",
            reason=(
                f"issue_refund for '{order_id}' blocked: eligibility check "
                f"returned eligible=false. Reason: {elig.get('reason', 'n/a')}"
            ),
            risk=RiskLevel.CRITICAL,
        )
    return None


def refund_amount_within_eligible(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """Cannot refund more than the eligibility check authorised."""
    if tool != "issue_refund":
        return None
    order_id = args.get("order_id")
    elig = state.eligibility_result(order_id) if order_id else None
    if not elig:
        return None  # handled by refund_requires_eligibility
    amount = _f(args.get("amount"))
    allowed = _f(elig.get("refundable_amount"))
    if amount is not None and allowed is not None and amount > allowed + AMOUNT_TOLERANCE:
        return PolicyDecision(
            verdict=Verdict.DENY,
            rule="refund_amount_within_eligible",
            reason=(
                f"issue_refund for '{order_id}' blocked: requested ${amount:.2f} "
                f"exceeds the eligible amount ${allowed:.2f}."
            ),
            risk=RiskLevel.CRITICAL,
        )
    return None


def refund_amount_positive(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """No zero or negative refunds."""
    if tool != "issue_refund":
        return None
    amount = _f(args.get("amount"))
    if amount is None or amount <= 0:
        return PolicyDecision(
            verdict=Verdict.DENY,
            rule="refund_amount_positive",
            reason=f"issue_refund blocked: invalid amount {args.get('amount')!r} (must be > 0).",
            risk=RiskLevel.HIGH,
        )
    return None


# ─────────────────────────────────────────────────────────────
#  Human-in-the-loop gates (safe, but too consequential to automate)
# ─────────────────────────────────────────────────────────────
def large_refund_needs_human(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """Even a valid, eligible refund goes to a human above the threshold."""
    if tool != "issue_refund":
        return None
    amount = _f(args.get("amount"))
    if amount is not None and amount > REFUND_HITL_THRESHOLD:
        return PolicyDecision(
            verdict=Verdict.REQUIRE_HUMAN,
            rule="large_refund_needs_human",
            reason=(
                f"Refund of ${amount:.2f} exceeds the ${REFUND_HITL_THRESHOLD:.0f} "
                "auto-approval limit; routing to a human for approval."
            ),
            risk=RiskLevel.HIGH,
        )
    return None


def low_confidence_blocks_irreversible(tool: str, args: dict, state: TicketState) -> PolicyDecision | None:
    """The self-awareness gate: when triage confidence is low, don't let the
    agent take an irreversible action on its own — hand off to a human."""
    if tool not in IRREVERSIBLE_TOOLS:
        return None
    if state.classification_confidence < LOW_CONFIDENCE_THRESHOLD:
        return PolicyDecision(
            verdict=Verdict.REQUIRE_HUMAN,
            rule="low_confidence_blocks_irreversible",
            reason=(
                f"Triage confidence {state.classification_confidence:.2f} is below "
                f"{LOW_CONFIDENCE_THRESHOLD:.2f}; irreversible action '{tool}' "
                "routed to a human."
            ),
            risk=RiskLevel.HIGH,
        )
    return None


# Evaluated in this order; the PolicyEngine takes the strongest verdict.
DEFAULT_RULES = [
    refund_requires_eligibility,
    refund_must_be_eligible,
    refund_amount_within_eligible,
    refund_amount_positive,
    large_refund_needs_human,
    low_confidence_blocks_irreversible,
]
