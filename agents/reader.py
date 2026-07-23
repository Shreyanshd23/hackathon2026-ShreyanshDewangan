"""
ReaderAgent — deterministic entity/signal extraction (no LLM)
─────────────────────────────────────────────────────────────
Cheap, fast, and reproducible: regex for order IDs, word-set membership for
threat/urgency signals. No LLM call, so it never fails and costs nothing —
the right tool for the parts of triage that don't need reasoning.
"""

from __future__ import annotations

import re

_ORDER_RE = re.compile(r"ORD-\d{4}", re.IGNORECASE)
_THREAT_WORDS = {"lawyer", "legal", "sue", "dispute", "chargeback", "attorney", "court", "complaint", "bbb"}
_URGENCY_WORDS = {"urgent", "immediately", "asap", "critical", "emergency", "today"}


class ReaderAgent:
    @staticmethod
    def run(ticket: dict) -> dict:
        body = ticket.get("body", "")
        text = f"{ticket.get('subject', '')} {body}".lower()
        words = set(re.findall(r"\w+", text))
        return {
            "ticket_id": ticket["ticket_id"],
            "customer_email": ticket.get("customer_email", ""),
            "subject": ticket.get("subject", ""),
            "body": body,
            "source": ticket.get("source", ""),
            "tier": ticket.get("tier", 1),
            "created_at": ticket.get("created_at", ""),
            "expected_action": ticket.get("expected_action", ""),
            "extracted_order_ids": sorted(set(_ORDER_RE.findall(body.upper()))),
            "has_threatening_language": bool(_THREAT_WORDS & words),
            "has_urgency_signals": bool(_URGENCY_WORDS & words),
        }
