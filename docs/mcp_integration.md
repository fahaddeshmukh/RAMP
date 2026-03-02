---
**Author:** Fahad Deshmukh
**Document Type:** Integration Guide (Informative, Non-Normative)
**Related Spec:** [RAMP Protocol Specification v0.2](./ramp_protocol_spec_v2.md)
**Date:** 2026-03-01
**MCP Compatibility:** MCP 2024-11-05 (latest stable)

---

# RAMP ↔ MCP Integration Guide

## Overview

This document defines canonical [Model Context Protocol (MCP)](https://spec.modelcontextprotocol.io/) tool definitions that allow any MCP-enabled agent to interact with a RAMP Gateway. These tools expose RAMP's three core functions — **Observe**, **Decide**, and **Govern** — as standard MCP tools that agents can invoke without RAMP-specific SDK integration.

> **Non-Normative.** This document is an informative integration guide. The RAMP protocol specification (§1–§16) is the normative reference. These tool definitions offer one recommended mapping; alternative MCP bindings are equally valid.

---

## Tool Definitions

### 1. `ramp_send_telemetry`

**Function:** Observe — report agent state, progress, and resource usage.

```json
{
  "name": "ramp_send_telemetry",
  "description": "Report current agent state, progress, and resource usage to the human principal via RAMP. Call this periodically during task execution to maintain visibility.",
  "inputSchema": {
    "type": "object",
    "required": ["state"],
    "properties": {
      "state": {
        "type": "string",
        "enum": ["EXECUTING", "ERRORED"],
        "description": "Current agent lifecycle state. This is an intentional simplified subset of the full RAMP 7-state lifecycle (spec §5); MCP tool calls typically only self-report active execution or error conditions."
      },
      "task_description": {
        "type": "string",
        "description": "Human-readable summary of what the agent is currently doing."
      },
      "progress_pct": {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "description": "Task completion percentage (0–100)."
      },
      "resources": {
        "type": "object",
        "properties": {
          "llm_tokens_consumed": { "type": "integer" },
          "llm_cost_usd": { "type": "number" },
          "api_calls_made": { "type": "integer" },
          "wall_time_seconds": { "type": "number" }
        },
        "description": "Cumulative resource usage for the current session."
      }
    }
  }
}
```

**Behavior:** The MCP server constructs a RAMP telemetry envelope, signs it with the agent's shared secret, and sends it to `POST /ramp/v1/agents/{agent_id}/messages`. Returns `"accepted"` on success or the policy warning/violation message.

---

### 2. `ramp_request_approval`

**Function:** Decide — request human authorization for a high-stakes action.

```json
{
  "name": "ramp_request_approval",
  "description": "Request human approval before taking a consequential action. The agent will be blocked until the human responds or the timeout elapses. ALWAYS use this before irreversible actions (deleting data, spending money, sending communications).",
  "inputSchema": {
    "type": "object",
    "required": ["title", "body", "options", "risk_level"],
    "properties": {
      "title": {
        "type": "string",
        "description": "Short title for the approval request."
      },
      "body": {
        "type": "string",
        "description": "Detailed explanation of what the agent wants to do and why."
      },
      "options": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["action_id", "label"],
          "properties": {
            "action_id": { "type": "string" },
            "label": { "type": "string" },
            "description": { "type": "string" }
          }
        },
        "description": "Available actions the human can choose from."
      },
      "risk_level": {
        "type": "string",
        "enum": ["low", "medium", "high", "critical"],
        "description": "Risk level of the proposed action."
      },
      "reversibility": {
        "type": "string",
        "enum": ["reversible", "partially_reversible", "irreversible"],
        "default": "reversible"
      },
      "estimated_cost_usd": {
        "type": "number",
        "description": "Estimated cost in USD, if applicable."
      },
      "timeout_seconds": {
        "type": "integer",
        "default": 300,
        "description": "How long to wait for human response before fallback."
      },
      "fallback_action_id": {
        "type": "string",
        "description": "Which action_id to execute if the human doesn't respond in time. SHOULD be the safest option."
      }
    }
  }
}
```

**Behavior:** The MCP server constructs an Action Request envelope. The tool call **blocks** (enters `AWAITING_HUMAN_INPUT` state) until:
- The human responds → returns `{ "decision": "approve_deploy", "resolved_by": "user:alice" }`
- The timeout elapses → returns `{ "decision": "<fallback_action_id>", "resolution_type": "timeout_fallback" }`
- Policy denies it → returns an error with the policy violation details

The MCP server uses long-polling (`GET /ramp/v1/agents/{agent_id}/actions/{message_id}/response?wait=30`) to wait for the response.

---

### 3. `ramp_check_status`

**Function:** Observe — check the agent's current governance status and budget.

```json
{
  "name": "ramp_check_status",
  "description": "Check the agent's current state, remaining budget, and any active policy warnings. Use this to proactively check if the agent is approaching governance limits before taking actions.",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

**Behavior:** The MCP server queries `GET /ramp/v1/agents/{agent_id}` and returns the agent's current state, resource consumption, and any policy thresholds approaching their limits.

---

### 4. `ramp_notify`

**Function:** Observe — send a notification to the human principal.

```json
{
  "name": "ramp_notify",
  "description": "Send a notification to the human principal. Use for task completion, warnings, or informational updates. This is fire-and-forget — the agent does not wait for a response.",
  "inputSchema": {
    "type": "object",
    "required": ["title", "body"],
    "properties": {
      "title": {
        "type": "string",
        "description": "Notification title."
      },
      "body": {
        "type": "string",
        "description": "Notification body text."
      },
      "priority": {
        "type": "string",
        "enum": ["low", "normal", "high", "critical"],
        "default": "normal"
      },
      "category": {
        "type": "string",
        "enum": ["completion", "warning", "error", "info", "cost_alert", "security"],
        "default": "info"
      }
    }
  }
}
```

**Behavior:** The MCP server constructs a RAMP notification envelope and sends it. Returns `"accepted"` immediately — notifications are non-blocking.

---

## Architecture

```
┌────────────────────┐     MCP Tool Calls      ┌────────────────────┐
│                    │ ◄──────────────────────► │                    │
│    LLM / Agent     │   ramp_request_approval  │   RAMP MCP Server  │
│   (Claude, GPT,    │   ramp_send_telemetry    │   (Tool Provider)  │
│    Gemini, etc.)   │   ramp_check_status      │                    │
│                    │   ramp_notify            │                    │
└────────────────────┘                          └─────────┬──────────┘
                                                          │
                                                   RAMP HTTP API
                                                          │
                                                ┌─────────▼──────────┐
                                                │                    │
                                                │   RAMP Gateway     │
                                                │   (Policy Engine,  │
                                                │    Audit Trail)    │
                                                │                    │
                                                └─────────┬──────────┘
                                                          │
                                                   Push / WebSocket
                                                          │
                                                ┌─────────▼──────────┐
                                                │   Human Principal  │
                                                │   (Web UI, Mobile) │
                                                └────────────────────┘
```

## Implementation Notes

- The RAMP MCP Server is a thin adapter: it translates MCP tool calls into signed RAMP envelopes and routes them to the gateway's HTTP API.
- The `ramp_request_approval` tool is the only blocking tool. All others return immediately.
- The MCP server handles envelope construction, signing, sequence numbering, and nonce generation — the agent never sees RAMP wire format.
- A reference implementation of the RAMP MCP Server is planned for v0.3.
