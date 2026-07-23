"""
Token-bucket rate limiter
──────────────────────────
Replaces the original `time.sleep(10)` between ticket submissions, which made
"concurrency" effectively serial and hardcoded a magic delay to a provider's
free-tier limit.

Instead we model the two real constraints explicitly:
  • requests per minute (RPM)
  • tokens per minute (TPM)

Two independent token buckets refill continuously. A worker acquires the
capacity it needs *just before* a call and blocks only as long as necessary.
This lets many tickets run genuinely in parallel, self-throttled to stay under
the provider ceiling — the honest version of what the sleep hack faked.

Thread-safe. Uses a monotonic clock, so it is immune to wall-clock changes.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Classic token bucket. `capacity` tokens, refilled at `rate` tokens/sec."""

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._cv = threading.Condition()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self, amount: float = 1.0, timeout: float | None = None) -> bool:
        """Block until `amount` tokens are available (capped at capacity), then
        consume them. Returns False if `timeout` elapses first."""
        amount = min(amount, self.capacity)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                self._refill_locked()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return True
                missing = amount - self._tokens
                wait = missing / self.rate if self.rate > 0 else None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(wait, remaining) if wait else remaining
                self._cv.wait(timeout=wait)


class LLMRateLimiter:
    """Combines an RPM bucket and a TPM bucket for a single LLM provider."""

    def __init__(self, rpm: int, tpm: int) -> None:
        # capacity == per-minute limit; refill == limit / 60 per second.
        self._req = TokenBucket(capacity=rpm, refill_per_sec=rpm / 60.0)
        self._tok = TokenBucket(capacity=tpm, refill_per_sec=tpm / 60.0)

    def acquire(self, estimated_tokens: int) -> None:
        """Reserve one request and its estimated token cost before a call."""
        self._req.acquire(1)
        self._tok.acquire(estimated_tokens)


def estimate_tokens(messages: list, max_tokens: int) -> int:
    """Cheap, dependency-free token estimate: ~4 chars/token for the prompt,
    plus the reserved completion budget. Good enough for rate accounting."""
    chars = 0
    for m in messages:
        content = m.get("content") or ""
        chars += len(content) if isinstance(content, str) else len(str(content))
    return chars // 4 + max_tokens
