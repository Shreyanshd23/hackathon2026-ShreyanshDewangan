"""
Resilient tool execution
──────────────────────────
Wraps raw tool calls with a real retry budget and exponential backoff — the
thing failure_modes.md used to *claim* but never implemented.

Distinguishes transient from permanent failures:
  • Transient (TransientToolError, timeouts, "temporarily unavailable")
    → retried up to `tool_max_retries` with backoff.
  • Permanent ("not found", "already refunded", bad args)
    → returned immediately; retrying would be pointless.

If the retry budget is exhausted, the failure is logged and returned as a
clean error dict (never a crash), and the caller can escalate.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from core.config import settings
from core.telemetry import log_event, tracer
from tools.impls import TransientToolError, execute_tool

_TRANSIENT_MARKERS = ("temporarily unavailable", "timeout", "timed out", "please retry", "rate limit")


def _is_transient_result(result: dict) -> bool:
    if not isinstance(result, dict) or "error" not in result:
        return False
    return any(m in str(result["error"]).lower() for m in _TRANSIENT_MARKERS)


def resilient_execute(
    name: str,
    arguments: dict,
    max_retries: int | None = None,
    backoff_base: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Execute a tool with retry + exponential backoff on transient errors."""
    retries = settings.tool_max_retries if max_retries is None else max_retries
    base = settings.tool_backoff_base if backoff_base is None else backoff_base

    last: dict = {"error": f"Tool '{name}' failed before any attempt."}
    for attempt in range(retries + 1):
        try:
            result = execute_tool(name, arguments)
        except TransientToolError as exc:
            result = {"error": str(exc)}

        if not _is_transient_result(result):
            return result  # success or permanent failure — done

        last = result
        if attempt < retries:
            wait = base * (2 ** attempt)
            with tracer.span("tool.retry", tool=name, attempt=attempt + 1, wait_s=round(wait, 2)):
                sleep(wait)
        else:
            log_event("tool_exhausted_retries", tool=name, attempts=attempt + 1, error=last.get("error"))
    return last
