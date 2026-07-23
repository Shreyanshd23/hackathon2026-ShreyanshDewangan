"""
Tool implementations
──────────────────────
The 8 mock backend tools. Compared with the original:
  • Refund state mutation is guarded by a lock (thread-safe under concurrency).
  • Inputs are validated before use (bad args -> clean error, never a crash).
  • Errors are classified transient vs permanent so the resilient wrapper knows
    what is worth retrying.
  • Failure injection is centralised and configurable (0 in tests).

These are pure backend simulations. All *safety* decisions (e.g. "may this
refund happen?") live in the policy layer, not here.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
from datetime import datetime

from core.config import settings

DATA_DIR = settings.data_dir
NOW = datetime.strptime(settings.simulated_date, "%Y-%m-%d")

# ── data loaded once ─────────────────────────────────────────
def _load(fname: str):
    with open(os.path.join(DATA_DIR, fname), encoding="utf-8") as f:
        return json.load(f)


_orders_list = _load("orders.json")
_customers_list = _load("customers.json")
_products_list = _load("products.json")

ORDERS = {o["order_id"]: o for o in _orders_list}
CUSTOMERS = {c["email"]: c for c in _customers_list}
PRODUCTS = {p["product_id"]: p for p in _products_list}

with open(os.path.join(DATA_DIR, "knowledge-base.md"), encoding="utf-8") as f:
    _KB_RAW = f.read()
_KB_SECTIONS = [b.strip() for b in re.split(r"\n## ", _KB_RAW) if b.strip()]

# ── action logs + refund lock ────────────────────────────────
refund_log: list = []
reply_log: list = []
escalation_log: list = []
_refund_lock = threading.Lock()

# Overridable at runtime; tests set this to 0.0 for determinism.
FAILURE_INJECTION_RATE = settings.failure_injection_rate


class TransientToolError(Exception):
    """Raised for a simulated transient failure — the resilient wrapper retries it."""


def _maybe_fail(tool_name: str) -> None:
    if FAILURE_INJECTION_RATE > 0 and random.random() < FAILURE_INJECTION_RATE:
        raise TransientToolError(f"[{tool_name}] Service temporarily unavailable. Please retry.")


# ═══ READ TOOLS ═══════════════════════════════════════════════
def get_order(order_id: str) -> dict:
    _maybe_fail("get_order")
    if not isinstance(order_id, str) or not order_id.strip():
        return {"error": "order_id must be a non-empty string."}
    order = ORDERS.get(order_id)
    return order if order else {"error": f"Order '{order_id}' not found in the system."}


def get_customer(email: str) -> dict:
    _maybe_fail("get_customer")
    if not isinstance(email, str) or "@" not in email:
        return {"error": f"Invalid email: {email!r}."}
    cust = CUSTOMERS.get(email)
    if not cust:
        return {"error": f"No customer found with email '{email}'."}
    cust_copy = dict(cust)
    cust_copy["order_ids"] = [o["order_id"] for o in _orders_list if o["customer_id"] == cust["customer_id"]]
    return cust_copy


def get_product(product_id: str) -> dict:
    _maybe_fail("get_product")
    prod = PRODUCTS.get(product_id)
    return prod if prod else {"error": f"Product '{product_id}' not found."}


def search_knowledge_base(query: str) -> dict:
    _maybe_fail("search_knowledge_base")
    words = set((query or "").lower().split())
    scored = []
    for section in _KB_SECTIONS:
        lower = section.lower()
        score = sum(1 for w in words if w in lower)
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [s[1] for s in scored[:3]] if scored else ["No relevant policy found."]
    return {"query": query, "results": results}


def check_refund_eligibility(order_id: str) -> dict:
    _maybe_fail("check_refund_eligibility")
    order = ORDERS.get(order_id)
    if not order:
        return {"eligible": False, "reason": f"Order '{order_id}' not found.", "order_id": order_id}

    product = PRODUCTS.get(order.get("product_id", ""))
    if order.get("refund_status") == "refunded":
        return {"eligible": False, "reason": f"Order {order_id} has already been refunded.",
                "order_id": order_id, "refund_status": "refunded"}
    if order["status"] == "processing":
        return {"eligible": True, "reason": "Order is still processing and can be cancelled for a full refund.",
                "refundable_amount": order["amount"], "order_id": order_id}
    if order["status"] == "shipped":
        return {"eligible": False, "reason": "Order is in transit. Customer must wait for delivery, then initiate a return.",
                "order_id": order_id, "tracking": order.get("notes", "")}

    within_window = False
    if order.get("return_deadline"):
        within_window = NOW <= datetime.strptime(order["return_deadline"], "%Y-%m-%d")

    if within_window:
        if product:
            notes_lower = (order.get("notes", "") + " " + product.get("notes", "")).lower()
            if "non-returnable" in notes_lower or "registered online" in notes_lower:
                return {"eligible": False,
                        "reason": f"Item is non-returnable per policy. Details: {product.get('notes', '')}. Order notes: {order.get('notes', '')}",
                        "order_id": order_id}
        return {"eligible": True, "reason": f"Within return window (deadline {order.get('return_deadline')}).",
                "refundable_amount": order["amount"], "order_id": order_id}

    warranty_info = ""
    if product and product.get("warranty_months", 0) > 0:
        warranty_info = f" Product has a {product['warranty_months']}-month warranty — warranty claims should be escalated."
    return {"eligible": False,
            "reason": f"Return window expired (deadline was {order.get('return_deadline')}).{warranty_info}",
            "order_id": order_id, "warranty_active": bool(warranty_info)}


# ═══ WRITE TOOLS ══════════════════════════════════════════════
def issue_refund(order_id: str, amount: float) -> dict:
    """Irreversible. Thread-safe + idempotent on refund_status.
    NOTE: safety preconditions (eligibility checked, amount ≤ eligible, ≤ limit)
    are enforced by the PolicyEngine *before* this ever runs."""
    _maybe_fail("issue_refund")
    with _refund_lock:
        order = ORDERS.get(order_id)
        if not order:
            return {"success": False, "error": f"Order '{order_id}' not found."}
        if order.get("refund_status") == "refunded":
            return {"success": False, "error": f"Order {order_id} was already refunded."}
        order["refund_status"] = "refunded"
        entry = {"order_id": order_id, "amount": float(amount),
                 "timestamp": datetime.now().isoformat(), "status": "processed"}
        refund_log.append(entry)
    return {"success": True,
            "message": f"Refund of ${float(amount):.2f} issued for order {order_id}. Customer will see it in 5-7 business days.",
            **entry}


def send_reply(ticket_id: str, message: str) -> dict:
    _maybe_fail("send_reply")
    if not message or not str(message).strip():
        return {"success": False, "error": "Reply message must not be empty."}
    entry = {"ticket_id": ticket_id, "message": message,
             "timestamp": datetime.now().isoformat(), "status": "sent"}
    reply_log.append(entry)
    return {"success": True, **entry}


def escalate(ticket_id: str, summary: str, priority: str = "medium") -> dict:
    _maybe_fail("escalate")
    if priority not in {"low", "medium", "high", "urgent"}:
        priority = "medium"
    entry = {"ticket_id": ticket_id, "summary": summary, "priority": priority,
             "timestamp": datetime.now().isoformat(), "status": "escalated"}
    escalation_log.append(entry)
    return {"success": True, **entry}


TOOL_MAP = {
    "get_order": get_order,
    "get_customer": get_customer,
    "get_product": get_product,
    "search_knowledge_base": search_knowledge_base,
    "check_refund_eligibility": check_refund_eligibility,
    "issue_refund": issue_refund,
    "send_reply": send_reply,
    "escalate": escalate,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """Raw dispatch. Validates the tool exists and never raises for a bad name
    or bad args. Transient failures propagate as TransientToolError so the
    resilient wrapper can retry them."""
    fn = TOOL_MAP.get(name)
    if not fn:
        return {"error": f"Unknown tool '{name}'."}
    try:
        return fn(**(arguments or {}))
    except TransientToolError:
        raise
    except TypeError as exc:
        return {"error": f"Bad arguments for '{name}': {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Tool '{name}' crashed: {exc}"}
