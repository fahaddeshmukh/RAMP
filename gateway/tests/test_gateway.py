"""Smoke tests for the RAMP Gateway — FastAPI integration tests."""

import asyncio
import os
import tempfile
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set audit DB to a temp file before importing the app
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["RAMP_AUDIT_DB"] = _tmp_db.name

from app.main import app
from app import store
from app.policies import reset_session_costs


API_KEY = "ramp-demo-key-2026"
HEADERS = {"X-RAMP-API-Key": API_KEY}
AGENT_ID = "agent:test_agent"


@pytest_asyncio.fixture
async def client():
    """Provide a fresh test client with a clean store for each test."""
    # Reset all in-memory state
    store.agents.clear()
    store.sessions.clear()
    store.agent_states.clear()
    store.pending_actions.clear()
    store.resolved_actions.clear()
    store.seen_message_ids.clear()
    store.last_seq.clear()
    store._global_events.clear()
    reset_session_costs(AGENT_ID)

    # Manually trigger lifespan (init_db)
    await store.init_db()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await store.close_db()


def _sign(envelope: dict) -> dict:
    """Sign an envelope for testing."""
    from ramp_sdk.signing import sign_envelope
    envelope["signature"] = sign_envelope(envelope, API_KEY)
    return envelope


def _envelope(msg_id: str, seq: int, msg_type: str, payload: dict, session_id: str = "sess_test") -> dict:
    """Build a signed RAMP envelope."""
    env = {
        "ramp_version": "0.2.0",
        "message_id": msg_id,
        "agent_id": AGENT_ID,
        "session_id": session_id,
        "principal_id": "user:test",
        "timestamp": "2026-02-23T00:00:00Z",
        "sequence_number": seq,
        "message_type": msg_type,
        "nonce": f"nonce_{msg_id}",
        "signature": "",
        "payload": payload,
    }
    return _sign(env)


# ---------------------------------------------------------------------------
# Registration & Sessions
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_register_agent(client):
    resp = await client.post("/ramp/v1/agents/register", json={
        "agent_id": AGENT_ID,
        "agent_name": "Test Agent",
        "capabilities": ["test"],
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"


@pytest.mark.anyio
async def test_register_and_start_session(client):
    await client.post("/ramp/v1/agents/register", json={
        "agent_id": AGENT_ID,
    }, headers=HEADERS)

    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={
        "session_id": "sess_test",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_telemetry_accepted(client):
    # Setup
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    env = _envelope("msg_001", 1, "telemetry", {"state": "EXECUTING", "task_description": "testing"})
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.anyio
async def test_invalid_signature_rejected(client):
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    env = _envelope("msg_002", 1, "telemetry", {"state": "EXECUTING"})
    env["signature"] = "bad_signature"  # tamper

    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "RAMP-4002"


@pytest.mark.anyio
async def test_invalid_state_transition_rejected(client):
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    # IDLE -> AWAITING_HUMAN_INPUT is not a valid transition via telemetry
    env = _envelope("msg_003", 1, "telemetry", {"state": "AWAITING_HUMAN_INPUT"})
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4004"


@pytest.mark.anyio
async def test_duplicate_message_rejected(client):
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    env = _envelope("msg_dup", 1, "telemetry", {"state": "EXECUTING"})
    resp1 = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp1.status_code == 200

    resp2 = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# HITL Flow
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_hitl_approve_flow(client):
    """Full HITL flow: agent requests action, human approves, agent gets response."""
    # Setup
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    # Agent must be EXECUTING to send an action request
    env_exec = _envelope("msg_exec", 1, "telemetry", {"state": "EXECUTING"})
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_exec, headers=HEADERS)

    # Agent sends action request
    action_payload = {
        "title": "Approve test?",
        "body": "This is a test action request.",
        "options": [
            {"action_id": "yes", "label": "Yes"},
            {"action_id": "no", "label": "No"},
        ],
        "timeout_seconds": 60,
        "fallback_action_id": "no",
        "risk_assessment": {"level": "low", "factors": ["test"]},
    }
    env_action = _envelope("msg_action", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_action, headers=HEADERS)
    assert resp.status_code == 200

    # Check agent is now AWAITING_HUMAN_INPUT
    agent_resp = await client.get(f"/ramp/v1/agents/{AGENT_ID}")
    assert agent_resp.json()["state"] == "AWAITING_HUMAN_INPUT"

    # Human resolves the action
    resolve_resp = await client.post("/ramp/v1/actions/msg_action/resolve", json={
        "resolution": "approved",
        "selected_action_id": "yes",
        "reason": "Looks good",
    }, headers=HEADERS)
    assert resolve_resp.status_code == 200

    # Agent polls for response
    poll_resp = await client.get(
        f"/ramp/v1/agents/{AGENT_ID}/actions/msg_action/response",
        headers=HEADERS,
    )
    assert poll_resp.status_code == 200
    data = poll_resp.json()
    assert data["status"] == "resolved"
    assert data["response"]["resolution"] == "approved"
    assert data["response"]["selected_action_id"] == "yes"


@pytest.mark.anyio
async def test_action_request_from_idle_rejected(client):
    """Action request from IDLE state should be rejected (must be EXECUTING)."""
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    # Agent is in IDLE — action request should fail
    action_payload = {
        "title": "Should fail",
        "body": "Agent is IDLE",
        "options": [{"action_id": "ok", "label": "OK"}],
        "timeout_seconds": 30,
        "risk_assessment": {"level": "low", "factors": ["test"]},
    }
    env = _envelope("msg_idle_action", 1, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4004"


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cost_tracking_cumulative(client):
    """Cost tracking should use cumulative values, not add them up."""
    await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)

    # Send telemetry with cumulative costs: 10, 50, 90 (under the 100 limit)
    for i, cost in enumerate([10.0, 50.0, 90.0], start=1):
        env = _envelope(f"msg_cost_{i}", i, "telemetry", {
            "state": "EXECUTING",
            "resources": {"estimated_cost_usd": cost},
        })
        resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
        assert resp.status_code == 200, f"Cost {cost} should be under limit but got {resp.status_code}: {resp.json()}"

    # If costs were added (10+50+90=150), this would have already failed at 90.
    # With cumulative tracking, 90 < 100, so all pass. Now send 110 which should fail.
    env = _envelope("msg_cost_over", 4, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 110.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "RAMP-4011"
