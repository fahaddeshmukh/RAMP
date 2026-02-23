"""RAMP data models — mirrors the protocol spec v0.2."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AgentState(str, enum.Enum):
    REGISTERED = "REGISTERED"
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    AWAITING_HUMAN_INPUT = "AWAITING_HUMAN_INPUT"
    SUSPENDED = "SUSPENDED"
    ERRORED = "ERRORED"
    TERMINATED = "TERMINATED"


class NotificationPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationCategory(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    COMPLETION = "completion"
    COST_ALERT = "cost_alert"


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Reversibility(str, enum.Enum):
    REVERSIBLE = "reversible"
    PARTIALLY_REVERSIBLE = "partially_reversible"
    IRREVERSIBLE = "irreversible"


class MessageType(str, enum.Enum):
    TELEMETRY = "telemetry"
    NOTIFICATION = "notification"
    ACTION_REQUEST = "action_request"
    ACTION_RESPONSE = "action_response"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ResourceUsage(BaseModel):
    tokens_used: int | None = None
    api_calls_made: int | None = None
    estimated_cost_usd: float | None = None
    custom: dict[str, Any] | None = None


class ActionOption(BaseModel):
    action_id: str
    label: str
    description: str | None = None
    confirmation_required: bool = False
    risk_level: RiskLevel = RiskLevel.LOW


class RiskAssessment(BaseModel):
    level: RiskLevel
    reversibility: Reversibility = Reversibility.REVERSIBLE
    factors: list[str] = Field(default_factory=list)
    estimated_cost_usd: float | None = None
    action_category: str | None = None
    explanation: str | None = None


# ---------------------------------------------------------------------------
# Envelope (Section 4.1 of the spec)
# ---------------------------------------------------------------------------

class Envelope(BaseModel):
    """RAMP message envelope per spec Section 4.1."""

    ramp_version: str = "0.2.0"
    message_id: str                # UUID v7
    message_type: MessageType
    session_id: str
    agent_id: str
    principal_id: str
    sequence_number: int
    timestamp: str                 # ISO 8601
    nonce: str                     # Cryptographic random, unique per session
    signature: str = ""            # HMAC-SHA256 hex digest
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

class TelemetryPayload(BaseModel):
    state: AgentState
    task_description: str | None = None
    progress_pct: int | None = Field(None, ge=0, le=100)
    resources: ResourceUsage | None = None
    context: dict[str, Any] | None = None


class NotificationPayload(BaseModel):
    title: str
    body: str
    body_format: str = "text/plain"
    priority: NotificationPriority = NotificationPriority.MEDIUM
    category: NotificationCategory = NotificationCategory.INFO
    metadata: dict[str, Any] | None = None


class ActionRequestPayload(BaseModel):
    title: str
    body: str
    body_format: str = "text/plain"
    options: list[ActionOption]
    timeout_seconds: int = 300
    fallback_action_id: str | None = None
    risk_assessment: RiskAssessment
    context: dict[str, Any] | None = None


class ActionResponsePayload(BaseModel):
    request_message_id: str
    resolution: str            # "approved", "denied", "modified", "timed_out"
    selected_action_id: str | None = None
    principal_id: str | None = None
    modifications: dict[str, Any] | None = None
    reason: str | None = None
