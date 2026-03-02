---
name: ramp-governance
description: >
  Enforce human oversight on autonomous agent actions using the RAMP protocol.
  Use this skill when performing consequential, irreversible, or high-cost actions
  that require human approval — such as spending money, deleting data, sending
  communications, modifying infrastructure, or deploying code. Also use for
  periodic telemetry reporting and sending notifications to the human principal.
license: Apache-2.0
compatibility: Requires network access to a RAMP Gateway endpoint and Python 3.10+.
metadata:
  author: Fahad Deshmukh
  version: "0.2.0"
  protocol: RAMP v0.2
  repository: https://github.com/fahaddeshmukh/RAMP
---

# RAMP Governance Skill

## Overview

RAMP (Remote Agent Monitoring Protocol) provides three functions for safe agent operation:

| Function | What it does | When to use it |
|---|---|---|
| **Observe** | Report state, progress, and resource usage | Periodically during task execution |
| **Decide** | Request human approval for high-stakes actions | Before any irreversible or costly action |
| **Govern** | Operate within enforceable policy boundaries | Automatic — the gateway enforces this |

## When to Use This Skill

**ALWAYS use RAMP before:**
- Spending money (API calls with cost, purchases, subscriptions)
- Deleting or modifying data that cannot be undone
- Sending communications (emails, messages, notifications) on behalf of the user
- Deploying code to production or staging environments
- Modifying infrastructure (servers, databases, DNS)
- Any action the user has marked as requiring approval

**Use RAMP telemetry when:**
- Starting a new task or subtask
- Making significant progress (every 20-30% completion)
- Encountering errors or unexpected situations
- Completing a task

## How to Request Human Approval

When you need to take a consequential action, use the RAMP client script to request approval:

```bash
python3 scripts/ramp_client.py request-approval \
  --title "Deploy to production" \
  --body "Ready to deploy v2.3.1 to the production cluster. This will affect 12,000 active users." \
  --options '[{"action_id": "deploy", "label": "Deploy Now"}, {"action_id": "cancel", "label": "Cancel"}]' \
  --risk-level "high" \
  --reversibility "partially_reversible" \
  --estimated-cost 0.0 \
  --fallback "cancel" \
  --timeout 300
```

The script will **block** until the human responds or the timeout elapses. The output is JSON:

```json
{"decision": "deploy", "resolution_type": "human_decision", "resolved_by": "user:alice"}
```

Or on timeout:
```json
{"decision": "cancel", "resolution_type": "timeout_fallback"}
```

**Always check the `decision` field** and act accordingly. Never proceed with a denied action.

## How to Send Telemetry

Report your current state periodically:

```bash
python3 scripts/ramp_client.py telemetry \
  --state "EXECUTING" \
  --task "Analyzing dataset: sales_2026.csv" \
  --progress 45
```

## How to Send Notifications

Send fire-and-forget notifications to the human:

```bash
python3 scripts/ramp_client.py notify \
  --title "Analysis Complete" \
  --body "Found 3 anomalies in the sales data. See report at /tmp/report.html" \
  --priority "normal" \
  --category "completion"
```

## Risk Level Guide

Choose the appropriate risk level for your approval requests:

| Risk Level | When to use | Examples |
|---|---|---|
| `low` | Easily reversible, no cost | Creating a draft, reading data |
| `medium` | Some effort to reverse, minor cost | Modifying config files, small API calls |
| `high` | Difficult to reverse, significant cost | Database migrations, deployments, bulk operations |
| `critical` | Irreversible, major cost or impact | Deleting production data, financial transactions, public communications |

## Error Handling

The RAMP gateway may reject your request for policy reasons:

| Error Code | Meaning | What to do |
|---|---|---|
| `RAMP-4011` | Policy violation (action denied) | Do not proceed. Inform the user why the action was blocked. |
| `RAMP-4014` | Rate limited | Wait and retry after a brief delay. |
| `RAMP-4004` | Invalid state transition | Check your agent state — you may need to send telemetry first. |

If the gateway is unreachable, **do not proceed with the action**. Fail safely and inform the user.

## Configuration

The script reads configuration from environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `RAMP_GATEWAY_URL` | Yes | `http://localhost:8000` | Gateway base URL |
| `RAMP_API_KEY` | Yes | — | API key for authentication |
| `RAMP_AGENT_ID` | Yes | — | This agent's unique identifier |
| `RAMP_SESSION_ID` | No | Auto-generated | Session identifier |

## Further Reference

- See [references/policy_rules.md](references/policy_rules.md) for the 6 governance rule types
- Full protocol spec: https://github.com/fahaddeshmukh/RAMP/blob/master/docs/ramp_protocol_spec_v2.md
