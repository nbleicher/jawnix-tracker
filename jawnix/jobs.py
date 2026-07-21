from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import Job, JobStatus


def enqueue_job(session: Session, kind: str, request_id: uuid.UUID | None = None, payload: dict | None = None) -> Job:
    job = Job(kind=kind, request_id=request_id, payload=payload or {}, status=JobStatus.queued.value)
    session.add(job)
    session.flush()
    return job


def claim_next_job(session: Session, worker_id: str, lock_timeout_seconds: int = 900) -> Job | None:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=lock_timeout_seconds)
    job = session.scalar(
        select(Job)
        .where(
            or_(
                and_(Job.status == JobStatus.queued.value, Job.run_after <= now),
                and_(Job.status == JobStatus.running.value, Job.locked_at <= stale_before),
            )
        )
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not job:
        return None
    job.status = JobStatus.running.value
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    session.flush()
    return job
