from __future__ import annotations

import csv
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, func, nullsfirst, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .jobs import enqueue_job
from .models import Agent, BatchArtifact, DistributionEvent, Lead, LeadRequest, RequestStatus
from .states import truncate_utf8


@dataclass(frozen=True)
class AllocationResult:
    status: str
    allocated: int
    available: int
    artifact_id: int | None = None


def _same_recipient_clause(request: LeadRequest):
    if request.agent.agency_id is None:
        return DistributionEvent.agent_id == request.agent_id
    same_agency_agents = select(Agent.id).where(Agent.agency_id == request.agent.agency_id)
    return DistributionEvent.agent_id.in_(same_agency_agents)


def eligible_query(request: LeadRequest, settings: Settings):
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.global_cooldown_days)
    previously_sent = exists(
        select(DistributionEvent.id).where(
            DistributionEvent.lead_id == Lead.id,
            _same_recipient_clause(request),
        )
    )
    return (
        select(Lead)
        .where(
            Lead.state.in_(request.states_snapshot),
            or_(Lead.last_distributed_at.is_(None), Lead.last_distributed_at <= cutoff),
            ~previously_sent,
        )
        .order_by(nullsfirst(Lead.last_distributed_at), Lead.id)
    )


def inventory_count(session: Session, request: LeadRequest, settings: Settings) -> int:
    eligible_ids = eligible_query(request, settings).with_only_columns(Lead.id).order_by(None).subquery()
    return int(session.scalar(select(func.count()).select_from(eligible_ids)) or 0)


def _artifact_path(settings: Settings, request: LeadRequest) -> tuple[Path, str]:
    date_text = datetime.now(timezone.utc).date().isoformat()
    filename = f"{request.agent.slug}_batch_{request.id}_{date_text}.csv"
    directory = Path(settings.batch_dir) / date_text
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename, filename


def generate_artifact(session: Session, request: LeadRequest, leads: list[Lead], settings: Settings) -> BatchArtifact:
    final_path, filename = _artifact_path(settings, request)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{request.id}-", suffix=".csv", dir=final_path.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["phone", "title"], lineterminator="\n")
            writer.writeheader()
            for lead in leads:
                writer.writerow({"phone": lead.phone, "title": truncate_utf8(lead.title)})
        os.replace(temp_path, final_path)
    finally:
        temp_path.unlink(missing_ok=True)
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    artifact = session.scalar(select(BatchArtifact).where(BatchArtifact.request_id == request.id))
    if artifact is None:
        artifact = BatchArtifact(
            request_id=request.id,
            path=str(final_path),
            filename=filename,
            row_count=len(leads),
            byte_count=final_path.stat().st_size,
            sha256=digest,
        )
        session.add(artifact)
    else:
        artifact.path = str(final_path)
        artifact.filename = filename
        artifact.row_count = len(leads)
        artifact.byte_count = final_path.stat().st_size
        artifact.sha256 = digest
    session.flush()
    return artifact


def allocate_request(session: Session, request_id: uuid.UUID, settings: Settings) -> AllocationResult:
    request = session.scalar(
        select(LeadRequest).where(LeadRequest.id == request_id).with_for_update()
    )
    if request is None:
        raise LookupError(f"Request {request_id} was not found.")

    existing_events = list(
        session.scalars(
            select(DistributionEvent).where(DistributionEvent.request_id == request.id).order_by(DistributionEvent.id)
        )
    )
    if existing_events:
        if len(existing_events) != request.lead_count:
            raise RuntimeError("Existing allocation has an unexpected row count.")
        leads_by_id = {
            lead.id: lead
            for lead in session.scalars(select(Lead).where(Lead.id.in_([event.lead_id for event in existing_events])))
        }
        leads = [leads_by_id[event.lead_id] for event in existing_events]
        artifact = generate_artifact(session, request, leads, settings)
        request.status = RequestStatus.generated.value
        request.status_message = "Batch generated; email delivery is queued."
        enqueue_job(session, "deliver_request", request.id)
        enqueue_job(session, "update_slack", request.id)
        return AllocationResult(request.status, len(leads), len(leads), artifact.id)

    if request.status not in {RequestStatus.approved.value, RequestStatus.processing.value}:
        return AllocationResult(request.status, 0, request.available_count or 0)

    request.status = RequestStatus.processing.value
    request.status_message = "Selecting eligible inventory."
    session.flush()

    candidates = list(
        session.scalars(
            eligible_query(request, settings)
            .limit(request.lead_count)
            .with_for_update(skip_locked=True)
        )
    )
    if len(candidates) < request.lead_count:
        available = inventory_count(session, request, settings)
        request.status = RequestStatus.waiting_inventory.value
        request.available_count = available
        request.status_message = f"Inventory shortage: requested {request.lead_count:,}; available {available:,}. No rows were allocated."
        enqueue_job(session, "update_slack", request.id)
        return AllocationResult(request.status, 0, available)

    distributed_at = datetime.now(timezone.utc)
    for lead in candidates:
        lead.last_distributed_at = distributed_at
        session.add(
            DistributionEvent(
                lead_id=lead.id,
                agent_id=request.agent_id,
                request_id=request.id,
                delivered_at=distributed_at,
                source="request",
            )
        )
    artifact = generate_artifact(session, request, candidates, settings)
    request.status = RequestStatus.generated.value
    request.available_count = len(candidates)
    request.processed_at = distributed_at
    request.status_message = "Batch generated; email delivery is queued."
    enqueue_job(session, "deliver_request", request.id)
    enqueue_job(session, "update_slack", request.id)
    session.flush()
    return AllocationResult(request.status, len(candidates), len(candidates), artifact.id)
