"""RAMP Gateway — in-memory + SQLite state stores."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# ---------------------------------------------------------------------------
# In-memory stores (replaced by a real DB in production)
# ---------------------------------------------------------------------------

# agent_id -> agent metadata
agents: dict[str, dict[str, Any]] = {}

# session_id -> session metadata
sessions: dict[str, dict[str, Any]] = {}

# agent_id -> latest state
agent_states: dict[str, str] = {}

# message_id -> action request (pending human decision)
pending_actions: dict[str, dict[str, Any]] = {}

# message_id -> action response (human decision made)
resolved_actions: dict[str, dict[str, Any]] = {}

# Processed message IDs for idempotency
seen_message_ids: set[str] = set()

# agent_id -> last seq seen
last_seq: dict[str, int] = {}

# agent_id -> list of recent events (for WebSocket broadcast)
event_queues: dict[str, list[dict[str, Any]]] = {}

# Global event list for the web UI
_global_events: list[dict[str, Any]] = []

# ---------------------------------------------------------------------------
# SQLite audit trail
# ---------------------------------------------------------------------------

import os
_DB_PATH = os.environ.get("RAMP_AUDIT_DB", "ramp_audit.db")
_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    global _db
    _db = await aiosqlite.connect(_DB_PATH)
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS audit (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_id      TEXT UNIQUE NOT NULL,
            event_type    TEXT NOT NULL,
            agent_id      TEXT,
            session_id    TEXT,
            principal_id  TEXT,
            timestamp     TEXT NOT NULL,
            details       TEXT,
            record_hash   TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            chain_index   INTEGER NOT NULL
        )
    """)
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


async def get_last_hash(agent_id: str) -> tuple[str, int]:
    """Return (last_hash, chain_index) for the given agent."""
    assert _db is not None
    cursor = await _db.execute(
        "SELECT record_hash, chain_index FROM audit WHERE agent_id = ? ORDER BY chain_index DESC LIMIT 1",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row:
        return row[0], row[1]
    return "sha256:" + "0" * 64, -1


async def append_audit(
    event_type: str,
    agent_id: str | None = None,
    session_id: str | None = None,
    principal_id: str | None = None,
    details: dict | None = None,
) -> dict:
    """Append a hash-chained audit record."""
    assert _db is not None

    prev_hash, prev_index = await get_last_hash(agent_id or "__global__")
    chain_index = prev_index + 1
    audit_id = f"aud_{uuid.uuid4().hex[:12]}"
    ts = datetime.now(timezone.utc).isoformat()
    details_json = json.dumps(details or {}, sort_keys=True)

    # Compute hash: SHA-256(audit_id + event_type + agent_id + timestamp + details + previous_hash)
    hash_input = f"{audit_id}|{event_type}|{agent_id}|{ts}|{details_json}|{prev_hash}"
    record_hash = "sha256:" + hashlib.sha256(hash_input.encode()).hexdigest()

    await _db.execute(
        """INSERT INTO audit (audit_id, event_type, agent_id, session_id, principal_id,
           timestamp, details, record_hash, previous_hash, chain_index)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (audit_id, event_type, agent_id, session_id, principal_id,
         ts, details_json, record_hash, prev_hash, chain_index),
    )
    await _db.commit()

    record = {
        "audit_id": audit_id,
        "event_type": event_type,
        "agent_id": agent_id,
        "session_id": session_id,
        "principal_id": principal_id,
        "timestamp": ts,
        "details": details or {},
        "integrity": {
            "record_hash": record_hash,
            "previous_hash": prev_hash,
            "chain_index": chain_index,
        },
    }
    return record


async def query_audit(
    agent_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Query audit records with optional filters."""
    assert _db is not None
    clauses = []
    params: list[Any] = []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])

    cursor = await _db.execute(
        f"SELECT audit_id, event_type, agent_id, session_id, principal_id, timestamp, details, record_hash, previous_hash, chain_index FROM audit{where} ORDER BY chain_index DESC LIMIT ? OFFSET ?",
        params,
    )
    rows = await cursor.fetchall()
    return [
        {
            "audit_id": r[0],
            "event_type": r[1],
            "agent_id": r[2],
            "session_id": r[3],
            "principal_id": r[4],
            "timestamp": r[5],
            "details": json.loads(r[6]),
            "integrity": {
                "record_hash": r[7],
                "previous_hash": r[8],
                "chain_index": r[9],
            },
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Global event bus (for WebSocket broadcast to web UI)
# ---------------------------------------------------------------------------

def push_event(event: dict[str, Any]) -> None:
    """Push an event to the global event list for the web UI."""
    event["_ts"] = time.time()
    _global_events.append(event)
    # Keep only last 1000 events in memory
    if len(_global_events) > 1000:
        _global_events.pop(0)


def get_events_since(since_ts: float = 0) -> list[dict[str, Any]]:
    """Get events newer than the given timestamp."""
    return [e for e in _global_events if e.get("_ts", 0) > since_ts]
