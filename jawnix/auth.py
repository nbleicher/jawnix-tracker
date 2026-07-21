from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings, get_settings


SESSION_COOKIE = "jawnix_session"
CSRF_COOKIE = "jawnix_csrf"


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str
    role: str
    csrf: str


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="jawnix-vps-session-v1")


async def verify_supabase_token(access_token: str, settings: Settings) -> dict:
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(status_code=503, detail="Supabase Auth is not configured.")
    headers = {
        "apikey": settings.supabase_anon_key,
        "Authorization": f"Bearer {access_token}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{settings.supabase_url.rstrip('/')}/auth/v1/user", headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Your Supabase session is invalid or expired.")
    return response.json()


def issue_session(response: Response, user: dict, settings: Settings) -> Principal:
    user_id = uuid.UUID(str(user["id"]))
    email = str(user.get("email") or "").strip().lower()
    role = str((user.get("app_metadata") or {}).get("jawnix_role") or "customer")
    csrf = uuid.uuid4().hex
    token = _serializer(settings).dumps({"sub": str(user_id), "email": email, "role": role, "csrf": csrf})
    cookie_common = {
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_ttl_seconds,
    }
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **cookie_common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **cookie_common)
    return Principal(user_id=user_id, email=email, role=role, csrf=csrf)


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax")


def principal_from_request(request: Request, settings: Settings) -> Principal:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="Login required.")
    try:
        payload = _serializer(settings).loads(token, max_age=settings.session_ttl_seconds)
        return Principal(
            user_id=uuid.UUID(str(payload["sub"])),
            email=str(payload["email"]),
            role=str(payload["role"]),
            csrf=str(payload["csrf"]),
        )
    except (BadSignature, SignatureExpired, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Session expired.") from None


def require_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    principal = principal_from_request(request, settings)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        header = request.headers.get("X-CSRF-Token", "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not header or not cookie or not hmac.compare_digest(header, cookie) or not hmac.compare_digest(header, principal.csrf):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed.")
    return principal


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return principal

