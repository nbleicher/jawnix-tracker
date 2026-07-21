#!/usr/bin/env python3
"""Prove two simultaneous approvals cannot allocate the same phone."""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jawnix.allocation import allocate_request
from jawnix.config import Settings
from jawnix.models import Agent, CustomerProfile, DistributionEvent, Lead, LeadRequest, RequestStatus, utcnow


database_url = os.environ["DATABASE_URL"]
engine = create_engine(database_url, pool_pre_ping=True)
sessions = sessionmaker(bind=engine, expire_on_commit=False)
run = uuid.uuid4().hex[:10]
settings = Settings(JAWNIX_BATCH_DIR=Path("/tmp") / f"jawnix-concurrency-{run}")
request_ids: list[uuid.UUID] = []

with sessions.begin() as session:
    for suffix in ("a", "b"):
        agent = Agent(slug=f"concurrency-{run}-{suffix}", name=f"Concurrency {suffix}")
        user_id = uuid.uuid4()
        profile = CustomerProfile(
            user_id=user_id,
            email=f"concurrency-{run}-{suffix}@example.invalid",
            licensed_states=["DC"],
            agent=agent,
            mapping_confirmed_at=utcnow(),
        )
        request = LeadRequest(
            user_id=user_id,
            agent=agent,
            lead_count=50,
            state_mode="all_saved",
            states_snapshot=["DC"],
            delivery_email=profile.email,
            status=RequestStatus.approved.value,
        )
        session.add_all([profile, request])
        session.flush()
        request_ids.append(request.id)
    session.add_all(
        Lead(phone=f"202{number:07d}", title=f"Synthetic {number}", state="DC")
        for number in range(100)
    )

barrier = threading.Barrier(2)
errors: list[BaseException] = []


def allocate(request_id: uuid.UUID) -> None:
    try:
        with sessions.begin() as session:
            barrier.wait(timeout=10)
            result = allocate_request(session, request_id, settings)
            if result.allocated != 50:
                raise AssertionError(f"expected 50 rows, got {result}")
    except BaseException as exc:
        errors.append(exc)


threads = [threading.Thread(target=allocate, args=(request_id,)) for request_id in request_ids]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=60)
if errors:
    raise errors[0]
if any(thread.is_alive() for thread in threads):
    raise TimeoutError("concurrent allocation did not finish")

with sessions() as session:
    events = list(
        session.execute(
            select(DistributionEvent.request_id, DistributionEvent.lead_id).where(
                DistributionEvent.request_id.in_(request_ids)
            )
        )
    )
    distinct = session.scalar(
        select(func.count(func.distinct(DistributionEvent.lead_id))).where(
            DistributionEvent.request_id.in_(request_ids)
        )
    )
if len(events) != 100 or distinct != 100:
    raise AssertionError(f"expected 100 unique allocations, got {len(events)} events / {distinct} unique")
print(f"PASS: {len(events)} events, {distinct} unique phones across two concurrent requests")
