#!/usr/bin/env python3
"""Disposable-staging 10M inventory / 100k allocation performance gate."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from jawnix.allocation import allocate_request
from jawnix.config import Settings
from jawnix.models import Agent, CustomerProfile, LeadRequest, RequestStatus, utcnow


parser = argparse.ArgumentParser()
parser.add_argument("--inventory-rows", type=int, default=10_000_000)
parser.add_argument("--request-rows", type=int, default=100_000)
parser.add_argument("--max-seconds", type=float, default=300)
parser.add_argument("--skip-populate", action="store_true")
args = parser.parse_args()
if os.environ.get("JAWNIX_ALLOW_SYNTHETIC_LOAD_TEST") != "YES":
    raise SystemExit("Refusing to load synthetic data; set JAWNIX_ALLOW_SYNTHETIC_LOAD_TEST=YES on disposable staging.")
if args.inventory_rows < args.request_rows:
    raise SystemExit("inventory rows must be at least request rows")

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
sessions = sessionmaker(bind=engine, expire_on_commit=False)
run = uuid.uuid4().hex[:12]
settings = Settings(JAWNIX_BATCH_DIR=Path("/tmp") / f"jawnix-load-{run}")

with sessions.begin() as session:
    if args.skip_populate:
        available = session.scalar(text("SELECT count(*) FROM lead_inventory WHERE state = 'HI'"))
        if available < args.inventory_rows:
            raise SystemExit(f"expected at least {args.inventory_rows} existing HI rows, found {available}")
    else:
        session.execute(
            text(
                """
                INSERT INTO lead_inventory
                    (phone, title, state, first_seen_at, source_flow)
                SELECT lpad((9000000000 + number)::text, 10, '0'),
                       'Synthetic ' || number,
                       'HI', now(), :flow
                  FROM generate_series(1, :rows) AS number
                ON CONFLICT (phone) DO NOTHING
                """
            ),
            {"rows": args.inventory_rows, "flow": f"load-test:{run}"},
        )
    agent = Agent(slug=f"load-test-{run}", name="Load Test")
    user_id = uuid.uuid4()
    profile = CustomerProfile(
        user_id=user_id,
        email=f"load-test-{run}@example.invalid",
        licensed_states=["HI"],
        agent=agent,
        mapping_confirmed_at=utcnow(),
    )
    request = LeadRequest(
        user_id=user_id,
        agent=agent,
        lead_count=args.request_rows,
        state_mode="all_saved",
        states_snapshot=["HI"],
        delivery_email=profile.email,
        status=RequestStatus.approved.value,
    )
    session.add_all([profile, request])
    session.flush()
    request_id = request.id

health_url = os.environ.get("JAWNIX_LOAD_TEST_HEALTH_URL", "")
stop_health = threading.Event()
health_errors: list[str] = []


def poll_health() -> None:
    while not stop_health.wait(0.25):
        try:
            response = httpx.get(health_url, timeout=2)
            response.raise_for_status()
        except Exception as exc:
            health_errors.append(str(exc))


health_thread = threading.Thread(target=poll_health) if health_url else None
if health_thread:
    health_thread.start()
started = time.perf_counter()
with sessions.begin() as session:
    result = allocate_request(session, request_id, settings)
elapsed = time.perf_counter() - started
stop_health.set()
if health_thread:
    health_thread.join(timeout=5)

if result.allocated != args.request_rows:
    raise AssertionError(f"expected {args.request_rows} rows, got {result}")
if elapsed > args.max_seconds:
    raise AssertionError(f"allocation and CSV took {elapsed:.2f}s; limit is {args.max_seconds:.2f}s")
if health_errors:
    raise AssertionError(f"API health failed during allocation: {health_errors[:3]}")

report = {
    "inventoryRows": args.inventory_rows,
    "requestRows": args.request_rows,
    "elapsedSeconds": round(elapsed, 3),
    "limitSeconds": args.max_seconds,
    "apiHealthChecks": "passed" if health_url else "not configured",
    "requestId": str(request_id),
}
print(json.dumps(report, indent=2))
