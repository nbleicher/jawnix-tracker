from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from svix.webhooks import Webhook, WebhookVerificationError

from .auth import Principal, clear_session, issue_session, require_admin, require_principal, verify_supabase_token
from .config import Settings, get_settings
from .database import get_db
from .jobs import enqueue_job
from .models import Agent, BatchArtifact, CustomerProfile, LeadRequest, RequestStatus, WebhookReceipt, utcnow
from .schemas import CustomerCreate, ProfileOut, ProfileUpdate, RecipientMappingUpdate, RequestCreate, RequestOut, SessionExchange
from .slack import verify_slack_request
from .states import normalize_states


app = FastAPI(title="Jawnix VPS API", version="1.0.0")


@app.get("/api/healthz")
def healthz(settings: Settings = Depends(get_settings)):
    return {"ok": True, "billingEnabled": settings.billing_enabled}


@app.get("/api/readyz")
def readyz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}


@app.post("/api/auth/session")
async def create_session(
    payload: SessionExchange,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = await verify_supabase_token(payload.access_token, settings)
    principal = issue_session(response, user, settings)
    profile = db.get(CustomerProfile, principal.user_id)
    if profile is None:
        metadata = user.get("user_metadata") or {}
        profile = CustomerProfile(
            user_id=principal.user_id,
            email=principal.email,
            first_name=str(metadata.get("first_name") or ""),
            last_name=str(metadata.get("last_name") or ""),
            licensed_states=[],
        )
        db.add(profile)
    else:
        profile.email = principal.email
    db.commit()
    return {"ok": True, "role": principal.role, "next": "/admin.html" if principal.role == "admin" else "/portal.html"}


@app.post("/api/auth/logout")
def logout(
    response: Response,
    _: Principal = Depends(require_principal),
    settings: Settings = Depends(get_settings),
):
    clear_session(response, settings)
    return {"ok": True}


@app.get("/api/me/profile", response_model=ProfileOut)
def get_profile(principal: Principal = Depends(require_principal), db: Session = Depends(get_db)):
    profile = db.get(CustomerProfile, principal.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile was not found.")
    return profile


@app.patch("/api/me/profile", response_model=ProfileOut)
def update_profile(
    payload: ProfileUpdate,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    profile = db.get(CustomerProfile, principal.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile was not found.")
    profile.first_name = payload.first_name.strip()
    profile.last_name = payload.last_name.strip()
    profile.phone = payload.phone.strip()
    profile.licensed_states = payload.licensed_states
    profile.updated_at = utcnow()
    db.commit()
    db.refresh(profile)
    return profile


@app.get("/api/me/requests", response_model=list[RequestOut])
def list_my_requests(principal: Principal = Depends(require_principal), db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(LeadRequest).where(LeadRequest.user_id == principal.user_id).order_by(LeadRequest.created_at.desc())
        )
    )


@app.post("/api/me/requests", response_model=RequestOut, status_code=201)
def create_request(
    payload: RequestCreate,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    profile = db.get(CustomerProfile, principal.user_id)
    if profile is None or profile.agent_id is None or profile.mapping_confirmed_at is None:
        raise HTTPException(status_code=409, detail="Your distribution mapping must be confirmed before requesting a batch.")
    saved_states = normalize_states(profile.licensed_states)
    if not saved_states:
        raise HTTPException(status_code=422, detail="Save at least one licensed state first.")
    states = saved_states if payload.state_mode == "all_saved" else payload.states
    if not set(states).issubset(saved_states):
        raise HTTPException(status_code=422, detail="Request states must be selected from your saved profile states.")
    request = LeadRequest(
        user_id=principal.user_id,
        agent_id=profile.agent_id,
        lead_count=payload.lead_count,
        state_mode=payload.state_mode,
        states_snapshot=states,
        delivery_email=profile.email,
        status=RequestStatus.pending.value,
        status_message="Awaiting Slack approval.",
    )
    db.add(request)
    db.flush()
    enqueue_job(db, "notify_request", request.id)
    db.commit()
    db.refresh(request)
    return request


@app.delete("/api/me/requests/{request_id}")
def cancel_request(
    request_id: uuid.UUID,
    principal: Principal = Depends(require_principal),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(LeadRequest)
        .where(LeadRequest.id == request_id, LeadRequest.user_id == principal.user_id)
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Request was not found.")
    if item.status != RequestStatus.pending.value:
        raise HTTPException(status_code=409, detail="Only pending requests can be canceled.")
    item.status = RequestStatus.canceled.value
    item.status_message = "Canceled by customer."
    enqueue_job(db, "update_slack", item.id)
    db.commit()
    return {"ok": True}


def _request_dict(item: LeadRequest) -> dict:
    return {
        "id": str(item.id),
        "userId": str(item.user_id),
        "customer": " ".join(part for part in (item.profile.first_name, item.profile.last_name) if part).strip() or item.profile.email,
        "email": item.delivery_email,
        "agent": item.agent.name,
        "leadCount": item.lead_count,
        "states": item.states_snapshot,
        "status": item.status,
        "availableCount": item.available_count,
        "statusMessage": item.status_message,
        "createdAt": item.created_at,
        "deliveredAt": item.delivered_at,
        "hasArtifact": item.artifact is not None,
    }


@app.get("/api/admin/requests")
def admin_requests(_: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    return [_request_dict(item) for item in db.scalars(select(LeadRequest).order_by(LeadRequest.created_at.desc()))]


@app.get("/api/admin/recipients")
def admin_recipients(_: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    profiles = list(db.scalars(select(CustomerProfile).order_by(CustomerProfile.email)))
    agents = list(db.scalars(select(Agent).where(Agent.active.is_(True)).order_by(Agent.name)))
    return {
        "recipients": [
            {
                "userId": str(profile.user_id),
                "email": profile.email,
                "name": " ".join(part for part in (profile.first_name, profile.last_name) if part).strip(),
                "states": profile.licensed_states,
                "agentId": profile.agent_id,
                "agent": profile.agent.name if profile.agent else "",
                "confirmed": profile.mapping_confirmed_at is not None,
            }
            for profile in profiles
        ],
        "agents": [
            {"id": agent.id, "slug": agent.slug, "name": agent.name, "agency": agent.agency.name if agent.agency else ""}
            for agent in agents
        ],
    }


@app.post("/api/admin/recipients/sync")
async def sync_recipients(
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    agents = {agent.slug: agent for agent in db.scalars(select(Agent).where(Agent.active.is_(True)))}
    seen = created = proposed = 0
    page = 1
    while True:
        data = await _supabase_admin(settings, "GET", f"/auth/v1/admin/users?page={page}&per_page=1000")
        users = data.get("users") or []
        if not users:
            break
        for user in users:
            role = str((user.get("app_metadata") or {}).get("jawnix_role") or "customer")
            if role != "customer":
                continue
            seen += 1
            user_id = uuid.UUID(str(user["id"]))
            email = str(user.get("email") or "").strip().lower()
            metadata = user.get("user_metadata") or {}
            profile = db.get(CustomerProfile, user_id)
            is_new = profile is None
            if profile is None:
                profile = CustomerProfile(
                    user_id=user_id,
                    email=email,
                    first_name=str(metadata.get("first_name") or ""),
                    last_name=str(metadata.get("last_name") or ""),
                    licensed_states=[],
                )
                db.add(profile)
                created += 1
            else:
                profile.email = email
            if profile.agent_id is None:
                candidates = {
                    re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0]).strip("-"),
                    re.sub(r"[^a-z0-9]+", "-", profile.first_name.lower()).strip("-"),
                    re.sub(r"[^a-z0-9]+", "-", f"{profile.first_name} {profile.last_name}".lower()).strip("-"),
                }
                match = next((agents[value] for value in candidates if value in agents), None)
                if match:
                    profile.agent_id = match.id
                    profile.mapping_confirmed_at = None
                    proposed += 1
            if is_new:
                profile.mapping_confirmed_at = None
        if len(users) < 1000:
            break
        page += 1
    db.commit()
    return {"seen": seen, "created": created, "proposedMappings": proposed, "allMappingsRequireConfirmation": True}


@app.patch("/api/admin/recipients/{user_id}")
def map_recipient(
    user_id: uuid.UUID,
    payload: RecipientMappingUpdate,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    profile = db.get(CustomerProfile, user_id)
    agent = db.get(Agent, payload.agent_id)
    if profile is None or agent is None or not agent.active:
        raise HTTPException(status_code=404, detail="Recipient or agent was not found.")
    profile.agent_id = agent.id
    profile.mapping_confirmed_at = datetime.now(timezone.utc) if payload.confirmed else None
    db.commit()
    return {"ok": True}


async def _supabase_admin(settings: Settings, method: str, path: str, payload: dict | None = None):
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(status_code=503, detail="Supabase administration is not configured.")
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, f"{settings.supabase_url.rstrip('/')}{path}", headers=headers, json=payload)
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Supabase administration failed: {response.text}")
    return response.json()


@app.post("/api/admin/customers", status_code=201)
async def create_customer(
    payload: CustomerCreate,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    created = await _supabase_admin(
        settings,
        "POST",
        "/auth/v1/admin/users",
        {
            "email": str(payload.email).lower(),
            "password": payload.password,
            "email_confirm": True,
            "user_metadata": {"first_name": payload.first_name, "last_name": payload.last_name},
            "app_metadata": {"jawnix_role": "customer"},
        },
    )
    profile = CustomerProfile(
        user_id=uuid.UUID(str(created["id"])),
        email=str(payload.email).lower(),
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        licensed_states=[],
    )
    db.add(profile)
    db.commit()
    return {"ok": True, "userId": str(profile.user_id), "mappingConfirmed": False}


def transition_request(db: Session, request_id: uuid.UUID, action: str) -> LeadRequest:
    item = db.scalar(select(LeadRequest).where(LeadRequest.id == request_id).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail="Request was not found.")
    if action == "approve" and item.status == RequestStatus.pending.value:
        item.status = RequestStatus.approved.value
        item.approved_at = utcnow()
        item.status_message = "Approved; allocation is queued."
        enqueue_job(db, "allocate_request", item.id)
    elif action == "retry" and item.status in {RequestStatus.waiting_inventory.value, RequestStatus.failed.value}:
        item.status = RequestStatus.approved.value
        item.status_message = "Retry approved; allocation is queued."
        enqueue_job(db, "allocate_request", item.id)
    elif action == "retry_delivery" and item.status == RequestStatus.failed.value:
        if db.scalar(select(BatchArtifact).where(BatchArtifact.request_id == item.id)) is None:
            raise HTTPException(status_code=409, detail="No generated artifact is available for delivery retry.")
        item.status = RequestStatus.generated.value
        item.status_message = "Delivery retry queued."
        enqueue_job(db, "deliver_request", item.id)
    elif action == "reject" and item.status in {RequestStatus.pending.value, RequestStatus.waiting_inventory.value}:
        item.status = RequestStatus.rejected.value
        item.status_message = "Rejected by admin."
        enqueue_job(db, "update_slack", item.id)
    else:
        raise HTTPException(status_code=409, detail=f"Action {action} is not valid while request is {item.status}.")
    db.flush()
    return item


@app.post("/api/admin/requests/{request_id}/{action}")
def admin_request_action(
    request_id: uuid.UUID,
    action: str,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if action not in {"approve", "reject", "retry", "retry_delivery"}:
        raise HTTPException(status_code=404, detail="Unknown action.")
    item = transition_request(db, request_id, action)
    db.commit()
    return _request_dict(item)


@app.post("/api/integrations/slack/actions")
async def slack_actions(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    body = await request.body()
    if not verify_slack_request(
        body,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature.")
    form = parse_qs(body.decode("utf-8"))
    try:
        payload = json.loads(form["payload"][0])
        user_id = str(payload["user"]["id"])
        action = payload["actions"][0]
        action_map = {
            "approve_request": "approve",
            "reject_request": "reject",
            "retry_request": "retry",
            "retry_delivery": "retry_delivery",
        }
        transition = action_map[action["action_id"]]
        request_id = uuid.UUID(action["value"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Malformed Slack action.") from None
    if user_id not in settings.slack_approvers:
        raise HTTPException(status_code=403, detail="This Slack user is not an authorized approver.")
    replay_key = hashlib.sha256(
        request.headers.get("X-Slack-Request-Timestamp", "").encode("ascii") + b":" + body
    ).hexdigest()
    try:
        with db.begin_nested():
            db.add(WebhookReceipt(provider="slack", event_key=replay_key))
            db.flush()
    except IntegrityError:
        return {"response_type": "ephemeral", "text": "This Slack action was already processed."}
    try:
        item = transition_request(db, request_id, transition)
        db.commit()
    except HTTPException as exc:
        if exc.status_code == 409:
            return JSONResponse({"response_type": "ephemeral", "text": exc.detail}, status_code=200)
        raise
    return {"response_type": "ephemeral", "text": f"Request {item.id}: {item.status.replace('_', ' ')}"}


@app.post("/api/integrations/resend/webhook")
async def resend_webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    body = await request.body()
    if not settings.resend_webhook_secret:
        raise HTTPException(status_code=503, detail="Resend webhook verification is not configured.")
    try:
        event = Webhook(settings.resend_webhook_secret).verify(body, dict(request.headers))
    except WebhookVerificationError:
        raise HTTPException(status_code=401, detail="Invalid Resend webhook signature.") from None
    event_key = request.headers.get("svix-id") or hashlib.sha256(body).hexdigest()
    try:
        with db.begin_nested():
            db.add(WebhookReceipt(provider="resend", event_key=event_key))
            db.flush()
    except IntegrityError:
        return {"ok": True, "duplicate": True}
    event_type = str(event.get("type") or "")
    message_id = str((event.get("data") or {}).get("email_id") or "")
    artifact = db.scalar(select(BatchArtifact).where(BatchArtifact.resend_message_id == message_id))
    if artifact:
        if event_type == "email.delivered":
            artifact.delivery_status = "delivered"
            artifact.last_error = ""
        elif event_type in {"email.bounced", "email.complained", "email.failed"}:
            artifact.delivery_status = "failed"
            artifact.last_error = event_type
            item = db.get(LeadRequest, artifact.request_id)
            if item:
                item.status = RequestStatus.failed.value
                item.status_message = f"Email provider reported {event_type.replace('email.', '')}."
                enqueue_job(db, "update_slack", item.id)
    db.commit()
    return {"ok": True}
