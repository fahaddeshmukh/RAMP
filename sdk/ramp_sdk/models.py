"""RAMP data models — mirrors the protocol spec v0.2."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


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
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationCategory(str, enum.Enum):
    COMPLETION = "completion"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"
    COST_ALERT = "cost_alert"
    SECURITY = "security"


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
    POLICY_VIOLATION = "policy_violation"
    AUDIT = "audit"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ResourceUsage(BaseModel):
    """Agent resource consumption — field names match spec §5.2 wire format."""
    llm_tokens_consumed: int | None = None
    llm_cost_usd: float | None = None
    api_calls_made: int | None = None
    wall_time_seconds: float | None = None
    custom: dict[str, Any] | None = None  # non-spec extension field


class ActionOption(BaseModel):
    action_id: str
    label: str
    description: str | None = None
    confirmation_required: bool = False
    style: str | None = None
    confirmation_message: str | None = None


class RiskAssessment(BaseModel):
    risk_level: RiskLevel
    reversibility: Reversibility = Reversibility.REVERSIBLE
    impact_scope: str | None = None
    estimated_cost_usd: float | None = None
    action_category: str | None = None
    justification: str | None = None


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
    body_format: str = "plaintext"  # spec §6.5: "plaintext" or "markdown"
    priority: NotificationPriority = NotificationPriority.NORMAL
    category: NotificationCategory = NotificationCategory.INFO
    expires_after_seconds: int | None = None  # spec §6.6: OPTIONAL
    attachments: list[dict[str, Any]] | None = None  # spec §6.2
    metadata: dict[str, Any] | None = None


class ActionRequestPayload(BaseModel):
    title: str
    body: str
    body_format: str = "plaintext"  # spec §6.5: "plaintext" or "markdown"
    options: list[ActionOption]
    timeout_seconds: int = 300
    fallback_action_id: str | None = None
    risk_assessment: RiskAssessment
    context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_fallback(self) -> "ActionRequestPayload":
        if self.fallback_action_id is None:
            import warnings as _w
            _w.warn(
                "RAMP spec §7.3: fallback_action_id SHOULD reference a valid "
                "option action_id. Omitting it means no safe fallback on timeout.",
                UserWarning,
                stacklevel=2,
            )
        elif self.options:
            valid_ids = {o.action_id for o in self.options}
            if self.fallback_action_id not in valid_ids:
                raise ValueError(
                    f"fallback_action_id '{self.fallback_action_id}' is not "
                    f"among the option action_ids: {valid_ids}"
                )
        return self


class ActionResponsePayload(BaseModel):
    request_message_id: str
    resolution_type: str      # "human_decision", "timeout_fallback", "policy_auto_approved", "policy_auto_denied", "delegated", "escalated"
    selected_action_id: str | None = None
    resolved_by: str | None = None
    resolver_role: str | None = None
    freeform_input: str | None = None
    resolved_at: str | None = None
    response_latency_ms: int | None = None
    reason: str | None = None
    evidence: dict[str, Any] | None = None  # spec §8.4: opaque human authentication proof
