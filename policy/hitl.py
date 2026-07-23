"""
Human-in-the-loop queue
────────────────────────
When the PolicyEngine returns REQUIRE_HUMAN, the proposed action is NOT
executed. Instead it is parked here as a pending approval item with full
context, so a human can approve or reject it later.

This is deliberately a small, in-memory, thread-safe queue for now. It is
the seam where a durable store (storage/) or a real ticketing/approval
system would plug in — the interface stays the same.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from core.schemas import PolicyDecision


class HITLQueue:
    """Thread-safe queue of actions awaiting human approval."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def enqueue(self, ticket_id: str, tool: str, arguments: dict, decision: PolicyDecision) -> dict:
        item = {
            "id": f"HITL-{ticket_id}-{len(self._items) + 1}",
            "ticket_id": ticket_id,
            "tool": tool,
            "arguments": arguments,
            "rule": decision.rule,
            "reason": decision.reason,
            "risk": decision.risk.value,
            "status": "pending_approval",
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._items.append(item)
        return item

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items if i["status"] == "pending_approval"]

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
