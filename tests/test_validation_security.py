from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import ValidationError

from jawnix.schemas import RequestCreate
from jawnix.slack import verify_slack_request
from jawnix.states import derive_state, normalize_phone, normalize_states


def test_normalization_and_request_limit():
    assert normalize_phone("+1 (215) 555-1212") == "2155551212"
    assert normalize_phone("123") is None
    assert derive_state("4155551212") == "CA"
    assert normalize_states(["tx", "FL", "TX"]) == ["FL", "TX"]
    with pytest.raises(ValueError, match="Unsupported state"):
        normalize_states(["IO"])
    with pytest.raises(ValidationError):
        RequestCreate(lead_count=100_001, state_mode="all_saved")
    with pytest.raises(ValidationError):
        RequestCreate(lead_count=1, state_mode="selected", states=[])


def test_slack_signature_timestamp_and_tampering():
    body = b"payload=%7B%22ok%22%3Atrue%7D"
    timestamp = "1700000000"
    secret = "signing-secret"
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_request(body, timestamp, signature, secret, now=1700000000)
    assert not verify_slack_request(body + b"x", timestamp, signature, secret, now=1700000000)
    assert not verify_slack_request(body, timestamp, signature, secret, now=1700000301)
