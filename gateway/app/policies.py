"""Basic governance policy engine — enforces 3 rule types for the MVP."""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Default policies (can be overridden per-agent)
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: dict[str, list[dict[str, Any]]] = {
    "__default__": [
        {
            "rule_id": "default_spend_limit",
            "type": "resource_constraint",
            "resource": "estimated_cost_usd",
            "limit": 100.0,
            "window": "session",
            "on_violation": "deny_and_notify",
        },
        {
            "rule_id": "default_rate_limit",
            "type": "rate_constraint",
            "max_messages": 60,
            "window_seconds": 60,
            "on_violation": "throttle_and_warn",
        },
    ]
}

# agent_id -> list of rules
agent_policies: dict[str, list[dict[str, Any]]] = {}

# agent_id -> session cost accumulator
session_costs: dict[str, float] = {}

# agent_id -> message timestamps for rate limiting
message_timestamps: dict[str, list[float]] = {}


def get_policies(agent_id: str) -> list[dict[str, Any]]:
    """Get active policies for an agent."""
    return agent_policies.get(agent_id, DEFAULT_POLICIES["__default__"])


def set_policies(agent_id: str, rules: list[dict[str, Any]]) -> None:
    """Set policies for a specific agent."""
    agent_policies[agent_id] = rules


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

class PolicyViolation(Exception):
    def __init__(self, rule_id: str, rule_type: str, message: str, on_violation: str):
        self.rule_id = rule_id
        self.rule_type = rule_type
        self.message = message
        self.on_violation = on_violation
        super().__init__(message)


def evaluate_message(agent_id: str, envelope: dict[str, Any]) -> list[str]:
    """Evaluate all policies for a message. Returns list of warnings.

    Raises PolicyViolation if a hard violation occurs.
    """
    import time

    rules = get_policies(agent_id)
    warnings: list[str] = []
    msg_type = envelope.get("message_type", "")
    payload = envelope.get("payload", {})

    for rule in rules:
        rule_type = rule.get("type")

        # --- resource_constraint ---
        if rule_type == "resource_constraint":
            if msg_type == "telemetry":
                resources = payload.get("resources", {})
                cost = resources.get("estimated_cost_usd", 0) or 0
                if cost > 0:
                    # Agent reports cumulative totals, not deltas — use direct assignment
                    session_costs[agent_id] = cost
                    if session_costs[agent_id] > rule.get("limit", float("inf")):
                        raise PolicyViolation(
                            rule_id=rule["rule_id"],
                            rule_type=rule_type,
                            message=f"Session cost ${session_costs[agent_id]:.2f} exceeds limit ${rule['limit']:.2f}",
                            on_violation=rule.get("on_violation", "deny_and_notify"),
                        )

        # --- rate_constraint ---
        elif rule_type == "rate_constraint":
            now = time.time()
            window = rule.get("window_seconds", 60)
            max_msgs = rule.get("max_messages", 60)

            if agent_id not in message_timestamps:
                message_timestamps[agent_id] = []

            # Prune old timestamps
            message_timestamps[agent_id] = [
                t for t in message_timestamps[agent_id] if t > now - window
            ]
            message_timestamps[agent_id].append(now)

            if len(message_timestamps[agent_id]) > max_msgs:
                violation = rule.get("on_violation", "throttle_and_warn")
                if violation == "throttle_and_warn":
                    warnings.append(
                        f"Rate limit: {len(message_timestamps[agent_id])}/{max_msgs} messages in {window}s window"
                    )
                else:
                    raise PolicyViolation(
                        rule_id=rule["rule_id"],
                        rule_type=rule_type,
                        message=f"Rate limit exceeded: {len(message_timestamps[agent_id])}/{max_msgs} in {window}s",
                        on_violation=violation,
                    )

        # --- mandatory_hitl ---
        elif rule_type == "mandatory_hitl":
            if msg_type == "action_request":
                # Mandatory HITL rules define actions that MUST NOT be auto-resolved
                risk_level = payload.get("risk_assessment", {}).get("level", "low")
                trigger_risk = rule.get("trigger_risk_level", "high")
                risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
                if risk_order.get(risk_level, 0) >= risk_order.get(trigger_risk, 2):
                    # Mark this action as requiring human approval (no auto-resolve)
                    payload["_mandatory_hitl"] = True

    return warnings


def reset_session_costs(agent_id: str) -> None:
    """Reset cost tracking for a new session."""
    session_costs.pop(agent_id, None)
    message_timestamps.pop(agent_id, None)
