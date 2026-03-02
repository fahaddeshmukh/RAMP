"""Governance policy engine — enforces Level 3 conformance rule types.

Implements 6 rule types per RAMP spec Section 9:
  - mandatory_hitl       (precedence 1)
  - action_scope         (precedence 2)
  - aggregate_constraint (precedence 3)
  - resource_constraint  (precedence 4)
  - time_constraint      (precedence 5)
  - rate_constraint      (precedence 6)

Rules are evaluated in precedence order (lowest number = highest priority).
The first hard violation wins; warnings accumulate.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Default policies (can be overridden per-agent)
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: dict[str, list[dict[str, Any]]] = {
    "__default__": [
        {
            "rule_id": "default_spend_limit",
            "type": "resource_constraint",
            "resource": "llm_cost_usd",
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

# scope_key -> cumulative cost across agents (for aggregate_constraint)
aggregate_costs: dict[str, float] = {}

# scope_key -> per-agent latest cumulative telemetry cost (overwrite on each telemetry msg)
aggregate_telemetry_costs: dict[str, dict[str, float]] = {}

# scope_key -> per-agent running sum of action request costs (additive across requests)
aggregate_action_costs: dict[str, dict[str, float]] = {}

# agent_id -> accumulated estimated cost from Action Requests in this session
# Tracked separately from telemetry costs (which are cumulative, direct assignment).
# Action request costs are additive — each request contributes its estimated_cost_usd.
action_request_costs: dict[str, float] = {}

# agent_id -> session start time (epoch seconds) — set by the gateway, used
# for objective wall_time_seconds enforcement without relying on agent telemetry.
session_start_times: dict[str, float] = {}


def get_policies(agent_id: str) -> list[dict[str, Any]]:
    """Get active policies for an agent."""
    return agent_policies.get(agent_id, DEFAULT_POLICIES["__default__"])


def set_policies(agent_id: str, rules: list[dict[str, Any]]) -> None:
    """Set policies for a specific agent."""
    agent_policies[agent_id] = rules


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

# Evaluation precedence per spec Section 9.4
_RULE_PRECEDENCE: dict[str, int] = {
    "mandatory_hitl": 1,
    "action_scope": 2,
    "aggregate_constraint": 3,
    "resource_constraint": 4,
    "time_constraint": 5,
    "rate_constraint": 6,
}


class PolicyViolation(Exception):
    def __init__(self, rule_id: str, rule_type: str, message: str, on_violation: str):
        self.rule_id = rule_id
        self.rule_type = rule_type
        self.message = message
        self.on_violation = on_violation
        super().__init__(message)


def evaluate_message(
    agent_id: str,
    envelope: dict[str, Any],
    *,
    _now: datetime | None = None,
) -> list[str]:
    """Evaluate all policies for a message. Returns list of warnings.

    Raises PolicyViolation if a hard violation occurs.

    The ``_now`` parameter is for testing only — it overrides the current time.
    """
    rules = get_policies(agent_id)
    warnings: list[str] = []
    msg_type = envelope.get("message_type", "")
    payload = envelope.get("payload", {})
    principal_id = envelope.get("principal_id", "")

    # Sort rules by precedence (highest priority first)
    sorted_rules = sorted(rules, key=lambda r: _RULE_PRECEDENCE.get(r.get("type", ""), 99))

    for rule in sorted_rules:
        rule_type = rule.get("type")

        if rule_type == "mandatory_hitl":
            _eval_mandatory_hitl(rule, msg_type, payload)

        elif rule_type == "action_scope":
            _eval_action_scope(rule, msg_type, payload)

        elif rule_type == "aggregate_constraint":
            _eval_aggregate_constraint(rule, msg_type, payload, agent_id, principal_id, warnings)

        elif rule_type == "resource_constraint":
            _eval_resource_constraint(rule, msg_type, payload, agent_id)

        elif rule_type == "time_constraint":
            _eval_time_constraint(rule, _now=_now)

        elif rule_type == "rate_constraint":
            _eval_rate_constraint(rule, agent_id, warnings)

    return warnings


# ---------------------------------------------------------------------------
# Individual rule evaluators
# ---------------------------------------------------------------------------

def _eval_mandatory_hitl(rule: dict, msg_type: str, payload: dict) -> None:
    """mandatory_hitl — actions above a risk threshold MUST NOT be auto-resolved."""
    if msg_type != "action_request":
        return
    risk_assessment = payload.get("risk_assessment", {})
    # Support both wire format (risk_level, per spec) and SDK format (level, compat)
    risk_level = risk_assessment.get("risk_level") or risk_assessment.get("level", "low")
    trigger_risk = rule.get("trigger_risk_level", "high")
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if risk_order.get(risk_level, 0) >= risk_order.get(trigger_risk, 2):
        payload["_mandatory_hitl"] = True


def _eval_action_scope(rule: dict, msg_type: str, payload: dict) -> None:
    """action_scope — category-based allow/deny for action requests."""
    if msg_type != "action_request":
        return

    risk_assessment = payload.get("risk_assessment", {})
    category = risk_assessment.get("action_category", "").lower()
    if not category:
        return

    # Normalise policy lists to lowercase (spec §9.3.3: MUST be case-insensitive exact match)
    denied = [c.lower() for c in rule.get("denied_categories", [])]
    allowed = [c.lower() for c in rule.get("allowed_categories", [])]

    # Deny takes precedence (spec Section 9.4: action_scope deny is rule #3)
    if category in denied:
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="action_scope",
            message=f"Action category '{category}' is explicitly denied",
            on_violation=rule.get("on_violation", "deny_and_notify"),
        )

    if allowed and category not in allowed:
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="action_scope",
            message=f"Action category '{category}' is not in the allowed list: {allowed}",
            on_violation=rule.get("on_violation", "deny_and_notify"),
        )


def _eval_aggregate_constraint(
    rule: dict, msg_type: str, payload: dict,
    agent_id: str, principal_id: str, warnings: list[str],
) -> None:
    """aggregate_constraint — cross-agent budget enforcement.

    Tracks costs from both telemetry (cumulative llm_cost_usd) and
    Action Requests (estimated_cost_usd per spec §9), per spec requirement:
    'The Gateway MUST track cumulative spend using resources.llm_cost_usd from
    telemetry AND risk_assessment.estimated_cost_usd from Action Requests.'
    """
    cost = 0.0
    if msg_type == "telemetry":
        resources = payload.get("resources", {})
        # Accept both spec wire name (llm_cost_usd) and SDK convenience name (estimated_cost_usd)
        cost = resources.get("llm_cost_usd") or resources.get("estimated_cost_usd", 0) or 0
    elif msg_type == "action_request":
        cost = payload.get("risk_assessment", {}).get("estimated_cost_usd", 0) or 0
    else:
        return

    if cost <= 0:
        return

    scope = rule.get("scope", "principal")
    scope_key = principal_id if scope == "principal" else "__global__"
    limit = rule.get("limit", float("inf"))
    warning_pct = rule.get("warning_threshold_pct", 80)

    # Track per-agent contribution separately for telemetry (cumulative overwrite)
    # vs action requests (additive delta), then sum both for the total.
    if msg_type == "telemetry":
        aggregate_telemetry_costs.setdefault(scope_key, {})[agent_id] = cost
    else:  # action_request
        prev = aggregate_action_costs.get(scope_key, {}).get(agent_id, 0.0)
        aggregate_action_costs.setdefault(scope_key, {})[agent_id] = prev + cost

    # Sum across all agents in scope (both telemetry and action request contributions)
    total = (
        sum(aggregate_telemetry_costs.get(scope_key, {}).values())
        + sum(aggregate_action_costs.get(scope_key, {}).values())
    )
    aggregate_costs[scope_key] = total

    # Check warning threshold
    if warning_pct and total > limit * warning_pct / 100 and total <= limit:
        warnings.append(
            f"Aggregate cost ${total:.2f} is at {total / limit * 100:.0f}% of ${limit:.2f} limit"
        )

    if total > limit:
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="aggregate_constraint",
            message=f"Aggregate cost ${total:.2f} exceeds limit ${limit:.2f} across agents in scope '{scope_key}'",
            on_violation=rule.get("on_violation", "suspend_all_and_notify"),
        )


def _eval_resource_constraint(rule: dict, msg_type: str, payload: dict, agent_id: str) -> None:
    """resource_constraint — per-agent session spending or duration limit.

    For cost-based resources (llm_cost_usd): tracks costs from both telemetry
    (cumulative, direct assignment) and Action Requests (additive: each request
    contributes its estimated_cost_usd).

    For wall_time_seconds: the Gateway computes elapsed time from its own clock
    (session_start_times), requiring no agent self-reporting.  This is fully
    objective — principal-declared limit, Gateway-measured time.
    """
    resource = rule.get("resource", "llm_cost_usd")

    # --- Wall-clock duration enforcement (gateway-measured, objective) ---
    if resource == "wall_time_seconds":
        start = session_start_times.get(agent_id)
        if start is None:
            return  # no session tracked yet
        elapsed = time.time() - start
        limit = rule.get("limit", float("inf"))
        if elapsed > limit:
            raise PolicyViolation(
                rule_id=rule["rule_id"],
                rule_type="resource_constraint",
                message=f"Session duration {elapsed:.0f}s exceeds limit {limit:.0f}s",
                on_violation=rule.get("on_violation", "suspend_and_notify"),
            )
        return

    # --- Cost-based resource enforcement ---
    if msg_type == "telemetry":
        resources = payload.get("resources", {})
        # Accept both spec wire name (llm_cost_usd) and SDK convenience name (estimated_cost_usd)
        cost = resources.get("llm_cost_usd") or resources.get("estimated_cost_usd", 0) or 0
        if cost <= 0:
            return
        # Telemetry reports cumulative totals — direct assignment
        session_costs[agent_id] = cost
    elif msg_type == "action_request":
        cost = payload.get("risk_assessment", {}).get("estimated_cost_usd", 0) or 0
        if cost <= 0:
            return
        # Action request cost is a delta — accumulate per session
        action_request_costs[agent_id] = action_request_costs.get(agent_id, 0.0) + cost
    else:
        return

    total = session_costs.get(agent_id, 0.0) + action_request_costs.get(agent_id, 0.0)
    if total > rule.get("limit", float("inf")):
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="resource_constraint",
            message=f"Session cost ${total:.2f} exceeds limit ${rule['limit']:.2f}",
            on_violation=rule.get("on_violation", "deny_and_notify"),
        )


def _eval_time_constraint(rule: dict, *, _now: datetime | None = None) -> None:
    """time_constraint — operating hours enforcement."""
    now = _now or datetime.now(timezone.utc)
    day_name = now.strftime("%a").lower()  # mon, tue, wed, ...

    allowed_days = rule.get("allowed_days", [])
    if allowed_days and day_name not in allowed_days:
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="time_constraint",
            message=f"Agent operation not allowed on {day_name} (allowed: {allowed_days})",
            on_violation=rule.get("on_violation", "suspend_until_allowed"),
        )

    allowed_hours = rule.get("allowed_hours_utc", {})
    if allowed_hours:
        start = allowed_hours.get("start", "00:00")
        end = allowed_hours.get("end", "23:59")
        current_time = now.strftime("%H:%M")
        if not (start <= current_time <= end):
            raise PolicyViolation(
                rule_id=rule["rule_id"],
                rule_type="time_constraint",
                message=f"Agent operation not allowed at {current_time} UTC (allowed: {start}-{end})",
                on_violation=rule.get("on_violation", "suspend_until_allowed"),
            )


def _eval_rate_constraint(rule: dict, agent_id: str, warnings: list[str]) -> None:
    """rate_constraint — message rate limiting."""
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
        raise PolicyViolation(
            rule_id=rule["rule_id"],
            rule_type="rate_constraint",
            message=f"Rate limit exceeded: {len(message_timestamps[agent_id])}/{max_msgs} in {window}s window",
            on_violation=violation,
        )


# ---------------------------------------------------------------------------
# Session lifecycle helpers
# ---------------------------------------------------------------------------

def reset_session_costs(agent_id: str) -> None:
    """Reset cost tracking for a new session."""
    session_costs.pop(agent_id, None)
    action_request_costs.pop(agent_id, None)
    message_timestamps.pop(agent_id, None)
    session_start_times.pop(agent_id, None)


def set_session_start_time(agent_id: str, started_at: float) -> None:
    """Record when a session started (gateway clock)."""
    session_start_times[agent_id] = started_at
