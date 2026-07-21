from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select

from .allocation import allocate_request
from .config import get_settings
from .database import SessionLocal
from .delivery import deliver_request, mark_delivery_failed
from .jobs import claim_next_job, enqueue_job
from .models import Job, JobStatus, LeadRequest, RequestStatus, SlackNotification
from .slack import SlackClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jawnix.worker")


def _update_slack(session, request: LeadRequest, slack: SlackClient) -> None:
    notification = session.scalar(select(SlackNotification).where(SlackNotification.request_id == request.id))
    if notification is None:
        channel, message_ts = slack.post_request(request)
        session.add(SlackNotification(request_id=request.id, channel_id=channel, message_ts=message_ts))
    else:
        slack.update_request(request, notification.channel_id, notification.message_ts)


def process_job(job_id: int) -> None:
    settings = get_settings()
    try:
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            request = session.get(LeadRequest, job.request_id) if job.request_id else None
            if job.kind == "notify_request" or job.kind == "update_slack":
                if request is None:
                    raise LookupError("Request was not found.")
                _update_slack(session, request, SlackClient(settings))
            elif job.kind == "allocate_request":
                allocate_request(session, uuid.UUID(str(job.request_id)), settings)
            elif job.kind == "deliver_request":
                deliver_request(session, uuid.UUID(str(job.request_id)), settings)
            else:
                raise ValueError(f"Unknown job kind: {job.kind}")
            job.status = JobStatus.complete.value
            job.last_error = ""
    except Exception as exc:
        # The processing transaction has rolled back here, so allocation and
        # artifact state can never be partially committed. Record the failure
        # in a new transaction and preserve any previously generated artifact.
        log.exception("Job %s failed", job_id)
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = JobStatus.failed.value
            job.last_error = str(exc)[:4000]
            request = session.get(LeadRequest, job.request_id) if job.request_id else None
            if request is not None and job.kind in {"allocate_request", "deliver_request"}:
                request.status = RequestStatus.failed.value
                request.status_message = (
                    "Batch generation failed; no partial allocation was committed."
                    if job.kind == "allocate_request"
                    else "Email delivery failed. The existing batch is preserved for retry."
                )
                if job.kind == "deliver_request":
                    mark_delivery_failed(session, request.id, str(exc))
                else:
                    enqueue_job(session, "update_slack", request.id)


def run() -> None:
    settings = get_settings()
    log.info("Worker %s started", settings.worker_id)
    while True:
        job_id = None
        with SessionLocal.begin() as session:
            job = claim_next_job(session, settings.worker_id, settings.job_lock_timeout_seconds)
            if job:
                job_id = job.id
        if job_id is None:
            time.sleep(settings.worker_poll_seconds)
            continue
        process_job(job_id)


if __name__ == "__main__":
    run()
