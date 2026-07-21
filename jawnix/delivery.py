from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .jobs import enqueue_job
from .models import BatchArtifact, LeadRequest, RequestStatus


def mark_delivery_failed(session: Session, request_id: uuid.UUID, error: str) -> None:
    request = session.get(LeadRequest, request_id)
    artifact = session.scalar(select(BatchArtifact).where(BatchArtifact.request_id == request_id))
    if request is not None:
        request.status = RequestStatus.failed.value
        request.status_message = "Email delivery failed. The existing batch is preserved for retry."
        enqueue_job(session, "update_slack", request.id)
    if artifact is not None:
        artifact.delivery_status = "failed"
        artifact.last_error = error[:2000]


def deliver_request(session: Session, request_id: uuid.UUID, settings: Settings) -> str:
    request = session.scalar(select(LeadRequest).where(LeadRequest.id == request_id).with_for_update())
    artifact = session.scalar(select(BatchArtifact).where(BatchArtifact.request_id == request_id).with_for_update())
    if request is None or artifact is None:
        raise LookupError("Request artifact was not found.")
    if artifact.delivery_status == "sent" and request.status == RequestStatus.delivered.value:
        return artifact.resend_message_id
    path = Path(artifact.path)
    if not path.is_file():
        raise FileNotFoundError(f"Batch artifact is missing: {path}")
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured.")

    artifact.delivery_attempts += 1
    payload = {
        "from": settings.batch_from_email,
        "to": [request.delivery_email],
        "subject": f"Your Jawnix batch — {artifact.row_count:,} rows",
        "text": (
            f"Your requested Jawnix batch is attached.\n\n"
            f"Rows: {artifact.row_count:,}\n"
            f"States: {', '.join(request.states_snapshot)}\n"
            f"Request: {request.id}\n"
        ),
        "attachments": [{"filename": artifact.filename, "content": base64.b64encode(path.read_bytes()).decode("ascii")}],
    }
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": f"jawnix-batch/{request.id}",
        },
        json=payload,
        timeout=60,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Resend failed with HTTP {response.status_code}")
    message_id = str(response.json().get("id") or "")
    artifact.delivery_status = "sent"
    artifact.resend_message_id = message_id
    artifact.last_error = ""
    artifact.sent_at = datetime.now(timezone.utc)
    request.status = RequestStatus.delivered.value
    request.delivered_at = artifact.sent_at
    request.status_message = f"CSV emailed to {request.delivery_email}."
    enqueue_job(session, "update_slack", request.id)
    session.flush()
    return message_id
