"""
PolicyEngine
─────────────
The deterministic gate that sits between the agent and every tool call.

Two responsibilities, kept separate so they stay testable:
  • evaluate(...)  — PURE. Runs the rules and returns a PolicyDecision.
                     No side effects, so it is trivial to unit-test.
  • guard(...)     — INTEGRATION. Uses evaluate() to decide whether to run
                     the real tool, block it, or park it for a human.

Verdict precedence when several rules fire: DENY > REQUIRE_HUMAN > ALLOW.
A hard invariant violation (DENY) always wins over a "needs a human"
(REQUIRE_HUMAN), which is why a $500 refund with no eligibility check is
DENIED (fix the process) rather than merely queued.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.schemas import PolicyDecision, TicketState, Verdict
from policy.hitl import HITLQueue
from policy.rules import DEFAULT_RULES

Rule = Callable[[str, dict, TicketState], PolicyDecision | None]

_PRECEDENCE = {Verdict.DENY: 3, Verdict.REQUIRE_HUMAN: 2, Verdict.ALLOW: 1}


class PolicyEngine:
    def __init__(self, rules: list[Rule] | None = None, hitl: HITLQueue | None = None) -> None:
        self.rules: list[Rule] = rules if rules is not None else list(DEFAULT_RULES)
        self.hitl = hitl or HITLQueue()

    # ── pure evaluation ──────────────────────────────────────
    def evaluate(self, tool: str, args: dict, state: TicketState) -> PolicyDecision:
        """Run every rule and return the strongest objection, or ALLOW."""
        objections = [d for d in (rule(tool, args, state) for rule in self.rules) if d is not None]
        if not objections:
            return PolicyDecision(
                verdict=Verdict.ALLOW,
                rule="default_allow",
                reason=f"No policy restricts '{tool}'.",
            )
        return max(objections, key=lambda d: _PRECEDENCE[d.verdict])

    # ── guarded execution (integration seam) ─────────────────
    def guard(
        self,
        tool: str,
        args: dict,
        state: TicketState,
        executor: Callable[[str, dict], Any],
    ) -> tuple[dict, PolicyDecision]:
        """
        Evaluate the proposed call, then act on the verdict:

          ALLOW         → run `executor`, record the result in state, return it.
          DENY          → do NOT run; return a policy-error result to the LLM.
          REQUIRE_HUMAN → do NOT run; enqueue for approval; return a
                          "pending human review" result to the LLM.

        Returns (result_dict, decision) so the caller can both feed the result
        back to the model and log the decision to the audit trail.
        """
        decision = self.evaluate(tool, args, state)

        if decision.verdict == Verdict.ALLOW:
            result = executor(tool, args)
            state.record(tool, args, result)
            return result, decision

        if decision.verdict == Verdict.REQUIRE_HUMAN:
            item = self.hitl.enqueue(state.ticket_id, tool, args, decision)
            result = {
                "status": "pending_human_review",
                "message": (
                    "This action requires human approval and has been routed to "
                    f"a specialist (ref {item['id']}). Reason: {decision.reason}"
                ),
                "hitl_ref": item["id"],
            }
            # Record the *attempt* (as not-ok) so the audit trail is complete.
            state.record(tool, args, {"error": "deferred_to_human", "hitl_ref": item["id"]})
            return result, decision

        # DENY
        result = decision.as_tool_error()
        state.record(tool, args, result)
        return result, decision
