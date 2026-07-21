from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jawnix.api import app
from jawnix.auth import Principal, require_principal
from jawnix.database import get_db
from jawnix.models import Agent, CustomerProfile, Job, LeadRequest, utcnow


def test_request_mapping_state_validation_cancel_and_billing_404(session):
    user_id = uuid.uuid4()
    agent = Agent(slug="api-agent", name="API Agent")
    profile = CustomerProfile(
        user_id=user_id,
        email="customer@example.com",
        licensed_states=["TX", "FL"],
    )
    session.add_all([agent, profile])
    session.commit()

    def database_override():
        yield session

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[require_principal] = lambda: Principal(
        user_id=user_id,
        email=profile.email,
        role="customer",
        csrf="test",
    )
    try:
        client = TestClient(app)
        assert client.post("/api/generate-invoice", json={}).status_code == 404
        assert client.post(
            "/api/me/requests",
            json={"lead_count": 10, "state_mode": "all_saved", "states": []},
        ).status_code == 409

        profile.agent_id = agent.id
        profile.mapping_confirmed_at = utcnow()
        session.commit()
        outside = client.post(
            "/api/me/requests",
            json={"lead_count": 10, "state_mode": "selected", "states": ["PA"]},
        )
        assert outside.status_code == 422

        created = client.post(
            "/api/me/requests",
            json={"lead_count": 10, "state_mode": "selected", "states": ["TX"]},
        )
        assert created.status_code == 201
        request_id = created.json()["id"]
        assert created.json()["states_snapshot"] == ["TX"]
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "notify_request")) == 1
        assert client.delete(f"/api/me/requests/{request_id}").status_code == 200
        assert session.get(LeadRequest, uuid.UUID(request_id)).status == "canceled"
        assert client.delete(f"/api/me/requests/{request_id}").status_code == 409
    finally:
        app.dependency_overrides.clear()
