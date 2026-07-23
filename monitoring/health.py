"""
HealthMonitor — live self-assessment of each resolution
─────────────────────────────────────────────────────────
Watches the observable signals of a completed resolution and decides whether
it went well or is "degraded" and should be flagged for human review. This is
the agent *acting on* its own uncertainty, not merely displaying it:

  • incomplete (never reached a terminal action)  → degraded
  • low self-confidence                            → degraded
  • high tool error rate                           → degraded
  • the loop ran to its iteration cap              → degraded
  • policy had to block an action                  → flag for review

Kept deliberately rule-based and transparent so every "degraded" verdict is
explainable — no black-box health score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import settings

LOW_CONFIDENCE = 0.6
HIGH_ERROR_RATE = 0.34


@dataclass
class HealthVerdict:
    health: str                       # "ok" | "degraded"
    needs_review: bool
    reasons: list[str] = field(default_factory=list)


class HealthMonitor:
    @staticmethod
    def assess(resolution: dict) -> HealthVerdict:
        reasons: list[str] = []
        calls = resolution.get("tool_calls", 0) or 0
        audit = resolution.get("audit_trail", []) or []
        errors = sum(1 for e in audit if isinstance(e.get("result"), dict) and "error" in e["result"])
        error_rate = (errors / calls) if calls else 0.0

        if resolution.get("status") == "incomplete":
            reasons.append("Resolution never reached a terminal action (send_reply/escalate).")
        if resolution.get("self_confidence", 1.0) < LOW_CONFIDENCE:
            reasons.append(f"Self-confidence {resolution.get('self_confidence')} below {LOW_CONFIDENCE}.")
        if error_rate > HIGH_ERROR_RATE:
            reasons.append(f"Tool error rate {error_rate:.0%} exceeds {HIGH_ERROR_RATE:.0%}.")
        if calls >= settings.max_tool_iterations:
            reasons.append("Loop reached the iteration cap — possible reasoning loop.")

        policy_blocks = resolution.get("policy_decisions", []) or []
        if policy_blocks:
            reasons.append(f"Policy intervened on {len(policy_blocks)} action(s).")

        # 'degraded' if any *quality* signal tripped. A clean policy-gated
        # escalation is healthy, so policy blocks alone only flag for review.
        quality_tripped = any(
            "terminal action" in r or "Self-confidence" in r or "error rate" in r or "iteration cap" in r
            for r in reasons
        )
        return HealthVerdict(
            health="degraded" if quality_tripped else "ok",
            needs_review=bool(reasons),
            reasons=reasons,
        )
