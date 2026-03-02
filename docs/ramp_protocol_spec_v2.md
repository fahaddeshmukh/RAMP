# RAMP Protocol Specification v0.2
# Remote Agent Monitoring Protocol

**Status:** Public Draft  
**Version:** 0.2.0  
**Date:** 2026-02-22  
**Author:** Fahad Deshmukh (fahad.deshmukh@htw-berlin.de)  
**License:** Apache-2.0  
**Repository:** https://github.com/fahaddeshmukh/RAMP  

---

## Abstract

As autonomous AI agents are deployed at scale across personal and enterprise environments, a critical gap has emerged in the agent protocol ecosystem. Existing standards address Agent-to-Tool interoperability (MCP) and Agent-to-Agent communication (A2A), but no formal protocol governs the **Agent-to-Human** supervisory channel — the mechanism by which agents report state, request human decisions, and submit to governance policies.

RAMP (Remote Agent Monitoring Protocol) fills this gap. It defines a transport-agnostic, cryptographically verifiable protocol for:
1. **Telemetry** — Agents report lifecycle state and progress to human supervisors.
2. **Human-in-the-Loop (HITL)** — Agents request and receive human decisions for sensitive or irreversible actions.
3. **Governance** — Agents operate within declarative policy boundaries enforced by the protocol gateway.
4. **Auditability** — All agent actions and human decisions produce a tamper-evident, hash-chained audit trail.

RAMP is designed to support compliance with the human oversight requirements of the EU AI Act (Article 14) and ISO/IEC 42001. Formal regulatory conformance mappings are planned for future versions; this specification establishes the architectural foundations necessary for such mappings.

---

## 1. Introduction

### 1.1 Problem Statement

The current agent ecosystem suffers from a fragmented human oversight model:

- A coding agent in VS Code sends messages to a terminal; the user must watch it.
- A scheduling agent on Telegram sends messages mixed with human conversations.
- A deployment agent sends emails that sit in a crowded inbox.
- A financial trading agent may have no human notification mechanism at all.

This fragmentation produces three critical failures:

1. **Notification fatigue:** High-priority agent requests (e.g., "approve $10K spend") are lost in low-priority noise.
2. **Inconsistent HITL patterns:** Every agent framework invents its own approval flow, making it impossible to audit across agents.
3. **No governance enforcement:** There is no standardized way to impose spending limits, time-of-day restrictions, or action-class permissions across heterogeneous agent deployments.

### 1.2 Scope

RAMP governs the communication channel between an **Agent** (the supervised entity) and a **Human Principal** (the supervisor). It does NOT govern:

- Agent-to-Agent communication (see Google A2A)
- Agent-to-Tool binding (see Anthropic MCP)
- The internal reasoning or architecture of the agent
- The UI implementation of client applications

### 1.3 Design Principles

1. **Transport-agnostic:** RAMP defines message semantics, not wire format. Conformant implementations MAY use HTTP, WebSocket, gRPC, MQTT, or any reliable transport.
2. **Minimal agent burden:** An agent MUST be able to emit a valid RAMP message with a single HTTP POST using only a standard library.
3. **Safe by default:** All HITL requests MUST include a timeout and fallback action. An agent MUST NOT hang indefinitely awaiting human input.
4. **Auditable by design:** Every state transition and human decision produces an immutable, hash-chained audit record.
5. **Governance-first:** The protocol natively supports declarative policy enforcement, not as an extension but as a core primitive.

### 1.4 Relationship to Existing Protocols

| Protocol | Relationship to RAMP |
|---|---|
| **MCP (Anthropic)** | Complementary. MCP defines how agents access tools/resources. RAMP defines how agents report actions taken via MCP tools to human supervisors. |
| **A2A (Google)** | Complementary. A2A defines inter-agent delegation. RAMP defines how the originating human principal maintains oversight of delegated sub-tasks across agent chains. |
| **OpenTelemetry** | RAMP telemetry is semantically compatible with OTel traces/spans. A conformant gateway MAY export RAMP telemetry as OTel spans for integration with existing observability backends (Datadog, Grafana, etc.). |
| **OAuth 2.0 / OIDC** | RAMP uses standard OAuth 2.0 for human principal authentication. Agent identity uses scoped API keys with HMAC-signed messages. |

---

## 2. Terminology

| Term | Definition |
|---|---|
| **Agent** | An autonomous software entity that performs tasks on behalf of a Human Principal. |
| **Human Principal** | The person (or role) who owns, supervises, and is ultimately responsible for the Agent's actions. |
| **Gateway** | The server-side intermediary that receives RAMP messages from Agents, enforces policies, and routes notifications to Human Principals. |
| **Client** | The application (mobile, watch, web, CLI) through which the Human Principal receives RAMP notifications and issues decisions. |
| **Session** | A logical grouping of messages from a single Agent execution lifecycle (start → termination). |
| **Action Request** | A HITL message requiring an explicit human decision before the Agent proceeds. |
| **Policy** | A declarative set of constraints governing what an Agent MAY do autonomously vs. what requires human approval. |
| **Audit Record** | An immutable, hash-chained log entry recording a state transition or decision. |
| **Principal Binding** | An association between an Agent and a Human Principal, scoped by role (`owner`, `approver`, `observer`, `auditor`). An agent may have multiple bindings. |

---

## 3. Agent Lifecycle State Machine

Every RAMP-conformant agent MUST model its execution as a finite state machine with the following states and transitions. The Gateway MUST reject telemetry messages that represent invalid state transitions.

### 3.1 States

| State | Description |
|---|---|
| `REGISTERED` | Agent has authenticated with the Gateway but has not started executing. |
| `IDLE` | Agent is running but not actively performing a task. |
| `EXECUTING` | Agent is actively working on a task. |
| `AWAITING_HUMAN_INPUT` | Agent has paused execution and is waiting for a human decision. |
| `SUSPENDED` | Agent has been paused by the Human Principal or by policy enforcement. |
| `ERRORED` | Agent has encountered a non-fatal error and may recover. |
| `TERMINATED` | Agent has stopped execution. Terminal state. |

### 3.2 Valid Transitions

```
REGISTERED       → IDLE
IDLE             → EXECUTING | SUSPENDED | TERMINATED
EXECUTING        → IDLE | AWAITING_HUMAN_INPUT | ERRORED | SUSPENDED | TERMINATED
AWAITING_HUMAN_INPUT → EXECUTING | SUSPENDED | ERRORED | TERMINATED
SUSPENDED        → IDLE | TERMINATED
ERRORED          → IDLE | EXECUTING | TERMINATED
TERMINATED       → (none — terminal state)
```

### 3.3 Transition Rules

- An agent in `EXECUTING` that sends an Action Request MUST transition to `AWAITING_HUMAN_INPUT`.
- An agent in `AWAITING_HUMAN_INPUT` that receives a human decision MUST transition to `EXECUTING` or `TERMINATED` (if the decision was "abort").
- An agent in `AWAITING_HUMAN_INPUT` whose timeout expires receives the Gateway-selected `fallback_action_id` as an Action Response and transitions accordingly.
- An agent in `AWAITING_HUMAN_INPUT` MAY transition to `ERRORED` if a fault is detected during the waiting period (e.g., principal binding revoked, agent internal error, or Gateway connectivity loss).
- A Human Principal MAY force any non-terminal agent into `SUSPENDED` at any time (the "Kill Switch").
- A Gateway MAY force an agent into `SUSPENDED` if a policy violation is detected.

---

## 4. Message Envelope

All RAMP messages share a common envelope. The payload varies by message type.

### 4.1 Envelope Schema

```json
{
  "ramp_version": "0.2.0",
  "message_id": "01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c",
  "message_type": "telemetry",
  "session_id": "sess_x9y8z7",
  "agent_id": "agent:ci_deployer_v3",
  "principal_id": "user:fahad",
  "sequence_number": 42,
  "timestamp": "2026-02-22T03:30:00.000Z",
  "nonce": "n_7f8a9b0c1d2e",
  "signature": "hmac-sha256:a3f2b9c8d7e6f5...",
  "payload": { ... }
}
```

### 4.2 Field Definitions

| Field | Type | Required | Description |
|---|---|---|---|
| `ramp_version` | string | YES | Semantic version of the RAMP protocol this message conforms to. |
| `message_id` | string | YES | Globally unique identifier for this message. MUST be a UUID v7 or equivalent time-sortable ID. |
| `message_type` | enum | YES | One of: `telemetry`, `notification`, `action_request`, `action_response`, `policy_violation`, `audit`. |
| `session_id` | string | YES | Identifier for the agent's current execution session. Sessions are created explicitly via `POST /ramp/v1/agents/{agent_id}/sessions` (Section 4.5) before the agent begins sending messages. |
| `agent_id` | string | YES | Unique identifier for the agent. Format: `agent:<name>`. |
| `principal_id` | string | YES | Identifier of the *primary* Human Principal (owner). The Gateway uses this plus the agent's Principal Bindings (Section 4.4) to route messages to all bound principals according to their roles. Format: `user:<id>` or `role:<id>`. |
| `sequence_number` | integer | YES | Monotonically increasing integer per session. The Gateway MUST reject messages with sequence numbers ≤ the last received sequence number for that session (duplicate/replay protection). |
| `timestamp` | string | YES | ISO 8601 timestamp with millisecond precision. |
| `nonce` | string | YES | Cryptographically random value. Used for replay protection. The Gateway MUST reject any message whose `timestamp` deviates from Gateway time by more than **5 minutes** (clock skew window). Within this window, the Gateway MUST reject messages with a previously seen (message_id, nonce) pair. The Gateway need only cache nonces within the 5-minute window; nonces from messages older than 5 minutes are considered expired and MAY be evicted from the cache. This bounds nonce cache memory to a fixed, predictable size. |
| `signature` | string | YES | HMAC-SHA256 of the canonical JSON serialization of the message (excluding the `signature` field itself), signed with the agent's shared secret. |
| `payload` | object | YES | Message-type-specific payload. See sections 5-8. |

### 4.3 Agent Registration

Before an agent can send any RAMP messages, it MUST register with the Gateway. Registration establishes the agent's identity, declares its capabilities, and enables the Gateway to enforce appropriate policies.

**Endpoint:** `POST /ramp/v1/agents/register`

**Request Body:**
```json
{
  "agent_id": "agent:ci_deployer_v3",
  "agent_name": "CI/CD Deployment Agent",
  "description": "Automated deployment agent for the payments microservice.",
  "version": "3.1.0",
  "conformance_level": 2,
  "capabilities": {
    "supports_hitl": true,
    "supports_telemetry": true,
    "supports_websocket": true,
    "supports_webhook": false
  },
  "callback": {
    "type": "websocket",
    "uri": null
  },
  "principal_bindings": [
    {"principal_id": "user:fahad", "role": "owner"},
    {"principal_id": "user:sarah", "role": "approver"},
    {"principal_id": "role:devops_team", "role": "observer"},
    {"principal_id": "user:compliance_officer", "role": "auditor"}
  ],
  "metadata": {
    "framework": "langchain",
    "runtime": "python-3.12",
    "owner": "user:fahad"
  }
}
```

**Response (200 OK):**
```json
{
  "status": "registered",
  "agent_id": "agent:ci_deployer_v3",
  "negotiated_version": "0.2",
  "ramp_versions_supported": {"min": "0.2.0", "max": "0.2.0"},
  "shared_secret": "ramp_sec_..."
}
```

**Registration Rules:**
- The `shared_secret` returned during registration is used to compute HMAC signatures on all subsequent messages. It MUST be stored securely by the agent and MUST NOT be transmitted again after registration.
- The Gateway SHOULD return discovery metadata in the registration response. Gateway capabilities are always discoverable via `GET /ramp/v1/info` (§4.7.3).
- If the agent specifies `conformance_level: 2` but the Gateway only supports Level 1, the Gateway MUST reject registration with error `RAMP-4015: CONFORMANCE_MISMATCH`.
- Re-registration of an existing `agent_id` is rejected with `RAMP-4010: AGENT_ALREADY_REGISTERED`. Key rotation (re-registration with a new API key) is reserved for v0.3.
- The `principal_bindings` array defines which Human Principals have access to this agent and what role each has. See Section 4.4 for the full binding model.

### 4.4 Principal Bindings

A single agent MAY be monitored, controlled, or audited by multiple Human Principals. Principal Bindings define who has access to an agent and what they can do. This enables team-based monitoring, compliance oversight, and shared agent ownership.

#### 4.4.1 Binding Roles

| Role | Receives Telemetry | Receives Notifications | Receives Action Requests (read-only) | Can Resolve Actions | Can Suspend/Resume | Can Edit Policies | Can Manage Bindings |
|---|---|---|---|---|---|---|---|
| `owner` | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `approver` | Yes | Yes | Yes | Yes | No | No | No |
| `observer` | Yes | Yes | Yes (read-only) | No | No | No | No |
| `auditor` | No | No | No | No | No | No | No |

**Role definitions:**
- **`owner`**: Full control. Can suspend, terminate, edit policies, add/remove bindings. There MUST be at least one owner per agent at all times. An agent MUST NOT exist without an owner.
- **`approver`**: Can see everything the owner sees and can resolve Action Requests. Cannot modify the agent's policies or bindings. Ideal for: team members who share on-call responsibility, both partners in a shared family agent.
- **`observer`**: Read-only real-time visibility. Receives telemetry, notifications, and Action Requests in **read-only** mode. Observers can see the full Action Request payload (title, body, options, risk assessment) and can see when and how it was resolved, but they MUST NOT be able to submit a resolution. The Client MUST render Action Requests for observers without interactive buttons — showing the request as informational with a status indicator (e.g., "Pending", "Approved by user:fahad", "Timed out — fallback executed"). Ideal for: managers, NOC dashboards, live status screens, stakeholders who need awareness without authority.
- **`auditor`**: Access to the audit trail (Section 10) only. No real-time notifications, no telemetry stream, no ability to interact. All access is via the audit log API. Ideal for: compliance officers, external auditors, regulators.

#### 4.4.2 Action Request Routing with Multiple Principals

When an agent sends an Action Request and multiple principals are bound with the `owner` or `approver` role, the Gateway MUST apply the following routing rules:

| Mode | Behavior | Use Case |
|---|---|---|
| `first_response_wins` | Action Request is delivered to ALL owners/approvers simultaneously. The first to respond resolves it. All others are notified that the action was resolved. | Default. Team on-call. |
| `designated_approver` | Action Request is delivered to a specific principal based on `escalation` policy or rotation schedule. Others with approver role still see it as read-only. | Formal approval workflows. |
| `n_of_m` (Section 7.6) | Action Request requires N approvals from M possible approvers. | High-risk actions. |

The default routing mode is `first_response_wins`. The routing mode is configured per-agent via a `routing_mode` field in the agent's policy document (Section 9.2). `n_of_m` routing is configured inline on the Action Request via the `approval_policy` field (Section 7.6). `designated_approver` routing uses an `escalation` rule (Section 9.3.9) to specify which principal receives a given request.

**Conflict resolution:** If two approvers submit responses to the same Action Request within a race window:
- The Gateway MUST accept the first response received (by Gateway timestamp).
- The Gateway MUST reject the second response with `RAMP-4017: ACTION_ALREADY_RESOLVED`.
- The Gateway MUST notify the rejected responder that their response was not applied.
- The Gateway MUST record both responses in the audit trail (the accepted one as `resolution`, the rejected one as `attempted_resolution`).

#### 4.4.3 Managing Bindings

**Add Binding — Endpoint:** `POST /ramp/v1/agents/{agent_id}/bindings`

```json
{
  "principal_id": "user:new_team_member",
  "role": "approver",
  "added_by": "user:fahad",
  "reason": "Joining the on-call rotation"
}
```

**Remove Binding — Endpoint:** `DELETE /ramp/v1/agents/{agent_id}/bindings/{principal_id}`

**Binding Rules:**
- Only principals with the `owner` role MAY add or remove bindings.
- The last remaining `owner` binding MUST NOT be removable (prevents orphaned agents).
- Adding or removing a binding MUST produce a `binding_changed` audit record.
- A principal MAY remove their own binding (un-follow an agent) regardless of their role.
- Bindings can be scoped to specific sessions or time-bounded: `{"expires_at": "2026-03-01T00:00:00Z"}`. After expiry the Gateway automatically removes the binding and creates an audit record.

#### 4.4.4 Notification Filtering Per Role

The Gateway MUST respect role-based delivery rules:

- **Telemetry**: Delivered to `owner`, `approver`, `observer`. NOT delivered to `auditor`.
- **Notifications**: Delivered to `owner`, `approver`, `observer`. NOT delivered to `auditor`.
- **Action Requests**: Delivered to `owner` and `approver` (interactive, can resolve). Also delivered to `observer` (read-only, no interactive buttons). NOT delivered to `auditor`.
- **Policy Violations**: Delivered to `owner` only (plus any `notification_targets` specified in the violated rule).
- **Emergency Override alerts**: Delivered to ALL bound principals regardless of role (safety mechanism).

#### 4.4.5 Interaction with Escalation

Principal Bindings and Escalation (Section 9.3.9) serve different purposes:

- **Bindings** = "Who has ongoing access to this agent?" (persistent)
- **Escalation** = "Who should be asked *next* if the current person doesn't respond?" (per-action-request)

Escalation targets do NOT need to be bound principals. The Gateway MAY deliver an escalated Action Request to a principal who has no binding — they receive that single request but do not get ongoing telemetry or notification access. This is equivalent to a "one-time guest approval."

Conversely, an `approver` binding does NOT automatically make someone an escalation target. Escalation order is defined in the `escalation` policy rule (Section 9.3.9), not derived from bindings.

### 4.5 Session Management

Sessions group related messages from a single agent execution lifecycle. An agent MUST create a session before sending telemetry, notifications, or action requests.

**Create Session — Endpoint:** `POST /ramp/v1/agents/{agent_id}/sessions`

**Request Body:**
```json
{
  "session_id": "sess_x9y8z7",
  "session_metadata": {
    "trigger": "github_push",
    "trigger_ref": "https://github.com/acme/payments/commit/abc123",
    "expected_duration_seconds": 600
  }
}
```

**Response (200 OK):**
```json
{
  "status": "created",
  "session_id": "sess_x9y8z7"
}
```

**End Session — Endpoint:** `POST /ramp/v1/agents/{agent_id}/sessions/{session_id}/end`

**Response (200 OK):**
```json
{
  "status": "ended",
  "session_id": "sess_x9y8z7"
}
```

**Session Rules:**
- An agent MUST have exactly one active session at a time. Attempting to create a second session while one is active MUST return `RAMP-4016: SESSION_ALREADY_ACTIVE`.

  > **Multi-tenant agents:** The single-session constraint is intentional — it preserves monotonic sequence number integrity (Section 4.2) and prevents audit trail fragmentation across concurrent sessions. For agents that serve multiple Human Principals simultaneously, the correct model is to register **multiple principal bindings** (Section 4.4) within a single session, not to open multiple simultaneous sessions. Each principal binding carries its own policy scope and can independently approve or deny Action Requests. Support for concurrent sessions with isolated sequence number namespaces is a v0.3 consideration (see Section 16, item 5: Shared Agents).

- Ending a session automatically transitions the agent to `TERMINATED` state and creates a `session_ended` audit record.
- If an agent disconnects without ending its session, the Gateway MUST apply an inactivity timeout (configurable, default: 3× the heartbeat interval) after which the session is force-closed and an audit record is created with `resolution_type: timeout`.
- The `session_metadata` object is OPTIONAL but RECOMMENDED for traceability. It allows audit logs to correlate sessions with external triggers (e.g., a specific Git commit, a cron schedule, a user command).

### 4.6 Webhook Callback Verification

When an agent registers with `callback.type: "webhook"`, the Gateway MUST verify ownership of the callback URI before accepting it. This prevents an agent from registering an arbitrary third-party URL as its callback, which would allow an attacker to weaponize the Gateway as a traffic amplifier or exfiltrate Action Response data to an unintended recipient.

#### 4.6.1 Verification Handshake

The Gateway MUST perform the following challenge-response verification:

1. Upon receiving a registration request with `callback.type: "webhook"`, the Gateway generates a cryptographically random challenge token (`challenge_token`).
2. The Gateway sends an HTTP `POST` request to the agent's declared `callback.uri` with the following body:

```json
{
  "ramp_event": "webhook_verification",
  "challenge_token": "ramp_challenge_8f3a7b2c1d4e5f6a",
  "gateway_id": "gateway:ramp-protocol.dev",
  "timestamp": "2026-02-22T03:00:00.000Z"
}
```

3. The agent's webhook endpoint MUST respond with HTTP `200 OK` and the following body:

```json
{
  "challenge_token": "ramp_challenge_8f3a7b2c1d4e5f6a"
}
```

4. If the response does not arrive within 10 seconds, or the echoed `challenge_token` does not match, the Gateway MUST reject the registration with error code `RAMP-4019: WEBHOOK_VERIFICATION_FAILED`.

#### 4.6.2 Ongoing Verification

- The Gateway SHOULD re-verify webhook endpoints periodically (RECOMMENDED: every 24 hours) by repeating the challenge-response handshake.
- To avoid false positives from transient failures (server restarts, brief network blips), the Gateway MUST retry verification 3 times with exponential backoff (delays of 10s, 30s, 90s) before declaring failure.
- If re-verification fails after all retries, the Gateway MUST:
  - Suspend delivery to the webhook.
  - Notify the agent's owner principal(s) with a `notification` of category `warning`.
  - Create a `webhook_verification_failed` audit record.
- The agent MAY update its webhook URI at any time by sending a `PATCH /ramp/v1/agents/{agent_id}/callback` request with a new URI. This triggers a fresh verification handshake.

#### 4.6.3 Webhook Signing

The Gateway MUST sign all outbound webhook payloads to allow the agent to verify that incoming callbacks genuinely originate from the Gateway:

- The Gateway computes an HMAC-SHA256 signature over the raw JSON body of the webhook payload using the agent's `shared_secret` (established during registration, Section 4.3).
- The signature is included in the HTTP header: `X-RAMP-Signature: sha256=<hex-encoded-hmac>`.
- The agent SHOULD verify this signature before processing any webhook payload. Agents that do not verify signatures MUST document this in their conformance declaration.

#### 4.6.4 Webhook Delivery Retry Policy

When the Gateway delivers an Action Response or policy enforcement message to an agent's webhook endpoint and the delivery fails (HTTP 5xx, connection timeout, DNS resolution failure), the Gateway MUST:

1. Retry delivery up to 5 times with exponential backoff: delays of 1s, 5s, 30s, 120s, 600s.
2. After each failed attempt, the Gateway MUST log a `delivery_retry` event (not a full audit record, to avoid audit spam).
3. If all 5 retries fail, the Gateway MUST:
   - Create a `delivery_failed` audit record.
   - Notify the agent's owner principal(s) with a `notification` of category `warning`, body: "Failed to deliver action response to agent after 5 attempts."
   - If the failed delivery was an Action Response, the Gateway MUST hold the response and retry when the agent next sends any message (reconnection-triggered delivery).
4. The Gateway MUST include a `X-RAMP-Delivery-Attempt: N` header (1-indexed) on each delivery attempt so the agent can detect retries.

### 4.7 Protocol Version Negotiation

RAMP is designed for long-term evolution. To ensure backward compatibility and smooth upgrades, the protocol defines explicit version negotiation semantics.

#### 4.7.1 Version Format

RAMP versions follow [Semantic Versioning 2.0.0](https://semver.org/):
- **MAJOR** version: Incremented for breaking changes to the envelope schema, state machine, or core message types. Agents MUST NOT assume compatibility across major versions.
- **MINOR** version: Incremented for backward-compatible additions (e.g., new optional fields, new message types, new governance rule types). A Gateway supporting version `0.3.0` MUST accept messages from agents declaring `0.2.0`.
- **PATCH** version: Incremented for clarifications, editorial corrections, or non-functional changes to the specification text. No behavioral difference between patch versions.

#### 4.7.2 Negotiation Rules

1. **Agent → Gateway:** Every RAMP message envelope MUST include the `ramp_version` field declaring the protocol version the message conforms to.
2. **Gateway Version Advertisement:** The Gateway MUST advertise its supported version range in:
   - The registration response (Section 4.3): `"ramp_versions_supported": {"min": "0.2.0", "max": "0.3.0"}`
   - A publicly accessible discovery endpoint: `GET /ramp/v1/info`
3. **Compatibility Evaluation:** Upon receiving a message, the Gateway MUST:
   - **Accept** the message if the declared `ramp_version` falls within the Gateway's supported range and shares the same MAJOR version.
   - **Reject** the message with `RAMP-4018: UNSUPPORTED_VERSION` if the MAJOR version differs or the version is outside the supported range.
4. **Error Response for Version Mismatch:**

```json
{
  "error_code": "RAMP-4018",
  "error_name": "UNSUPPORTED_VERSION",
  "message": "This Gateway does not support RAMP version 1.0.0.",
  "supported_versions": {
    "min": "0.2.0",
    "max": "0.3.0"
  }
}
```

5. **Forward Compatibility:** Gateways MUST ignore unknown fields in message payloads from agents declaring a higher MINOR version within the same MAJOR version. This allows newer agents to include optional fields that older Gateways simply skip, without causing message rejection.

#### 4.7.3 Discovery Endpoint

`GET /ramp/v1/info`

```json
{
  "gateway_id": "gateway:ramp-protocol.dev",
  "ramp_versions_supported": {
    "min": "0.2.0",
    "max": "0.3.0"
  },
  "conformance_level": 3,
  "transport_bindings": ["http", "websocket"],
  "features": {
    "delegation": true,
    "multi_party_approval": true,
    "emergency_override": true
  },
  "server_time": "2026-02-22T04:00:00.000Z"
}
```

This endpoint requires no authentication for basic version and transport information. The `features` object is OPTIONAL and MAY be omitted by Gateways that prefer not to advertise capabilities publicly. Jurisdiction information MUST only be returned to authenticated principals (via the registration response, Section 4.3).

### 4.8 Canonical JSON Serialization

The integrity of RAMP's security model depends on deterministic message serialization. The HMAC-SHA256 signature (Section 4.2) is computed over the canonical form of the JSON message. If two implementations serialize the same logical JSON object differently (e.g., different key ordering, whitespace, or Unicode escaping), signature verification will fail, breaking interoperability.

#### 4.8.1 Normative Reference

RAMP adopts **RFC 8785: JSON Canonicalization Scheme (JCS)** as the REQUIRED serialization method for all signature computation.

**Reference:** Rundgren, A., Jordan, B., and S. Erdtman, "JSON Canonicalization Scheme (JCS)", RFC 8785, DOI 10.17487/RFC8785, June 2020.

#### 4.8.2 Rules Summary (per RFC 8785)

Conformant implementations MUST apply the following rules when serializing a RAMP message for signature computation:

1. **Key Ordering:** Object members MUST be serialized in lexicographic (Unicode code point) order of their keys.
2. **No Whitespace:** No whitespace (spaces, tabs, newlines) between tokens. The canonical form is the most compact valid JSON representation.
3. **Number Serialization:** Numbers MUST be serialized according to the ECMAScript `JSON.stringify()` specification (no trailing zeroes, no leading `+`, lowercase `e` for exponents).
4. **String Serialization:** Strings MUST use the shortest valid JSON escape sequence. Code points `U+0000` through `U+001F` MUST be escaped as `\uXXXX`. The characters `"` and `\` MUST be escaped as `\"` and `\\`. All other code points MUST be represented as their literal UTF-8 encoding (no unnecessary `\uXXXX` escaping).
5. **No BOM:** The serialized output MUST NOT include a UTF-8 Byte Order Mark.
6. **Excluded Fields:** The `signature` field MUST be excluded from the object before canonicalization. The canonical form is computed over the message envelope with the `signature` field removed entirely (not set to `null`, not set to empty string — removed).

#### 4.8.3 Signature Computation Procedure

```
1. Let M = the RAMP message envelope as a JSON object.
2. Let M' = M with the "signature" field removed.
3. Let C = JCS_Canonicalize(M') — the canonical JSON string per RFC 8785.
4. Let K = the agent's shared_secret (obtained during registration).
5. Let S = HMAC-SHA256(K, C) — the raw HMAC output (32 bytes).
6. Let SIG = "hmac-sha256:" + hexencode(S) — lowercase hexadecimal.
7. Set M.signature = SIG.
```

#### 4.8.4 Implementation Notes

- Reference implementations of RFC 8785 exist for all major languages:
  - **Python:** `json_canonicalization` (PyPI)
  - **JavaScript/TypeScript:** `json-canonicalize` (npm)
  - **Go:** `go-jose/canonicaljson`
  - **Java:** `org.erdtman:java-json-canonicalization`
- The RAMP SDK (Appendix C) should handle canonicalization internally. Agent developers should not need to implement JCS manually.
- The Gateway MUST reject messages whose `signature` does not match the recomputed HMAC over the canonical form with error code `RAMP-4002: INVALID_SIGNATURE`.

### 4.9 Idempotency

The Gateway MUST treat messages with the same `message_id` as idempotent. If a message with a given `message_id` has already been processed, the Gateway MUST return the original response without re-processing.

### 4.10 Message Ordering

The Gateway MUST process messages from a given session in `sequence_number` order. If a message arrives out of order, the Gateway MUST either:
- Buffer it until the preceding messages arrive (with a configurable timeout), OR
- Reject it with error code `RAMP-4003: SEQUENCE_VIOLATION`

---

## 5. Telemetry Messages

### 5.1 Purpose

Telemetry messages report the agent's current lifecycle state, progress on tasks, and resource consumption. They are the "heartbeat" of the agent.

### 5.2 Payload Schema

```json
{
  "state": "EXECUTING",
  "previous_state": "IDLE",
  "task": {
    "task_id": "task_refactor_auth",
    "description": "Refactoring authentication module",
    "progress": {
      "current_step": 3,
      "total_steps": 10,
      "percentage": 30,
      "step_description": "Migrating OAuth2 token validation"
    }
  },
  "resources": {
    "llm_tokens_consumed": 15420,
    "llm_cost_usd": 0.23,
    "api_calls_made": 7,
    "wall_time_seconds": 120
  },
  "context": {
    "framework": "langchain",
    "model": "claude-opus-4-20250514",
    "environment": "local"
  }
}
```

### 5.3 Heartbeat Interval

- Agents SHOULD emit telemetry at least once every 60 seconds while in `EXECUTING` state.
- Agents in `IDLE` state SHOULD emit telemetry at least once every 300 seconds.
- The Gateway MAY mark an agent as `UNRESPONSIVE` if no telemetry is received for 3× the expected interval.
- **Important:** `UNRESPONSIVE` is a Gateway-internal status indicator, **not** a lifecycle state in the state machine defined in Section 3. The agent itself never reports `UNRESPONSIVE` — the Gateway infers it from the absence of telemetry. This status does NOT appear in the `state` field of any RAMP message. Instead, the Gateway MUST: (1) send a `warning` notification to all `owner` and `approver` principals indicating the agent has become unresponsive, (2) log an `agent_unresponsive` audit event, and (3) optionally transition the agent to `SUSPENDED` if unresponsiveness persists beyond a configurable threshold (default: 5× the expected heartbeat interval). If the agent resumes sending telemetry before the suspension threshold, the Gateway MUST clear the `UNRESPONSIVE` status silently without a state transition.

### 5.4 Resource Tracking

The `resources` field is OPTIONAL but RECOMMENDED. Gateways that support policy enforcement (Section 9) MUST use resource data to evaluate spend limits and rate constraints.

---

## 6. Notification Messages

### 6.1 Purpose

Notifications are one-way informational messages that do not require a human decision. They inform the Human Principal of events, completions, or warnings.

### 6.2 Payload Schema

```json
{
  "priority": "normal",
  "category": "completion",
  "title": "Document Analysis Complete",
  "body": "Successfully generated summary of the Q3 earnings report. 47 pages processed, 3 key findings identified.",
  "body_format": "plaintext",
  "expires_after_seconds": 3600,
  "attachments": [
    {
      "type": "link",
      "label": "View Summary",
      "uri": "https://notion.so/my-workspace/q3-summary"
    },
    {
      "type": "data",
      "label": "Raw Output",
      "mime_type": "application/json",
      "size_bytes": 2048,
      "uri": "ramp://artifacts/01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c/output.json"
    }
  ]
}
```

### 6.3 Priority Levels

| Priority | Behavior |
|---|---|
| `low` | Silent delivery. Added to inbox only. |
| `normal` | Standard push notification. |
| `high` | Persistent notification. Badge on app icon. |
| `critical` | Highest urgency. SHOULD be treated as requiring immediate attention. Client rendering behavior (e.g., overriding silent mode, requiring explicit dismissal) is an implementation decision. |

### 6.4 Categories

| Category | Description |
|---|---|
| `completion` | Task finished successfully. |
| `warning` | Non-critical issue detected. |
| `error` | Error occurred (see also ERRORED state telemetry). |
| `info` | General informational update. |
| `cost_alert` | Spending threshold reached. |
| `security` | Security-relevant event detected. |

### 6.5 Body Format Rendering

The `body_format` field indicates the format of the `body` text. Clients MUST support the following rendering rules:

| Format | Client Requirement |
|---|---|
| `plaintext` | **MUST** support. All conformant Clients MUST be able to render plaintext bodies. This is the baseline format. |
| `markdown` | **RECOMMENDED**. Clients SHOULD render Markdown (CommonMark subset). Clients that cannot render Markdown (e.g., Apple Watch with limited display) MUST gracefully fall back to rendering the raw text with Markdown syntax stripped. |

If `body_format` is omitted, the Client MUST treat the body as `plaintext`.

### 6.6 Notification Expiry

The `expires_after_seconds` field is OPTIONAL. When present, it defines the maximum age (in seconds from `timestamp`) after which the notification is considered stale.

- The Gateway MUST NOT deliver a notification to a Client if the notification has expired.
- The Gateway SHOULD drop expired notifications from its delivery queue rather than accumulating them.
- If `expires_after_seconds` is omitted, the notification does not expire and MUST be delivered regardless of delay.

---

## 7. Action Request Messages (HITL)

### 7.1 Purpose

Action Requests are the core HITL mechanism. They pause agent execution and request an explicit human decision. This is the most critical message type in the protocol.

### 7.2 Payload Schema

```json
{
  "priority": "high",
  "category": "approval",
  "title": "Approval Required: Production Deployment",
  "body": "All 247 unit tests passed. Integration tests passed. Do you want to deploy branch `feature-auth` to production?",
  "body_format": "markdown",
  "risk_assessment": {
    "reversibility": "irreversible",
    "impact_scope": "production",
    "estimated_cost_usd": 0,
    "risk_level": "high",
    "action_category": "deploy",
    "justification": "Production deployment cannot be automatically rolled back. Manual rollback requires ~30 minutes downtime."
  },
  "options": [
    {
      "action_id": "deploy_prod",
      "label": "Deploy to Production",
      "style": "destructive",
      "confirmation_required": true,
      "confirmation_message": "This will deploy to production. Are you sure?"
    },
    {
      "action_id": "deploy_staging",
      "label": "Deploy to Staging Only",
      "confirmation_required": false
    },
    {
      "action_id": "abort",
      "label": "Abort",
      "confirmation_required": false
    }
  ],
  "timeout_seconds": 300,
  "fallback_action_id": "abort",
  "context": {
    "test_results_url": "https://ci.example.com/run/1234",
    "diff_summary": "+423 lines, -187 lines across 12 files"
  }
}
```

### 7.3 Action Request Rules

1. An agent MUST transition to `AWAITING_HUMAN_INPUT` immediately after sending an Action Request.
2. An agent MUST NOT send another Action Request while one is pending (one outstanding HITL per agent per session).
3. The `timeout_seconds` field is REQUIRED. Maximum allowed value: 86400 (24 hours).
4. The `fallback_action_id` MUST reference one of the `action_id` values in `options`.
5. If the human does not respond within `timeout_seconds`, the Gateway MUST:
   a. Create an `action_resolved` audit record with `resolution_type: "timeout_fallback"`.
   b. Send an Action Response to the agent with `resolution_type: "timeout_fallback"` and `selected_action_id` set to the `fallback_action_id`.
6. The `risk_assessment` object is REQUIRED on all Action Requests. Consistent with RAMP's governance-first design principle (Section 1.3, Principle 5), every action requiring human input MUST declare its risk profile, even if all fields are set to their lowest values (e.g., `risk_level: "low"`, `reversibility: "reversible"`). This forces agent developers to explicitly reason about risk at design time. The Gateway MUST reject Action Requests that omit `risk_assessment` with error code `RAMP-4001: INVALID_ENVELOPE`.
7. Action Requests MUST NOT contain secrets, credentials, API keys, or unmasked payment instrument numbers in any field (`title`, `body`, `context`, or `options`). Agents MUST reference sensitive data by masked identifier only (e.g., "card ending in ••33", "AWS account ••7294"). The Gateway is not assumed to be a secure vault; sensitive data MUST remain in the agent's own secure storage.
8. The `style` field on action options is OPTIONAL and is a rendering hint only — it MUST NOT affect protocol semantics (routing, policy evaluation, or audit recording). `style: "destructive"` and `confirmation_required: true` are independent: `confirmation_required` is the protocol-level gate that enforces an extra confirmation step before the action is submitted; `style: "destructive"` is a visual hint to the Client to render that option with warning emphasis. They may be combined, used independently, or omitted. Clients SHOULD render options with `style: "destructive"` with visual distinction (e.g., red or warning color). Recommended style values are listed in Appendix D.

### 7.4 Risk Assessment (REQUIRED)

The `risk_assessment` object is a REQUIRED field on every Action Request. It serves three purposes: (1) it helps the Client render appropriate UI urgency, (2) it enables the Gateway's policy engine to evaluate governance rules (e.g., auto-approve low-risk actions), and (3) it creates an auditable record of the agent developer's risk classification for regulatory compliance.

| Field | Values |
|---|---|
| `reversibility` | `reversible`, `partially_reversible`, `irreversible` |
| `impact_scope` | `local`, `staging`, `production`, `external`, `financial` |
| `risk_level` | `low`, `medium`, `high`, `critical` |
| `action_category` | User-defined string (e.g., `send_email`, `purchase`, `deploy`, `delete_file`). Used by `action_scope` governance rules (Section 9.3.3) to enforce capability permissions. Also used by `data_access` rules (Section 9.3.8) when the category describes a data domain access intent (e.g., `read_calendar`, `read_health_data`). |
| `estimated_cost_usd` | Numeric. Estimated monetary cost of the action. Used by `resource_constraint` and `aggregate_constraint` rules. |
| `justification` | Free-text explanation of why this action carries its declared risk level. |

### 7.5 Delegation

Action requests MAY support delegation to other authorized users. This enables:
- Team-based approval workflows
- Escalation when the primary principal is unavailable
- Multi-party approval for high-risk actions (require N-of-M approvals)

### 7.6 Multi-Party Approval

For high-risk actions, the agent MAY include an `approval_policy` field as a top-level sibling of `options` in the Action Request payload to require multiple approvals:

```json
{
  "options": [
    {"action_id": "approve", "label": "Approve", "confirmation_required": true},
    {"action_id": "deny", "label": "Deny"}
  ],
  "approval_policy": {
    "type": "n_of_m",
    "required_approvals": 2,
    "approvers": ["user:fahad", "user:sarah", "role:security_team"],
    "timeout_per_approver_seconds": 600
  }
}
```

---

## 8. Action Response Messages

### 8.1 Purpose

Sent from the Client (via the Gateway) back to the Agent when a Human Principal resolves an Action Request.

### 8.2 Payload Schema

```json
{
  "request_message_id": "01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c",
  "selected_action_id": "deploy_prod",
  "resolved_by": "user:fahad",
  "resolver_role": "owner",
  "resolution_type": "human_decision",
  "freeform_input": null,
  "resolved_at": "2026-02-22T03:32:00.000Z",
  "response_latency_ms": 120000
}
```

### 8.3 Resolution Types

| Type | Description |
|---|---|
| `human_decision` | A human explicitly selected an option. |
| `timeout_fallback` | The timeout expired and the fallback action was executed. |
| `policy_auto_approved` | The Gateway auto-approved based on policy rules (e.g., cost < auto-approve threshold). |
| `policy_auto_denied` | The Gateway auto-denied based on policy rules (e.g., action outside operating hours). |
| `delegated` | The action was resolved by a delegate, not the primary principal. |
| `escalated` | The action was resolved by an escalation-tier responder, not the primary principal. |

### 8.4 Evidence (OPTIONAL)

Action Responses MAY include an `evidence` field containing cryptographic proof of the human's authentication at the time of resolution. This field enables non-repudiable audit trails without changing the core HMAC signing mechanism.

```json
{
  "request_message_id": "01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c",
  "selected_action_id": "deploy_prod",
  "resolved_by": "user:fahad",
  "resolution_type": "human_decision",
  "resolved_at": "2026-03-01T12:30:00.000Z",
  "evidence": {
    "factor": "passkey.webauthn.v1",
    "proof": { "credentialId": "...", "authenticatorData": "...", "signature": "..." },
    "collected_at": "2026-03-01T12:29:58.000Z"
  }
}
```

The `evidence` field is an opaque JSON object defined by the gateway implementation. The specification does not mandate a schema for evidence. Common patterns include:

| Factor type | Description |
|---|---|
| `passkey.webauthn.v1` | WebAuthn/FIDO2 assertion from a passkey |
| `otp.totp.v1` | Time-based OTP verification receipt |
| `oauth2.id_token.v1` | Truncated OIDC id_token claims (sub, iat, auth_time) |

When present, the `evidence` field MUST be included in the audit trail record. Auditors MAY independently verify evidence proofs to establish non-repudiation.

### 8.5 Identity Extensibility

The `agent_id`, `principal_id`, and `resolved_by` fields are opaque strings. Implementations MAY use any naming convention including URIs, Decentralized Identifiers (DIDs), OAuth client IDs, or application-specific identifiers.

---

## 9. Governance & Policy Engine

### 9.1 Purpose

RAMP natively supports declarative policies that constrain agent behavior. Policies are evaluated at the Gateway level, enabling governance even if the agent is untrusted or buggy.

### 9.2 Policy Structure

A policy is a versioned, per-agent document containing an ordered list of governance rules. Policies are stored and enforced at the Gateway.

```json
{
  "policy_id": "pol_travel_agent_v2",
  "agent_id": "agent:travel_booker",
  "principal_id": "user:fahad",
  "version": 3,
  "effective_from": "2026-02-01T00:00:00Z",
  "rules": [ /* See Section 9.3 for all rule types */ ]
}
```

### 9.3 Rule Types

The Gateway MUST support the following governance rule types. Each rule is independently evaluated against incoming messages and Action Requests.

#### 9.3.1 `resource_constraint` — Per-Agent Spending Limits

Controls how much a single agent can spend within a session or time period.

```json
{
  "rule_id": "spend_limit",
  "type": "resource_constraint",
  "resource": "llm_cost_usd",
  "limit": 500,
  "window": "session",
  "on_violation": "suspend_and_notify"
}
```

`window` values: `session`, `hourly`, `daily`, `sliding_window_24h`. The `sliding_window_*` variants prevent boundary gaming (e.g., spending $499 at 11:59 PM and $499 at 12:01 AM to bypass a $500 daily limit). The `daily` period resets at midnight in the principal's configured timezone; if no timezone is configured, UTC is used.

> **Implementation note (v0.2):** The reference Gateway enforces `session` semantics only. `hourly`, `daily`, and `sliding_window_*` values are accepted but treated as `session`. Full window-type support is reserved for v0.3.

#### 9.3.2 `auto_resolution` — Automatic Approval/Denial *(Informative, Non-Normative)*

Enables the Gateway to resolve Action Requests without human intervention when conditions are met.

```json
{
  "rule_id": "auto_approve_low_cost",
  "type": "auto_resolution",
  "condition": {
    "field": "risk_assessment.estimated_cost_usd",
    "operator": "lt",
    "value": 50
  },
  "resolution": "auto_approve",
  "notify_principal": true
}
```

**Critical constraint:** `auto_resolution` rules are always overridden by `mandatory_hitl` and `action_scope` deny rules (see Section 9.4).

#### 9.3.3 `action_scope` — Capability Permissions

Controls *what categories of actions* an agent is permitted to take, regardless of cost. This is the most critical governance rule type — without it, a $0-cost action (like sending an email or deleting a file) would bypass all cost-based controls.

```json
{
  "rule_id": "allowed_actions",
  "type": "action_scope",
  "allowed_categories": ["read_email", "draft_email", "search_flights", "compare_prices"],
  "denied_categories": ["send_email", "purchase", "delete_file", "modify_calendar"],
  "on_violation": "deny_and_notify"
}
```

**Evaluation rules:**
- If `allowed_categories` is specified, only those action categories are permitted. All others are implicitly denied.
- If `denied_categories` is specified, those categories are explicitly forbidden. All others are implicitly allowed.
- If both are specified, `denied_categories` takes precedence over `allowed_categories` (explicit deny wins).
- Action categories are matched against the `risk_assessment.action_category` field in the Action Request.
- Action categories are user-defined strings. The Gateway MUST perform case-insensitive exact matching. Hierarchical categories (e.g., `email.send` vs `email.read`) are RECOMMENDED but not required.

#### 9.3.4 `time_constraint` — Operating Hours

Restricts when an agent is permitted to execute or send Action Requests.

```json
{
  "rule_id": "operating_hours",
  "type": "time_constraint",
  "allowed_days": ["mon", "tue", "wed", "thu", "fri"],
  "allowed_hours_utc": {"start": "09:00", "end": "17:00"},
  "on_violation": "suspend_until_allowed"
}
```

#### 9.3.5 `mandatory_hitl` — Force Human Approval

Forces human approval for specific categories of actions, overriding any `auto_resolution` rules.

```json
{
  "rule_id": "require_approval_high_risk",
  "type": "mandatory_hitl",
  "trigger_risk_level": "high",
  "override_auto_approve": true
}
```

`trigger_risk_level` values: `low`, `medium`, `high`, `critical`. All actions with a `risk_assessment.risk_level` at or above `trigger_risk_level` require human approval; `policy_auto_approved` and `policy_auto_denied` resolution types are blocked for those actions.

#### 9.3.6 `rate_constraint` — Rate Limiting

Prevents agent spam and runaway loops by limiting message throughput.

```json
{
  "rule_id": "rate_limit",
  "type": "rate_constraint",
  "max_messages": 10,
  "window_seconds": 60,
  "on_violation": "throttle_and_warn"
}
```

#### 9.3.7 `aggregate_constraint` — Cross-Agent Spending

Controls total spending across *all* agents for a given principal. Prevents the exploit where $N$ agents each spend just under the per-agent auto-approve threshold, resulting in uncontrolled aggregate expenditure.

```json
{
  "rule_id": "global_daily_budget",
  "type": "aggregate_constraint",
  "scope": "principal",
  "metric": "total_cost_usd",
  "limit": 200,
  "warning_threshold_pct": 80,
  "on_warning": "notify_principal",
  "on_violation": "suspend_all_and_notify"
}
```

**Aggregate constraint rules:**
- `scope` values: `principal` (all agents for the authenticated principal), `agent_group:<name>` (subset of agents tagged into a group).
- When `warning_threshold` is reached, the Gateway MUST send a `cost_alert` notification but MUST NOT suspend agents.
- When `limit` is reached, the Gateway MUST apply the `on_violation` action to ALL agents in scope.
- `suspend_all_and_notify` suspends all agents in scope and sends a `policy_violation` notification listing aggregate spend breakdown per agent.
- The Gateway MUST track cumulative spend using `resources.llm_cost_usd` from telemetry AND `risk_assessment.estimated_cost_usd` from Action Requests.

#### 9.3.8 `data_access` — Privacy Governance — *Informative (Non-Normative)*

> **Note:** This rule type is informative. It describes a product-level governance extension that Gateway implementations MAY support. Data-domain access control can also be achieved through `action_scope` rules (Section 9.3.3) using data-domain action categories (e.g., `action_category: "read_health_data"`). Conformant RAMP implementations are NOT required to implement `data_access` as a separate rule type.

Controls which data domains an agent may access, supporting GDPR Article 25 (data protection by design / data minimization). Data access governance is enforced independently of the mechanism agents use to access data, making it composable with external context protocols such as MCP (Anthropic's Model Context Protocol).

```json
{
  "rule_id": "health_data_access",
  "type": "data_access",
  "domain": "health",
  "access": "deny",
  "justification": "Travel agent does not need health data"
}
```

```json
{
  "rule_id": "calendar_read_only",
  "type": "data_access",
  "domain": "calendar",
  "access": "read",
  "justification": "Scheduling agent needs to check availability but not create events"
}
```

**Data access rules:**
- `domain` is a user-defined string (e.g., `health`, `calendar`, `financial`, `location`, `contacts`, `email`). Domains are not a fixed enum — principals may define custom domains to match their data sources.
- `access` values: `deny`, `read`, `write`, `read_write`.
- `data_access` rules are evaluated when an agent's Action Request declares a data access intent via the `action_category` field (e.g., `action_category: "read_calendar"` triggers evaluation against the `calendar` domain). The Gateway MUST reject the Action Request with `RAMP-4011: POLICY_VIOLATION` and create an audit record if the declared domain is denied.
- If no `data_access` rule exists for a declared domain, the Gateway's default policy MUST be `deny` (deny-by-default, allowlist model).

#### 9.3.9 `escalation` — Escalation Policy — *Informative (Non-Normative)*

> **Note:** This rule type is informative. It describes a product-level routing extension that Gateway implementations MAY support. The core protocol supports escalation through the `fallback_action_id` timeout mechanism (Section 7.3). Multi-tier escalation with timezone-aware availability is a gateway product concern, not a protocol primitive.

Defines an ordered chain of human responders when the primary principal is unavailable. Replaces the simple `timeout → fallback_action` model with a multi-tier escalation before resorting to the fallback.

```json
{
  "rule_id": "escalation_chain",
  "type": "escalation",
  "escalation_tiers": [
    {
      "target": "user:fahad",
      "timeout_seconds": 120,
      "availability": {"hours": {"start": "08:00", "end": "23:00"}, "timezone": "America/New_York"}
    },
    {
      "target": "role:senior_engineer",
      "timeout_seconds": 180,
      "availability": {"hours": {"start": "06:00", "end": "22:00"}, "timezone": "America/Chicago"}
    },
    {
      "target": "user:sarah",
      "timeout_seconds": 120,
      "availability": {"hours": {"start": "09:00", "end": "18:00"}, "timezone": "Europe/London"}
    }
  ],
  "final_fallback": "abort",
  "notify_all_on_escalation": true
}
```

**Escalation rules:**
- The Gateway evaluates tiers in order. If the current tier's target is outside their `availability` window, that tier is SKIPPED immediately (no timeout delay).
- If all tiers are exhausted without a response, the `final_fallback` action is executed.
- When `notify_all_on_escalation` is true, previously skipped/timed-out principals receive a notification that the action was escalated (for awareness, not for action).
- Escalation does NOT override `action_scope` or `mandatory_hitl` rules — it only changes *who* is asked, not *whether* asking is required.
- Each escalation step produces an `escalation_triggered` audit record.

#### 9.3.10 `geo_constraint` — Jurisdictional Boundaries — *Informative (Non-Normative)*

> **Note:** This rule type is informative. Jurisdictional data residency is an infrastructure deployment concern, not a protocol primitive. Where a Gateway stores or processes data is determined by the Gateway operator's deployment architecture, not by the communication protocol. Gateway implementations MAY support `geo_constraint` rules, but conformant RAMP implementations are NOT required to.

Restricts where agent data may be processed or stored, for compliance with data residency requirements (GDPR, data sovereignty laws, Schrems II).

```json
{
  "rule_id": "eu_data_residency",
  "type": "geo_constraint",
  "constraint": {
    "allowed_jurisdictions": ["EU", "GB"],
    "scope": "data_processing_and_storage"
  },
  "on_violation": "deny_and_notify"
}
```

**Geo constraint rules:**
- `allowed_jurisdictions` uses ISO 3166-1 alpha-2 country codes, or the following recognized region groups: `EU` (all EU member states), `EEA` (EU + Iceland, Liechtenstein, Norway), `FVEY` (Five Eyes nations).
- `scope` values: `data_processing_only`, `data_storage_only`, `data_processing_and_storage`.
- The Gateway MUST include its own jurisdiction in registration responses (Section 4.3) so agents can verify compliance before sending data.
- This rule is evaluated at the Gateway level. Agent SDK is NOT required to enforce geo constraints — the Gateway is the enforcement point.
- For multi-region Gateway deployments, the Gateway MUST route messages to nodes within the allowed jurisdictions.

#### 9.3.11 `emergency_override` — Break Glass — *Informative (Non-Normative)*

> **Note:** This rule type is informative. It describes a product-level safety feature that Gateway implementations MAY support. The specific mechanics (MFA requirements, cooldown timers, notification targets, enhanced audit levels) are gateway product decisions, not protocol primitives. The core protocol principle — that a Human Principal MUST always be able to suspend or terminate an agent (Section 3.3) — provides the baseline safety guarantee.

Allows a Human Principal to temporarily bypass ALL governance rules in a genuine emergency. This is a safety mechanism — in extreme scenarios, governance rules must not prevent a human from taking necessary action.

```json
{
  "rule_id": "break_glass",
  "type": "emergency_override",
  "requires_mfa": true,
  "requires_justification": true,
  "auto_expire_minutes": 30,
  "cooldown_minutes": 60,
  "audit_level": "enhanced",
  "notification_targets": ["role:security_team", "user:cto"]
}
```

**Emergency override rules:**
- **ONLY Human Principals may activate an emergency override.** An agent MUST NEVER be able to request or trigger a break glass. Any agent message referencing emergency override MUST be rejected with `RAMP-4011: POLICY_VIOLATION`.
- When activated, the Gateway temporarily disables all `action_scope`, `resource_constraint`, `time_constraint`, `aggregate_constraint`, and `geo_constraint` rules for the specified agent(s). `mandatory_hitl` rules remain active — the human is still required to approve actions, but the scope of what they can approve is unrestricted.
- `requires_mfa`: If true, the Client MUST require multi-factor authentication before activating.
- `requires_justification`: If true, the principal MUST provide a free-text justification that is included in the audit record.
- `auto_expire_minutes`: Override automatically deactivates after this duration. Maximum allowed value: 480 (8 hours).
- `cooldown_minutes`: After an override expires or is deactivated, another override cannot be activated for this duration (prevents abuse).
- `audit_level: "enhanced"`: During override, EVERY agent message (including telemetry) generates a full audit record, not just state transitions and actions. This creates a forensic-grade log.
- `notification_targets`: The Gateway MUST notify all listed targets when an override is activated, extended, or deactivated. This ensures organizational visibility.
- The Gateway MUST generate `emergency_override_activated` and `emergency_override_expired` audit records.

### 9.4 Policy Evaluation Precedence

When multiple rules apply to the same Action Request, the Gateway MUST evaluate them in the following precedence order (highest priority first):

**Normative (MUST implement for Level 3 conformance):**
```
1. mandatory_hitl        — Always enforced. Cannot be overridden.
2. action_scope (deny)   — Explicit deny. Cannot be auto-approved.
3. aggregate_constraint  — Cross-agent budget exceeded.
4. resource_constraint   — Per-agent budget exceeded.
5. time_constraint       — Outside operating hours.
6. rate_constraint       — Rate limit exceeded (evaluated on message receipt, before content evaluation).
```

**Informative (MAY implement — product-level extensions):**
```
7. emergency_override    — If active, bypasses rules 2-5 (but NOT mandatory_hitl).
8. geo_constraint        — Jurisdictional deny.
9. data_access           — Evaluated on data access requests.
10. auto_resolution      — Auto-approve/deny. Only applied if NO higher-priority rule has triggered.
11. escalation           — Determines WHO receives the Action Request, not WHETHER it's allowed.
```

**Critical invariant:** A `deny` from rules 1-6 MUST NEVER be overridden by an `auto_resolution` rule. If an action is explicitly denied by scope or budget, the auto-approve rule is not evaluated. This prevents the dangerous case where a research agent auto-approves a $0-cost `send_email` action that should have been blocked by `action_scope`.

**Evaluation output:** The Gateway MUST include the full policy evaluation trace in the audit record:

```json
{
  "policy_evaluation": {
    "rules_evaluated": [
      {"rule_id": "rate_limit", "result": "pass"},
      {"rule_id": "allowed_actions", "result": "pass", "matched_category": "search_flights"},
      {"rule_id": "eu_data_residency", "result": "pass"},
      {"rule_id": "spend_limit", "result": "pass", "remaining_budget": 80},
      {"rule_id": "global_daily_budget", "result": "pass", "remaining_budget": 120},
      {"rule_id": "operating_hours", "result": "pass"},
      {"rule_id": "require_approval_irreversible", "result": "not_applicable"},
      {"rule_id": "auto_approve_low_cost", "result": "applied", "resolution": "auto_approve"}
    ],
    "final_decision": "auto_approve",
    "override_active": false
  }
}
```

### 9.5 Policy Violation Messages

When the Gateway detects a policy violation, it MUST:
1. Enforce the `on_violation` action.
2. Notify the Human Principal with a `policy_violation` message.
3. Create an audit record.

```json
{
  "message_type": "policy_violation",
  "payload": {
    "violated_rule_id": "spend_limit",
    "violated_policy_id": "pol_travel_agent_v2",
    "agent_id": "agent:travel_booker",
    "details": "Agent attempted action with estimated cost $750, exceeding session limit of $500.",
    "enforcement_action": "suspend_and_notify",
    "agent_new_state": "SUSPENDED"
  }
}
```

### 9.6 Violation Response Behaviors

Each governance rule specifies an `on_violation` field that determines the Gateway's enforcement response. This section formally defines the semantics of each violation response behavior.

#### 9.6.1 Behavior Definitions

| Behavior | Semantics |
|---|---|
| `deny_and_notify` | The Gateway MUST reject the triggering message (e.g., Action Request, data access request) with `RAMP-4011: POLICY_VIOLATION`. The action is NOT executed. The Gateway MUST send a `policy_violation` notification to all principals with `owner` role. The agent remains in its current state (no state transition forced). |
| `suspend_and_notify` | The Gateway MUST reject the triggering message AND force the agent into `SUSPENDED` state. The Gateway MUST send a `policy_violation` notification to all `owner` principals. The agent MUST NOT send further messages (except `telemetry` acknowledging the `SUSPENDED` state) until a principal with `owner` role explicitly resumes it via `POST /ramp/v1/agents/{agent_id}/resume`. |
| `suspend_all_and_notify` | Same as `suspend_and_notify`, but applied to ALL agents in the scope defined by the rule (e.g., all agents for a given principal, or all agents in an agent group). Used exclusively by `aggregate_constraint` rules. The Gateway MUST send a single consolidated `policy_violation` notification listing all suspended agents and their individual contributions to the aggregate metric. |
| `suspend_until_allowed` | The Gateway MUST force the agent into `SUSPENDED` state. Unlike `suspend_and_notify`, the Gateway automatically resumes the agent (transitions to `IDLE`) when the constraint condition is no longer violated (e.g., the operating hours window reopens). No manual human intervention is required for resumption. The Gateway MUST send a notification of category `info` to `owner` principals when the agent is automatically resumed, so owners are aware the agent has restarted. The Gateway MUST create audit records for both the suspension and the automatic resumption. |
| `throttle_and_warn` | The Gateway MUST NOT reject or suspend the agent. Instead, the Gateway MUST: (1) Begin dropping messages that exceed the rate limit, returning `RAMP-4014: RATE_LIMITED` with the following standard rate limit headers: `X-RateLimit-Limit` (the maximum number of messages allowed in the current window), `X-RateLimit-Remaining` (the number of messages remaining in the current window), `X-RateLimit-Reset` (Unix timestamp in seconds when the current window resets), and `Retry-After` (the number of seconds the agent should wait before retrying). (2) Send a single `warning` notification to the `owner` principal(s) per throttle window (to avoid notification spam about the throttling itself). (3) Continue accepting messages that fall within the rate limit. Throttling is stateful and resets when the rate window elapses. |

#### 9.6.2 Resumption Semantics

Agents in `SUSPENDED` state (due to `suspend_and_notify` or `suspend_all_and_notify`) require explicit human intervention to resume:

**Endpoint:** `POST /ramp/v1/agents/{agent_id}/resume`

**Request Body:**
```json
{
  "resumed_by": "user:fahad",
  "reason": "Reviewed the policy violation. Increasing session budget.",
  "policy_adjustments": [
    {
      "rule_id": "spend_limit",
      "adjustment": "increase_limit",
      "new_value": 1000
    }
  ]
}
```

**Resumption Rules:**
- Only principals with `owner` role MAY resume an agent.
- The optional `policy_adjustments` array allows the owner to modify the violated rule at resumption time (e.g., increasing a budget limit after reviewing the situation). This avoids the cycle of: resume → immediate re-violation → re-suspension.
- Resumption MUST produce an `agent_resumed` audit record that includes the `reason`, the identity of the resuming principal, and any policy adjustments made.
- Upon resumption, the agent transitions from `SUSPENDED` to `IDLE`. The agent MUST NOT automatically resume the task that triggered the violation — it must explicitly transition to `EXECUTING` via a new telemetry message.

#### 9.6.3 Warning Behavior

For rules that support `on_warning` (e.g., `aggregate_constraint`), the Gateway MUST:
- Send a `cost_alert` notification to the `owner` principal(s).
- NOT suspend or throttle the agent.
- Log an `aggregate_budget_warning` audit record.
- Continue normal message processing.

The purpose of warnings is to give the human principal time to intervene proactively before a hard limit is reached.

---

## 10. Audit Trail

### 10.1 Purpose

Every meaningful event in the RAMP ecosystem produces an immutable audit record. Audit records are hash-chained to provide tamper evidence.

### 10.2 Audit Record Schema

```json
{
  "audit_id": "aud_001",
  "event_type": "action_resolved",
  "session_id": "sess_x9y8z7",
  "agent_id": "agent:ci_deployer_v3",
  "principal_id": "user:fahad",
  "timestamp": "2026-02-22T03:32:00.000Z",
  "details": {
    "action_request_id": "01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c",
    "selected_action_id": "deploy_prod",
    "resolution_type": "human_decision",
    "resolved_by": "user:fahad",
    "request_to_resolution_ms": 120000
  },
  "policy_evaluation": {
    "rules_checked": ["spend_limit", "operating_hours", "require_approval_irreversible"],
    "all_passed": true
  },
  "integrity": {
    "record_hash": "sha256:a3f2b9c8d7e6f5a4b3c2d1e0f9...",
    "previous_hash": "sha256:7d1e4f8a9b0c1d2e3f4a5b6c7d...",
    "chain_index": 42
  }
}
```

### 10.3 Supported Event Types

| Event | Trigger |
|---|---|
| `agent_registered` | Agent authenticates with Gateway for the first time. |
| `session_started` | Agent begins a new execution session. |
| `state_transition` | Agent changes lifecycle state. |
| `notification_sent` | Agent sends a notification to principal. |
| `action_requested` | Agent sends an HITL action request. |
| `action_resolved` | Human (or policy engine) resolves an action request. |
| `policy_violated` | Agent violates a governance policy. |
| `agent_suspended` | Agent is suspended (by human or policy). |
| `agent_terminated` | Agent execution ends. |
| `session_ended` | Agent session closes. |
| `emergency_override_activated` | Human Principal activated break glass. |
| `emergency_override_expired` | Break glass override auto-expired or was manually deactivated. |
| `escalation_triggered` | Action Request escalated to next tier in escalation chain. |
| `aggregate_budget_warning` | Cross-agent spending reached 80% of aggregate limit. |
| `aggregate_budget_exceeded` | Cross-agent spending exceeded aggregate limit. |
| `binding_changed` | Principal Binding added, removed, or expired. |
| `webhook_verification_failed` | Webhook callback verification handshake failed for a registered callback URL. |
| `agent_resumed` | A suspended agent was resumed by a principal (includes resuming principal and any policy adjustments). |
| `policy_adjusted` | A governance rule was modified at resumption time via `policy_adjustments` (Section 9.6.2). |
| `attempted_resolution` | An `observer` or insufficient-role principal attempted to resolve an Action Request and was denied. Security-relevant. |
| `audit_exported` | An audit export was performed (Section 10.5.3). Captures who, what range, and format (meta-audit). |
| `agent_unresponsive` | Gateway detected agent unresponsiveness (no telemetry for 3× expected interval). Section 5.3. |
| `action_expired_late_response` | A human approval was received after the Action Request timeout had elapsed and the agent had already executed the fallback. Records both the late response and the agent's `RAMP-4023` rejection. |
| `corroboration_timeout` | Corroboration Hook did not return within the configured timeout. Gateway applied maximum-risk fallback (Section 11.1.2). |
| `delivery_failed` | Gateway failed to deliver an Action Response or policy enforcement message to the agent webhook after all retries (Section 4.6.4). |

> **Note:** `delivery_retry` events (logged between retry attempts per Section 4.6.4) are non-audit operational log entries — they are intentionally excluded from the hash-chained audit trail to avoid audit spam. Only the final `delivery_failed` event enters the audit chain.

### 10.4 Integrity Guarantees

- Each audit record includes a SHA-256 hash of its own contents and the hash of the previous record in the chain.
- The Gateway MUST store audit records in append-only storage.
- Clients and auditors can verify chain integrity by recomputing hashes sequentially.
- The Gateway SHOULD periodically publish chain anchors (root hashes) to an external timestamping service for non-repudiation.

### 10.5 Audit Trail Query API

The audit trail is only useful if it is queryable. This section defines the API through which principals and external auditors retrieve and verify audit records.

#### 10.5.1 Query Endpoint

**Endpoint:** `GET /ramp/v1/audit`

**Query Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | No | Filter by agent. Omit to query across all agents the principal has audit access to. |
| `session_id` | string | No | Filter by session. |
| `event_type` | string | No | Filter by event type (e.g., `action_resolved`, `policy_violated`). Comma-separated for multiple types. |
| `from` | ISO 8601 | No | Start of time range (inclusive). |
| `to` | ISO 8601 | No | End of time range (inclusive). |
| `principal_id` | string | No | Filter by the principal involved in the event. |
| `limit` | integer | No | Maximum number of records to return. Default: 50. Maximum: 500. |
| `cursor` | string | No | Opaque pagination cursor returned from a previous query. |

**Response Body (200 OK):**

```json
{
  "records": [
    {
      "audit_id": "aud_001",
      "event_type": "action_resolved",
      "session_id": "sess_x9y8z7",
      "agent_id": "agent:ci_deployer_v3",
      "principal_id": "user:fahad",
      "timestamp": "2026-02-22T03:32:00.000Z",
      "details": { },
      "integrity": {
        "record_hash": "sha256:a3f2b9c8...",
        "previous_hash": "sha256:7d1e4f8a...",
        "chain_index": 42
      }
    }
  ],
  "pagination": {
    "next_cursor": "eyJsYXN0X2lkIjoiYXVkXzA1MCJ9",
    "has_more": true,
    "total_count": 1247
  }
}
```

**Access Control:**
- Principals with `auditor` role may query audit records for their bound agents only.
- Principals with `owner` role may query all audit records for their owned agents.
- Principals with `observer` or `approver` roles may query audit records for events they were involved in or notified about.
- The Gateway MUST NOT return audit records for agents the requesting principal has no binding to.

#### 10.5.2 Integrity Verification Endpoint

**Endpoint:** `GET /ramp/v1/audit/verify`

**Query Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `agent_id` | string | Yes | Agent whose audit chain to verify. |
| `session_id` | string | No | Verify a specific session's chain. Omit to verify the full agent chain. |
| `from_index` | integer | No | Start verification from this chain index. Default: 0. |
| `to_index` | integer | No | End verification at this chain index. Default: latest. |

**Response Body (200 OK):**

```json
{
  "agent_id": "agent:ci_deployer_v3",
  "chain_length": 1247,
  "verified_range": {
    "from_index": 0,
    "to_index": 1247
  },
  "integrity_status": "valid",
  "first_record_hash": "sha256:0a1b2c3d...",
  "last_record_hash": "sha256:f9e8d7c6...",
  "mismatches": [],
  "verified_at": "2026-02-22T04:05:00.000Z"
}
```

**If integrity violations are detected:**

```json
{
  "integrity_status": "corrupted",
  "mismatches": [
    {
      "chain_index": 87,
      "expected_hash": "sha256:aaa...",
      "actual_hash": "sha256:bbb...",
      "audit_id": "aud_087"
    }
  ]
}
```

**Verification Rules:**
- The Gateway MUST recompute the SHA-256 hash of each audit record and compare it against the stored `record_hash`, and verify that each record's `previous_hash` matches the preceding record's `record_hash`.
- The endpoint MUST be available to any principal with `auditor` or `owner` role for the specified agent.
- For large chains (>10,000 records), the Gateway MAY perform verification asynchronously and return a `202 Accepted` with a polling URL for the result.
- External auditors without a principal binding may verify chain integrity if the Gateway supports the optional **Public Verification Mode**, where the verification endpoint accepts a chain anchor (root hash) published by the Gateway to an external timestamping service (Section 10.4).

#### 10.5.3 Audit Export

For compliance reporting and integration with external SIEM (Security Information and Event Management) systems, the Gateway MUST support bulk export of audit records.

**Endpoint:** `GET /ramp/v1/audit/export`

**Query Parameters:** Same filters as Section 10.5.1, plus:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `format` | string | Yes | Export format. MUST support: `jsonl` (JSON Lines, one record per line), `otel` (OpenTelemetry-compatible spans). |

**Response:** Streamed download with appropriate `Content-Type`:
- `jsonl`: `Content-Type: application/x-ndjson`
- `otel`: `Content-Type: application/x-protobuf` (OpenTelemetry Protocol format)

**Export Rules:**
- Exports MUST include integrity hashes so the recipient can independently verify the chain.
- The Gateway MUST log an `audit_exported` audit record capturing who exported what range and in what format (meta-audit).
- Rate limit: The Gateway SHOULD limit export requests to prevent denial-of-service via expensive bulk queries. RECOMMENDED: maximum 10 export requests per hour per principal.

---

## 11. Security Model

### 11.1 Threat Model

| Threat | Mitigation |
|---|---|
| **Agent impersonation** | Each agent has a unique API key + shared secret for HMAC signing. The Gateway verifies signatures on every message. |
| **Replay attacks** | Monotonic sequence numbers + nonces, bounded by a 5-minute timestamp window (Section 4.2). Gateway rejects duplicate (message_id, nonce) pairs within the window. |
| **Man-in-the-middle** | All transport MUST use TLS 1.3+. HMAC signatures provide payload integrity even if TLS is compromised. |
| **Runaway/spam agent** | Per-agent rate limiting at the Gateway. Policy engine can auto-suspend. |
| **Compromised Gateway** | Audit trail hash chain allows detection of tampered records. Agents MAY independently log their own messages for reconciliation. |
| **Social engineering via agent** | Action Requests with `risk_level: critical` or `irreversible` MUST require `confirmation_required: true`. Clients MUST display the agent's identity prominently. |
| **Privilege escalation** | Policy engine enforces least-privilege per agent. An agent's API key is scoped to specific action categories. |
| **Risk payload manipulation** | Out-of-scope for honest-agent deployments (Section 11.1.1). Gateway implementors requiring verified risk corroboration SHOULD deploy a Corroboration Hook (Section 11.1.2), which overrides agent-declared `risk_assessment` values before policy evaluation and applies maximum-risk fallback on hook timeout. |

#### 11.1.1 Honest Agent Scope

RAMP's Gateway-enforced governance (Section 9) is designed to protect against **unsupervised autonomous drift, runaway loops, and unintentional policy violations by honest but imperfect agents**. The risk classification declared in `risk_assessment` (reversibility, estimated cost, action category) is agent-reported and is not independently verified by the Gateway at the protocol level.

RAMP does **NOT** provide enforcement against actively malicious agents that deliberately misreport their `risk_assessment` payload to bypass governance controls (e.g., declaring `estimated_cost_usd: 0` for a $1,000 transaction to circumvent `resource_constraint` rules). This scoping is consistent with analogous security boundaries in other infrastructure protocols: HTTP does not protect against a server that lies about its `Content-Length` header.

#### 11.1.2 Corroboration Hook (Gateway Extension Point)

Gateway implementors that require verified risk corroboration MUST implement a **Corroboration Hook** — a synchronous, pluggable interface invoked by the Gateway **before** policy evaluation runs. The hook receives the full RAMP envelope and may override any agent-declared `risk_assessment` field with an externally verified value.

**Interface contract:**

- The Gateway MUST invoke the Corroboration Hook (if registered) on every inbound Action Request, before evaluating any policy rule.
- The hook MUST return a corroborated `risk_assessment` object (full or partial override) within the Corroboration Timeout (configurable, default: 2 seconds).
- The Gateway MUST use the hook's returned values (not agent-declared values) for all downstream policy evaluation when a hook is registered.
- If the hook returns within the timeout, the Gateway MUST replace the agent-declared `risk_assessment` fields with the corroborated values and log **both** (agent-declared and corroborated) in the audit record for forensic comparison.
- **Timeout behavior:** If the hook does not return within the Corroboration Timeout, the Gateway MUST treat the request as **maximum-risk** — applying `risk_level: critical` and `estimated_cost_usd: Infinity` — and route the Action Request to the Human Principal regardless of any `auto_resolution` rules. The Gateway MUST log a `corroboration_timeout` audit record.

Hook implementations are not defined by RAMP — backend integrations are implementation details. RAMP defines the interface semantics only.

### 11.2 Authentication

- **Agents → Gateway:** Scoped API key in the `Authorization` header + HMAC signature in the message envelope.
- **Human Principal → Gateway:** OAuth 2.0 / OIDC (standard identity providers: Google, GitHub, corporate SSO).
- **Gateway → Client (push):** APNs/FCM device tokens registered during client setup.
- **Gateway → Agent (webhook):** HMAC-SHA256 signed payloads using the agent's `shared_secret` (Section 4.6.3). This is the pre-shared secret mechanism. Gateway implementations MAY additionally require mutual TLS as a transport-layer supplement, but HMAC signing is the normative authenticity mechanism.

### 11.3 Key Rotation

- Agent API keys MUST support rotation without downtime. The Gateway MUST accept both the current and previous key for a configurable grace period (default: 24 hours).
- The Gateway MUST emit an audit record when a key is rotated.

### 11.4 Cryptographic Agility (Informative)

> **Status:** This section is informative. HMAC-SHA256 remains the MUST-support signing mechanism for RAMP v0.2. JWS support is a SHOULD-level recommendation for production deployments.

#### 11.4.1 Motivation

The baseline HMAC-SHA256 signing scheme (Section 4.2) provides message integrity and authentication using pre-shared secrets. This is sufficient for single-tenant deployments where the agent and gateway share a trust boundary. However, HMAC provides **symmetric** authentication — any party holding the shared secret can produce valid signatures. This limits non-repudiation: the gateway cannot prove to a third party that a specific agent (rather than the gateway itself) authored a message.

For multi-tenant, cross-organizational, or regulatory-grade deployments, **asymmetric** signatures provide stronger guarantees:

- **Non-repudiation:** Only the private key holder can produce a valid signature.
- **Third-party verification:** Auditors can verify signatures using public keys without holding secrets.
- **Key distribution:** Public keys can be distributed via standard discovery mechanisms (JWKS endpoints) without exposing secrets.

#### 11.4.2 Recommended JWS Profile

Production deployments SHOULD support JSON Web Signature (JWS, RFC 7515) as an alternative signing mechanism. The recommended profile:

| Parameter | Recommendation |
|---|---|
| **Algorithm** | `Ed25519` (EdDSA, RFC 8037) preferred; `ES256` (ECDSA P-256, RFC 7518) acceptable |
| **Key representation** | JWK (RFC 7517) |
| **Key discovery** | JWKS endpoint at `/.well-known/jwks.json` on the agent's domain |
| **Signature format** | Detached JWS (RFC 7797) over RFC 8785 JCS-canonicalized payload |

When JWS is used, the `signature` field in the message envelope MUST contain a detached JWS compact serialization instead of the HMAC hex digest. Gateways MUST distinguish between HMAC and JWS signatures by the presence of the `.` delimiter (JWS compact serialization contains two periods; HMAC hex digests do not).

#### 11.4.3 Version Negotiation

Gateways supporting JWS SHOULD advertise this in the `/ramp/v1/info` discovery response:

```json
{
  "signing_methods": ["hmac-sha256", "jws-ed25519", "jws-es256"],
  "jwks_uri": "https://gateway.example.com/.well-known/jwks.json"
}
```

Agents SHOULD check the gateway's supported signing methods during registration and select the strongest mutually supported method.

#### 11.4.4 Backward Compatibility

Gateways MUST continue to accept HMAC-SHA256 signatures even when JWS is supported. This ensures backward compatibility with agents that cannot perform asymmetric cryptography (e.g., constrained environments, rapid prototyping). The Conformance Levels (Section 14) do not change: HMAC-SHA256 is sufficient for all levels.

## 12. Transport Bindings

RAMP is transport-agnostic. This section defines conformant bindings for common transports.

### 12.1 HTTP Binding

- Agent → Gateway: `POST /ramp/v1/agents/{agent_id}/messages` with JSON body containing the RAMP envelope (Section 4).
- Response: `200 OK` with `{"status": "accepted", "message_id": "..."}`.
- Errors: Standard RAMP error codes (Section 13) in the response body.

### 12.2 WebSocket Binding

- Used for persistent bidirectional connections (e.g., local agents like VS Code extensions).
- Agent establishes WebSocket connection to `wss://gateway.example.com/ramp/v1/ws`.
- Authentication via initial handshake message containing the API key.
- Messages are RAMP envelopes serialized as JSON text frames.
- The Gateway sends Action Responses and policy enforcement messages via the same WebSocket.

### 12.3 MQTT Binding (IoT/Edge) — *Informative (Non-Normative)*

> **Note:** This section is informative and non-normative. It describes a possible future transport binding for resource-constrained environments. Conformant RAMP implementations are NOT required to support MQTT. The normative transport bindings are HTTP (Section 12.1) and WebSocket (Section 12.2).

- For resource-constrained agents (IoT devices, edge deployments).
- Agent publishes to topic: `ramp/{principal_id}/{agent_id}/messages`
- Gateway subscribes and processes.
- Responses published to: `ramp/{principal_id}/{agent_id}/responses`
- QoS level 1 (at least once delivery) is RECOMMENDED to ensure message delivery without the overhead of QoS 2.
- Agents using MQTT MUST still include the full RAMP envelope (Section 4.1) in the message payload.

---

## 13. Error Codes

| Code | Name | Description |
|---|---|---|
| `RAMP-4001` | `INVALID_ENVELOPE` | Message envelope missing required fields or malformed. |
| `RAMP-4002` | `INVALID_SIGNATURE` | HMAC signature verification failed. |
| `RAMP-4003` | `SEQUENCE_VIOLATION` | Sequence number is not strictly greater than the last received (duplicate or regression), OR message arrived out of order and the Gateway does not buffer. The Gateway MUST include the expected sequence number in the error response: `{"expected_sequence": N, "received_sequence": M}`. |
| `RAMP-4004` | `INVALID_STATE_TRANSITION` | The reported state transition violates the lifecycle state machine. |
| `RAMP-4005` | `PAYLOAD_TOO_LARGE` | Message payload exceeds the maximum size (default: 64 KB). |
| `RAMP-4006` | `DUPLICATE_MESSAGE` | A message with this `message_id` has already been processed. |
| `RAMP-4007` | `DUPLICATE_NONCE` | The `(message_id, nonce)` pair has already been seen within the replay window (Section 4.2). |
| `RAMP-4008` | `UNKNOWN_AGENT` | The `agent_id` is not registered with this Gateway. |
| `RAMP-4009` | `UNAUTHORIZED_PRINCIPAL` | The agent is not authorized to send messages to this principal. |
| `RAMP-4010` | `AGENT_ALREADY_REGISTERED` | Re-registration attempted with an identical payload for an already-registered `agent_id`. Use the key rotation path instead if the shared secret has changed. |
| `RAMP-4011` | `POLICY_VIOLATION` | The action violates a governance policy. |
| `RAMP-4012` | `HITL_ALREADY_PENDING` | Agent already has an outstanding Action Request in this session. |
| `RAMP-4013` | `SESSION_EXPIRED` | The session has been terminated or has timed out. |
| `RAMP-4014` | `RATE_LIMITED` | Agent has exceeded its rate limit. Retry after the specified duration. |
| `RAMP-4015` | `CONFORMANCE_MISMATCH` | Agent requested a conformance level not supported by this Gateway. |
| `RAMP-4016` | `SESSION_ALREADY_ACTIVE` | Agent attempted to create a new session while one is already active. |
| `RAMP-4017` | `ACTION_ALREADY_RESOLVED` | Another principal already resolved this Action Request. |
| `RAMP-4018` | `UNSUPPORTED_VERSION` | The requested RAMP protocol version is not supported by this Gateway. |
| `RAMP-4019` | `WEBHOOK_VERIFICATION_FAILED` | Webhook callback verification handshake failed. The callback URL did not return the expected challenge token. |
| `RAMP-4020` | `BINDING_NOT_FOUND` | The specified principal binding does not exist for this agent. |
| `RAMP-4021` | `LAST_OWNER_BINDING` | Cannot remove the last `owner` binding. An agent MUST have at least one owner at all times. |
| `RAMP-4022` | `INSUFFICIENT_ROLE` | The requesting principal does not have the required role for this operation (e.g., `observer` attempting to resolve an Action Request). |
| `RAMP-4023` | `ACTION_EXPIRED` | Sent by an **Agent** to the Gateway after it has already executed the `fallback_action_id` and subsequently receives a late human Action Response. The Gateway MUST relay this error to the resolving principal with the message: "Your approval arrived after the timeout had elapsed and the fallback action was already executed." This prevents the agent from entering a split-state where both the fallback and the human-approved action execute concurrently. The Gateway MUST create an `action_expired_late_response` audit record capturing both the late approval and the agent's rejection of it. |
| `RAMP-5001` | `GATEWAY_ERROR` | Internal Gateway error. |
| `RAMP-5002` | `DELIVERY_FAILED` | Gateway could not deliver the message to the Client (push notification failed). |

---

## 14. Conformance Levels

Implementations may claim conformance at different levels:

### 14.1 Level 1: Basic (Agent SDK)

- MUST support telemetry messages (Section 5).
- MUST support notification messages (Section 6).
- MUST include valid envelope with signature (Section 4).
- MUST model lifecycle states (Section 3).

### 14.2 Level 2: Interactive (Agent SDK + HITL)

- All Level 1 requirements.
- MUST support Action Request and Response messages (Sections 7-8).
- MUST implement timeout and fallback behavior.

### 14.3 Level 3: Governed (Gateway)

- All Level 2 requirements.
- MUST support policy evaluation and enforcement (Section 9), including at minimum: `resource_constraint`, `action_scope`, `mandatory_hitl`, `time_constraint`, `rate_constraint`.
- MUST support `aggregate_constraint` for cross-agent budget enforcement.
- MUST enforce policy evaluation precedence (Section 9.4).
- MUST implement violation response behaviors (Section 9.6), including `deny_and_notify`, `suspend_and_notify`, `suspend_until_allowed`, and `throttle_and_warn`.
- MUST produce hash-chained audit records (Section 10).
- MUST support the audit query API (Section 10.5) with pagination and access control.
- MUST enforce state machine transitions (Section 3).
- MUST implement canonical JSON serialization per RFC 8785 (Section 4.8) for all signature computations.
- MUST support protocol version negotiation (Section 4.7).

### 14.4 Level 4: Enterprise (Gateway + Multi-tenant) — *Informative (Non-Normative)*

> **Note:** Level 4 describes product-level capabilities that enterprise Gateway implementations MAY support. These are not protocol conformance requirements. They are included to illustrate the governance surface that the protocol’s extension points enable.

- All Level 3 requirements.
- MUST support multi-principal routing.
- MUST support delegation and multi-party approval (Section 7.5-7.6).
- MUST support `escalation` rules with timezone-aware availability (Section 9.3.9).
- MUST support `geo_constraint` for jurisdictional data residency (Section 9.3.10).
- MUST support `data_access` rules with deny-by-default (Section 9.3.8).
- MUST support `emergency_override` with MFA and enhanced audit (Section 9.3.11).
- MUST implement webhook callback verification (Section 4.6), including the challenge-response handshake and webhook signing.
- MUST support the audit integrity verification endpoint (Section 10.5.2).
- MUST support audit record export in standard formats: JSON Lines and OpenTelemetry (Section 10.5.3).
- MUST support self-hosted deployment behind corporate VPN.

---

## 15. Comparison: RAMP vs. Extensions of Existing Protocols

| Capability | MCP Extension? | A2A Extension? | RAMP (Native) |
|---|---|---|---|
| Agent → Human notifications | Not designed for this | Possible but awkward (human as "agent") | First-class primitive |
| Structured HITL with timeout/fallback | No | Task "input needed" exists but lacks timeout, risk assessment, delegation | Full lifecycle support |
| Governance policy enforcement | No | No | 6 governance rule types + optional auto-resolution + 4 informative enterprise extensions, with formal precedence (Section 9) |
| Capability permissions (action scope) | No | No | `action_scope` rules controlling what agents can do, not just spend |
| Cross-agent budget controls | No | No | `aggregate_constraint` with sliding windows |
| Privacy / data minimization (GDPR Art. 25) | No | No | Achievable via `action_scope` rules; dedicated `data_access` rule type available as informative extension |
| Jurisdictional data residency | No | No | `geo_constraint` available as informative extension (infrastructure concern) |
| Escalation chains | No | No | Informative extension — multi-tier with timezone-aware availability |
| Emergency override (break glass) | No | No | Informative extension — MFA-gated with enhanced audit and auto-expiry |
| Tamper-evident audit trail | No | No | Hash-chained audit records |
| Agent lifecycle state machine | No | Task states exist but are task-scoped, not agent-scoped | Agent-scoped formal FSM |
| Multi-party approval | No | No | N-of-M approval support |
| Rate limiting / spend controls | No | No | Policy-based enforcement |
| Cross-agent dependency tracking | No | Implicit via task delegation | `aggregate_constraint` provides cross-agent budget awareness. Full dependency tracking is out of scope. |
| Multi-principal agent monitoring | No | No | Role-based bindings (owner, approver, observer, auditor) with first-response-wins and N-of-M routing |

---

## 16. Future Work (v0.3+)

The following capabilities are explicitly deferred to future protocol versions:

1. **Context Pull (Intentionally Out of Scope — Use MCP):** In-band agent access to personal data sources (health, calendar, financial) is intentionally outside RAMP's scope. RAMP is designed to **compose with** [Anthropic's Model Context Protocol (MCP)](https://modelcontextprotocol.io/), which already provides standardized, consent-gated agent access to tools and data sources. RAMP handles governance, oversight, and human-in-the-loop routing; MCP handles context and tool access. Combining them yields a complete, non-overlapping stack: MCP answers "what can the agent access?" and RAMP answers "what is the agent about to do, and does a human need to approve it?" Adding a competing context-pull mechanism to RAMP would duplicate existing standardization work and dilute this positioning.

2. **Cross-Gateway Federation:** Mechanism for agents supervised by different Gateways to participate in coordinated workflows while maintaining independent audit trails.

3. **Batch Action Requests:** Support for agents to request approval for bulk operations (e.g., "Send 200 personalized emails") via a `batch_action_request` message type. The Client would render a sample of representative items (e.g., 3 of 200 emails) with options to [Approve All], [Review Individually], or [Abort]. This prevents the UX antipattern of 200 sequential HITL requests while maintaining human oversight over bulk operations.

4. **Agent Versioning & Hot-Swap:** Protocol support for updating an agent's code mid-lifecycle. Defines whether a running session should be gracefully terminated and re-registered, or whether the session can be inherited by the new version with a version-transition audit record.

5. **Shared Agents (Multi-Principal):** Support for agents that serve multiple Human Principals simultaneously (e.g., a family calendar agent serving both partners). Defines shared ownership semantics, per-principal policy scoping, and conflict resolution when principals issue contradictory directives to the same agent.

> **Note:** High-frequency telemetry (e.g., sub-second progress updates) is already supported via the WebSocket transport binding (Section 12.2) and does not require a separate protocol extension.

---

## Appendix A: Example — Full HITL Flow

```
1. Agent sends Telemetry (state: EXECUTING, progress: 100%)
2. Agent sends Action Request ("Deploy to production?", timeout: 300s)
3. Agent sends Telemetry (state: AWAITING_HUMAN_INPUT)
4. Gateway evaluates policies → all rules pass
5. Gateway delivers push notification to Client
6. Client renders approval UI with [Deploy] [Staging Only] [Abort]
7. Human taps [Deploy] → Client sends confirmation dialog → Human confirms
8. Client sends Action Response (action_id: deploy_prod) to Gateway
9. Gateway creates audit record (hash-chained)
10. Gateway delivers Action Response to Agent (via WebSocket)
11. Agent sends Telemetry (state: EXECUTING, task: "deploying to prod")
12. Agent sends Notification (category: completion, "Deployed successfully")
13. Agent sends Telemetry (state: IDLE)
```

## Appendix B: Example — Policy Auto-Deny

```
1. Agent sends Action Request ("Book $750 flight", estimated_cost: 750)
2. Gateway evaluates policy "spend_limit" (max: $500/session) → VIOLATION
3. Gateway sends Policy Violation message to Agent
4. Gateway sends Policy Violation notification to Human Principal
5. Gateway forces Agent state → SUSPENDED
6. Gateway creates audit record (policy_violated, agent_suspended)
7. Human opens Client, reviews violation, taps [Resume Agent]
8. Gateway sends state change to Agent → IDLE
```

## Appendix C: SDK Pseudocode (Python)

```python
import asyncio
from ramp_sdk import RampAgent, RiskAssessment, AgentState

async def main():
    async with RampAgent(
        agent_id="travel_booker_v2",
        api_key="ramp_agt_sk_...",
        gateway_url="https://gateway.ramp-protocol.dev",
    ) as agent:
        # Report progress
        await agent.send_telemetry(
            state=AgentState.EXECUTING,
            task_description="Searching flights",
            progress_pct=30,
        )

        # Request human approval
        response = await agent.request_action(
            title="Book Flight?",
            body="Found NYC→LON for $420 on British Airways, Mar 15.",
            risk=RiskAssessment(
                risk_level="medium",
                reversibility="partially_reversible",
                estimated_cost_usd=420,
                action_category="purchase",
            ),
            options=[
                {"action_id": "book", "label": "Book It"},
                {"action_id": "skip", "label": "Skip"},
            ],
            timeout_seconds=600,
            fallback_action_id="skip",
        )

        if response.selected_action_id == "book":
            await agent.send_notification(
                title="Flight Booked",
                body="NYC→LON, Mar 15, $420",
                priority="normal",
            )
        # Session ends automatically when the `async with` block exits

asyncio.run(main())
```

## Appendix D: Action Option Style Values — *Informative (Non-Normative)*

> **Note:** This appendix is informative. Conformant implementations are NOT required to support or render these style values. The `style` field is defined in Section 7.3.

The `style` field on action options is an OPTIONAL hint to Client applications for rendering visual emphasis. Recommended values:

| Style | Recommended Rendering |
|---|---|
| `"primary"` | Prominent or highlighted button (e.g., filled, bold). Used for the recommended or default action. |
| `"secondary"` | Standard button appearance. Used for alternative actions. |
| `"destructive"` | Warning-colored button (e.g., red). Used for irreversible or high-risk actions. Clients SHOULD render destructive options with visual distinction to signal risk. |

Clients that do not recognize the `style` value MUST fall back to rendering the option as a standard button. The `style` field MUST NOT affect protocol semantics — it is a rendering hint only.
