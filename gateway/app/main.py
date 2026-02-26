"""RAMP Gateway — FastAPI reference implementation.

Implements RAMP v0.2 at Level 3 conformance (Governed) with 6 policy rule types:
mandatory_hitl, action_scope, aggregate_constraint, resource_constraint,
time_constraint, and rate_constraint.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import store
from app.policies import evaluate_message, PolicyViolation, reset_session_costs
from app.store import (
    action_events,
    agents,
    sessions,
    agent_states,
    pending_actions,
    resolved_actions,
    seen_message_ids,
    seen_nonces,
    last_seq,
    push_event,
    get_events_since,
    append_audit,
    query_audit,
)

# We import signing from the SDK package if available, otherwise inline
try:
    from ramp_sdk.signing import verify_signature
except ImportError:
    import hashlib
    import hmac

    def _canonical_json(obj: Any) -> bytes:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def verify_signature(envelope_dict: dict[str, Any], secret: str) -> bool:
        received_sig = envelope_dict.get("signature", "")
        # Remove signature key entirely — must match SDK sign_envelope canonicalization (spec §4.8.3)
        signable = {k: v for k, v in envelope_dict.items() if k != "signature"}
        canonical = _canonical_json(signable)
        # Spec §4.8.3 step 6: prefix with "hmac-sha256:"
        expected = "hmac-sha256:" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(received_sig, expected)


# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

# Simple static API key for the MVP (replaced with OAuth in production)
API_KEY = "ramp-demo-key-2026"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_db()
    yield
    await store.close_db()


app = FastAPI(
    title="RAMP Gateway",
    description="Reference implementation of the Remote Agent Monitoring Protocol",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_api_key(request: Request) -> None:
    key = request.headers.get("X-RAMP-API-Key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail={
            "error_code": "RAMP-4009",
            "message": "Invalid or missing API key",
        })


# ---------------------------------------------------------------------------
# Valid state transitions (Section 3.2)
# ---------------------------------------------------------------------------

VALID_TRANSITIONS = {
    "REGISTERED": {"IDLE"},
    "IDLE": {"EXECUTING", "SUSPENDED", "TERMINATED"},
    "EXECUTING": {"IDLE", "AWAITING_HUMAN_INPUT", "ERRORED", "SUSPENDED", "TERMINATED"},
    "AWAITING_HUMAN_INPUT": {"EXECUTING", "ERRORED", "SUSPENDED", "TERMINATED"},
    "SUSPENDED": {"IDLE", "TERMINATED"},
    "ERRORED": {"IDLE", "EXECUTING", "TERMINATED"},
    "TERMINATED": set(),
}


# ---------------------------------------------------------------------------
# Routes: Agent registration
# ---------------------------------------------------------------------------

@app.post("/ramp/v1/agents/register")
async def register_agent(request: Request):
    _check_api_key(request)
    body = await request.json()
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(400, {"error_code": "RAMP-4001", "message": "agent_id is required"})

    if agent_id in agents:
        raise HTTPException(409, {"error_code": "RAMP-4010", "message": f"Agent '{agent_id}' is already registered"})

    agents[agent_id] = {
        "agent_id": agent_id,
        "agent_name": body.get("agent_name", agent_id),
        "capabilities": body.get("capabilities", []),
        "registered_at": time.time(),
        # Store the presenting API key as this agent's signing secret.
        # In production, agents should register with a dedicated signing key;
        # here we re-use the gateway auth key so the demo works out of the box.
        "shared_secret": request.headers.get("X-RAMP-API-Key", API_KEY),
    }
    agent_states[agent_id] = "REGISTERED"

    await append_audit("agent_registered", agent_id=agent_id, details=agents[agent_id])
    push_event({"type": "agent_registered", "agent_id": agent_id, "data": agents[agent_id]})

    return {"status": "registered", "agent_id": agent_id, "negotiated_version": "0.2",
            "ramp_versions_supported": {"min": "0.2.0", "max": "0.2.0"},
            "shared_secret": agents[agent_id]["shared_secret"]}


# ---------------------------------------------------------------------------
# Routes: Session management
# ---------------------------------------------------------------------------

@app.post("/ramp/v1/agents/{agent_id}/sessions")
async def start_session(agent_id: str, request: Request):
    _check_api_key(request)
    if agent_id not in agents:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": f"Unknown agent: {agent_id}"})

    body = await request.json()
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(400, {"error_code": "RAMP-4001", "message": "session_id is required"})

    # Reject duplicate active sessions (spec §4.5: RAMP-4016)
    if any(s["agent_id"] == agent_id and s.get("active") for s in sessions.values()):
        raise HTTPException(409, {"error_code": "RAMP-4016",
                                  "message": "Session already active for this agent"})

    sessions[session_id] = {
        "session_id": session_id,
        "agent_id": agent_id,
        "started_at": time.time(),
        "active": True,
    }
    agent_states[agent_id] = "IDLE"
    last_seq[agent_id] = 0
    reset_session_costs(agent_id)

    await append_audit("session_started", agent_id=agent_id, session_id=session_id)
    push_event({"type": "session_started", "agent_id": agent_id, "session_id": session_id})

    return {"status": "created", "session_id": session_id}


@app.post("/ramp/v1/agents/{agent_id}/sessions/{session_id}/end")
async def end_session(agent_id: str, session_id: str, request: Request):
    _check_api_key(request)
    if agent_id not in agents:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": f"Unknown agent: {agent_id}"})
    if session_id in sessions:
        sessions[session_id]["active"] = False
    agent_states[agent_id] = "TERMINATED"

    await append_audit("session_ended", agent_id=agent_id, session_id=session_id)
    push_event({"type": "session_ended", "agent_id": agent_id, "session_id": session_id})

    return {"status": "ended", "session_id": session_id}


@app.post("/ramp/v1/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, request: Request):
    """Resume a SUSPENDED agent, transitioning it back to IDLE (spec §9.6.2)."""
    _check_api_key(request)
    if agent_id not in agents:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": f"Unknown agent: {agent_id}"})

    current = agent_states.get(agent_id, "UNKNOWN")
    if current != "SUSPENDED":
        raise HTTPException(409, {"error_code": "RAMP-4001",
                                  "message": f"Cannot resume agent in state '{current}' (must be SUSPENDED)"})

    agent_states[agent_id] = "IDLE"
    await append_audit("agent_resumed", agent_id=agent_id,
                       details={"from_state": "SUSPENDED", "to_state": "IDLE"})
    push_event({"type": "agent_resumed", "agent_id": agent_id})

    return {"status": "resumed", "agent_id": agent_id, "state": "IDLE"}


@app.post("/ramp/v1/agents/{agent_id}/messages")
async def receive_message(agent_id: str, request: Request):
    _check_api_key(request)

    if agent_id not in agents:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": f"Unknown agent: {agent_id}"})

    envelope = await request.json()

    # --- Validate envelope ---
    for field in ("message_id", "agent_id", "session_id", "timestamp", "sequence_number", "message_type", "payload", "signature", "principal_id", "nonce", "ramp_version"):
        if field not in envelope:
            raise HTTPException(400, {"error_code": "RAMP-4001", "message": f"Missing field: {field}"})

    # --- Idempotency ---
    msg_id = envelope["message_id"]
    if msg_id in seen_message_ids:
        raise HTTPException(409, {"error_code": "RAMP-4006", "message": "Duplicate message"})

    # --- Timestamp window (spec §4.2: reject if > 5 minutes from Gateway time) ---
    try:
        msg_ts = datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00"))
        skew = abs(datetime.now(timezone.utc).timestamp() - msg_ts.timestamp())
        if skew > 300:
            raise HTTPException(400, {
                "error_code": "RAMP-4001",
                "message": f"Timestamp outside 5-minute window (skew: {skew:.0f}s)",
            })
    except (ValueError, TypeError):
        raise HTTPException(400, {"error_code": "RAMP-4001", "message": "Invalid timestamp format"})

    # --- Nonce replay protection (spec §4.2: RAMP-4007) ---
    # Prune nonces older than the 5-minute window first
    cutoff = time.time() - 300
    stale = [n for n, t in seen_nonces.items() if t < cutoff]
    for n in stale:
        del seen_nonces[n]
    nonce = envelope["nonce"]
    if nonce in seen_nonces:
        raise HTTPException(400, {"error_code": "RAMP-4007", "message": "Duplicate nonce — possible replay attack"})

    # --- Verify signature (use per-agent shared_secret when available) ---
    agent_secret = agents.get(agent_id, {}).get("shared_secret", API_KEY)
    if not verify_signature(envelope, agent_secret):
        raise HTTPException(401, {"error_code": "RAMP-4002", "message": "Invalid HMAC signature"})

    # --- Sequence check ---
    seq = envelope["sequence_number"]
    if seq <= last_seq.get(agent_id, 0):
        raise HTTPException(400, {"error_code": "RAMP-4003", "message": f"Sequence {seq} <= last seen {last_seq.get(agent_id, 0)}"})

    # --- Policy evaluation ---
    try:
        warnings = evaluate_message(agent_id, envelope)
    except PolicyViolation as pv:
        # Determine enforcement action based on on_violation behavior
        new_state = None
        if pv.on_violation in ("suspend_and_notify", "suspend_until_allowed", "suspend_all_and_notify"):
            new_state = "SUSPENDED"
            agent_states[agent_id] = new_state

        await append_audit("policy_violated", agent_id=agent_id,
                           session_id=envelope.get("session_id"),
                           details={
                               "rule_id": pv.rule_id,
                               "rule_type": pv.rule_type,
                               "message": pv.message,
                               "on_violation": pv.on_violation,
                               "agent_new_state": new_state,
                           })
        push_event({
            "type": "policy_violation",
            "agent_id": agent_id,
            "rule_id": pv.rule_id,
            "message": pv.message,
            "on_violation": pv.on_violation,
            "agent_new_state": new_state,
        })

        status_code = 429 if pv.on_violation == "throttle_and_warn" else 403
        raise HTTPException(status_code, {
            "error_code": "RAMP-4011",
            "message": pv.message,
            "rule_id": pv.rule_id,
            "on_violation": pv.on_violation,
            "agent_new_state": new_state,
        })

    msg_type = envelope["message_type"]
    payload = envelope["payload"]

    # --- Type-specific validation (BEFORE accepting the message) ---
    if msg_type == "telemetry":
        new_state = payload.get("state")
        old_state = agent_states.get(agent_id, "IDLE")

        # Validate state transition
        if new_state and new_state != old_state:
            if new_state not in VALID_TRANSITIONS.get(old_state, set()):
                raise HTTPException(400, {
                    "error_code": "RAMP-4004",
                    "message": f"Invalid transition: {old_state} -> {new_state}",
                })

    elif msg_type == "action_request":
        # Validate state transition: only EXECUTING -> AWAITING_HUMAN_INPUT is valid
        old_state = agent_states.get(agent_id, "IDLE")
        if old_state != "EXECUTING":
            raise HTTPException(400, {
                "error_code": "RAMP-4004",
                "message": f"Invalid transition: {old_state} -> AWAITING_HUMAN_INPUT. Agent must be EXECUTING to request an action.",
            })
        # Spec §7.3 Rule 6: risk_assessment is REQUIRED on every Action Request
        if not payload.get("risk_assessment"):
            raise HTTPException(400, {
                "error_code": "RAMP-4001",
                "message": "action_request payload MUST include risk_assessment (spec §7.3 Rule 6)",
            })
        # Check no pending action already
        for mid, ar in pending_actions.items():
            if ar["agent_id"] == agent_id and ar["status"] == "pending":
                raise HTTPException(409, {
                    "error_code": "RAMP-4012",
                    "message": "Agent already has a pending Action Request",
                })

    # --- Accept message (only after all validation passes) ---
    seen_message_ids.add(msg_id)
    seen_nonces[nonce] = time.time()
    last_seq[agent_id] = seq

    # --- Process by type ---
    if msg_type == "telemetry":
        new_state = payload.get("state")
        old_state = agent_states.get(agent_id, "IDLE")

        if new_state and new_state != old_state:
            agent_states[agent_id] = new_state
            await append_audit("state_transition", agent_id=agent_id,
                               session_id=envelope["session_id"],
                               details={"from": old_state, "to": new_state})

        push_event({
            "type": "telemetry",
            "agent_id": agent_id,
            "session_id": envelope["session_id"],
            "message_id": msg_id,
            "payload": payload,
            "timestamp": envelope["timestamp"],
        })

    elif msg_type == "notification":
        await append_audit("notification_sent", agent_id=agent_id,
                           session_id=envelope["session_id"],
                           details={"title": payload.get("title"), "priority": payload.get("priority")})
        push_event({
            "type": "notification",
            "agent_id": agent_id,
            "session_id": envelope["session_id"],
            "message_id": msg_id,
            "payload": payload,
            "timestamp": envelope["timestamp"],
        })

    elif msg_type == "action_request":
        # Store pending action
        pending_actions[msg_id] = {
            "agent_id": agent_id,
            "session_id": envelope["session_id"],
            "message_id": msg_id,
            "payload": payload,
            "status": "pending",
            "created_at": time.time(),
            "timeout_seconds": payload.get("timeout_seconds", 300),
            # mandatory_hitl flag: if True, policy_auto_* resolutions are blocked (spec §9.3.1)
            "mandatory_hitl": payload.get("_mandatory_hitl", False),
        }
        agent_states[agent_id] = "AWAITING_HUMAN_INPUT"

        await append_audit("action_requested", agent_id=agent_id,
                           session_id=envelope["session_id"],
                           details={"title": payload.get("title"),
                                    "risk_level": payload.get("risk_assessment", {}).get("risk_level")})
        push_event({
            "type": "action_request",
            "agent_id": agent_id,
            "session_id": envelope["session_id"],
            "message_id": msg_id,
            "payload": payload,
            "timestamp": envelope["timestamp"],
        })

    response: dict[str, Any] = {"status": "accepted", "message_id": msg_id}
    if warnings:
        response["warnings"] = warnings
    return response


# ---------------------------------------------------------------------------
# Routes: Action response (human approves/denies via web UI)
# ---------------------------------------------------------------------------

@app.post("/ramp/v1/actions/{message_id}/resolve")
async def resolve_action(message_id: str, request: Request):
    """Human principal resolves an Action Request."""
    _check_api_key(request)

    if message_id not in pending_actions:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": "Action request not found"})

    action = pending_actions[message_id]
    if action["status"] != "pending":
        raise HTTPException(409, {"error_code": "RAMP-4017", "message": "Action already resolved"})

    body = await request.json()
    resolution = body.get("resolution", "approved")  # operator convenience: "approved" or "denied"
    resolution_type_req = body.get("resolution_type", "human_decision")
    selected_action_id = body.get("selected_action_id")
    reason = body.get("reason", "")
    resolver_id = body.get("resolver_id", "user:principal")

    # mandatory_hitl guard: policy auto-resolution is forbidden when the rule fired
    if action.get("mandatory_hitl") and resolution_type_req in ("policy_auto_approved", "policy_auto_denied"):
        raise HTTPException(403, {
            "error_code": "RAMP-4011",
            "message": "Action is marked mandatory_hitl — automated resolution is not permitted",
        })

    action["status"] = "resolved"
    resolved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    response = {
        "request_message_id": message_id,
        "resolution_type": "human_decision",
        "selected_action_id": selected_action_id,
        "resolved_by": resolver_id,
        "resolver_role": body.get("resolver_role", "owner"),
        "resolved_at": resolved_at,
        "reason": reason,
    }
    resolved_actions[message_id] = response

    # Transition agent back to EXECUTING
    agent_id = action["agent_id"]
    agent_states[agent_id] = "EXECUTING"

    # Wake up any long-polling agent waiting for this response
    event = action_events.pop(message_id, None)
    if event:
        event.set()

    await append_audit("action_resolved", agent_id=agent_id,
                       session_id=action["session_id"],
                       principal_id=resolver_id,
                       details={"resolution_type": "human_decision", "selected_action_id": selected_action_id})
    push_event({
        "type": "action_resolved",
        "agent_id": agent_id,
        "message_id": message_id,
        "resolution_type": "human_decision",
        "selected_action_id": selected_action_id,
    })

    return {"status": "resolved", "resolution_type": "human_decision"}


@app.get("/ramp/v1/agents/{agent_id}/actions/{message_id}/response")
async def get_action_response(agent_id: str, message_id: str, request: Request, wait: int = 0):
    """Agent polls for the human's response to an Action Request.

    Supports long-polling via the ``wait`` query parameter (seconds, max 120).
    When ``wait > 0``, the gateway holds the connection open until the action
    is resolved or the wait period elapses.
    """
    _check_api_key(request)
    wait = min(max(wait, 0), 120)

    if message_id in resolved_actions:
        return {"status": "resolved", "response": resolved_actions[message_id]}

    if message_id not in pending_actions:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": "Action request not found"})

    action = pending_actions[message_id]

    # Check timeout
    elapsed = time.time() - action["created_at"]
    if elapsed > action["timeout_seconds"]:
        fallback = action["payload"].get("fallback_action_id")
        response = {
            "request_message_id": message_id,
            "resolution_type": "timeout_fallback",
            "selected_action_id": fallback,
        }
        action["status"] = "timed_out"
        resolved_actions[message_id] = response
        agent_states[agent_id] = "EXECUTING"
        await append_audit("action_resolved", agent_id=agent_id,
                           session_id=action.get("session_id"),
                           details={"resolution_type": "timeout_fallback",
                                    "selected_action_id": fallback,
                                    "reason": "action timed out before human response"})
        return {"status": "resolved", "response": response}

    # Long-poll: wait for resolution or timeout
    if wait > 0:
        event = action_events.get(message_id)
        if event is None:
            event = asyncio.Event()
            action_events[message_id] = event

        # Don't wait longer than the action's own timeout
        remaining = action["timeout_seconds"] - elapsed
        effective_wait = min(wait, remaining)

        try:
            await asyncio.wait_for(event.wait(), timeout=effective_wait)
        except asyncio.TimeoutError:
            pass

        # Re-check after waiting
        if message_id in resolved_actions:
            action_events.pop(message_id, None)
            return {"status": "resolved", "response": resolved_actions[message_id]}

        # Check if action timed out while we were waiting
        elapsed = time.time() - action["created_at"]
        if elapsed > action["timeout_seconds"]:
            fallback = action["payload"].get("fallback_action_id")
            response = {
                "request_message_id": message_id,
                "resolution_type": "timeout_fallback",
                "selected_action_id": fallback,
            }
            action["status"] = "timed_out"
            resolved_actions[message_id] = response
            agent_states[agent_id] = "EXECUTING"
            action_events.pop(message_id, None)
            await append_audit("action_resolved", agent_id=agent_id,
                               session_id=action.get("session_id"),
                               details={"resolution_type": "timeout_fallback",
                                        "selected_action_id": fallback,
                                        "reason": "action timed out during long poll"})
            return {"status": "resolved", "response": response}

    return {"status": "pending", "elapsed_seconds": int(time.time() - action["created_at"])}


# ---------------------------------------------------------------------------
# Routes: Web UI data endpoints
# ---------------------------------------------------------------------------

@app.get("/ramp/v1/info")
async def gateway_info():
    """Gateway capability discovery endpoint (spec §4.7.3).

    Returns the gateway's supported RAMP versions and conformance level so
    that clients can perform version negotiation before sending messages.
    """
    return {
        "gateway_id": "gateway:ramp-reference",
        "ramp_versions_supported": {"min": "0.2.0", "max": "0.2.0"},
        "conformance_level": 3,
        "transport_bindings": ["http", "websocket"],
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


@app.get("/ramp/v1/agents")
async def list_agents():
    """List all registered agents with their current state."""
    result = []
    for aid, meta in agents.items():
        result.append({
            **meta,
            "state": agent_states.get(aid, "UNKNOWN"),
        })
    return {"agents": result}


@app.get("/ramp/v1/agents/{agent_id}")
async def get_agent(agent_id: str):
    if agent_id not in agents:
        raise HTTPException(404, {"error_code": "RAMP-4008", "message": f"Unknown agent: {agent_id}"})
    return {
        **agents[agent_id],
        "state": agent_states.get(agent_id, "UNKNOWN"),
    }


@app.get("/ramp/v1/actions/pending")
async def list_pending_actions():
    """List all pending Action Requests (for the web UI)."""
    pending = [
        {**v, "elapsed_seconds": int(time.time() - v["created_at"])}
        for v in pending_actions.values()
        if v["status"] == "pending"
    ]
    return {"pending_actions": pending}


@app.get("/ramp/v1/audit")
async def get_audit(
    agent_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Query the audit trail."""
    records = await query_audit(agent_id=agent_id, event_type=event_type, limit=limit, offset=offset)
    return {"records": records, "count": len(records)}


# ---------------------------------------------------------------------------
# WebSocket: real-time event stream for the web UI
# ---------------------------------------------------------------------------

_ws_clients: set[WebSocket] = set()


@app.websocket("/ramp/v1/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # Send current state snapshot
        await ws.send_json({
            "type": "snapshot",
            "agents": [
                {**meta, "state": agent_states.get(aid, "UNKNOWN")}
                for aid, meta in agents.items()
            ],
            "pending_actions": [
                {**v, "elapsed_seconds": int(time.time() - v["created_at"])}
                for v in pending_actions.values()
                if v["status"] == "pending"
            ],
        })

        # Stream events
        last_ts = time.time()
        while True:
            # Check for new events every 500ms
            await asyncio.sleep(0.5)
            new_events = get_events_since(last_ts)
            for event in new_events:
                await ws.send_json(event)
                last_ts = event.get("_ts", last_ts)
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2.0", "protocol": "RAMP"}
