"""
ResolverAgent — the agentic tool-calling loop
───────────────────────────────────────────────
A ReAct-style loop that reasons, calls tools, and finishes with a terminal
action (send_reply or escalate). Every tool call goes through the PolicyEngine
(so it can be blocked or routed to a human) and the resilient executor (so
transient failures are retried), and every step is traced.

At the end the agent produces a *self-assessed confidence* — its own estimate
of whether it handled the ticket correctly, derived from concrete signals
(triage confidence, whether it reached a terminal action, tool error rate,
whether policy caught a mistake). The monitoring layer acts on this number.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import prompts
from core import llm_client
from core.schemas import Classification, ResolutionResult, TicketState, Verdict
from core.telemetry import tracer
from policy.engine import PolicyEngine
from tools import TOOL_DEFINITIONS, resilient_execute

TERMINAL_TOOLS = {"send_reply", "escalate"}


class ResolverAgent:
    MAX_ITER = 12

    @staticmethod
    def run(
        parsed: dict,
        classification: Classification,
        engine: PolicyEngine,
        store=None,
        callback: Callable[[dict], None] | None = None,
    ) -> ResolutionResult:
        tid = parsed["ticket_id"]
        state = TicketState(ticket_id=tid, classification_confidence=classification.confidence)

        context = {
            "ticket_id": tid,
            "customer_email": parsed["customer_email"],
            "subject": parsed["subject"],
            "body": parsed["body"],
            "extracted_order_ids": parsed["extracted_order_ids"],
            "has_threatening_language": parsed["has_threatening_language"],
            "classification": classification.model_dump(),
        }
        messages = [
            {"role": "system", "content": prompts.load("resolver")},
            {"role": "user", "content": f"Process this support ticket:\n```json\n{json.dumps(context, indent=2)}\n```"},
        ]

        audit_trail: list[dict] = []
        policy_decisions: list[dict] = []
        tool_calls_made = 0
        errors = 0
        policy_blocks = 0
        terminal_reached = False
        final_text = ""

        for _ in range(ResolverAgent.MAX_ITER):
            try:
                with tracer.span("agent.resolver.llm", ticket_id=tid):
                    msg = llm_client.chat(messages, tools=TOOL_DEFINITIONS)
            except Exception as exc:  # noqa: BLE001
                audit_trail.append({"step": tool_calls_made + 1, "action": "llm_error", "error": str(exc)})
                errors += 1
                break

            if not msg.tool_calls:
                final_text = msg.content or ""
                if not terminal_reached and tool_calls_made < 3:
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "user", "content":
                        "You have not reached a resolution. Continue calling tools and finish with send_reply or escalate."})
                    continue
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })

            for tc in msg.tool_calls:
                fname = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                # Policy gate + resilient execution, both inside one span.
                with tracer.span("tool.call", tool=fname, ticket_id=tid) as span:
                    result, decision = engine.guard(fname, args, state, executor=resilient_execute)
                    span.set(verdict=decision.verdict.value, rule=decision.rule)

                tool_calls_made += 1
                ok = not (isinstance(result, dict) and ("error" in result or result.get("success") is False))
                if not ok:
                    errors += 1
                if decision.verdict != Verdict.ALLOW:
                    policy_blocks += 1
                    policy_decisions.append({"tool": fname, "verdict": decision.verdict.value,
                                             "rule": decision.rule, "reason": decision.reason})

                entry = {"step": tool_calls_made, "tool": fname, "arguments": args, "result": result,
                         "verdict": decision.verdict.value, "rule": decision.rule}
                audit_trail.append(entry)
                if store:
                    store.record_action(tid, fname, args, result, ok, decision.verdict.value)
                if callback:
                    callback(entry)

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

                # A terminal tool counts only if it actually executed (was ALLOWed).
                if fname in TERMINAL_TOOLS and decision.verdict == Verdict.ALLOW:
                    terminal_reached = True

            if terminal_reached:
                try:
                    summary = llm_client.chat(messages, tools=None)
                    final_text = summary.content or ""
                except Exception:  # noqa: BLE001
                    pass
                break

        tools_used = [e["tool"] for e in audit_trail if "tool" in e]
        resolved = any(e.get("tool") == "send_reply" and "error" not in e.get("result", {}) for e in audit_trail)
        escalated = any(e.get("tool") == "escalate" and "error" not in e.get("result", {}) for e in audit_trail)
        status = "escalated" if escalated else ("resolved" if resolved else "incomplete")

        return ResolutionResult(
            ticket_id=tid,
            status=status,
            tool_calls=tool_calls_made,
            tools_used=tools_used,
            audit_trail=audit_trail,
            policy_decisions=policy_decisions,
            self_confidence=_self_confidence(classification.confidence, terminal_reached, tool_calls_made, errors, policy_blocks),
            final_text=final_text,
            resolved=resolved,
            escalated=escalated,
        )


def _self_confidence(base: float, terminal: bool, calls: int, errors: int, policy_blocks: int) -> float:
    """The agent's own estimate that it handled the ticket correctly.
    Grounded in observable signals, not vibes — this is what makes the
    'knows what it doesn't know' claim measurable rather than decorative."""
    conf = float(base)
    if not terminal:
        conf *= 0.5                       # never reached a resolution
    if calls:
        conf *= (1 - 0.5 * (errors / calls))  # penalise a noisy tool trace
    if policy_blocks:
        conf *= 0.7                       # policy had to catch a mistake
    return round(max(0.05, min(0.99, conf)), 3)
