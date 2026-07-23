"""
LLM client
───────────
Thin, rate-limited, retrying wrapper over Groq (reasoning) and Gemini (judge),
with a deterministic OFFLINE mock so the whole system runs with no API keys.

Why the mock matters: it lets the eval suite gate CI, lets a reviewer run the
full 20-ticket pipeline in one command, and makes the tool-calling loop +
policy layer testable without spending tokens. It is a scripted agent, not an
LLM — clearly separated so it is never mistaken for real reasoning.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.rate_limiter import LLMRateLimiter, estimate_tokens
from core.telemetry import log_event

_rate_limiter = LLMRateLimiter(rpm=settings.llm_rpm, tpm=settings.llm_tpm)

# Real SDK clients are created lazily and only when not in offline mode.
_groq = None
_gemini = None


def _groq_client():
    global _groq
    if _groq is None:
        from groq import Groq
        _groq = Groq(api_key=settings.groq_api_key)
    return _groq


def _gemini_client():
    global _gemini
    if _gemini is None and settings.gemini_api_key:
        from google import genai
        _gemini = genai.Client(api_key=settings.gemini_api_key)
    return _gemini


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────
def chat(messages: list, tools: list | None = None, retries: int = 3) -> Any:
    """Chat completion. Returns a message object with `.content` and
    `.tool_calls` (Groq SDK shape, mirrored by the mock)."""
    if settings.offline_mode:
        return _mock_chat(messages, tools)

    _rate_limiter.acquire(estimate_tokens(messages, settings.llm_max_tokens))
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            kwargs: dict[str, Any] = dict(
                model=settings.llm_model,
                messages=messages,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
            )
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            resp = _groq_client().chat.completions.create(**kwargs)
            return resp.choices[0].message
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = 2 ** attempt
            log_event("llm_retry", attempt=attempt + 1, error=str(exc), wait_s=wait)
            if attempt < retries - 1:
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def chat_json(messages: list, retries: int = 3) -> dict:
    """Chat expecting a JSON object; robust to markdown fences / stray prose."""
    msg = chat(messages, tools=None, retries=retries)
    text = (msg.content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"raw": text, "parse_error": True}


def gemini_evaluate(prompt: str) -> dict:
    """Independent judge via Gemini (or the mock judge offline)."""
    if settings.offline_mode:
        return _mock_judge(prompt)
    client = _gemini_client()
    if not client:
        return {"error": "Gemini API key missing"}
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        return json.loads(response.text)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Gemini evaluation failed: {exc}"}


# ═════════════════════════════════════════════════════════════
#  OFFLINE MOCK — a scripted agent, NOT an LLM
# ═════════════════════════════════════════════════════════════
@dataclass
class _MockFunction:
    name: str
    arguments: str


@dataclass
class _MockToolCall:
    id: str
    function: _MockFunction
    type: str = "function"


@dataclass
class _MockMessage:
    content: str = ""
    tool_calls: list | None = None


_CATEGORY_KEYWORDS = {
    "refund": ["refund", "money back", "reimburse"],
    "cancellation": ["cancel"],
    "return": ["return", "send back"],
    "delivery_status": ["where", "track", "delivery", "arrive", "shipping", "shipped"],
    "warranty": ["warranty", "broken", "defective", "stopped working"],
    "exchange": ["exchange", "replace", "replacement"],
}


def _ticket_context(messages: list) -> dict:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            s, e = m["content"].find("{"), m["content"].rfind("}") + 1
            if s != -1 and e > s:
                try:
                    return json.loads(m["content"][s:e])
                except json.JSONDecodeError:
                    continue
    return {}


def _scan_history(messages: list) -> tuple[set, dict]:
    """Return (tools already called, {tool_name: last_parsed_result})."""
    id_to_name: dict[str, str] = {}
    called: set = set()
    results: dict[str, Any] = {}
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                name = tc["function"]["name"] if isinstance(tc, dict) else tc.function.name
                tcid = tc["id"] if isinstance(tc, dict) else tc.id
                id_to_name[tcid] = name
                called.add(name)
        elif m.get("role") == "tool":
            name = id_to_name.get(m.get("tool_call_id", ""), "")
            try:
                results[name] = json.loads(m.get("content", "{}"))
            except json.JSONDecodeError:
                results[name] = {}
    return called, results


def _mock_classify(ctx: dict) -> str:
    text = f"{ctx.get('subject','')} {ctx.get('body','')}".lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in text for k in kws):
            return cat
    return "general_inquiry"


def _mk_call(name: str, args: dict) -> _MockMessage:
    return _MockMessage(
        content="",
        tool_calls=[_MockToolCall(id=f"call_{name}", function=_MockFunction(name=name, arguments=json.dumps(args)))],
    )


def _mock_chat(messages: list, tools: list | None) -> _MockMessage:
    system = messages[0]["content"] if messages else ""

    # ── classifier call ──
    if "Triage Classifier" in system:
        ctx = _ticket_context(messages)
        cat = _mock_classify(ctx)
        conf = 0.55 if cat in ("ambiguous", "general_inquiry") else 0.9
        can_auto = cat not in ("warranty",) and conf >= 0.6
        return _MockMessage(content=json.dumps({
            "category": cat, "priority": "medium", "confidence": conf,
            "can_auto_resolve": can_auto,
            "reasoning": f"Matched category '{cat}' from ticket text (mock).",
        }))

    # ── resolver: decide next tool, or a final summary ──
    ctx = _ticket_context(messages)
    called, results = _scan_history(messages)

    if tools is None:  # post-terminal summary round
        return _MockMessage(content="Ticket handled. Summary generated by mock resolver.")

    email = ctx.get("customer_email", "")
    ticket_id = ctx.get("ticket_id", "TKT-000")
    category = (ctx.get("classification") or {}).get("category", "general_inquiry")

    # order id: from extracted ids, else customer's order list, else order result
    order_id = (ctx.get("extracted_order_ids") or [None])[0]
    if not order_id and "get_customer" in results:
        order_id = (results["get_customer"].get("order_ids") or [None])[0]

    # 1. always look up the customer first
    if "get_customer" not in called:
        return _mk_call("get_customer", {"email": email})
    # 2. look up the order
    if order_id and "get_order" not in called:
        return _mk_call("get_order", {"order_id": order_id})
    # 3. verify policy in the knowledge base
    if "search_knowledge_base" not in called:
        return _mk_call("search_knowledge_base", {"query": f"{category} policy"})
    # 4. for money categories, check eligibility before acting
    if category in ("refund", "cancellation", "return") and order_id and "check_refund_eligibility" not in called:
        return _mk_call("check_refund_eligibility", {"order_id": order_id})
    # 5. if eligible, attempt the refund (policy engine will gate it)
    elig = results.get("check_refund_eligibility", {})
    if category == "refund" and order_id and "issue_refund" not in called and elig.get("eligible"):
        amount = elig.get("refundable_amount") or results.get("get_order", {}).get("amount") or 50.0
        return _mk_call("issue_refund", {"order_id": order_id, "amount": amount})
    # 6. terminal: escalate warranty/ineligible-with-warranty, else reply
    if category == "warranty" or elig.get("warranty_active"):
        return _mk_call("escalate", {
            "ticket_id": ticket_id,
            "summary": f"{category} case for {order_id or 'order'}; routing to specialists.",
            "priority": "high",
        })
    return _mk_call("send_reply", {
        "ticket_id": ticket_id,
        "message": "Hi, thanks for reaching out — we've reviewed your request and taken the appropriate action. (mock reply)",
    })


def _mock_judge(prompt: str) -> dict:
    """Deterministic stand-in for the Gemini judge (offline)."""
    lower = prompt.lower()
    cat = "general_inquiry"
    for c in ("refund", "cancellation", "return", "warranty", "exchange", "delivery_status"):
        if c in lower:
            cat = c
            break
    return {
        "true_category": cat,
        "is_correct": True,
        "semantic_similarity": 0.8,
        "logic_score": 0.85,
        "tone_score": 0.9,
        "judging_readiness_score": 0.85,
        "feedback": "Mock judge: tool sequence looks sensible and reply is on-policy.",
    }
