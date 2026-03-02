"""Smoke tests for the RAMP Gateway — FastAPI integration tests."""

import asyncio
import os
import tempfile
import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set audit DB to a temp file before importing the app
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["RAMP_AUDIT_DB"] = _tmp_db.name

from app.main import app
from app import store
from app.policies import (
    reset_session_costs,
    set_policies,
    aggregate_costs,
    aggregate_telemetry_costs,
    aggregate_action_costs,
    action_request_costs,
    session_start_times,
    message_timestamps,
)


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
    store.seen_nonces.clear()
    store.last_seq.clear()
    store._global_events.clear()
    store.action_events.clear()
    reset_session_costs(AGENT_ID)
    aggregate_costs.clear()
    aggregate_telemetry_costs.clear()
    aggregate_action_costs.clear()
    action_request_costs.clear()
    session_start_times.clear()

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
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "sequence_number": seq,
        "message_type": msg_type,
        "nonce": f"nonce_{msg_id}",
        "signature": "",
        "payload": payload,
    }
    return _sign(env)


async def _setup_agent(client, agent_id=AGENT_ID):
    """Register agent and start session."""
    await client.post("/ramp/v1/agents/register", json={"agent_id": agent_id}, headers=HEADERS)
    await client.post(f"/ramp/v1/agents/{agent_id}/sessions", json={"session_id": "sess_test"}, headers=HEADERS)


async def _set_executing(client, msg_id="msg_exec", seq=1):
    """Send telemetry to put agent in EXECUTING state."""
    env = _envelope(msg_id, seq, "telemetry", {"state": "EXECUTING", "task_description": "testing"})
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200
    return resp


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
    await _setup_agent(client)

    env = _envelope("msg_001", 1, "telemetry", {"state": "EXECUTING", "task_description": "testing"})
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.anyio
async def test_invalid_signature_rejected(client):
    await _setup_agent(client)

    env = _envelope("msg_002", 1, "telemetry", {"state": "EXECUTING"})
    env["signature"] = "bad_signature"  # tamper

    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "RAMP-4002"


@pytest.mark.anyio
async def test_invalid_state_transition_rejected(client):
    await _setup_agent(client)

    # IDLE -> AWAITING_HUMAN_INPUT is not a valid transition via telemetry
    env = _envelope("msg_003", 1, "telemetry", {"state": "AWAITING_HUMAN_INPUT"})
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4004"


@pytest.mark.anyio
async def test_duplicate_message_rejected(client):
    await _setup_agent(client)

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
    await _setup_agent(client)
    await _set_executing(client)

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
        "risk_assessment": {"risk_level": "low"},
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
    assert data["response"]["resolution_type"] == "human_decision"
    assert data["response"]["selected_action_id"] == "yes"


@pytest.mark.anyio
async def test_action_request_from_idle_rejected(client):
    """Action request from IDLE state should be rejected (must be EXECUTING)."""
    await _setup_agent(client)

    # Agent is in IDLE — action request should fail
    action_payload = {
        "title": "Should fail",
        "body": "Agent is IDLE",
        "options": [{"action_id": "ok", "label": "OK"}],
        "timeout_seconds": 30,
        "risk_assessment": {"risk_level": "low"},
    }
    env = _envelope("msg_idle_action", 1, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4004"


# ---------------------------------------------------------------------------
# Policy enforcement: resource_constraint (existing)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cost_tracking_cumulative(client):
    """Cost tracking should use cumulative values, not add them up."""
    await _setup_agent(client)

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


# ---------------------------------------------------------------------------
# Policy enforcement: resource_constraint — wall_time_seconds (gateway clock)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_wall_time_constraint_within_limit(client):
    """Messages within the wall-time limit should pass."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "max_duration",
        "type": "resource_constraint",
        "resource": "wall_time_seconds",
        "limit": 3600,  # 1 hour
        "on_violation": "suspend_and_notify",
    }])

    env = _envelope("msg_wt_ok", 1, "telemetry", {
        "state": "EXECUTING", "task_description": "working",
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_wall_time_constraint_exceeded(client):
    """Session exceeding wall-time limit should be suspended."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "max_duration",
        "type": "resource_constraint",
        "resource": "wall_time_seconds",
        "limit": 10,  # 10 seconds
        "on_violation": "suspend_and_notify",
    }])

    # Backdate the session start so elapsed > 10 seconds
    from app.policies import session_start_times
    session_start_times[AGENT_ID] = time.time() - 60  # 60 seconds ago

    env = _envelope("msg_wt_fail", 1, "telemetry", {
        "state": "EXECUTING", "task_description": "overtime",
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error_code"] == "RAMP-4011"
    assert "duration" in detail["message"].lower() or "exceeds limit" in detail["message"].lower()


# ---------------------------------------------------------------------------
# Policy enforcement: action_scope (new)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_action_scope_allowed(client):
    """Action with an allowed category should pass."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "scope_test",
        "type": "action_scope",
        "allowed_categories": ["search", "notify"],
        "denied_categories": [],
        "on_violation": "deny_and_notify",
    }])

    await _set_executing(client)

    action_payload = {
        "title": "Search flights",
        "body": "Searching...",
        "options": [{"action_id": "ok", "label": "OK"}],
        "timeout_seconds": 30,
        "risk_assessment": {"risk_level": "low", "action_category": "search"},
    }
    env = _envelope("msg_scope_ok", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_action_scope_denied(client):
    """Action with a denied category should be rejected."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "scope_deny",
        "type": "action_scope",
        "allowed_categories": [],
        "denied_categories": ["purchase", "delete"],
        "on_violation": "deny_and_notify",
    }])

    await _set_executing(client)

    action_payload = {
        "title": "Buy something",
        "body": "Purchasing...",
        "options": [{"action_id": "buy", "label": "Buy"}],
        "timeout_seconds": 30,
        "risk_assessment": {"risk_level": "high", "action_category": "purchase"},
    }
    env = _envelope("msg_scope_deny", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "RAMP-4011"
    assert "purchase" in resp.json()["detail"]["message"]


@pytest.mark.anyio
async def test_action_scope_unlisted_category(client):
    """Category not in the allowlist should be denied."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "scope_allowlist",
        "type": "action_scope",
        "allowed_categories": ["search"],
        "denied_categories": [],
        "on_violation": "deny_and_notify",
    }])

    await _set_executing(client)

    action_payload = {
        "title": "Send email",
        "body": "Sending...",
        "options": [{"action_id": "send", "label": "Send"}],
        "timeout_seconds": 30,
        "risk_assessment": {"risk_level": "low", "action_category": "email"},
    }
    env = _envelope("msg_scope_unlisted", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    assert "email" in resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Policy enforcement: time_constraint (new)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_time_constraint_within_hours(client):
    """Messages during allowed hours should pass."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    await _setup_agent(client)

    # Wednesday at 10:00 UTC
    fake_now = datetime(2026, 2, 25, 10, 0, tzinfo=timezone.utc)

    set_policies(AGENT_ID, [{
        "rule_id": "biz_hours",
        "type": "time_constraint",
        "allowed_days": ["mon", "tue", "wed", "thu", "fri"],
        "allowed_hours_utc": {"start": "08:00", "end": "18:00"},
        "on_violation": "suspend_until_allowed",
    }])

    with patch("app.policies._eval_time_constraint", wraps=None) as _:
        # We need to pass _now through evaluate_message, so we patch at a higher level
        pass

    # Use the _now parameter via direct policy evaluation
    from app.policies import evaluate_message
    env_dict = {
        "message_type": "telemetry",
        "payload": {"state": "EXECUTING"},
        "principal_id": "user:test",
    }
    # Should not raise
    warnings = evaluate_message(AGENT_ID, env_dict, _now=fake_now)
    assert isinstance(warnings, list)


@pytest.mark.anyio
async def test_time_constraint_outside_hours(client):
    """Messages outside allowed hours should be rejected."""
    from datetime import datetime, timezone

    await _setup_agent(client)

    # Wednesday at 22:00 UTC (outside 08:00-18:00)
    fake_now = datetime(2026, 2, 25, 22, 0, tzinfo=timezone.utc)

    set_policies(AGENT_ID, [{
        "rule_id": "biz_hours",
        "type": "time_constraint",
        "allowed_days": ["mon", "tue", "wed", "thu", "fri"],
        "allowed_hours_utc": {"start": "08:00", "end": "18:00"},
        "on_violation": "suspend_until_allowed",
    }])

    from app.policies import evaluate_message, PolicyViolation
    env_dict = {
        "message_type": "telemetry",
        "payload": {"state": "EXECUTING"},
        "principal_id": "user:test",
    }
    with pytest.raises(PolicyViolation) as exc_info:
        evaluate_message(AGENT_ID, env_dict, _now=fake_now)
    assert exc_info.value.rule_type == "time_constraint"
    assert exc_info.value.on_violation == "suspend_until_allowed"


@pytest.mark.anyio
async def test_time_constraint_wrong_day(client):
    """Messages on disallowed days should be rejected."""
    from datetime import datetime, timezone

    await _setup_agent(client)

    # Saturday at 10:00 UTC (weekend, not in allowed_days)
    fake_now = datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc)

    set_policies(AGENT_ID, [{
        "rule_id": "weekday_only",
        "type": "time_constraint",
        "allowed_days": ["mon", "tue", "wed", "thu", "fri"],
        "allowed_hours_utc": {"start": "08:00", "end": "18:00"},
        "on_violation": "suspend_until_allowed",
    }])

    from app.policies import evaluate_message, PolicyViolation
    env_dict = {
        "message_type": "telemetry",
        "payload": {"state": "EXECUTING"},
        "principal_id": "user:test",
    }
    with pytest.raises(PolicyViolation) as exc_info:
        evaluate_message(AGENT_ID, env_dict, _now=fake_now)
    assert exc_info.value.rule_type == "time_constraint"
    assert "sat" in exc_info.value.message


# ---------------------------------------------------------------------------
# Policy enforcement: aggregate_constraint (new)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_aggregate_constraint_under_limit(client):
    """Costs under aggregate limit should pass."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "global_budget",
        "type": "aggregate_constraint",
        "resource": "estimated_cost_usd",
        "limit": 500.0,
        "scope": "principal",
        "warning_threshold_pct": 80,
        "on_violation": "suspend_all_and_notify",
    }])

    env = _envelope("msg_agg_1", 1, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 200.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_aggregate_constraint_over_limit(client):
    """Telemetry + action request costs combined should trigger aggregate limit."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "global_budget",
        "type": "aggregate_constraint",
        "resource": "estimated_cost_usd",
        "limit": 500.0,
        "scope": "principal",
        "warning_threshold_pct": 80,
        "on_violation": "suspend_all_and_notify",
    }])

    # Telemetry reports $300 cumulative spend — under $500 limit alone
    await _set_executing(client)
    env_telem = _envelope("msg_agg_telem", 2, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 300.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_telem, headers=HEADERS)
    assert resp.status_code == 200  # $300 — still under limit

    # Now send an action request with $250 estimated cost: $300 + $250 = $550 > $500
    action_payload = {
        "title": "Launch marketing campaign",
        "body": "This will cost $250.",
        "options": [
            {"action_id": "launch", "label": "Launch"},
            {"action_id": "cancel", "label": "Cancel"},
        ],
        "timeout_seconds": 60,
        "fallback_action_id": "cancel",
        "risk_assessment": {"risk_level": "medium", "estimated_cost_usd": 250.0},
    }
    env_action = _envelope("msg_agg_action", 3, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_action, headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "RAMP-4011"
    assert resp.json()["detail"]["agent_new_state"] == "SUSPENDED"


@pytest.mark.anyio
async def test_aggregate_constraint_warning_threshold(client):
    """Costs at warning threshold should return a warning but not block."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "global_budget",
        "type": "aggregate_constraint",
        "resource": "estimated_cost_usd",
        "limit": 500.0,
        "scope": "principal",
        "warning_threshold_pct": 80,
        "on_violation": "suspend_all_and_notify",
    }])

    # 410 is 82% of 500 — above warning threshold but under limit
    env = _envelope("msg_agg_warn", 1, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 410.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200
    assert "warnings" in resp.json()
    assert any("82%" in w for w in resp.json()["warnings"])


# ---------------------------------------------------------------------------
# Policy enforcement: suspend behavior
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_suspend_and_notify_sets_suspended_state(client):
    """Violations with suspend_and_notify should force agent to SUSPENDED."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "strict_budget",
        "type": "resource_constraint",
        "resource": "estimated_cost_usd",
        "limit": 50.0,
        "window": "session",
        "on_violation": "suspend_and_notify",
    }])

    env = _envelope("msg_suspend", 1, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 60.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    assert store.agent_states[AGENT_ID] == "SUSPENDED"


# ---------------------------------------------------------------------------
# Long-polling
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_long_poll_immediate_resolve(client):
    """When action is resolved before wait expires, response returns immediately."""
    await _setup_agent(client)
    await _set_executing(client)

    # Send action request
    action_payload = {
        "title": "Long poll test",
        "body": "Testing",
        "options": [{"action_id": "yes", "label": "Yes"}, {"action_id": "no", "label": "No"}],
        "timeout_seconds": 60,
        "fallback_action_id": "no",
        "risk_assessment": {"risk_level": "low"},
    }
    env = _envelope("msg_lp", 2, "action_request", action_payload)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)

    # Resolve the action after a short delay (in background)
    async def resolve_soon():
        await asyncio.sleep(0.3)
        await client.post("/ramp/v1/actions/msg_lp/resolve", json={
            "resolution": "approved",
            "selected_action_id": "yes",
        }, headers=HEADERS)

    task = asyncio.create_task(resolve_soon())

    # Long-poll with 5-second wait — should return in ~0.3s, not 5s
    start = time.time()
    resp = await client.get(
        f"/ramp/v1/agents/{AGENT_ID}/actions/msg_lp/response?wait=5",
        headers=HEADERS,
    )
    elapsed = time.time() - start

    await task
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["response"]["selected_action_id"] == "yes"
    assert elapsed < 3.0  # Should resolve well before the 5s wait


@pytest.mark.anyio
async def test_long_poll_timeout(client):
    """When no resolution arrives within wait, returns pending."""
    await _setup_agent(client)
    await _set_executing(client)

    action_payload = {
        "title": "Long poll timeout test",
        "body": "Testing",
        "options": [{"action_id": "ok", "label": "OK"}],
        "timeout_seconds": 60,
        "risk_assessment": {"risk_level": "low"},
    }
    env = _envelope("msg_lp_timeout", 2, "action_request", action_payload)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)

    # Long-poll with 1-second wait — no one resolves
    resp = await client.get(
        f"/ramp/v1/agents/{AGENT_ID}/actions/msg_lp_timeout/response?wait=1",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


@pytest.mark.anyio
async def test_long_poll_backward_compat(client):
    """Without wait param, behaves like instant poll (backward compatible)."""
    await _setup_agent(client)
    await _set_executing(client)

    action_payload = {
        "title": "Compat test",
        "body": "Testing",
        "options": [{"action_id": "ok", "label": "OK"}],
        "timeout_seconds": 60,
        "risk_assessment": {"risk_level": "low"},
    }
    env = _envelope("msg_compat", 2, "action_request", action_payload)
    await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)

    # No wait param — should return immediately with pending
    start = time.time()
    resp = await client.get(
        f"/ramp/v1/agents/{AGENT_ID}/actions/msg_compat/response",
        headers=HEADERS,
    )
    elapsed = time.time() - start

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert elapsed < 1.0  # Should be nearly instant


# ---------------------------------------------------------------------------
# Policy evaluation precedence
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_precedence_action_scope_before_resource(client):
    """action_scope (precedence 3) should fire before resource_constraint (precedence 6)."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [
        {
            "rule_id": "budget",
            "type": "resource_constraint",
            "resource": "estimated_cost_usd",
            "limit": 1000.0,
            "window": "session",
            "on_violation": "deny_and_notify",
        },
        {
            "rule_id": "scope_block",
            "type": "action_scope",
            "denied_categories": ["purchase"],
            "on_violation": "deny_and_notify",
        },
    ])

    await _set_executing(client)

    action_payload = {
        "title": "Buy something",
        "body": "Cost $5",
        "options": [{"action_id": "buy", "label": "Buy"}],
        "timeout_seconds": 30,
        "risk_assessment": {"risk_level": "low", "action_category": "purchase", "estimated_cost_usd": 5.0},
    }
    env = _envelope("msg_prec", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 403
    # Should be blocked by action_scope, not resource_constraint
    assert resp.json()["detail"]["rule_id"] == "scope_block"


# ---------------------------------------------------------------------------
# Replay protection: nonce + timestamp window
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_nonce_replay_rejected(client):
    """A message with a previously seen nonce is rejected with RAMP-4007."""
    await _setup_agent(client)

    # First message: establishes nonce in the cache
    env1 = _envelope("msg_nonce_1", 1, "telemetry", {"state": "EXECUTING"})
    resp1 = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env1, headers=HEADERS)
    assert resp1.status_code == 200

    # Second message: NEW message_id (passes idempotency), but SAME nonce — replay
    env2 = _envelope("msg_nonce_2", 2, "telemetry", {"state": "EXECUTING"})
    env2["nonce"] = env1["nonce"]   # reuse the first message's nonce
    env2 = _sign(env2)              # re-sign with the recycled nonce
    resp2 = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env2, headers=HEADERS)
    assert resp2.status_code == 400
    assert resp2.json()["detail"]["error_code"] == "RAMP-4007"


@pytest.mark.anyio
async def test_stale_timestamp_rejected(client):
    """A message with a timestamp > 5 minutes old is rejected."""
    await _setup_agent(client)
    from ramp_sdk.signing import sign_envelope

    stale_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    env = {
        "ramp_version": "0.2.0",
        "message_id": "msg_stale_001",
        "agent_id": AGENT_ID,
        "session_id": "sess_test",
        "principal_id": "user:test",
        "timestamp": stale_ts,
        "sequence_number": 1,
        "message_type": "telemetry",
        "nonce": "nonce_stale_001",
        "signature": "",
        "payload": {"state": "EXECUTING"},
    }
    env["signature"] = sign_envelope(env, API_KEY)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4001"
    assert "window" in resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# mandatory_hitl enforcement
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mandatory_hitl_blocks_auto_resolution(client):
    """mandatory_hitl flags an action; policy_auto_approved resolution is then rejected."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "hitl_high_risk",
        "type": "mandatory_hitl",
        "trigger_risk_level": "high",
        "on_violation": "deny_and_notify",
    }])

    await _set_executing(client)

    # Send a high-risk action request — mandatory_hitl should fire and set the flag
    action_payload = {
        "title": "Delete production database",
        "body": "This will permanently delete all prod data.",
        "options": [
            {"action_id": "delete", "label": "Delete"},
            {"action_id": "abort", "label": "Abort"},
        ],
        "timeout_seconds": 60,
        "fallback_action_id": "abort",
        "risk_assessment": {"risk_level": "high"},
    }
    env = _envelope("msg_hitl", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 200  # action accepted — HITL doesn't block storing it

    # Verify the flag is recorded on the pending action
    assert store.pending_actions["msg_hitl"]["mandatory_hitl"] is True

    # Attempt policy auto-approval — MUST be rejected
    resolve_resp = await client.post("/ramp/v1/actions/msg_hitl/resolve", json={
        "resolution_type": "policy_auto_approved",
        "selected_action_id": "delete",
    }, headers=HEADERS)
    assert resolve_resp.status_code == 403
    assert resolve_resp.json()["detail"]["error_code"] == "RAMP-4011"

    # Human resolution must still be allowed
    human_resp = await client.post("/ramp/v1/actions/msg_hitl/resolve", json={
        "resolution_type": "human_decision",
        "selected_action_id": "abort",
        "resolver_id": "user:fahad",
    }, headers=HEADERS)
    assert human_resp.status_code == 200


# ---------------------------------------------------------------------------
# Policy enforcement: action_request cost tracking
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_action_request_cost_counts_against_budget(client):
    """estimated_cost_usd on an Action Request counts toward the resource budget."""
    await _setup_agent(client)

    set_policies(AGENT_ID, [{
        "rule_id": "tight_budget",
        "type": "resource_constraint",
        "resource": "estimated_cost_usd",
        "limit": 50.0,
        "window": "session",
        "on_violation": "deny_and_notify",
    }])

    await _set_executing(client)

    # Telemetry reports $20 cumulative spend — under the $50 limit
    env_telem = _envelope("msg_cost_telem", 2, "telemetry", {
        "state": "EXECUTING",
        "resources": {"estimated_cost_usd": 20.0},
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_telem, headers=HEADERS)
    assert resp.status_code == 200

    # Action request with $40 estimated cost: 20 (telemetry) + 40 (action) = $60 > $50 limit
    action_payload = {
        "title": "Book expensive hotel",
        "body": "Four Seasons for $40/night.",
        "options": [{"action_id": "book", "label": "Book"}, {"action_id": "skip", "label": "Skip"}],
        "timeout_seconds": 30,
        "fallback_action_id": "skip",
        "risk_assessment": {"risk_level": "medium", "estimated_cost_usd": 40.0},
    }
    env_action = _envelope("msg_cost_action", 3, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env_action, headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "RAMP-4011"


# ---------------------------------------------------------------------------
# Phase 3 additions: registration, session guard, resume, validation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_registration_returns_shared_secret(client):
    """Registration response MUST include shared_secret (spec §4.3)."""
    resp = await client.post("/ramp/v1/agents/register", json={"agent_id": AGENT_ID}, headers=HEADERS)
    assert resp.status_code == 200
    assert "shared_secret" in resp.json()
    assert resp.json()["shared_secret"]  # must be non-empty


@pytest.mark.anyio
async def test_duplicate_session_rejected(client):
    """Starting a second session while one is active MUST return RAMP-4016."""
    await _setup_agent(client)
    # Try to start another session while "sess_test" is active
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/sessions",
                             json={"session_id": "sess_duplicate"}, headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "RAMP-4016"


@pytest.mark.anyio
async def test_resume_suspended_agent(client):
    """POST /resume transitions a SUSPENDED agent back to IDLE (spec §9.6.2)."""
    await _setup_agent(client)
    # Suspend the agent manually
    store.agent_states[AGENT_ID] = "SUSPENDED"

    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/resume", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["state"] == "IDLE"
    assert store.agent_states[AGENT_ID] == "IDLE"


@pytest.mark.anyio
async def test_resume_non_suspended_agent_rejected(client):
    """Resuming an agent that is not SUSPENDED MUST be rejected."""
    await _setup_agent(client)
    # Agent is IDLE, not SUSPENDED
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/resume", headers=HEADERS)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_action_request_without_risk_assessment_rejected(client):
    """Action requests missing risk_assessment MUST be rejected with RAMP-4001 (spec §7.3 Rule 6)."""
    await _setup_agent(client)
    await _set_executing(client)

    action_payload = {
        "title": "Do something risky",
        "body": "No risk assessment attached.",
        "options": [{"action_id": "go", "label": "Go"}],
        "timeout_seconds": 60,
        # risk_assessment intentionally omitted
    }
    env = _envelope("msg_no_risk", 2, "action_request", action_payload)
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RAMP-4001"


# ---------------------------------------------------------------------------
# Policy enforcement: rate_constraint — throttle_and_warn drops messages
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_throttle_and_warn_drops_excess_messages(client):
    """throttle_and_warn MUST drop excess messages with 429 + RAMP-4014 (spec §9.6.1)."""
    await _setup_agent(client)

    # Very low rate limit: 2 messages per 60s window
    set_policies(AGENT_ID, [{
        "rule_id": "strict_rate",
        "type": "rate_constraint",
        "max_messages": 2,
        "window_seconds": 60,
        "on_violation": "throttle_and_warn",
    }])
    # Clear any timestamps from setup
    message_timestamps.pop(AGENT_ID, None)

    # First 2 messages should succeed
    for i in range(2):
        env = _envelope(f"msg_rate_{i}", i + 1, "telemetry", {
            "state": "EXECUTING", "task_description": "working",
        })
        resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
        assert resp.status_code == 200, f"Message {i} should pass (got {resp.status_code})"

    # Third message MUST be dropped with 429
    env = _envelope("msg_rate_excess", 3, "telemetry", {
        "state": "EXECUTING", "task_description": "one too many",
    })
    resp = await client.post(f"/ramp/v1/agents/{AGENT_ID}/messages", json=env, headers=HEADERS)
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error_code"] == "RAMP-4014"
    assert detail["on_violation"] == "throttle_and_warn"
