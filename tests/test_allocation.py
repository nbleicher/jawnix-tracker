from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from jawnix.allocation import allocate_request
from jawnix.models import Agency, Agent, BatchArtifact, DistributionEvent, Lead, RequestStatus

from conftest import make_request


def test_exact_allocation_and_csv(session, settings):
    agent = Agent(slug="alice", name="Alice")
    session.add(agent)
    session.flush()
    session.add_all(
        [
            Lead(phone="2155550001", title='Owner, "Acme"', state="PA"),
            Lead(phone="2155550002", title="Manager", state="PA"),
            Lead(phone="2125550003", title="Wrong state", state="NY"),
        ]
    )
    request = make_request(session, agent, 2, ["PA"])

    result = allocate_request(session, request.id, settings)
    session.commit()

    assert result.allocated == 2
    assert request.status == RequestStatus.generated.value
    artifact = session.scalar(select(BatchArtifact).where(BatchArtifact.request_id == request.id))
    assert artifact is not None and artifact.row_count == 2 and len(artifact.sha256) == 64
    with open(artifact.path, newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert list(rows[0]) == ["phone", "title"]
    assert len(rows) == 2
    assert len({row["phone"] for row in rows}) == 2
    assert {row["title"] for row in rows} == {'Owner, "Acme"', "Manager"}


def test_shortage_allocates_nothing(session, settings):
    agent = Agent(slug="short", name="Short")
    session.add(agent)
    session.flush()
    session.add(Lead(phone="2145550001", title="One", state="TX"))
    request = make_request(session, agent, 2)

    result = allocate_request(session, request.id, settings)
    session.commit()

    assert result.status == RequestStatus.waiting_inventory.value
    assert result.allocated == 0
    assert request.available_count == 1
    assert session.scalar(select(func.count(DistributionEvent.id))) == 0
    assert session.scalar(select(func.count(BatchArtifact.id))) == 0


def test_agency_history_is_permanent_and_global_cooldown_applies(session, settings):
    agency = Agency(slug="shared", name="Shared")
    first = Agent(slug="first", name="First", agency=agency)
    second = Agent(slug="second", name="Second", agency=agency)
    outsider = Agent(slug="outside", name="Outside")
    session.add_all([agency, first, second, outsider])
    session.flush()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    same_agency = Lead(phone="2145551001", title="Same agency", state="TX", last_distributed_at=old)
    cooling = Lead(phone="2145551002", title="Cooling", state="TX", last_distributed_at=recent)
    eligible = Lead(phone="2145551003", title="Eligible", state="TX", last_distributed_at=old)
    session.add_all([same_agency, cooling, eligible])
    session.flush()
    session.add_all(
        [
            DistributionEvent(lead_id=same_agency.id, agent_id=first.id, delivered_at=old, source="test"),
            DistributionEvent(lead_id=cooling.id, agent_id=outsider.id, delivered_at=recent, source="test"),
            DistributionEvent(lead_id=eligible.id, agent_id=outsider.id, delivered_at=old, source="test"),
        ]
    )
    request = make_request(session, second, 1)

    result = allocate_request(session, request.id, settings)
    session.commit()

    assert result.allocated == 1
    event = session.scalar(select(DistributionEvent).where(DistributionEvent.request_id == request.id))
    assert event.lead_id == eligible.id


def test_retry_reuses_existing_allocation(session, settings):
    agent = Agent(slug="reuse", name="Reuse")
    session.add(agent)
    session.flush()
    session.add(Lead(phone="2145552001", title="One", state="TX"))
    request = make_request(session, agent, 1)
    first = allocate_request(session, request.id, settings)
    session.flush()
    event_id = session.scalar(select(DistributionEvent.id).where(DistributionEvent.request_id == request.id))
    request.status = RequestStatus.approved.value
    second = allocate_request(session, request.id, settings)
    session.commit()

    assert first.allocated == second.allocated == 1
    assert session.scalar(select(func.count(DistributionEvent.id))) == 1
    assert session.scalar(select(DistributionEvent.id).where(DistributionEvent.request_id == request.id)) == event_id
