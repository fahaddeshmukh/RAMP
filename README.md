<div align="center">
  <img src="./ramp_logo.png" alt="RAMP Logo" width="160"/>
  <h1>RAMP: Remote Agent Monitoring Protocol</h1>
  <p><strong>The open standard for Agent-to-Human communication.</strong></p>

  ![RAMP Spec](https://img.shields.io/badge/RAMP_Spec-v0.2_Draft-blue)
  ![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)
  ![Status](https://img.shields.io/badge/Status-Experimental_RFC-orange)

  **Author:** [Fahad Deshmukh](https://github.com/fahaddeshmukh)
</div>

---

## The Missing Layer of Agentic AI

The AI ecosystem has standardized how agents talk to **tools** ([Anthropic MCP](https://modelcontextprotocol.io/)) and how agents talk to **each other** ([Google A2A](https://github.com/google/a2a)).

**RAMP** is the standard for how agents talk to **humans**.

It is a transport-agnostic, cryptographically verifiable protocol for **Agent-to-Human (A2H) observability, governance, and Human-in-the-Loop (HITL) approval.**

---

## The Agent Protocol Triangle

<div align="center">
  <img src="./docs/ramp_protocol_triangle.png" alt="The Agent Protocol Triangle — RAMP fills the missing A2H layer" width="520"/>
</div>

The AI agent ecosystem has two well-defined sides of the triangle:
- **MCP** (Anthropic) — Agents calling tools, APIs, and databases.
- **A2A** (Google) — Agents coordinating with other agents.

The top of the triangle — **agents communicating with humans** — had no standard. That is what RAMP defines.

---

## Why RAMP?

Without RAMP, deploying autonomous agents at scale leads to:
- **Notification fatigue** — agents dumping approvals into Slack/Telegram with no structure.
- **Zero auditability** — no tamper-evident record of what an agent did and who approved it.
- **No governance** — nothing stops an agent from spending $10,000 autonomously.

**With RAMP, you get:**
1. **Standardized Telemetry** — A unified view of every agent running on your behalf.
2. **Action Requests (HITL)** — A secure, timeout-enforced protocol for agents to request explicit human approval via native push notifications (Apple Watch, iOS, Web).
3. **Declarative Governance** — A gateway-level policy engine enforcing spending limits, capability permissions, and operating hours across your entire agent fleet.
4. **Tamper-Evident Audit Trail** — A hash-chained record of every agent action and human decision.

---

## 🚀 SDK Quickstart (In Development)

> **Note:** The Python SDK is currently under active development and is not yet published to PyPI. The example below shows the intended API surface. Track progress in [`/sdk/`](./sdk/).

Once released:
```bash
pip install ramp-sdk
```

```python
import asyncio
from ramp_sdk import RampAgent, ActionOption, RiskAssessment

async def main():
    agent = RampAgent(
        agent_id="agent:flight_search",
        gateway_url="http://localhost:8000",
        api_key="your-api-key",
        principal_id="user:you"
    )

    async with agent:
        # Report progress
        await agent.send_telemetry(state="EXECUTING", task_description="Found flights...")

        # Request human approval (blocks until approved, denied, or 5-min timeout)
        response = await agent.request_action(
            title="Book Delta DL-402?",
            body="Non-refundable ticket for $420.00. Card ending in ••33.",
            options=[
                ActionOption(action_id="book", label="Book Flight"),
                ActionOption(action_id="abort", label="Cancel")
            ],
            risk=RiskAssessment(
                level="high",
                reversibility="irreversible",
                factors=["Non-refundable charge"],
                estimated_cost_usd=420.0,
            ),
            timeout_seconds=300,
            fallback_action_id="abort"  # Safe default if no response
        )

        if response.selected_action_id == "book":
            print("Human approved — booking now.")
```

---

## Repository Structure

| Path | Contents |
|---|---|
| `/docs/ramp_protocol_spec_v2.md` | 📄 The complete RAMP v0.2 Protocol Specification |
| `/docs/concept.md` | 💡 Project concept and two-layer architecture overview |
| `/docs/advanced_architecture.md` | 🏗️ Advanced architecture: local agents, enterprise deployments, fleet dashboards |
| `/sdk/` | 🐍 Official Python RAMP SDK |
| `/gateway/` | ⚙️ FastAPI Reference Gateway (policy enforcement, HMAC, WebSockets) |
| `/examples/` | 🤖 Runnable example agents demonstrating HITL flows |

---

## Enterprise & Compliance

RAMP is designed to support compliance with the human oversight requirements of the **EU AI Act (Article 14)** and **ISO/IEC 42001**. Because all Action Requests and telemetry pass through the Gateway, RAMP natively generates an immutable, hash-chained audit trail of exactly what an agent requested and which human approved it.

> **Note:** Formal regulatory conformance mappings are planned for future versions. This draft establishes the architectural foundations for such mappings.

---

## Contributing

We invite the AI framework community (LangChain, CrewAI, AutoGen, LlamaIndex) to review the spec and propose integrations. Open an issue or PR.

**License:** Apache 2.0
