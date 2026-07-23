"""
Observability: structured logs + span tracing
───────────────────────────────────────────────
Replaces scattered print() calls. Every unit of work (ticket, agent stage,
tool call, LLM call) runs inside a `span`, producing a timed, attributed,
status-bearing record with a trace id that ties a whole ticket together.

Design choices:
  • Vendor-neutral. If `opentelemetry` is installed, spans are also emitted to
    it (view in Jaeger/Phoenix). If not, a lightweight in-process recorder
    keeps the exact same data — so CI and offline demos need no collector.
  • The recorded spans ARE the audit trail. `audit_log.json` becomes a derived
    export of the trace, not a hand-maintained parallel structure.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# ── optional OpenTelemetry bridge ────────────────────────────
try:  # pragma: no cover - exercised only when otel is installed
    from opentelemetry import trace as _otel_trace

    _OTEL_TRACER = _otel_trace.get_tracer("shopwave.support")
except Exception:  # noqa: BLE001
    _OTEL_TRACER = None


# ── structured JSON logging ──────────────────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)  # type: ignore[attr-defined]
        return json.dumps(payload, default=str)


def get_logger(name: str = "shopwave") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
        logger.propagate = False
    return logger


log = get_logger()


def log_event(msg: str, **fields: Any) -> None:
    """Structured log line with arbitrary key/value context."""
    log.info(msg, extra={"extra_fields": fields})


# ── span recorder ────────────────────────────────────────────
class Span:
    def __init__(self, trace_id: str, name: str, attributes: dict) -> None:
        self.trace_id = trace_id
        self.name = name
        self.attributes = dict(attributes)
        self.status = "ok"
        self.error: str | None = None
        self.start = time.monotonic()
        self.duration_ms: float = 0.0

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "attributes": self.attributes,
            **({"error": self.error} if self.error else {}),
        }


class Tracer:
    """Collects spans grouped by trace id (one trace == one ticket)."""

    def __init__(self) -> None:
        self._traces: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def current_trace_id(self) -> str | None:
        return getattr(self._local, "trace_id", None)

    @contextmanager
    def trace(self, trace_id: str | None = None, **attrs: Any) -> Iterator[str]:
        """Open a new trace context (a ticket). Nested spans attach to it."""
        tid = trace_id or uuid.uuid4().hex[:12]
        prev = getattr(self._local, "trace_id", None)
        self._local.trace_id = tid
        with self._lock:
            self._traces.setdefault(tid, [])
        try:
            yield tid
        finally:
            self._local.trace_id = prev

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        tid = self.current_trace_id or uuid.uuid4().hex[:12]
        s = Span(tid, name, attributes)
        otel_cm = _OTEL_TRACER.start_as_current_span(name) if _OTEL_TRACER else None
        if otel_cm:
            otel_cm.__enter__()
        try:
            yield s
        except Exception as exc:  # record and re-raise
            s.status = "error"
            s.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            s.duration_ms = (time.monotonic() - s.start) * 1000
            with self._lock:
                self._traces.setdefault(tid, []).append(s.to_dict())
            log_event(f"span:{name}", trace_id=tid, status=s.status,
                      duration_ms=round(s.duration_ms, 2), **s.attributes)
            if otel_cm:
                otel_cm.__exit__(None, None, None)

    def spans_for(self, trace_id: str) -> list[dict]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def reset(self) -> None:
        with self._lock:
            self._traces.clear()


tracer = Tracer()
