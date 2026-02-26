"""Smoke tests for ramp_sdk signing module."""

from ramp_sdk.signing import sign_envelope, verify_signature


def _make_envelope() -> dict:
    return {
        "ramp_version": "0.2.0",
        "message_id": "01936d87-7e1a-7f3b-a8c2-4d5e6f7a8b9c",
        "message_type": "telemetry",
        "session_id": "sess_test",
        "agent_id": "agent:test",
        "principal_id": "user:test",
        "sequence_number": 1,
        "timestamp": "2026-02-23T00:00:00.000Z",
        "nonce": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "signature": "",
        "payload": {"state": "EXECUTING", "task_description": "testing"},
    }


def test_sign_and_verify_roundtrip():
    """Signing an envelope and verifying with the same key succeeds."""
    env = _make_envelope()
    secret = "test-secret-key"
    env["signature"] = sign_envelope(env, secret)
    assert env["signature"] != ""
    assert verify_signature(env, secret)


def test_wrong_key_rejects():
    """Verification with a different key fails."""
    env = _make_envelope()
    env["signature"] = sign_envelope(env, "correct-key")
    assert not verify_signature(env, "wrong-key")


def test_tampered_payload_rejects():
    """Modifying the payload after signing invalidates the signature."""
    env = _make_envelope()
    env["signature"] = sign_envelope(env, "my-key")
    env["payload"]["state"] = "IDLE"  # tamper
    assert not verify_signature(env, "my-key")


def test_signature_is_deterministic():
    """Same envelope + same key always produces the same signature."""
    env1 = _make_envelope()
    env2 = _make_envelope()
    secret = "deterministic"
    assert sign_envelope(env1, secret) == sign_envelope(env2, secret)


def test_signature_field_removed_before_signing():
    """The signature field is removed entirely before canonicalization (spec §4.8.3).
    Its value before signing does not matter — any prior value is stripped."""
    env = _make_envelope()
    secret = "test"
    env["signature"] = "garbage_value_that_should_be_ignored"
    sig = sign_envelope(env, secret)

    env["signature"] = ""
    sig2 = sign_envelope(env, secret)
    assert sig == sig2


def test_signature_has_hmac_sha256_prefix():
    """Spec §4.8.3 step 6: signature MUST start with 'hmac-sha256:' followed by 64 hex chars."""
    env = _make_envelope()
    sig = sign_envelope(env, "any-key")
    assert sig.startswith("hmac-sha256:"), f"Expected prefix, got: {sig[:20]}"
    assert len(sig) == len("hmac-sha256:") + 64  # 64 lowercase hex chars for SHA-256


def test_canonical_json_excludes_signature_field():
    """Verify the canonical form used for signing does NOT contain the signature key."""
    import json
    from ramp_sdk.signing import _canonical_json

    env = _make_envelope()
    env["signature"] = "some_value"
    # Build signable dict the same way sign_envelope does
    signable = {k: v for k, v in env.items() if k != "signature"}
    canonical = _canonical_json(signable).decode("utf-8")
    parsed = json.loads(canonical)
    assert "signature" not in parsed
