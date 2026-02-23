"""RAMP SDK — Remote Agent Monitoring Protocol client library."""

from ramp_sdk.agent import RampAgent, RampError
from ramp_sdk.models import (
    ActionOption,
    ActionResponsePayload,
    AgentState,
    NotificationCategory,
    NotificationPriority,
    ResourceUsage,
    Reversibility,
    RiskAssessment,
    RiskLevel,
)

__version__ = "0.2.0"
__all__ = [
    "RampAgent",
    "RampError",
    "ActionOption",
    "ActionResponsePayload",
    "AgentState",
    "NotificationCategory",
    "NotificationPriority",
    "ResourceUsage",
    "Reversibility",
    "RiskAssessment",
    "RiskLevel",
]
