#!/usr/bin/env python3
"""Shared canonical JSON + HMAC-SHA256 signing for risk_state (single source of truth).

Imported by BOTH heartbeat.py and risk_state.py so the two never encode a
different canonical form.  If they ever did, the mismatch would surface as a
RISK_STATE_BAD_SIGNATURE (tampering suspicion) instead of silent divergence.
"""
import hashlib
import hmac
import json


def _canonical(obj: dict) -> str:
    """Deterministic JSON: keys sorted, no spaces, no newline, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(secret: str, obj: dict) -> str:
    """HMAC-SHA256 hex over canonical_form(obj).  obj must NOT contain a `sig` key."""
    msg = _canonical(obj).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(secret: str, obj: dict, sig: str) -> bool:
    """Constant-time check that sig == sign(secret, obj)."""
    expected = sign(secret, obj)
    return hmac.compare_digest(expected, sig)