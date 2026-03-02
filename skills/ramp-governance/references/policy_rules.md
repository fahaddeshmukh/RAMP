# RAMP Governance Rule Types

Quick reference for the 6 normative governance rules enforced by the RAMP Gateway.
Rules are evaluated in **precedence order** (1 = highest priority). The first hard violation stops evaluation.

## Rule Types

| # | Rule Type | What it does | Example |
|---|---|---|---|
| 1 | **mandatory_hitl** | Forces human approval — blocks auto-resolution | "All financial actions require human sign-off" |
| 2 | **action_scope** | Restricts which action categories are allowed | "This agent can only do `code_review`, not `deploy`" |
| 3 | **aggregate_constraint** | Cross-agent budget limits (sliding window) | "All agents combined: max $500/day" |
| 4 | **resource_constraint** | Per-agent resource limits (cost, time, tokens) | "Max $50 LLM spend per session" |
| 5 | **time_constraint** | Operating hours / day-of-week restrictions | "Only operate Mon-Fri, 09:00-17:00 UTC" |
| 6 | **rate_constraint** | Message rate limiting | "Max 60 messages per 60 seconds" |

## Violation Behaviors

When a rule is violated, the gateway takes one of these actions:

| Behavior | Effect | HTTP Status |
|---|---|---|
| `deny_and_notify` | Message rejected, human notified | 403 |
| `suspend_and_notify` | Agent suspended, human notified | 403 |
| `suspend_all_and_notify` | All agents suspended (aggregate) | 403 |
| `suspend_until_allowed` | Agent suspended, auto-resumes when constraint lifts (e.g., operating hours reopen) | 403 |
| `throttle_and_warn` | Message dropped, warning returned | 429 |

## What This Means for You (the Agent)

1. **You cannot bypass governance.** The gateway enforces rules regardless of what you send.
2. **If you get a 403**, stop and inform the user. Do not retry the same action.
3. **If you get a 429**, wait briefly and retry. You're being rate-limited, not blocked.
4. **Use `request-approval`** for anything that might trigger `mandatory_hitl` or `action_scope` rules. It's better to ask permission than to be denied.
5. **Monitor your resource usage** via telemetry. The gateway tracks cumulative costs and will suspend you if you exceed limits.
