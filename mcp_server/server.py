"""
MCP server — expose the support tools over the Model Context Protocol
──────────────────────────────────────────────────────────────────────
A THIN adapter, by design. It owns no business logic: reads go through the
same resilient executor, and writes go through the same PolicyEngine.guard()
as the in-process agent. So any MCP client (Claude Desktop, another agent) gets
the identical guardrails — a refund still can't fire without a prior
eligibility check, and large refunds still route to the human queue.

Run:  python -m mcp_server.server      (requires the optional `mcp` package)

This is an interface choice, not a new capability — which is exactly why it was
built last, after the safety/eval/observability core was solid.
"""

from __future__ import annotations

from core.schemas import TicketState
from policy import PolicyEngine
from tools import resilient_execute

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The optional 'mcp' package is required to run the MCP server.\n"
        "Install it with:  pip install mcp"
    ) from exc

mcp = FastMCP("shopwave-support")

# One policy engine + session state, shared across tool calls, so the
# eligibility-before-refund invariant holds across a client's call sequence.
_engine = PolicyEngine()
_state = TicketState(ticket_id="mcp-session")

_WRITE_TOOLS = {"issue_refund", "send_reply", "escalate"}


def _call(name: str, args: dict) -> dict:
    if name in _WRITE_TOOLS:
        result, _decision = _engine.guard(name, args, _state, executor=resilient_execute)
        return result
    result = resilient_execute(name, args)
    _state.record(name, args, result)   # keep read results (e.g. eligibility) in state
    return result


# ── READ tools ───────────────────────────────────────────────
@mcp.tool()
def get_order(order_id: str) -> dict:
    """Look up an order by ID."""
    return _call("get_order", {"order_id": order_id})


@mcp.tool()
def get_customer(email: str) -> dict:
    """Look up a customer by email."""
    return _call("get_customer", {"email": email})


@mcp.tool()
def get_product(product_id: str) -> dict:
    """Look up a product by ID."""
    return _call("get_product", {"product_id": product_id})


@mcp.tool()
def search_knowledge_base(query: str) -> dict:
    """Search the support knowledge base for policies/FAQs."""
    return _call("search_knowledge_base", {"query": query})


@mcp.tool()
def check_refund_eligibility(order_id: str) -> dict:
    """Check whether an order is eligible for a refund."""
    return _call("check_refund_eligibility", {"order_id": order_id})


# ── WRITE tools (policy-gated) ───────────────────────────────
@mcp.tool()
def issue_refund(order_id: str, amount: float) -> dict:
    """Issue a refund. Policy-gated: requires a prior successful eligibility
    check; refunds over the limit are routed to a human."""
    return _call("issue_refund", {"order_id": order_id, "amount": amount})


@mcp.tool()
def send_reply(ticket_id: str, message: str) -> dict:
    """Send a reply to the customer."""
    return _call("send_reply", {"ticket_id": ticket_id, "message": message})


@mcp.tool()
def escalate(ticket_id: str, summary: str, priority: str = "medium") -> dict:
    """Escalate a ticket to a human with full context."""
    return _call("escalate", {"ticket_id": ticket_id, "summary": summary, "priority": priority})


if __name__ == "__main__":
    mcp.run()
