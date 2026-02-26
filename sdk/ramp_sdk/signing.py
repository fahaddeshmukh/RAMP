"""RAMP signing utilities — HMAC-SHA256 over canonical JSON (RFC 8785)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def _canonical_json(obj: Any) -> bytes:
    """Produce canonical JSON per RFC 8785 (JCS).

    Python's json.dumps with sort_keys=True and ensure_ascii=False covers
    the data types RAMP uses (no lone surrogates, no special float values).
    For stricter RFC 8785 compliance (e.g. non-ASCII Unicode escaping rules,
    BigInt serialization) swap in a dedicated JCS library such as
    ``canonicaljson``.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_envelope(envelope_dict: dict[str, Any], secret: str) -> str:
    """Compute HMAC-SHA256 signature for a RAMP envelope.

    Per spec Section 4.8.3, the ``signature`` field MUST be removed entirely
    from the envelope before canonicalization — not set to null, not set to
    empty string, but fully absent from the JSON object.
    """
    # Remove the signature field entirely before canonicalization (spec §4.8.3)
    signable = {k: v for k, v in envelope_dict.items() if k != "signature"}
    canonical = _canonical_json(signable)
    # Spec §4.8.3 step 6: SIG = "hmac-sha256:" + hexencode(S)
    return "hmac-sha256:" + hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(envelope_dict: dict[str, Any], secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on a RAMP envelope."""
    received_sig = envelope_dict.get("signature", "")
    # sign_envelope removes the signature field before computing, so passing
    # the full envelope (with the received signature present) is correct.
    expected_sig = sign_envelope(envelope_dict, secret)
    return hmac.compare_digest(received_sig, expected_sig)
