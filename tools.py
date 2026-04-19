"""
ShopWave Support Tools
──────────────────────
Eight tools the AI agents can call to look up data, take actions,
and communicate.  Each tool:
  • Loads from the provided JSON / MD files
  • Includes 5 % random failure injection for resilience testing
  • Returns clean dicts (never raises – errors are in the return value)
"""

import json, os, random, re
from datetime import datetime, timedelta
from config import DATA_DIR, FAILURE_INJECTION_RATE, SIMULATED_DATE

# ── load data once at import ────────────────────────────────────
def _load(fname):
    with open(os.path.join(DATA_DIR, fname), encoding="utf-8") as f:
        return json.load(f)

_orders_list    = _load("orders.json")
_customers_list = _load("customers.json")
_products_list  = _load("products.json")

ORDERS    = {o["order_id"]:    o for o in _orders_list}
CUSTOMERS = {c["email"]:       c for c in _customers_list}
PRODUCTS  = {p["product_id"]:  p for p in _products_list}

with open(os.path.join(DATA_DIR, "knowledge-base.md"), encoding="utf-8") as f:
    KB_RAW = f.read()

# Split KB into titled sections for search
_KB_SECTIONS = []
for block in re.split(r"\n## ", KB_RAW):
    block = block.strip()
    if block:
        _KB_SECTIONS.append(block)

# ── action logs (in-memory, persisted per run) ──────────────────
refund_log     = []
reply_log      = []
escalation_log = []

# ── random failure injector ─────────────────────────────────────
def _maybe_fail(tool_name: str):
    if random.random() < FAILURE_INJECTION_RATE:
        return {"error": f"[{tool_name}] Service temporarily unavailable. Please retry."}
    return None

# ── simulated "now" ─────────────────────────────────────────────
NOW = datetime.strptime(SIMULATED_DATE, "%Y-%m-%d")

# ═══════════════════════════════════════════════════════════════
#  TOOL  1 – get_order
# ═══════════════════════════════════════════════════════════════
def get_order(order_id: str) -> dict:
    """Look up an order by ID."""
    err = _maybe_fail("get_order")
    if err:
        return err
    order = ORDERS.get(order_id)
    if not order:
        return {"error": f"Order '{order_id}' not found in the system."}
    return order

# ═══════════════════════════════════════════════════════════════
#  TOOL  2 – get_customer
# ═══════════════════════════════════════════════════════════════
def get_customer(email: str) -> dict:
    """Look up a customer by email."""
    err = _maybe_fail("get_customer")
    if err:
        return err
    cust = CUSTOMERS.get(email)
    if not cust:
        return {"error": f"No customer found with email '{email}'."}
    # Also attach their order IDs for convenience
    cust_copy = dict(cust)
    cust_copy["order_ids"] = [
        o["order_id"] for o in _orders_list if o["customer_id"] == cust["customer_id"]
    ]
    return cust_copy

# ═══════════════════════════════════════════════════════════════
#  TOOL  3 – get_product
# ═══════════════════════════════════════════════════════════════
def get_product(product_id: str) -> dict:
    """Look up a product by ID."""
    err = _maybe_fail("get_product")
    if err:
        return err
    prod = PRODUCTS.get(product_id)
    if not prod:
        return {"error": f"Product '{product_id}' not found."}
    return prod

# ═══════════════════════════════════════════════════════════════
#  TOOL  4 – search_knowledge_base
# ═══════════════════════════════════════════════════════════════
def search_knowledge_base(query: str) -> dict:
    """Keyword search over ShopWave's support knowledge base."""
    err = _maybe_fail("search_knowledge_base")
    if err:
        return err
    words = set(query.lower().split())
    scored = []
    for section in _KB_SECTIONS:
        lower = section.lower()
        score = sum(1 for w in words if w in lower)
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [s[1] for s in scored[:3]] if scored else ["No relevant policy found."]
    return {"query": query, "results": results}

# ═══════════════════════════════════════════════════════════════
#  TOOL  5 – check_refund_eligibility
# ═══════════════════════════════════════════════════════════════
def check_refund_eligibility(order_id: str) -> dict:
    """Business-logic check: is this order eligible for a refund?"""
    err = _maybe_fail("check_refund_eligibility")
    if err:
        return err

    order = ORDERS.get(order_id)
    if not order:
        return {"eligible": False, "reason": f"Order '{order_id}' not found.", "order_id": order_id}

    product = PRODUCTS.get(order.get("product_id", ""))

    # Already refunded?
    if order.get("refund_status") == "refunded":
        return {
            "eligible": False,
            "reason": f"Order {order_id} has already been refunded.",
            "order_id": order_id,
            "refund_status": "refunded"
        }

    # Still processing → can cancel
    if order["status"] == "processing":
        return {
            "eligible": True,
            "reason": "Order is still processing and can be cancelled for a full refund.",
            "refundable_amount": order["amount"],
            "order_id": order_id
        }

    # Shipped / in transit → cannot refund yet
    if order["status"] == "shipped":
        return {
            "eligible": False,
            "reason": "Order is in transit. Customer must wait for delivery, then initiate a return.",
            "order_id": order_id,
            "tracking": order.get("notes", "")
        }

    # Delivered → check return window
    within_window = False
    if order.get("return_deadline"):
        deadline = datetime.strptime(order["return_deadline"], "%Y-%m-%d")
        within_window = NOW <= deadline

    if within_window:
        # Check product-level restrictions
        if product:
            notes_lower = (order.get("notes", "") + " " + product.get("notes", "")).lower()
            if "non-returnable" in notes_lower or "registered online" in notes_lower:
                return {
                    "eligible": False,
                    "reason": f"Item is non-returnable per policy. Details: {product.get('notes', '')}. Order notes: {order.get('notes', '')}",
                    "order_id": order_id
                }
        return {
            "eligible": True,
            "reason": f"Within return window (deadline {order.get('return_deadline')}). Refund can be issued.",
            "refundable_amount": order["amount"],
            "order_id": order_id
        }
    else:
        # Expired window
        warranty_info = ""
        if product and product.get("warranty_months", 0) > 0:
            warranty_info = f" Product has a {product['warranty_months']}-month warranty — warranty claims should be escalated to the warranty team."
        return {
            "eligible": False,
            "reason": f"Return window expired (deadline was {order.get('return_deadline')}).{warranty_info}",
            "order_id": order_id,
            "warranty_active": bool(warranty_info)
        }

# ═══════════════════════════════════════════════════════════════
#  TOOL  6 – issue_refund
# ═══════════════════════════════════════════════════════════════
def issue_refund(order_id: str, amount: float) -> dict:
    """Simulate issuing a refund."""
    err = _maybe_fail("issue_refund")
    if err:
        return err

    order = ORDERS.get(order_id)
    if not order:
        return {"success": False, "error": f"Order '{order_id}' not found."}
    if order.get("refund_status") == "refunded":
        return {"success": False, "error": f"Order {order_id} was already refunded."}

    # Update in-memory state
    order["refund_status"] = "refunded"
    entry = {
        "order_id": order_id,
        "amount": amount,
        "timestamp": datetime.now().isoformat(),
        "status": "processed"
    }
    refund_log.append(entry)
    return {
        "success": True,
        "message": f"Refund of ${amount:.2f} issued for order {order_id}. Customer will see it in 5-7 business days.",
        **entry
    }

# ═══════════════════════════════════════════════════════════════
#  TOOL  7 – send_reply
# ═══════════════════════════════════════════════════════════════
def send_reply(ticket_id: str, message: str) -> dict:
    """Send a reply to the customer."""
    err = _maybe_fail("send_reply")
    if err:
        return err
    entry = {
        "ticket_id": ticket_id,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "status": "sent"
    }
    reply_log.append(entry)
    return {"success": True, **entry}

# ═══════════════════════════════════════════════════════════════
#  TOOL  8 – escalate
# ═══════════════════════════════════════════════════════════════
def escalate(ticket_id: str, summary: str, priority: str) -> dict:
    """Escalate a ticket to a human agent."""
    err = _maybe_fail("escalate")
    if err:
        return err
    entry = {
        "ticket_id": ticket_id,
        "summary": summary,
        "priority": priority,
        "timestamp": datetime.now().isoformat(),
        "status": "escalated"
    }
    escalation_log.append(entry)
    return {"success": True, **entry}

# ═══════════════════════════════════════════════════════════════
#  TOOL EXECUTION DISPATCHER
# ═══════════════════════════════════════════════════════════════
TOOL_MAP = {
    "get_order":                get_order,
    "get_customer":             get_customer,
    "get_product":              get_product,
    "search_knowledge_base":    search_knowledge_base,
    "check_refund_eligibility": check_refund_eligibility,
    "issue_refund":             issue_refund,
    "send_reply":               send_reply,
    "escalate":                 escalate,
}

def execute_tool(name: str, arguments: dict) -> dict:
    """Safely execute a tool by name.  Never raises."""
    fn = TOOL_MAP.get(name)
    if not fn:
        return {"error": f"Unknown tool '{name}'."}
    try:
        return fn(**arguments)
    except Exception as exc:
        return {"error": f"Tool '{name}' crashed: {str(exc)}"}

# ═══════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS (OpenAI-compatible schema for Groq)
# ═══════════════════════════════════════════════════════════════
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up an order by its ID. Returns order status, dates, amount, refund status, and notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID, e.g. ORD-1001"
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer",
            "description": "Look up a customer by email. Returns profile, tier (standard/premium/vip), order history summary, and notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Customer email address"
                    }
                },
                "required": ["email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Look up a product by ID. Returns name, category, price, warranty months, return window days, and policy notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Product ID, e.g. P001"
                    }
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search ShopWave's support knowledge base for policies, FAQs, and procedures (return policy, refund rules, warranty info, escalation guidelines, customer tiers, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'return policy for electronics' or 'refund processing time'"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund_eligibility",
            "description": "Check whether an order is eligible for a refund. Returns eligibility, reason, and refundable amount.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Order ID to check"
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Issue a refund for an order. Only call AFTER confirming eligibility with check_refund_eligibility.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Order ID to refund"
                    },
                    "amount": {
                        "type": "number",
                        "description": "Refund amount in USD"
                    }
                },
                "required": ["order_id", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_reply",
            "description": "Send a reply message to the customer. This is the standard way to resolve or respond to a ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Ticket ID, e.g. TKT-001"
                    },
                    "message": {
                        "type": "string",
                        "description": "Professional, empathetic reply addressing customer by first name"
                    }
                },
                "required": ["ticket_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Escalate ticket to a human agent. Use when: warranty claims, replacement requests, fraud/social engineering, refunds >$200, ambiguous or low-confidence situations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Ticket ID to escalate"
                    },
                    "summary": {
                        "type": "string",
                        "description": "Concise summary: issue, what was verified, recommended resolution"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "urgent"],
                        "description": "Escalation priority"
                    }
                },
                "required": ["ticket_id", "summary", "priority"]
            }
        }
    }
]
