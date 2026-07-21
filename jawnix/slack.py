from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from .config import Settings
from .models import LeadRequest


def verify_slack_request(body: bytes, timestamp: str, signature: str, signing_secret: str, now: int | None = None) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        request_time = int(timestamp)
        timestamp_bytes = timestamp.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - request_time) > 300:
        return False
    base = b"v0:" + timestamp_bytes + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _request_blocks(request: LeadRequest) -> list[dict]:
    customer = request.profile
    agent = request.agent
    name = " ".join(part for part in (customer.first_name, customer.last_name) if part).strip() or customer.email
    fields = [
        {"type": "mrkdwn", "text": f"*Customer*\n{name}"},
        {"type": "mrkdwn", "text": f"*Agent*\n{agent.name}"},
        {"type": "mrkdwn", "text": f"*Rows*\n{request.lead_count:,}"},
        {"type": "mrkdwn", "text": f"*States*\n{', '.join(request.states_snapshot)}"},
        {"type": "mrkdwn", "text": f"*Status*\n{request.status.replace('_', ' ').title()}"},
    ]
    if request.available_count is not None:
        fields.append({"type": "mrkdwn", "text": f"*Available*\n{request.available_count:,}"})
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Jawnix batch request"}},
        {"type": "section", "fields": fields},
    ]
    if request.status_message:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": request.status_message[:2900]}]})
    actions: list[dict] = []
    if request.status == "pending":
        actions = [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"}, "action_id": "approve_request", "value": str(request.id)},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"}, "action_id": "reject_request", "value": str(request.id)},
        ]
    elif request.status == "waiting_inventory":
        actions = [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Retry"}, "action_id": "retry_request", "value": str(request.id)},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"}, "action_id": "reject_request", "value": str(request.id)},
        ]
    elif request.status == "failed":
        if request.artifact is None:
            actions = [{"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Retry generation"}, "action_id": "retry_request", "value": str(request.id)}]
        else:
            actions = [{"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Retry delivery"}, "action_id": "retry_delivery", "value": str(request.id)}]
    if actions:
        blocks.append({"type": "actions", "elements": actions})
    return blocks


class SlackClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.slack_bot_token}", "Content-Type": "application/json"}

    def post_request(self, request: LeadRequest) -> tuple[str, str]:
        if not self.settings.slack_bot_token or not self.settings.slack_channel_id:
            raise RuntimeError("Slack is not configured.")
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers=self._headers(),
            json={"channel": self.settings.slack_channel_id, "text": f"Batch request {request.id}", "blocks": _request_blocks(request)},
            timeout=20,
        )
        data = response.json()
        if response.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"Slack post failed: {data.get('error', response.text)}")
        return str(data["channel"]), str(data["ts"])

    def update_request(self, request: LeadRequest, channel_id: str, message_ts: str) -> None:
        response = httpx.post(
            "https://slack.com/api/chat.update",
            headers=self._headers(),
            json={"channel": channel_id, "ts": message_ts, "text": f"Batch request {request.id}: {request.status}", "blocks": _request_blocks(request)},
            timeout=20,
        )
        data = response.json()
        if response.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"Slack update failed: {data.get('error', response.text)}")
