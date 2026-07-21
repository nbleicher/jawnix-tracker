from __future__ import annotations

import pytest
from sqlalchemy import func, select

from jawnix.allocation import allocate_request
from jawnix.delivery import deliver_request, mark_delivery_failed
from jawnix.models import Agent, BatchArtifact, DistributionEvent, Lead, RequestStatus

from conftest import make_request


class Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_delivery_retry_reuses_artifact_and_resend_idempotency(session, settings, monkeypatch):
    settings.resend_api_key = "test-key"
    agent = Agent(slug="delivery", name="Delivery")
    session.add(agent)
    session.flush()
    session.add(Lead(phone="2145559999", title="Lead", state="TX"))
    request = make_request(session, agent, 1)
    allocate_request(session, request.id, settings)
    session.commit()
    artifact = session.scalar(select(BatchArtifact).where(BatchArtifact.request_id == request.id))
    original_checksum = artifact.sha256
    captured: dict = {}

    def fail(*_args, **kwargs):
        captured.update(kwargs)
        return Response(503, text="temporary failure")

    monkeypatch.setattr("jawnix.delivery.httpx.post", fail)
    with pytest.raises(RuntimeError, match="503"):
        deliver_request(session, request.id, settings)
    session.rollback()
    mark_delivery_failed(session, request.id, "temporary failure")
    session.commit()
    assert request.status == RequestStatus.failed.value
    assert artifact.sha256 == original_checksum

    monkeypatch.setattr(
        "jawnix.delivery.httpx.post",
        lambda *_args, **kwargs: (captured.update(kwargs) or Response(200, {"id": "email-123"})),
    )
    message_id = deliver_request(session, request.id, settings)
    session.commit()

    assert message_id == "email-123"
    assert captured["headers"]["Idempotency-Key"] == f"jawnix-batch/{request.id}"
    assert request.status == RequestStatus.delivered.value
    assert artifact.sha256 == original_checksum
    assert session.scalar(select(func.count(DistributionEvent.id)).where(DistributionEvent.request_id == request.id)) == 1
