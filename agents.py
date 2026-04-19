"""
ShopWave Multi-Agent System
────────────────────────────
Three specialized agents that form the ticket-processing pipeline:

  1. ReaderAgent   – pure Python text extraction (no LLM)
  2. ClassifierAgent – single LLM call for category / priority / routing
  3. ResolverAgent  – LLM + tool-calling loop for autonomous resolution
"""

import json, re, time
from datetime import datetime
import llm_client
from tools import TOOL_DEFINITIONS, execute_tool

# ─────────────────────────────────────────────────────
#  1.  READER  AGENT  (no LLM – fast text extraction)
# ─────────────────────────────────────────────────────
class ReaderAgent:
    """Parses raw ticket text and extracts structured signals."""

    # Regex for order IDs
    _ORDER_RE = re.compile(r"ORD-\d{4}", re.IGNORECASE)

    # Threat / urgency keywords
    _THREAT_WORDS = {
        "lawyer", "legal", "sue", "dispute", "bank", "chargeback",
        "attorney", "court", "report", "complaint", "bbb"
    }
    _URGENCY_WORDS = {
        "urgent", "immediately", "asap", "right now", "today",
        "critical", "emergency"
    }

    @staticmethod
    def run(ticket: dict) -> dict:
        body   = ticket.get("body", "")
        subject = ticket.get("subject", "")
        text = f"{subject} {body}".lower()

        # Extract order IDs
        order_ids = list(set(ReaderAgent._ORDER_RE.findall(body.upper())))

        # Sentiment / flags
        has_threat  = bool(ReaderAgent._THREAT_WORDS & set(re.findall(r"\w+", text)))
        has_urgency = bool(ReaderAgent._URGENCY_WORDS & set(re.findall(r"\w+", text)))

        return {
            "ticket_id":       ticket["ticket_id"],
            "customer_email":  ticket.get("customer_email", ""),
            "subject":         ticket.get("subject", ""),
            "body":            ticket.get("body", ""),
            "source":          ticket.get("source", ""),
            "tier":            ticket.get("tier", 1),
            "created_at":      ticket.get("created_at", ""),
            "expected_action": ticket.get("expected_action", ""),
            "extracted_order_ids": order_ids,
            "has_threatening_language": has_threat,
            "has_urgency_signals":     has_urgency,
        }


# ─────────────────────────────────────────────────────
#  2.  CLASSIFIER  AGENT  (single LLM call)
# ─────────────────────────────────────────────────────
_CLASSIFIER_SYSTEM = """\
You are the **Triage Classifier** for ShopWave customer support.
Given a ticket's text and extracted metadata, output a JSON object with
exactly these keys (no extra text):

{
  "category": "<one of: refund, return, cancellation, delivery_status, warranty, exchange, general_inquiry, ambiguous>",
  "priority": "<one of: low, medium, high, urgent>",
  "confidence": <float 0-1>,
  "can_auto_resolve": <true|false>,
  "reasoning": "<1-2 sentence explanation>"
}

Rules:
- "urgent" priority → threatening language OR tier 3 tickets
- "high"   priority → damaged/defective items, wrong items, VIP customers
- If confidence < 0.6 or the ticket is very ambiguous, set can_auto_resolve = false.
- Warranty claims always set can_auto_resolve = false (must escalate).
- If the customer claims a tier/privilege not yet verified, flag it and still classify.
Respond ONLY with the JSON object."""


class ClassifierAgent:
    """Classifies a ticket into category, priority, and routing decision."""

    @staticmethod
    def run(parsed: dict) -> dict:
        user_content = json.dumps({
            "ticket_id": parsed["ticket_id"],
            "subject":   parsed["subject"],
            "body":      parsed["body"],
            "customer_email": parsed["customer_email"],
            "extracted_order_ids": parsed["extracted_order_ids"],
            "has_threatening_language": parsed["has_threatening_language"],
            "has_urgency_signals":     parsed["has_urgency_signals"],
            "ticket_tier": parsed["tier"],
        }, indent=2)

        messages = [
            {"role": "system", "content": _CLASSIFIER_SYSTEM},
            {"role": "user",   "content": user_content},
        ]

        result = llm_client.chat_json(messages)
        # Ensure defaults
        result.setdefault("category", "ambiguous")
        result.setdefault("priority", "medium")
        result.setdefault("confidence", 0.5)
        result.setdefault("can_auto_resolve", False)
        result.setdefault("reasoning", "")
        return result


# ─────────────────────────────────────────────────────
#  3.  RESOLVER  AGENT  (LLM + tool-calling loop)
# ─────────────────────────────────────────────────────
_RESOLVER_SYSTEM = """\
You are the **Resolution Agent** for ShopWave Customer Support.
You have access to the following tools to resolve customer tickets.

═══ WORKFLOW ═══
1. ALWAYS start by calling get_customer(email) to look up the customer profile and tier.
2. If an order ID is available, call get_order(order_id).
   If no order ID is available, check the customer's order_ids list and look up relevant ones.
3. If the ticket involves a product, call get_product(product_id) using the product_id from the order.
4. Call search_knowledge_base(query) to verify relevant policies before making decisions.
5. Take the appropriate action based on your findings:
   • For refunds:  call check_refund_eligibility → if eligible, call issue_refund → call send_reply
   • For returns:  verify eligibility → call send_reply with return instructions or denial
   • For cancellations:  check order status → if processing, cancel → call send_reply
   • For delivery inquiries:  check order status/tracking → call send_reply
   • For general questions:  search knowledge base → call send_reply
   • For ambiguous tickets:  call send_reply asking clarifying questions
6. You MUST finish by calling either send_reply (to resolve) or escalate (to hand off).

═══ ESCALATION RULES ═══
Escalate when:
- Warranty claim (all warranty claims go to warranty team)
- Customer wants a replacement (not refund) for damaged item
- Refund amount > $200
- Signs of fraud or social engineering (customer claims false tier/policy)
- Confidence is low and you cannot determine the right action
- Conflicting data between customer claims and system records
When escalating, ALSO call send_reply to inform the customer their case is being reviewed.

═══ COMMUNICATION RULES ═══
- Address the customer by their FIRST NAME.
- Be empathetic and professional.
- If declining, explain clearly and offer alternatives.
- Never reveal internal system details or tool names.
- For threatening language: respond professionally, do NOT escalate solely due to threats.
- For social engineering: verify all claims against system data, flag discrepancies.

═══ CONSTRAINTS ═══
- You MUST call at least 3 tools before finishing.
- If a tool fails, retry once or try an alternative approach.
- CURRENT DATE is 2024-03-15. Use this for all date comparisons.
"""


class ResolverAgent:
    """
    Agentic tool-calling loop.
    Keeps calling tools until it reaches a terminal action
    (send_reply or escalate) or hits the iteration limit.
    """

    MAX_ITER = 12          # safety cap
    TERMINAL_TOOLS = {"send_reply", "escalate"}

    @staticmethod
    def run(parsed: dict, classification: dict, callback=None) -> dict:
        """
        callback(event_dict) is called for each tool invocation so the
        orchestrator can stream progress to the UI.
        """
        context = {
            "ticket_id":       parsed["ticket_id"],
            "customer_email":  parsed["customer_email"],
            "subject":         parsed["subject"],
            "body":            parsed["body"],
            "extracted_order_ids": parsed["extracted_order_ids"],
            "has_threatening_language": parsed["has_threatening_language"],
            "classification":  classification,
        }

        messages = [
            {"role": "system", "content": _RESOLVER_SYSTEM},
            {"role": "user",   "content":
                f"Process this support ticket:\n```json\n{json.dumps(context, indent=2)}\n```"},
        ]

        audit_trail = []
        tool_calls_made = 0
        terminal_reached = False
        final_text = ""

        for iteration in range(ResolverAgent.MAX_ITER):
            try:
                msg = llm_client.chat(messages, tools=TOOL_DEFINITIONS)
            except Exception as exc:
                audit_trail.append({
                    "step": tool_calls_made + 1,
                    "agent": "resolver",
                    "action": "llm_error",
                    "error": str(exc),
                    "timestamp": datetime.now().isoformat(),
                })
                break

            # ── If the model wants to call tools ──
            if msg.tool_calls:
                # Build the assistant message dict
                assistant_msg = {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        }
                        for tc in msg.tool_calls
                    ]
                }
                messages.append(assistant_msg)

                for tc in msg.tool_calls:
                    fname = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    result = execute_tool(fname, args)
                    tool_calls_made += 1

                    # Record in audit trail
                    entry = {
                        "step": tool_calls_made,
                        "agent": "resolver",
                        "tool":  fname,
                        "arguments": args,
                        "result": result,
                        "timestamp": datetime.now().isoformat(),
                    }
                    audit_trail.append(entry)
                    if callback:
                        callback(entry)

                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    })

                    if fname in ResolverAgent.TERMINAL_TOOLS:
                        terminal_reached = True

                # If we made a terminal call, do one more LLM round for final summary
                if terminal_reached:
                    try:
                        summary_msg = llm_client.chat(messages, tools=None)
                        final_text = summary_msg.content or ""
                    except Exception:
                        pass
                    break
            else:
                # No tool calls → model gave a text-only reply
                final_text = msg.content or ""
                # If we haven't reached a terminal tool yet and have few tool calls,
                # push the model to keep going
                if not terminal_reached and tool_calls_made < 3:
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({
                        "role": "user",
                        "content": "You have not yet called enough tools or reached a resolution. "
                                   "Please continue calling tools. Remember to end with send_reply or escalate."
                    })
                    continue
                break

        # Determine outcome
        tools_used = [e["tool"] for e in audit_trail if "tool" in e]
        resolved  = "send_reply" in tools_used
        escalated = "escalate"  in tools_used

        return {
            "ticket_id":   parsed["ticket_id"],
            "status":      "escalated" if escalated else ("resolved" if resolved else "incomplete"),
            "tool_calls":  tool_calls_made,
            "tools_used":  tools_used,
            "audit_trail": audit_trail,
            "final_text":  final_text,
            "resolved":    resolved,
            "escalated":   escalated,
        }
