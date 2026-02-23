"""RAMP signing utilities — HMAC-SHA256 over canonical JSON (RFC 8785)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def _canonical_json(obj: Any) -> bytes:
    """Produce canonical JSON per RFC 8785 (JCS).

    Python's json.dumps with sort_keys=True and ensure_ascii=False is a
    conforming subset for the data types RAMP uses (no lone surrogates,
    no BigInt).  Full JCS libraries (e.g. `canonicaljson`) can be swapped
    in for stricter compliance.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_envelope(envelope_dict: dict[str, Any], secret: str) -> str:
    """Compute HMAC-SHA256 signature for a RAMP envelope.

    The signature is computed over the canonical JSON of the envelope
    with the ``signature`` field set to the empty string (Section 4.8.3).
    """
    # Ensure signature field is empty for signing
    signable = {**envelope_dict, "signature": ""}
    canonical = _canonical_json(signable)
    return hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(envelope_dict: dict[str, Any], secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on a RAMP envelope."""
    received_sig = envelope_dict.get("signature", "")
    expected_sig = sign_envelope(envelope_dict, secret)
    return hmac.compare_digest(received_sig, expected_sig)
