#!/usr/bin/env python3
# /// script
# dependencies = [
#   "httpx>=0.27,<1",
# ]
# requires-python = ">=3.10"
# ///
"""RAMP Gateway client for Agent Skills.

A thin CLI that constructs signed RAMP envelopes and sends them to a gateway.
Designed for agentic use: structured JSON output, no interactive prompts,
meaningful error messages.

Usage:
    python3 scripts/ramp_client.py request-approval --title "..." --body "..." ...
    python3 scripts/ramp_client.py telemetry --state EXECUTING --task "..." ...
    python3 scripts/ramp_client.py notify --title "..." --body "..." ...
    python3 scripts/ramp_client.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

GATEWAY_URL = os.environ.get("RAMP_GATEWAY_URL", "http://localhost:8000")
API_KEY = os.environ.get("RAMP_API_KEY", "")
AGENT_ID = os.environ.get("RAMP_AGENT_ID", "")
SESSION_ID = os.environ.get("RAMP_SESSION_ID", f"sess_{uuid.uuid4().hex[:8]}")

# Track sequence numbers across calls within this process
_seq_counter = 0


def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter


# ---------------------------------------------------------------------------
# Signing (HMAC-SHA256 over canonical JSON, excluding signature field)
# ---------------------------------------------------------------------------

def _canonical_json(obj: dict) -> str:
    """RFC 8785-compatible canonical JSON (approximation via sorted keys)."""
    filtered = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"))


def _sign(envelope: dict, secret: str) -> str:
    """Produce HMAC-SHA256 signature for a RAMP envelope."""
    canonical = _canonical_json(envelope)
    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256={sig}"


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------

def _build_envelope(message_type: str, payload: dict) -> dict:
    """Build a complete signed RAMP envelope."""
    envelope = {
        "ramp_version": "0.2.0",
        "message_id": f"msg_{uuid.uuid4().hex[:12]}",
        "agent_id": AGENT_ID,
        "session_id": SESSION_ID,
        "principal_id": "user:principal",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "sequence_number": _next_seq(),
        "message_type": message_type,
        "nonce": uuid.uuid4().hex,
        "signature": "",
        "payload": payload,
    }
    envelope["signature"] = _sign(envelope, API_KEY)
    return envelope


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _send_message(envelope: dict) -> dict:
    """Send a signed envelope to the gateway."""
    url = f"{GATEWAY_URL}/ramp/v1/agents/{AGENT_ID}/messages"
    resp = httpx.post(url, json=envelope, headers={"X-RAMP-API-Key": API_KEY}, timeout=30)
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        print(json.dumps({"error": True, "status_code": resp.status_code, "detail": detail}))
        sys.exit(1)
    return resp.json()


def _poll_response(message_id: str, timeout: int) -> dict:
    """Long-poll for action response."""
    url = f"{GATEWAY_URL}/ramp/v1/agents/{AGENT_ID}/actions/{message_id}/response"
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait = min(30, int(deadline - time.time()))
        if wait <= 0:
            break
        resp = httpx.get(url, params={"wait": wait},
                         headers={"X-RAMP-API-Key": API_KEY}, timeout=wait + 10)
        data = resp.json()
        if data.get("status") == "resolved":
            return data["response"]
    return {"resolution_type": "timeout_fallback", "selected_action_id": None}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_request_approval(args: argparse.Namespace) -> None:
    """Request human approval for an action."""
    options = json.loads(args.options)
    payload = {
        "title": args.title,
        "body": args.body,
        "options": options,
        "timeout_seconds": args.timeout,
        "fallback_action_id": args.fallback,
        "risk_assessment": {
            "risk_level": args.risk_level,
            "reversibility": args.reversibility,
            "estimated_cost_usd": args.estimated_cost,
        },
    }
    envelope = _build_envelope("action_request", payload)
    result = _send_message(envelope)
    message_id = envelope["message_id"]

    # Block until resolved
    response = _poll_response(message_id, args.timeout)
    output = {
        "decision": response.get("selected_action_id"),
        "resolution_type": response.get("resolution_type"),
        "resolved_by": response.get("resolved_by"),
    }
    print(json.dumps(output))


def cmd_telemetry(args: argparse.Namespace) -> None:
    """Send telemetry update."""
    payload = {
        "state": args.state,
        "task_description": args.task,
    }
    if args.progress is not None:
        payload["progress_pct"] = args.progress
    envelope = _build_envelope("telemetry", payload)
    result = _send_message(envelope)
    print(json.dumps({"status": "accepted", "message_id": envelope["message_id"]}))


def cmd_notify(args: argparse.Namespace) -> None:
    """Send a notification."""
    payload = {
        "title": args.title,
        "body": args.body,
        "priority": args.priority,
        "category": args.category,
    }
    envelope = _build_envelope("notification", payload)
    result = _send_message(envelope)
    print(json.dumps({"status": "accepted", "message_id": envelope["message_id"]}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if not API_KEY:
        print(json.dumps({"error": True, "message": "RAMP_API_KEY environment variable is required"}))
        sys.exit(1)
    if not AGENT_ID:
        print(json.dumps({"error": True, "message": "RAMP_AGENT_ID environment variable is required"}))
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="RAMP Gateway client — request human approval, send telemetry, notify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 scripts/ramp_client.py request-approval \\
    --title "Deploy v2.3" --body "Deploy to prod" \\
    --options '[{"action_id":"go","label":"Deploy"},{"action_id":"no","label":"Cancel"}]' \\
    --risk-level high --fallback no

  python3 scripts/ramp_client.py telemetry --state EXECUTING --task "Analyzing data" --progress 50

  python3 scripts/ramp_client.py notify --title "Done" --body "Task complete" --category completion
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- request-approval --
    ap = sub.add_parser("request-approval", help="Request human approval for an action")
    ap.add_argument("--title", required=True, help="Short title for the request")
    ap.add_argument("--body", required=True, help="Detailed explanation")
    ap.add_argument("--options", required=True, help='JSON array of options, e.g. [{"action_id":"go","label":"Go"}]')
    ap.add_argument("--risk-level", required=True, choices=["low", "medium", "high", "critical"])
    ap.add_argument("--reversibility", default="reversible", choices=["reversible", "partially_reversible", "irreversible"])
    ap.add_argument("--estimated-cost", type=float, default=0.0, help="Estimated cost in USD")
    ap.add_argument("--fallback", required=True, help="action_id to use on timeout (safest option)")
    ap.add_argument("--timeout", type=int, default=300, help="Seconds to wait for human response (default: 300)")

    # -- telemetry --
    tp = sub.add_parser("telemetry", help="Send state/progress telemetry")
    tp.add_argument("--state", required=True, choices=["EXECUTING", "ERRORED"], help="Current agent state")
    tp.add_argument("--task", required=True, help="Human-readable task description")
    tp.add_argument("--progress", type=int, help="Completion percentage (0-100)")

    # -- notify --
    np_ = sub.add_parser("notify", help="Send a notification to the human")
    np_.add_argument("--title", required=True)
    np_.add_argument("--body", required=True)
    np_.add_argument("--priority", default="normal", choices=["low", "normal", "high", "critical"])
    np_.add_argument("--category", default="info", choices=["completion", "warning", "error", "info", "cost_alert", "security"])

    args = parser.parse_args()

    if args.command == "request-approval":
        cmd_request_approval(args)
    elif args.command == "telemetry":
        cmd_telemetry(args)
    elif args.command == "notify":
        cmd_notify(args)


if __name__ == "__main__":
    main()
