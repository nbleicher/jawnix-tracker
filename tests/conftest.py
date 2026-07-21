from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jawnix.config import Settings
from jawnix.database import Base
from jawnix.models import Agent, CustomerProfile, LeadRequest, RequestStatus


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as value:
        yield value
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        JAWNIX_BATCH_DIR=tmp_path / "batches",
        JAWNIX_COOKIE_SECURE=False,
        JAWNIX_SESSION_SECRET="test-secret-at-least-long-enough",
    )


def make_request(session, agent: Agent, count: int, states: list[str] | None = None) -> LeadRequest:
    user_id = uuid.uuid4()
    profile = CustomerProfile(
        user_id=user_id,
        email=f"{user_id}@example.com",
        licensed_states=states or ["TX"],
        agent=agent,
    )
    request = LeadRequest(
        user_id=user_id,
        agent=agent,
        lead_count=count,
        states_snapshot=states or ["TX"],
        state_mode="all_saved",
        delivery_email=profile.email,
        status=RequestStatus.approved.value,
    )
    session.add_all([profile, request])
    session.flush()
    return request
