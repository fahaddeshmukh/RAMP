"""RAMP Demo Agent — Flight Search

This is the demo agent for the 90-second video. It:
1. Registers with the Gateway
2. Sends telemetry as it "searches" for flights
3. Sends a notification with search results
4. Sends an Action Request asking human to approve a booking
5. Waits for the human's decision
6. Completes or cancels based on the response

Run:
    pip install -e sdk/
    python examples/flight_agent.py
"""

import asyncio
import sys
import os

# Add SDK to path if not installed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))

from ramp_sdk import RampAgent, ActionOption, RiskAssessment, AgentState


GATEWAY_URL = os.environ.get("RAMP_GATEWAY_URL", "http://localhost:8000")
API_KEY = os.environ.get("RAMP_API_KEY", "ramp-demo-key-2026")
PRINCIPAL_ID = os.environ.get("RAMP_PRINCIPAL_ID", "user:demo")


async def main():
    agent = RampAgent(
        agent_id="agent:flight_search_v1",
        gateway_url=GATEWAY_URL,
        api_key=API_KEY,
        principal_id=PRINCIPAL_ID,
        agent_name="Flight Search Agent",
        capabilities=["flight_search", "booking"],
    )

    async with agent:
        # --- Step 1: Start searching ---
        print("[Agent] Starting flight search...")
        await agent.send_telemetry(
            state=AgentState.EXECUTING,
            task_description="Searching for flights: JFK → LAX, March 15",
            progress_pct=10,
        )
        await asyncio.sleep(2)

        # --- Step 2: Progress update ---
        print("[Agent] Checking airlines...")
        await agent.send_telemetry(
            state=AgentState.EXECUTING,
            task_description="Checking Delta, United, American...",
            progress_pct=40,
            resources={"tokens_used": 1200, "api_calls_made": 3, "estimated_cost_usd": 0.05},
        )
        await asyncio.sleep(2)

        # --- Step 3: Found results ---
        print("[Agent] Found flights! Sending notification...")
        await agent.send_telemetry(
            state=AgentState.EXECUTING,
            task_description="Found 3 matching flights",
            progress_pct=70,
            resources={"tokens_used": 2400, "api_calls_made": 6, "estimated_cost_usd": 0.12},
        )

        await agent.send_notification(
            title="Found 3 flights: JFK → LAX",
            body=(
                "1. **Delta DL-402** — $420, 5h 30m, nonstop\n"
                "2. **United UA-1891** — $385, 7h 15m, 1 stop\n"
                "3. **American AA-119** — $510, 5h 45m, nonstop"
            ),
            body_format="text/markdown",
            priority="medium",
            category="info",
        )
        await asyncio.sleep(1)

        # --- Step 4: Ask human to approve booking ---
        # Note: Per RAMP Rule 7, we MUST NOT include full card numbers.
        # Use masked identifiers only (e.g., "card ending in ••42").
        print("[Agent] Requesting approval to book Delta DL-402...")
        response = await agent.request_action(
            title="Book flight Delta DL-402?",
            body=(
                "**Delta DL-402** — JFK → LAX\n"
                "- Date: March 15, 2026\n"
                "- Price: $420.00\n"
                "- Duration: 5h 30m (nonstop)\n"
                "- Seat: 14A (window)\n\n"
                "Card ending in **••42** will be charged."
            ),
            body_format="text/markdown",
            options=[
                ActionOption(
                    action_id="book",
                    label="Book this flight",
                    description="Charge $420 to card ending ••42",
                    risk_level="medium",
                ),
                ActionOption(
                    action_id="book_cheapest",
                    label="Book cheapest instead",
                    description="Book United UA-1891 for $385",
                    risk_level="medium",
                ),
                ActionOption(
                    action_id="skip",
                    label="Don't book",
                    description="Cancel and keep searching",
                    risk_level="low",
                ),
            ],
            risk=RiskAssessment(
                level="medium",
                reversibility="irreversible",
                factors=["$420 charge to credit card", "Non-refundable ticket"],
                estimated_cost_usd=420.0,
                explanation="This will charge your credit card. The ticket is non-refundable after 24 hours.",
            ),
            timeout_seconds=120,
            fallback_action_id="skip",
        )

        # --- Step 5: Handle response ---
        print(f"[Agent] Human responded: {response.resolution} (action: {response.selected_action_id})")

        if response.resolution == "approved" and response.selected_action_id == "book":
            await agent.send_telemetry(
                state=AgentState.EXECUTING,
                task_description="Booking Delta DL-402...",
                progress_pct=90,
                resources={"tokens_used": 3000, "api_calls_made": 8, "estimated_cost_usd": 420.15},
            )
            await asyncio.sleep(2)
            await agent.send_notification(
                title="Flight booked! ✈️",
                body="Delta DL-402, JFK → LAX, March 15. Confirmation: DL-XK7291",
                priority="high",
                category="completion",
            )
            print("[Agent] Flight booked successfully!")

        elif response.resolution == "approved" and response.selected_action_id == "book_cheapest":
            await agent.send_telemetry(
                state=AgentState.EXECUTING,
                task_description="Booking United UA-1891...",
                progress_pct=90,
            )
            await asyncio.sleep(2)
            await agent.send_notification(
                title="Flight booked! ✈️",
                body="United UA-1891, JFK → LAX, March 15. Confirmation: UA-MN8834",
                priority="high",
                category="completion",
            )
            print("[Agent] Cheapest flight booked!")

        elif response.resolution == "timed_out":
            await agent.send_notification(
                title="Booking timed out",
                body="No response received within 2 minutes. Flight was not booked.",
                priority="medium",
                category="warning",
            )
            print("[Agent] Timed out, no booking made.")

        else:
            await agent.send_notification(
                title="Booking cancelled",
                body="You chose not to book. I'll keep searching if you need me.",
                priority="low",
                category="info",
            )
            print("[Agent] Booking cancelled by user.")

        # --- Done ---
        await agent.send_telemetry(
            state=AgentState.IDLE,
            task_description="Task complete",
            progress_pct=100,
        )
        print("[Agent] Done!")


if __name__ == "__main__":
    asyncio.run(main())
