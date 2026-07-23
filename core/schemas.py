"""
Core data models (Pydantic v2)
──────────────────────────────
Single source of truth for the shapes that flow through the system.

Why Pydantic and not plain dicts:
  • Tool arguments come from an LLM and are therefore untrusted. Validating
    them here means a malformed `issue_refund(amount="lots")` is rejected at
    the boundary instead of silently becoming a no-op deep in the code.
  • The PolicyEngine reasons over `TicketState`. Making that state a typed
    object (not a loose dict) is what lets the guardrails be deterministic
    and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────
#  Enums
# ─────────────────────────────────────────────────────────────
class Verdict(str, Enum):
    """The three things the PolicyEngine can say about a proposed action."""
    ALLOW = "allow"                # safe, execute it
    DENY = "deny"                  # violates an invariant, block it outright
    REQUIRE_HUMAN = "require_human"  # too risky to automate, route to a human


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ─────────────────────────────────────────────────────────────
#  Tool call records — the audit primitives
# ─────────────────────────────────────────────────────────────
class ToolCallRecord(BaseModel):
    """One executed tool call and its outcome. This is the audit primitive."""
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True                      # False if result carried an "error" key
    timestamp: str = Field(default_factory=_utcnow)

    @staticmethod
    def is_error(result: Any) -> bool:
        """A tool result counts as an error iff it is a dict with an 'error' key
        or an explicit success=False."""
        if not isinstance(result, dict):
            return False
        return "error" in result or result.get("success") is False


# ─────────────────────────────────────────────────────────────
#  Per-ticket state — what the PolicyEngine reads
# ─────────────────────────────────────────────────────────────
class TicketState(BaseModel):
    """
    Accumulated, verified knowledge for a single ticket in flight.

    The Resolver appends a ToolCallRecord after every tool call. The
    PolicyEngine reads this to answer questions like "was eligibility for
    ORD-1001 successfully checked *before* this refund was proposed?" — the
    precondition that the original prompt-only design could not enforce.
    """
    ticket_id: str
    classification_confidence: float = 1.0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)

    def record(self, tool: str, arguments: dict, result: Any) -> ToolCallRecord:
        rec = ToolCallRecord(
            tool=tool,
            arguments=arguments or {},
            result=result if isinstance(result, dict) else {"value": result},
            ok=not ToolCallRecord.is_error(result),
        )
        self.tool_calls.append(rec)
        return rec

    def successful_calls(self, tool: str) -> list[ToolCallRecord]:
        return [c for c in self.tool_calls if c.tool == tool and c.ok]

    def eligibility_result(self, order_id: str) -> dict | None:
        """Return the result of the most recent *successful* eligibility check
        for this order_id, or None if none has been performed."""
        for call in reversed(self.tool_calls):
            if (
                call.tool == "check_refund_eligibility"
                and call.ok
                and call.arguments.get("order_id") == order_id
            ):
                return call.result
        return None


# ─────────────────────────────────────────────────────────────
#  Policy decision — the PolicyEngine's output
# ─────────────────────────────────────────────────────────────
class PolicyDecision(BaseModel):
    """The verdict on a single proposed tool call. Fully explainable:
    every field is human-readable and logged verbatim to the audit trail."""
    verdict: Verdict
    rule: str                        # which rule fired, e.g. "refund_requires_eligibility"
    reason: str                      # plain-English justification
    risk: RiskLevel = RiskLevel.LOW
    timestamp: str = Field(default_factory=_utcnow)

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW

    def as_tool_error(self) -> dict:
        """When a call is blocked, this is what the tool layer returns to the
        LLM in place of the real result — so the model learns why and can
        adapt (e.g. run the eligibility check it skipped)."""
        return {
            "error": f"BLOCKED_BY_POLICY[{self.rule}]: {self.reason}",
            "verdict": self.verdict.value,
        }


# ─────────────────────────────────────────────────────────────
#  Pipeline I/O models (validate untrusted / cross-stage data)
# ─────────────────────────────────────────────────────────────
class Classification(BaseModel):
    """Validated output of the ClassifierAgent. Coerces the LLM's JSON into a
    known shape so downstream code never guesses at missing keys."""
    category: str = "ambiguous"
    priority: str = "medium"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    can_auto_resolve: bool = False
    reasoning: str = ""


class ResolutionResult(BaseModel):
    """Validated output of the ResolverAgent."""
    ticket_id: str
    status: str                       # resolved | escalated | incomplete
    tool_calls: int = 0
    tools_used: list[str] = Field(default_factory=list)
    audit_trail: list[dict] = Field(default_factory=list)
    policy_decisions: list[dict] = Field(default_factory=list)
    self_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    final_text: str = ""
    resolved: bool = False
    escalated: bool = False
    health: str = "ok"                # ok | degraded — set by the HealthMonitor


class TicketResult(BaseModel):
    """The full per-ticket record persisted and streamed to the UI."""
    ticket_id: str
    subject: str = ""
    customer_email: str = ""
    body: str = ""
    expected_action: str = ""
    status: str = "pending"
    classification: Classification | None = None
    resolution: ResolutionResult | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    timestamp: str = Field(default_factory=_utcnow)
