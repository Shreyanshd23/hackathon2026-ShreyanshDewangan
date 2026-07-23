"""
Durable state (SQLite)
────────────────────────
Replaces the original in-memory module globals that vanished on restart.

Four tables:
  • tickets       — the latest state/result of every ticket (survives restarts)
  • actions       — append-only audit of every tool call (the real audit log)
  • dead_letter   — tickets that failed terminally, so they are never lost
  • idempotency   — keyed results, so a retried irreversible action is not
                    applied twice

Concurrency: one connection shared across threads (check_same_thread=False),
serialised by a lock. Adequate and correct for a single-node ticket processor;
the interface is what a Postgres swap would keep.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    status    TEXT,
                    payload   TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id  TEXT,
                    tool       TEXT,
                    arguments  TEXT,
                    result     TEXT,
                    ok         INTEGER,
                    verdict    TEXT,
                    ts         TEXT
                );
                CREATE TABLE IF NOT EXISTS dead_letter (
                    ticket_id TEXT,
                    error     TEXT,
                    payload   TEXT,
                    ts        TEXT
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key    TEXT PRIMARY KEY,
                    result TEXT,
                    ts     TEXT
                );
                """
            )
            self._conn.commit()

    # ── tickets ──────────────────────────────────────────────
    def save_ticket(self, ticket_id: str, status: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tickets(ticket_id,status,payload,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (ticket_id, status, json.dumps(payload, default=str), _now()),
            )
            self._conn.commit()

    def get_ticket(self, ticket_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    # ── audit actions ────────────────────────────────────────
    def record_action(self, ticket_id: str, tool: str, arguments: dict, result: Any, ok: bool, verdict: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO actions(ticket_id,tool,arguments,result,ok,verdict,ts) VALUES(?,?,?,?,?,?,?)",
                (ticket_id, tool, json.dumps(arguments, default=str), json.dumps(result, default=str),
                 int(ok), verdict, _now()),
            )
            self._conn.commit()

    def actions_for(self, ticket_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool,arguments,result,ok,verdict,ts FROM actions WHERE ticket_id=? ORDER BY id", (ticket_id,)
            ).fetchall()
        return [{"tool": r["tool"], "arguments": json.loads(r["arguments"]), "result": json.loads(r["result"]),
                 "ok": bool(r["ok"]), "verdict": r["verdict"], "ts": r["ts"]} for r in rows]

    # ── dead-letter queue ────────────────────────────────────
    def add_to_dlq(self, ticket_id: str, error: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_letter(ticket_id,error,payload,ts) VALUES(?,?,?,?)",
                (ticket_id, error, json.dumps(payload, default=str), _now()),
            )
            self._conn.commit()

    def dlq(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT ticket_id,error,ts FROM dead_letter ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    # ── idempotency ──────────────────────────────────────────
    def idempotent(self, key: str, produce) -> dict:
        """Return the stored result for `key`, else call produce(), store, return it."""
        with self._lock:
            row = self._conn.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row["result"])
        result = produce()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency(key,result,ts) VALUES(?,?,?)",
                (key, json.dumps(result, default=str), _now()),
            )
            self._conn.commit()
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
