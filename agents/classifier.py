"""
ClassifierAgent — one LLM call → a validated Classification
────────────────────────────────────────────────────────────
The LLM's JSON is coerced through the Pydantic Classification model, so
downstream code (and the confidence-based policy gate) always sees a known
shape with a bounded confidence — never a raw dict with missing keys.
"""

from __future__ import annotations

import json

import prompts
from core import llm_client
from core.schemas import Classification
from core.telemetry import tracer


class ClassifierAgent:
    @staticmethod
    def run(parsed: dict) -> Classification:
        user_content = json.dumps({
            "ticket_id": parsed["ticket_id"],
            "subject": parsed["subject"],
            "body": parsed["body"],
            "customer_email": parsed["customer_email"],
            "extracted_order_ids": parsed["extracted_order_ids"],
            "has_threatening_language": parsed["has_threatening_language"],
            "has_urgency_signals": parsed["has_urgency_signals"],
            "ticket_tier": parsed["tier"],
        }, indent=2)

        messages = [
            {"role": "system", "content": prompts.load("classifier")},
            {"role": "user", "content": user_content},
        ]
        with tracer.span("agent.classifier", ticket_id=parsed["ticket_id"]) as span:
            raw = llm_client.chat_json(messages)
            try:
                classification = Classification(**{k: raw[k] for k in Classification.model_fields if k in raw})
            except Exception:  # noqa: BLE001 — never let a bad LLM shape crash triage
                classification = Classification()
            span.set(category=classification.category, confidence=classification.confidence)
            return classification
