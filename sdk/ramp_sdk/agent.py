"""RampAgent — the primary SDK interface.

Usage::

    from ramp_sdk import RampAgent, ActionOption, RiskAssessment

    agent = RampAgent(
        agent_id="agent:flight_search",
        gateway_url="http://localhost:8000",
        api_key="your-api-key",
        principal_id="user:fahad",
    )

    async with agent:
        await agent.send_telemetry(state="EXECUTING", task_description="Searching flights")
        await agent.send_notification(title="Found 3 flights", body="...")
        response = await agent.request_action(
            title="Book this flight?",
            body="Delta DL-402, $420, JFK→LAX. Card ending in ••33.",
            options=[
                ActionOption(action_id="book", label="Book it"),
                ActionOption(action_id="skip", label="Skip"),
            ],
            risk=RiskAssessment(level="medium", factors=["$420 charge"], estimated_cost_usd=420.0),
        )
        if response.resolution == "approved":
            print("Booking confirmed!")
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ramp_sdk.models import (
    ActionOption,
    ActionRequestPayload,
    ActionResponsePayload,
    AgentState,
    Envelope,
    MessageType,
    NotificationCategory,
    NotificationPayload,
    NotificationPriority,
    ResourceUsage,
    RiskAssessment,
    TelemetryPayload,
)
from ramp_sdk.signing import sign_envelope


class RampError(Exception):
    """Base exception for RAMP SDK errors."""

    def __init__(self, code: str, message: str, details: Any = None):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"{code}: {message}")


class RampAgent:
    """High-level RAMP agent client.

    Parameters
    ----------
    agent_id : str
        Unique agent identifier (e.g. ``"agent:flight_search_v2"``).
    gateway_url : str
        Base URL of the RAMP Gateway (e.g. ``"http://localhost:8000"``).
    api_key : str
        Shared secret used for HMAC-SHA256 signing.
    principal_id : str
        The primary human principal this agent reports to (e.g. ``"user:fahad"``).
    agent_name : str, optional
        Human-readable display name.
    capabilities : list[str], optional
        List of capability tags.
    """

    def __init__(
        self,
        agent_id: str,
        gateway_url: str,
        api_key: str,
        principal_id: str = "user:default",
        agent_name: str | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.principal_id = principal_id
        self.agent_name = agent_name or agent_id
        self.capabilities = capabilities or []

        self._session_id: str | None = None
        self._seq: int = 0
        self._client: httpx.AsyncClient | None = None
        self._registered: bool = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> RampAgent:
        self._client = httpx.AsyncClient(timeout=30.0)
        await self._register()
        await self._start_session()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.send_telemetry(state=AgentState.TERMINATED)
            await self._end_session()
        finally:
            if self._client:
                await self._client.aclose()
                self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_telemetry(
        self,
        state: AgentState | str,
        task_description: str | None = None,
        progress_pct: int | None = None,
        resources: ResourceUsage | dict | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict:
        """Send a telemetry heartbeat / state update."""
        if isinstance(state, str):
            state = AgentState(state)
        if isinstance(resources, dict):
            resources = ResourceUsage(**resources)

        payload = TelemetryPayload(
            state=state,
            task_description=task_description,
            progress_pct=progress_pct,
            resources=resources,
            context=context,
        )
        return await self._send(MessageType.TELEMETRY, payload.model_dump(exclude_none=True))

    async def send_notification(
        self,
        title: str,
        body: str,
        body_format: str = "text/plain",
        priority: NotificationPriority | str = NotificationPriority.MEDIUM,
        category: NotificationCategory | str = NotificationCategory.INFO,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Send a notification to the human principal."""
        if isinstance(priority, str):
            priority = NotificationPriority(priority)
        if isinstance(category, str):
            category = NotificationCategory(category)

        payload = NotificationPayload(
            title=title,
            body=body,
            body_format=body_format,
            priority=priority,
            category=category,
            metadata=metadata,
        )
        return await self._send(MessageType.NOTIFICATION, payload.model_dump(exclude_none=True))

    async def request_action(
        self,
        title: str,
        body: str,
        options: list[ActionOption | dict],
        risk: RiskAssessment | dict,
        body_format: str = "text/plain",
        timeout_seconds: int = 300,
        fallback_action_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ActionResponsePayload:
        """Send an Action Request (HITL) and wait for the human response.

        This method blocks until the human responds or the timeout expires.
        """
        parsed_options = [
            o if isinstance(o, ActionOption) else ActionOption(**o)
            for o in options
        ]
        parsed_risk = risk if isinstance(risk, RiskAssessment) else RiskAssessment(**risk)

        payload = ActionRequestPayload(
            title=title,
            body=body,
            body_format=body_format,
            options=parsed_options,
            timeout_seconds=timeout_seconds,
            fallback_action_id=fallback_action_id,
            risk_assessment=parsed_risk,
            context=context,
        )

        # Send the request
        result = await self._send(
            MessageType.ACTION_REQUEST,
            payload.model_dump(exclude_none=True),
        )

        request_message_id = result.get("message_id")

        # Poll for response (the gateway holds it until human responds)
        return await self._poll_action_response(request_message_id, timeout_seconds)

    # ------------------------------------------------------------------
    # Internal: registration & sessions
    # ------------------------------------------------------------------

    async def _register(self) -> None:
        """Register this agent with the gateway."""
        await self._post("/ramp/v1/agents/register", {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "capabilities": self.capabilities,
            "supported_versions": ["0.2"],
        })
        self._registered = True

    async def _start_session(self) -> None:
        """Start a new execution session."""
        self._session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self._seq = 0
        await self._post(f"/ramp/v1/agents/{self.agent_id}/sessions", {
            "session_id": self._session_id,
        })

    async def _end_session(self) -> None:
        """End the current session."""
        if self._session_id:
            await self._post(
                f"/ramp/v1/agents/{self.agent_id}/sessions/{self._session_id}/end",
                {},
            )
            self._session_id = None

    # ------------------------------------------------------------------
    # Internal: message sending
    # ------------------------------------------------------------------

    def _generate_message_id(self) -> str:
        """Generate a UUID v7-style message ID per spec Section 4.1."""
        return str(uuid.uuid4())  # TODO: switch to uuid7 when stdlib supports it

    async def _send(self, msg_type: MessageType, payload: dict) -> dict:
        """Build envelope, sign it, POST to gateway."""
        self._seq += 1
        message_id = self._generate_message_id()
        nonce = secrets.token_hex(16)

        envelope = Envelope(
            ramp_version="0.2.0",
            message_id=message_id,
            message_type=msg_type,
            session_id=self._session_id or "",
            agent_id=self.agent_id,
            principal_id=self.principal_id,
            sequence_number=self._seq,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds") + "Z",
            nonce=nonce,
            payload=payload,
        )

        env_dict = envelope.model_dump()
        env_dict["signature"] = sign_envelope(env_dict, self.api_key)

        result = await self._post(
            f"/ramp/v1/agents/{self.agent_id}/messages",
            env_dict,
        )
        result["message_id"] = message_id
        return result

    async def _poll_action_response(
        self,
        request_message_id: str,
        timeout_seconds: int,
    ) -> ActionResponsePayload:
        """Poll the gateway for an action response."""
        url = f"/ramp/v1/agents/{self.agent_id}/actions/{request_message_id}/response"
        deadline = asyncio.get_event_loop().time() + timeout_seconds

        while asyncio.get_event_loop().time() < deadline:
            resp = await self._get(url)
            if resp.get("status") == "resolved":
                return ActionResponsePayload(**resp["response"])
            # Wait before polling again
            await asyncio.sleep(2.0)

        # Timeout — return timed_out response
        return ActionResponsePayload(
            request_message_id=request_message_id,
            resolution="timed_out",
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, body: dict) -> dict:
        assert self._client is not None, "Agent not connected. Use 'async with agent:'"
        resp = await self._client.post(
            f"{self.gateway_url}{path}",
            json=body,
            headers={"X-RAMP-API-Key": self.api_key},
        )
        if resp.status_code >= 400:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            raise RampError(
                code=data.get("error_code", f"HTTP-{resp.status_code}"),
                message=data.get("message", resp.text),
                details=data,
            )
        return resp.json()

    async def _get(self, path: str) -> dict:
        assert self._client is not None, "Agent not connected. Use 'async with agent:'"
        resp = await self._client.get(
            f"{self.gateway_url}{path}",
            headers={"X-RAMP-API-Key": self.api_key},
        )
        if resp.status_code >= 400:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            raise RampError(
                code=data.get("error_code", f"HTTP-{resp.status_code}"),
                message=data.get("message", resp.text),
                details=data,
            )
        return resp.json()
