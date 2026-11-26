"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""Public REST Chat API for mobile clients (no WebView).

``/api/v1/mobile`` — the whole mobile surface, defined in
the public mobile Chat API (see ``docs/AI-INTEGRATION.md``).

Design: the mobile client is a widget visitor that talks REST instead of
Socket.IO. Everything that decides what a visitor may do, and everything the
AI turn does, is the widget's own code, reused — not re-implemented:

- Identity: the same conversation-token JWT the web widget uses
  (``create_conversation_token`` / ``app.services.conversation_token``), minted
  here short-lived (1 hour) with conversation + widget + device claims
  (``_mint_visitor_token``) and rotated via POST .../rotate; the same token is
  the Socket.IO auth for the ``/widget`` namespace, so the app gets live replies
  on the exact channel the web widget uses. See docs/API-SECURITY.md.
- Sessions: the same ``SessionToAgent`` rows, created the same way
  (``SessionToAgentRepository.create_session``), so a chat started in the app
  shows up in the dashboard inbox and a human can take it over there.
- Sending: files go through ``FileUploadService`` with the agent's allowed
  types, the visitor message is stored the same way, and the AI turn is the
  widget's turn — ``widget_agent_kwargs`` / ``create_widget_agent`` /
  ``prepare_media_context`` / ``deliver_ai_reply`` — including the workflow
  branch, the transferred/queued branch and the human-taken-over relay.
- Push: replies are pushed by ``app.services.push_service`` from the two
  delivery hook points (``deliver_ai_reply`` for AI, ``deliver_to_customer``
  for human replies); this module only registers devices and records the
  attempts endpoint.

No dashboard auth is accepted on the visitor endpoints: only the conversation
token, in the ``X-Conversation-Token`` header. Admin operations (FCM
credentials, blocked visitors, kill switch, abuse log) live in
``app/api/mobile_admin.py`` under ``/api/v1/admin/mobile`` and are NOT part
of this router — the public API exposes zero admin operations.
"""

import base64
import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.chat_agent import ChatResponse
from app.agents.host_actions.turns import PausedTurn
from app.api.widget_chat import _client_attachments, _insert_welcome_message, _media_inputs
from app.channels.constants import MessageSource
from app.core.logger import get_logger
from app.core.security import (
    decrypt_api_key,
    verify_conversation_token,
)
from app.core.socketio import sio
from app.database import SessionLocal, get_db, release_connection
from app.models import FileAttachment
from app.models.mobile import MobileDeviceToken, MobileReport, PushAttempt
from app.models.session_to_agent import SessionStatus
from app.models.widget import Widget
from app.repositories.agent import AgentRepository
from app.repositories.agent_shopify_config_repository import AgentShopifyConfigRepository
from app.repositories.ai_config import AIConfigRepository
from app.repositories.chat import ChatRepository
from app.repositories.customer import CustomerRepository
from app.repositories.jira import JiraRepository
from app.repositories.session_to_agent import SessionToAgentRepository, session_in_scope
from app.repositories.widget import WidgetRepository
from app.services import conversation_token as conversation_token_service
from app.services import customer_memory, widget_bootstrap
from app.services import mobile_security
from app.services.mobile_security import (
    BAN_SCORE,
    CHALLENGE_SCORE,
    TEMP_BAN_SECONDS,
    VISITOR_TOKEN_TTL_SECONDS,
)
from app.services.ai_access import plan_allows_ai, tell_customer_ai_is_off
from app.services.chat_notifications import notify_new_chat
from app.services.contact_capture import CONTACT_EMAIL_META_KEY
from app.services.feature_gate import WORKFLOW_FEATURE, plan_feature_on
from app.services.file_upload_service import FileUploadService
from app.services.host_action_runtime import offered_actions, waiting_on_page
from app.services.human_routing import HUMAN_ONLY_ACK, QUEUED_FOR_HUMAN_NOTICE, notify_customer
from app.services.journey_runtime import step_actions_hint
from app.services.media_ai import prepare_media_context
from app.services.reply_limits import DAILY_LIMIT_MESSAGE, TOO_FAST_MESSAGE, Refusal, check as reply_limit_check
from app.services.widget_reply import (
    FOLLOW_UPS_SESSION_KEY,
    LANGUAGES_SESSION_KEY,
    PANEL_SESSION_KEY,
    create_widget_agent,
    deliver_ai_reply,
    emit_bot_typing,
    reply_payload,
    send_handoff_contact_form,
    widget_agent_kwargs,
)
from app.services.workflow_chat import WorkflowChatService
from app.agents.transfer_agent import get_agent_availability_response

router = APIRouter()
logger = get_logger(__name__)

TOKEN_HEADER = "X-Conversation-Token"
SENDER_BY_MESSAGE_TYPE = {"user": "visitor", "bot": "ai", "agent": "agent"}


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------

class StartConversationRequest(BaseModel):
    widget_id: str
    visitor_name: Optional[str] = None
    visitor_email: Optional[str] = None
    conversation_token: Optional[str] = None
    # Client-generated stable device id (e.g. a UUID stored on device). The
    # issued visitor JWT is tied to it; later calls send it as X-Device-Id
    # and the server refuses a token presented from a different device.
    device_id: Optional[str] = None


class RegisterDeviceRequest(BaseModel):
    push_token: str
    platform: str


class ReportRequest(BaseModel):
    # Which agent's AI content is being reported. Must be an existing widget;
    # the widget ID is public, the same trust level as the widget itself.
    widget_id: str
    reason: Literal["Offensive content", "Wrong or misleading info",
                    "Spam or scam", "Something else"]
    details: Optional[str] = Field(default=None, max_length=1000)
    # Where the report came from, e.g. 'widget' | 'mobile' | 'web'.
    source: Optional[str] = Field(default=None, max_length=64)


# --------------------------------------------------------------------------
# Auth: the conversation token is the visitor's whole identity
# --------------------------------------------------------------------------

def _require_token(token: Optional[str]) -> dict:
    """The verified token payload, or 401. Mirrors the end-chat guard in
    widget.py: an unverifiable token is refused, never treated as anonymous."""
    payload = verify_conversation_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired conversation token")
    return payload


def _check_device(payload: dict, request: Request) -> None:
    """The visitor JWT is tied to the device that started the conversation.
    The SDK sends X-Device-Id on every call; a token presented from another
    device is refused. Standalone clients that never sent a device id carry
    no device claim, so nothing is checked for them."""
    bound_device = payload.get("device_id")
    if not bound_device:
        return
    presented = None
    if request is not None:
        presented = (request.headers.get("x-device-id")
                     or request.headers.get("X-Device-Id"))
    if presented and presented != bound_device:
        raise HTTPException(status_code=401, detail="Conversation token was issued to another device")


def _guard_chat_allowed(db: Session, *, organization_id, widget_id,
                        customer_id=None, ip: Optional[str] = None,
                        device_id: Optional[str] = None,
                        session_id=None) -> None:
    """Phase 1 abuse gates, checked on every mobile call: the per-widget kill
    switch, then the blocked-visitor list / temp bans. Both are 403s — the
    visitor learns nothing about which list they are on."""
    limits = mobile_security.widget_limits(db, widget_id)
    if limits.chat_disabled:
        mobile_security.record_abuse_event(
            db, event_type="kill_switch", organization_id=organization_id,
            widget_id=widget_id, session_id=session_id, ip=ip,
            device_id=device_id,
            detail="Chat disabled for this widget")
        raise HTTPException(status_code=403, detail="Chat is disabled for this widget")
    blocked = mobile_security.is_blocked(db, organization_id, customer_id=customer_id, ip=ip)
    if blocked is not None:
        mobile_security.record_abuse_event(
            db, event_type="blocked_visitor", organization_id=organization_id,
            widget_id=widget_id, session_id=session_id, ip=ip,
            device_id=device_id,
            detail="Blocked visitor attempted access")
        raise HTTPException(status_code=403, detail="This visitor is blocked")


def _load_own_session(db: Session, conversation_id: str, payload: dict, request: Request = None):
    """The session this token may act on: it exists, it belongs to the token's
    customer, and it is inside the token's scope. Anything else is 404, not
    403 — a visitor must not learn whether another customer's conversation
    exists. A token carrying a session_id claim must name this conversation."""
    _check_device(payload, request)
    session_repo = SessionToAgentRepository(db)
    session = session_repo.get_session(conversation_id)
    if not session:
        raise HTTPException(status_code=404, detail="Conversation not found")
    customer_id = payload.get("sub")
    if not customer_id or str(session.customer_id) != str(customer_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    bound_session = payload.get("session_id")
    if bound_session and str(bound_session) != str(session.session_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        scope = conversation_token_service.conversation_scope(payload)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid conversation token")
    if not session_in_scope(session, scope):
        raise HTTPException(status_code=404, detail="Conversation not found")
    widget_id = payload.get("widget_id")
    if widget_id:
        widget = db.query(Widget).filter(Widget.id == widget_id).first()
        if not widget or str(widget.agent_id) != str(session.agent_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        widget_id = None
    _guard_chat_allowed(
        db, organization_id=session.organization_id, widget_id=widget_id,
        customer_id=customer_id, ip=mobile_security.client_ip(request),
        device_id=payload.get("device_id"),
        session_id=str(session.session_id))
    return session, scope


def _mint_visitor_token(*, widget_id: str, customer_id: str, customer_email: Optional[str],
                        conversation_key: str, device_id: Optional[str],
                        session_id: str):
    """The short-lived signed visitor JWT (Phase 1 auth): 1 hour, tied to the
    conversation + widget + device. Same signing/verification and Redis
    revocation as the web widget's conversation token — only the lifetime and
    the claims differ. Returns (token, expires_in_seconds)."""
    claims = {
        "widget_id": widget_id,
        "sub": customer_id,
        "customer_id": customer_id,
        "customer_email": customer_email,
        conversation_token_service.CONVERSATION_KEY_CLAIM: conversation_key,
        "device_id": device_id,
        "session_id": session_id,
    }
    token, _expires_at, ttl = conversation_token_service.mint(
        claims, ttl_seconds=VISITOR_TOKEN_TTL_SECONDS)
    return token, ttl


def _format_message(msg) -> dict:
    """A stored row as the contract's message shape (§2/§3). Attachments
    already carry signed URLs — get_session_history signs them."""
    return {
        "id": msg.id,
        "sender": SENDER_BY_MESSAGE_TYPE.get(msg.message_type, msg.message_type),
        "text": msg.message,
        "message_type": msg.message_type,
        "attachments": [
            {
                "filename": a.filename,
                "file_url": a.file_url,
                "content_type": a.content_type,
                "file_size": a.file_size,
            }
            for a in (msg.attachments or [])
        ],
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "user_name": msg.user.full_name if getattr(msg, "user", None) else None,
        "agent_name": (msg.agent.display_name or msg.agent.name)
        if getattr(msg, "agent", None) else None,
    }


def _start_session(db: Session, agent_id, customer_id, org_id, scope: str) -> str:
    """Open a new conversation row, exactly like widget_chat._start_conversation."""
    session_id = str(uuid.uuid4())
    shopify_config = AgentShopifyConfigRepository(db).get_agent_shopify_config(str(agent_id))
    SessionToAgentRepository(db).create_session(
        session_id=session_id,
        agent_id=agent_id,
        customer_id=customer_id,
        organization_id=org_id,
        channel="shopify" if shopify_config and shopify_config.enabled else "web",
        conversation_key=conversation_token_service.session_key_for_scope(scope),
    )
    return session_id


# --------------------------------------------------------------------------
# 1. Start / resume a conversation
# --------------------------------------------------------------------------

@router.post("/conversations")
async def start_conversation(body: StartConversationRequest, request: Request,
                             db: Session = Depends(get_db)):
    widget = db.query(Widget).filter(Widget.id == body.widget_id).first()
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")
    agent = WidgetRepository(db).get_widget_agent(widget)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if getattr(agent, "require_token_auth", False):
        raise HTTPException(
            status_code=401,
            detail="This widget requires a site-issued token; mobile start is not available",
        )
    ip = mobile_security.client_ip(request)
    # Kill switch first: a disabled widget starts nothing, for anyone.
    if mobile_security.widget_limits(db, body.widget_id).chat_disabled:
        mobile_security.record_abuse_event(
            db, event_type="kill_switch", organization_id=widget.organization_id,
            widget_id=body.widget_id, ip=ip, detail="Start refused: chat disabled")
        raise HTTPException(status_code=403, detail="Chat is disabled for this widget")
    ai_config = AIConfigRepository(db).get_active_config(widget.organization_id)
    if not ai_config:
        # The widget socket refuses without an AI config too; starting a
        # conversation that can never answer would only strand the visitor.
        raise HTTPException(status_code=422, detail="AI configuration required")

    session_repo = SessionToAgentRepository(db)
    customer_repo = CustomerRepository(db)
    token_payload = None
    if body.conversation_token:
        token_payload = verify_conversation_token(body.conversation_token)

    # Resume: a live token naming an active conversation for this widget.
    if token_payload and token_payload.get("widget_id") == body.widget_id:
        try:
            scope = conversation_token_service.conversation_scope(token_payload)
        except ValueError:
            scope = None
        customer_id = token_payload.get("sub")
        if scope and customer_id:
            _check_device(token_payload, request)
            _guard_chat_allowed(
                db, organization_id=widget.organization_id, widget_id=body.widget_id,
                customer_id=customer_id, ip=ip,
                device_id=token_payload.get("device_id"))
            active = session_repo.get_active_widget_session(
                customer_id=customer_id, agent_id=agent.id, scope=scope)
            if active:
                import time as _time
                expires_in = None
                exp = token_payload.get("exp")
                if exp:
                    expires_in = max(0, int(exp - _time.time()))
                return {
                    "conversation_id": str(active.session_id),
                    "conversation_token": body.conversation_token,
                    "customer_id": str(active.customer_id),
                    "widget_id": body.widget_id,
                    "agent_name": agent.display_name or agent.name,
                    "created": False,
                    "expires_in": expires_in,
                }

    conversation_key = widget_bootstrap.conversation_key(token_payload)

    # The visitor: an existing customer for a known email, else a new one —
    # the same choice widget.py's authorize_visitor makes.
    customer = None
    if body.visitor_email:
        existing = customer_repo.get_customer_by_email(body.visitor_email, widget.organization_id)
        if existing is not None and not existing.is_authenticated:
            customer = existing
        elif existing is not None:
            customer = customer_repo.create_customer(
                CustomerRepository.anonymous_email(), widget.organization_id,
                meta_data={CONTACT_EMAIL_META_KEY: body.visitor_email},
                conversation_key=conversation_key)
        else:
            customer = customer_repo.create_customer(
                body.visitor_email, widget.organization_id,
                full_name=body.visitor_name, conversation_key=conversation_key)
    if customer is None:
        customer = customer_repo.create_customer(
            CustomerRepository.anonymous_email(), widget.organization_id,
            full_name=body.visitor_name, conversation_key=conversation_key)
    if body.visitor_name and not customer.full_name:
        customer.full_name = body.visitor_name
        db.commit()

    # A blocked visitor (customer or IP) starts nothing — 403, same as on
    # later calls.
    _guard_chat_allowed(
        db, organization_id=widget.organization_id, widget_id=body.widget_id,
        customer_id=str(customer.id), ip=ip,
        device_id=(body.device_id or "").strip() or None)

    session_id = _start_session(
        db, agent.id, customer.id, widget.organization_id, conversation_key)
    # The visitor JWT: short-lived (1h), tied to this conversation + widget +
    # device. The client stores it and rotates it (POST .../rotate) before it
    # lapses; the same token is the Socket.IO auth for /widget.
    device_id = (body.device_id or "").strip() or None
    token, expires_in = _mint_visitor_token(
        widget_id=body.widget_id, customer_id=str(customer.id),
        customer_email=customer.email, conversation_key=conversation_key,
        device_id=device_id, session_id=session_id)
    return {
        "conversation_id": session_id,
        "conversation_token": token,
        "customer_id": str(customer.id),
        "widget_id": body.widget_id,
        "agent_name": agent.display_name or agent.name,
        "created": True,
        "expires_in": expires_in,
    }


@router.post("/conversations/{conversation_id}/rotate")
async def rotate_token(
    conversation_id: str,
    request: Request,
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    """Rotate the visitor JWT: the old token is revoked (its jti is deleted
    from Redis) and a fresh short-lived one is issued for the same
    conversation + widget + device. Call before the token lapses (hourly at
    most; the SDK rotates when half the lifetime remains). A lapsed token
    cannot be rotated — start/resume via POST /conversations instead."""
    from app.core.security import revoke_token
    payload = _require_token(token)
    session, scope = _load_own_session(db, conversation_id, payload, request)
    old_jti = payload.get("jti")
    device_id = payload.get("device_id")
    # Keep the rotation chain's conversation key exactly: a token without one
    # (verified scope) must not gain a "verified" key, which conversation_scope
    # would refuse as a scope the token was never given.
    new_token, expires_in = _mint_visitor_token(
        widget_id=payload.get("widget_id"), customer_id=payload.get("sub"),
        customer_email=payload.get("customer_email") or payload.get("email"),
        conversation_key=payload.get(conversation_token_service.CONVERSATION_KEY_CLAIM),
        device_id=device_id, session_id=str(session.session_id))
    if old_jti:
        revoke_token(old_jti)
    return {"conversation_token": new_token, "expires_in": expires_in,
            "conversation_id": str(session.session_id)}


# --------------------------------------------------------------------------
# 2. Read messages (polling fallback)
# --------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    request: Request,
    after: Optional[int] = Query(None, description="Return only messages with id greater than this"),
    limit: int = Query(50, ge=1, le=100),
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    payload = _require_token(token)
    session, _scope = _load_own_session(db, conversation_id, payload, request)
    messages = await ChatRepository(db).get_session_history(str(session.session_id))
    formatted = [_format_message(m) for m in messages]
    if after is not None:
        formatted = [m for m in formatted if m["id"] > after]
    return {"conversation_id": str(session.session_id), "messages": formatted[:limit]}


# --------------------------------------------------------------------------
# 3. Send a message (+ files) — the widget's AI turn over REST
# --------------------------------------------------------------------------

def _mobile_session_dict(session, agent, ai_config, token: str, scope: str, widget_id: Optional[str]) -> dict:
    """The socket-session shape widget_agent_kwargs / deliver_ai_reply read,
    built from the database instead of a socket connect. No host page, so no
    panel, no host actions offered, no follow-up chips."""
    return {
        "widget_id": widget_id,
        "org_id": str(session.organization_id),
        "agent_id": str(session.agent_id),
        "customer_id": str(session.customer_id),
        "session_id": str(session.session_id),
        "ai_config": SimpleNamespace(
            encrypted_api_key=ai_config.get("encrypted_api_key") if isinstance(ai_config, dict)
            else getattr(ai_config, "encrypted_api_key", None),
            model_name=ai_config.get("model_name") if isinstance(ai_config, dict)
            else getattr(ai_config, "model_name", None),
            model_type=ai_config.get("model_type") if isinstance(ai_config, dict)
            else getattr(ai_config, "model_type", None),
            settings=ai_config.get("settings") if isinstance(ai_config, dict)
            else getattr(ai_config, "settings", None),
        ),
        "conversation_token": token,
        "conversation_scope": scope,
        "enable_rate_limiting": bool(getattr(agent, "enable_rate_limiting", False)),
        "overall_limit_per_ip": getattr(agent, "overall_limit_per_ip", None),
        "requests_per_sec": getattr(agent, "requests_per_sec", None),
        "messages_per_customer_per_minute": getattr(agent, "messages_per_customer_per_minute", None),
        "replies_per_customer_per_day": getattr(agent, "replies_per_customer_per_day", None),
        "ai_replies_enabled": getattr(agent, "ai_replies_enabled", True),
        "use_workflow": getattr(agent, "use_workflow", False),
        "offer_choices": bool(getattr(agent, "channel_quick_replies", False)),
        "active_workflow_id": getattr(agent, "active_workflow_id", None),
        "source": None,
        "authenticated": False,
        "page_url": None,
        PANEL_SESSION_KEY: False,
        LANGUAGES_SESSION_KEY: [],
        FOLLOW_UPS_SESSION_KEY: False,
    }


def _rate_limited(db, session, widget_id, ip, detail: str, retry_after: int,
                   event_type: str = "rate_limited", device_id: Optional[str] = None):
    """429 with a Retry-After header, and a row in the abuse log."""
    mobile_security.record_abuse_event(
        db, event_type=event_type, organization_id=session.organization_id,
        widget_id=widget_id, session_id=str(session.session_id), ip=ip,
        device_id=device_id, detail=detail)
    raise HTTPException(status_code=429, detail=detail,
                        headers={"Retry-After": str(retry_after)})


def _enforce_rate_limit(db, session, widget_id, ip, limits,
                        device_id: Optional[str] = None) -> None:
    """Sliding-window message rates: per conversation (30/min), per IP, per
    widget. Checked before uploads and model calls — a flood never reaches
    the expensive parts."""
    session_id = str(session.session_id)
    allowed, retry_after = mobile_security.sliding_window_allow(
        f"mobile:rl:conv:{session_id}", limits.msg_per_min, 60)
    if not allowed:
        _rate_limited(db, session, widget_id, ip,
                      f"Too many messages for this conversation ({limits.msg_per_min}/min)",
                      retry_after, device_id=device_id)
    allowed, retry_after = mobile_security.sliding_window_allow(
        f"mobile:rl:ip:{widget_id}:{ip}", limits.msg_per_ip_per_min, 60)
    if not allowed:
        _rate_limited(db, session, widget_id, ip,
                      f"Too many messages from this address ({limits.msg_per_ip_per_min}/min)",
                      retry_after, device_id=device_id)
    allowed, retry_after = mobile_security.sliding_window_allow(
        f"mobile:rl:widget:{widget_id}", limits.msg_per_widget_per_min, 60)
    if not allowed:
        _rate_limited(db, session, widget_id, ip,
                      f"This chat is receiving too many messages right now ({limits.msg_per_widget_per_min}/min)",
                      retry_after, device_id=device_id)


def _enforce_risk(db, session, widget_id, ip, message: str,
                  device_id: Optional[str] = None) -> None:
    """Spam signals: an identical repeat of the visitor's previous message, or
    a link flood. Medium risk slows the visitor down (429); high risk temp-bans
    them for 15 minutes (403). A CAPTCHA challenge is the next step for
    medium-risk traffic once a provider is wired in — see docs/API-SECURITY.md;
    until then the slow-down applies."""
    from app.models.chat_history import ChatHistory
    last = db.query(ChatHistory).filter(
        ChatHistory.session_id == session.session_id,
        ChatHistory.message_type == "user",
    ).order_by(ChatHistory.created_at.desc(), ChatHistory.id.desc()).first()
    score, reasons = mobile_security.risk_score(
        message, last.message if last else None)
    if score >= BAN_SCORE:
        mobile_security.temp_ban("customer", str(session.customer_id))
        mobile_security.record_abuse_event(
            db, event_type="temp_ban", organization_id=session.organization_id,
            widget_id=widget_id, session_id=str(session.session_id), ip=ip,
            device_id=device_id,
            detail="; ".join(reasons))
        raise HTTPException(status_code=403, detail="Temporarily blocked for suspicious activity")
    if score >= CHALLENGE_SCORE:
        mobile_security.record_abuse_event(
            db, event_type="challenge", organization_id=session.organization_id,
            widget_id=widget_id, session_id=str(session.session_id), ip=ip,
            device_id=device_id,
            detail="; ".join(reasons))
        _rate_limited(db, session, widget_id, ip,
                      "Suspicious activity detected; please wait a moment and try again",
                      30, event_type="challenge", device_id=device_id)


def _enforce_ai_quota(db, session, widget_id, ip, limits,
                      device_id: Optional[str] = None) -> None:
    """AI daily quotas (AI is the expensive part): per conversation and per
    widget. 429 with Retry-After to UTC midnight when either is spent. At 80%
    of the widget quota an ai_spike abuse event fires once per day."""
    day = mobile_security.today_suffix()
    session_id = str(session.session_id)
    allowed, retry_after = mobile_security.daily_counter_check(
        f"mobile:ai:conv:{session_id}:{day}", limits.ai_per_conversation_per_day)
    if not allowed:
        _rate_limited(db, session, widget_id, ip,
                      "Daily AI reply limit reached for this conversation",
                      retry_after, event_type="ai_quota", device_id=device_id)
    widget_count = mobile_security.daily_counter_value(f"mobile:ai:widget:{widget_id}:{day}")
    allowed, retry_after = mobile_security.daily_counter_check(
        f"mobile:ai:widget:{widget_id}:{day}", limits.ai_per_widget_per_day)
    if not allowed:
        _rate_limited(db, session, widget_id, ip,
                      "Daily AI reply limit reached for this chat",
                      retry_after, event_type="ai_quota", device_id=device_id)
    if widget_count + 1 >= int(limits.ai_per_widget_per_day * 0.8):
        client = mobile_security._redis()
        flag = f"mobile:ai:spike-flag:{widget_id}:{day}"
        try:
            if client is not None and client.set(flag, "1", nx=True, ex=86400):
                mobile_security.record_abuse_event(
                    db, event_type="ai_spike", organization_id=session.organization_id,
                    widget_id=widget_id, session_id=session_id, ip=ip,
                    device_id=device_id,
                    detail=f"Widget AI usage at {widget_count + 1}/{limits.ai_per_widget_per_day} for today")
        except Exception:
            pass


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    request: Request,
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    payload = _require_token(token)
    session, scope = _load_own_session(db, conversation_id, payload, request)
    session_id = str(session.session_id)
    org_id = str(session.organization_id)
    customer_id = str(session.customer_id)
    ip = mobile_security.client_ip(request)
    device_id = payload.get("device_id")
    widget_id = payload.get("widget_id")
    limits = mobile_security.widget_limits(db, widget_id)

    form = await request.form()
    raw_message = str(form.get("message") or "")
    uploads = list(form.getlist("files")) + list(form.getlist("files[]"))

    # Same sanitising as the widget socket handler.
    from app.utils.sanitize import sanitize_message
    from app.utils.secret_redaction import redact_secrets
    message, _ = redact_secrets(sanitize_message(raw_message.strip()))
    if not message.strip() and not uploads:
        raise HTTPException(status_code=422, detail="Message text or files are required")

    # Phase 1 rate limits, before any expensive work (uploads, model calls):
    # per conversation, per IP, per widget — sliding window, 429 + Retry-After.
    # Redis down means fail-open (a broken counter must not stop the chat).
    _enforce_rate_limit(db, session, widget_id, ip, limits, device_id=device_id)

    # Spam signals on this message: identical repeats and link floods.
    _enforce_risk(db, session, widget_id, ip, message, device_id=device_id)

    agent = AgentRepository(db).get_agent(session.agent_id)
    if uploads and agent and not agent.allow_attachments:
        raise HTTPException(status_code=422, detail="Attachments are not allowed for this agent")

    ai_config_repo = AIConfigRepository(db)
    ai_config = ai_config_repo.get_active_config(session.organization_id)
    if not ai_config:
        raise HTTPException(status_code=422, detail="AI configuration required")
    # get_active_config may return a dict (decrypted key) or a row; both work
    # with _mobile_session_dict. Normalise the model type for plan checks.
    model_type = ai_config.get("model_type") if isinstance(ai_config, dict) else ai_config.model_type

    # Reopen a closed conversation the way the widget does, instead of
    # stranding the visitor on a dead thread.
    session_repo = SessionToAgentRepository(db)
    if session.status == SessionStatus.CLOSED:
        if not session_repo.reopen_closed_session(session_id):
            raise HTTPException(status_code=410, detail="Conversation is closed; start a new one")
        session = session_repo.get_session(session_id)
    elif session.status not in (SessionStatus.OPEN, SessionStatus.TRANSFERRED):
        raise HTTPException(status_code=410, detail="Conversation is closed; start a new one")

    agent_uses_ai = getattr(agent, "ai_replies_enabled", True)
    plan_allows = (session.user_id is not None
                   or plan_allows_ai(db, session.organization_id, model_type))
    ai_replies_enabled = agent_uses_ai and plan_allows
    ai_branch = (ai_replies_enabled and session.status == SessionStatus.OPEN
                 and session.user_id is None and not session.workflow_id)
    workflow_branch = (ai_replies_enabled and session.workflow_id and session.user_id is None
                       and plan_feature_on(db, session.organization_id, WORKFLOW_FEATURE))

    # AI daily quotas (per conversation + per widget), before the turn: the
    # model is the expensive part. reply_limits.check below still applies
    # the agent's own per-customer limits (reused, not replaced).
    if ai_branch or workflow_branch:
        _enforce_ai_quota(db, session, widget_id, ip, limits, device_id=device_id)

    # Reply limits, before the message is stored: a refused message is never
    # stored, exactly like the widget (reply_limits.check counts per customer).
    if ai_branch:
        refusal = await reply_limit_check(
            str(session.agent_id), customer_id,
            getattr(agent, "messages_per_customer_per_minute", None),
            getattr(agent, "replies_per_customer_per_day", None))
        if refusal == Refusal.TOO_FAST:
            raise HTTPException(status_code=429, detail=TOO_FAST_MESSAGE)
        if refusal == Refusal.DAILY_LIMIT:
            raise HTTPException(status_code=429, detail=DAILY_LIMIT_MESSAGE)

    # Upload files first (same validation as the widget); a failure here
    # stores nothing. Daily per-conversation upload limits (count + bytes)
    # run before each file is accepted — the 5MB image / 10MB file caps are
    # still enforced inside FileUploadService (magic bytes included).
    raw_files, uploaded_files = [], []
    if uploads:
        day = mobile_security.today_suffix()
        allowed_types = FileUploadService.get_allowed_types_for_agent(
            agent.allowed_attachment_types if agent else None)
        for upload in uploads:
            raw = await upload.read()
            ok, retry_after = mobile_security.daily_counter_check(
                f"mobile:up:count:{session_id}:{day}", limits.upload_files_per_day)
            if not ok:
                _rate_limited(db, session, widget_id, ip,
                              "Daily upload limit reached for this conversation",
                              retry_after, event_type="rate_limited")
            used_bytes = mobile_security.daily_counter_value(f"mobile:up:bytes:{session_id}:{day}")
            if used_bytes + len(raw) > limits.upload_bytes_per_day:
                _rate_limited(db, session, widget_id, ip,
                              "Daily upload size limit reached for this conversation",
                              mobile_security._seconds_to_midnight_utc(),
                              event_type="rate_limited")
            mobile_security.daily_counter_incr(f"mobile:up:bytes:{session_id}:{day}", len(raw))
            file_data = {
                "content": base64.b64encode(raw).decode(),
                "filename": upload.filename or "file",
                "content_type": upload.content_type or "application/octet-stream",
                "size": len(raw),
            }
            try:
                uploaded_files.append(await FileUploadService.upload_file(
                    file_data=file_data, org_id=org_id, customer_id=customer_id,
                    allowed_types=allowed_types))
            except ValueError as val_err:
                raise HTTPException(status_code=422, detail=str(val_err))
            except Exception as upload_err:
                logger.error(f"Mobile upload failed for session {session_id}: {upload_err}")
                raise HTTPException(status_code=422, detail="Failed to upload file")
            raw_files.append(file_data)

    chat_repo = ChatRepository(db)
    is_first_customer_message = not chat_repo.has_customer_messages(session_id)
    if is_first_customer_message:
        await notify_new_chat(db, session)

    # The agent's welcome message is what the visitor is picking from, so
    # the AI needs it in the history before this first message is stored by
    # the branch below — whichever branch that is. Only on the first
    # message, only when one is set; the helper no-ops otherwise. Same call
    # as the widget socket handler.
    if is_first_customer_message:
        _insert_welcome_message(db, session_id, org_id, session.agent_id, customer_id)

    # Who stores the visitor message? NOT the AI/workflow branches:
    # ChatAgent.get_response and WorkflowChatService both store the user
    # message themselves (with the attachments) — pre-storing it here would
    # duplicate it, the way the widget flow avoids by never storing first.
    # The human/transferred branches make no agent call, so they store it.
    agent_stores_message = ai_branch or workflow_branch
    user_msg = None
    stored = None
    if not agent_stores_message:
        user_msg = chat_repo.create_message({
            "message": message,
            "message_type": "user",
            "session_id": session_id,
            "organization_id": org_id,
            "agent_id": session.agent_id,
            "customer_id": customer_id,
            "user_id": session.user_id,
            # Read by the auto-tagger only (payment proof needs to know
            # a file came with the message); popped before the row is
            # built.
            "attachments": uploaded_files,
        })
        if uploaded_files:
            for file_info in uploaded_files:
                db.add(FileAttachment(
                    file_url=file_info["file_url"],
                    filename=file_info["filename"],
                    content_type=file_info["content_type"],
                    file_size=file_info["size"],
                    chat_history_id=user_msg.id,
                    organization_id=session.organization_id,
                    uploaded_by_customer_id=session.customer_id,
                ))
            db.commit()
        # _format_message needs the row with attachments/user/agent joined, and
        # signed file URLs like get_session_history hands out.
        stored = _format_message(await _reload_message(db, user_msg.id))

    session_dict = _mobile_session_dict(session, agent, ai_config, token, scope,
                                        payload.get("widget_id"))
    await customer_memory.prepare_widget(db, session_dict)

    ai_reply_pending = False
    response = None
    chat_agent = None

    if session.user_id is not None or (not ai_branch and not workflow_branch
                                        and session.status != SessionStatus.TRANSFERRED):
        # Human has taken over, or nobody is going to answer on its own
        # (human-only agent / AI switched off for the plan): store-and-relay,
        # exactly like the widget's fall-through branch.
        await _relay_to_agents(session_id, user_msg.id, message, uploaded_files,
                               user_id=str(session.user_id) if session.user_id else None)
        if is_first_customer_message and not agent_uses_ai:
            await notify_customer(db, session, HUMAN_ONLY_ACK, channels_only=False)
        elif agent_uses_ai and not plan_allows and await tell_customer_ai_is_off(db, session):
            await send_handoff_contact_form(session_dict, db)
    elif workflow_branch:
        if waiting_on_page(db, session_id):
            raise HTTPException(status_code=409, detail="Still waiting for the app to finish the last step.")
        await emit_bot_typing(session_id)
        workflow_service = WorkflowChatService(db)
        response = await workflow_service.handle_workflow_chat(
            active_session=session, message=message, session_id=session_id,
            org_id=org_id, customer_id=customer_id, session=session_dict,
            sio=sio, namespace="/widget", source=None)
        if response is None:
            return {"message": await _stored_visitor_message(db, session_id),
                    "ai_reply_pending": False}
        if getattr(response, "transfer_to_human", False) and hasattr(response, "_is_transfer_response"):
            await sio.emit("chat_response", reply_payload(response, session_id),
                           room=session_id, namespace="/widget")
            return {"message": await _stored_visitor_message(db, session_id),
                    "ai_reply_pending": True}
        ai_reply_pending = True
    elif session.status == SessionStatus.TRANSFERRED and session.user_id is None and plan_allows:
        # Queued for a human: the availability/queued notice, as the widget
        # writes it — a stored bot message, delivered by deliver_ai_reply.
        await emit_bot_typing(session_id)
        response = await _transferred_notice(db, session_dict, session_id, org_id, customer_id, message)
        ai_reply_pending = True
    elif ai_branch:
        if waiting_on_page(db, session_id):
            raise HTTPException(status_code=409, detail="Still waiting for the app to finish the last step.")
        await emit_bot_typing(session_id)
        host_actions = offered_actions(db, session_dict)
        release_connection(db)
        chat_agent = await create_widget_agent(
            widget_agent_kwargs(session_dict, host_actions,
                                step_actions_hint(db, session_dict, host_actions)))
        media_images = None
        model_message = message
        if uploaded_files:
            try:
                media = await prepare_media_context(
                    _media_inputs(raw_files, uploaded_files),
                    api_key=decrypt_api_key(session_dict["ai_config"].encrypted_api_key),
                    model_type=session_dict["ai_config"].model_type,
                    model_name=session_dict["ai_config"].model_name,
                )
                media_images = media.images or None
                if media.context_text:
                    model_message = (f"{media.context_text}\n\n{message}"
                                     if message.strip() else media.context_text)
            except Exception as media_err:
                logger.error(f"Media preparation failed for session {session_id}: {media_err}")
        try:
            response = await chat_agent.get_response(
                message=message, model_message=model_message, images=media_images,
                attachments=uploaded_files or None, session_id=chat_agent.agent.session_id,
                org_id=org_id, agent_id=session_dict["agent_id"], customer_id=customer_id)
        finally:
            await chat_agent.safe_cleanup_mcp_tools()
        if isinstance(response, PausedTurn):
            # A host action needs the embedding page, which the app has not
            # got: no reply can follow, so say so instead of hanging.
            logger.info(f"Mobile turn paused on a host action for session {session_id}")
            return {"message": await _stored_visitor_message(db, session_id),
                    "ai_reply_pending": False, "note": "action_required"}
        ai_reply_pending = True
    else:
        await _relay_to_agents(session_id, user_msg.id, message, uploaded_files,
                               user_id=str(session.user_id) if session.user_id else None)

    if response is not None:
        # No socket id on REST: deliver_ai_reply only uses `sid` to repoint
        # the socket session after a lead-capture merge (guarded there), so
        # "" is safe — the reply still emits to the conversation room, and
        # the push hook inside still fires.
        await deliver_ai_reply("", session_dict, response, db, chat_agent=chat_agent)
        if getattr(response, "end_chat", False):
            ai_reply_pending = False

    if stored is None:
        stored = await _stored_visitor_message(db, session_id)
    return {"message": stored, "ai_reply_pending": ai_reply_pending}


# --------------------------------------------------------------------------
# 3b. Send a message, streamed — SSE (docs/STREAMING-PROTOCOL.md)
# --------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    """One server-sent event: `event: <name>` + JSON `data:`, blank-terminated."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: str,
    request: Request,
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    """The AI turn as a token stream (SSE). Same auth, rate limits, quotas
    and guards as POST .../messages, but text-only: files get 422 (use the
    non-streaming endpoint for uploads), and the human / transferred /
    workflow branches get 409 (they have no streamed reply — use the
    non-streaming endpoint). Events: start / token / done, or error instead
    of done when the turn fails. A turn the lean agent cannot take emits a
    single start (source model-full), no tokens, then done with the complete
    reply — a client that only handles done still works."""
    payload = _require_token(token)
    session, scope = _load_own_session(db, conversation_id, payload, request)
    session_id = str(session.session_id)
    org_id = str(session.organization_id)
    customer_id = str(session.customer_id)
    ip = mobile_security.client_ip(request)
    device_id = payload.get("device_id")
    widget_id = payload.get("widget_id")
    limits = mobile_security.widget_limits(db, widget_id)

    form = await request.form()
    raw_message = str(form.get("message") or "")
    uploads = list(form.getlist("files")) + list(form.getlist("files[]"))
    if uploads:
        raise HTTPException(
            status_code=422,
            detail="Files are not supported on the streaming endpoint; "
                   "use POST /conversations/{id}/messages instead")

    from app.utils.sanitize import sanitize_message
    from app.utils.secret_redaction import redact_secrets
    message, _ = redact_secrets(sanitize_message(raw_message.strip()))
    if not message.strip():
        raise HTTPException(status_code=422, detail="Message text is required")

    _enforce_rate_limit(db, session, widget_id, ip, limits, device_id=device_id)
    _enforce_risk(db, session, widget_id, ip, message, device_id=device_id)

    agent = AgentRepository(db).get_agent(session.agent_id)
    ai_config_repo = AIConfigRepository(db)
    ai_config = ai_config_repo.get_active_config(session.organization_id)
    if not ai_config:
        raise HTTPException(status_code=422, detail="AI configuration required")
    model_type = ai_config.get("model_type") if isinstance(ai_config, dict) else ai_config.model_type

    session_repo = SessionToAgentRepository(db)
    if session.status == SessionStatus.CLOSED:
        if not session_repo.reopen_closed_session(session_id):
            raise HTTPException(status_code=410, detail="Conversation is closed; start a new one")
        session = session_repo.get_session(session_id)
    elif session.status not in (SessionStatus.OPEN, SessionStatus.TRANSFERRED):
        raise HTTPException(status_code=410, detail="Conversation is closed; start a new one")

    agent_uses_ai = getattr(agent, "ai_replies_enabled", True)
    plan_allows = (session.user_id is not None
                   or plan_allows_ai(db, session.organization_id, model_type))
    ai_replies_enabled = agent_uses_ai and plan_allows
    ai_branch = (ai_replies_enabled and session.status == SessionStatus.OPEN
                 and session.user_id is None and not session.workflow_id)
    workflow_branch = (ai_replies_enabled and session.workflow_id and session.user_id is None
                       and plan_feature_on(db, session.organization_id, WORKFLOW_FEATURE))
    if not ai_branch:
        # Human takeover / transferred / workflow / AI off: no streamed
        # reply exists for these branches.
        raise HTTPException(
            status_code=409,
            detail="This conversation has no streamed reply right now; "
                   "use POST /conversations/{id}/messages instead")

    _enforce_ai_quota(db, session, widget_id, ip, limits, device_id=device_id)
    refusal = await reply_limit_check(
        str(session.agent_id), customer_id,
        getattr(agent, "messages_per_customer_per_minute", None),
        getattr(agent, "replies_per_customer_per_day", None))
    if refusal == Refusal.TOO_FAST:
        raise HTTPException(status_code=429, detail=TOO_FAST_MESSAGE)
    if refusal == Refusal.DAILY_LIMIT:
        raise HTTPException(status_code=429, detail=DAILY_LIMIT_MESSAGE)

    chat_repo = ChatRepository(db)
    is_first_customer_message = not chat_repo.has_customer_messages(session_id)
    if is_first_customer_message:
        await notify_new_chat(db, session)
    if waiting_on_page(db, session_id):
        raise HTTPException(status_code=409, detail="Still waiting for the app to finish the last step.")

    # Same as the non-streaming endpoint: the stream builds its history
    # from what is stored, so the welcome must be in place before the
    # generator runs — it runs after this handler returns, on a fresh db
    # session — and only for a turn that actually goes through (the guards
    # above have all passed by here).
    if is_first_customer_message:
        _insert_welcome_message(db, session_id, org_id, session.agent_id, customer_id)

    session_dict = _mobile_session_dict(session, agent, ai_config, token, scope,
                                        payload.get("widget_id"))
    await customer_memory.prepare_widget(db, session_dict)
    await emit_bot_typing(session_id)
    # Read everything the turn needs from this request's db session now: the
    # SSE generator runs after this handler returns, so it must not touch it.
    host_actions = offered_actions(db, session_dict)
    journey_hint = step_actions_hint(db, session_dict, host_actions)
    release_connection(db)

    async def event_gen():
        chat_agent = None
        try:
            chat_agent = await create_widget_agent(
                widget_agent_kwargs(session_dict, host_actions, journey_hint))
            saw_error = False
            last_source = "model-full"
            # No KB instant answers: every turn goes to the model, which
            # answers from the KB with its own search tool instead of the
            # server sending article text back directly.
            async for stream_event in chat_agent.stream_response(
                    message=message, session_id=chat_agent.agent.session_id,
                    org_id=org_id, agent_id=session_dict["agent_id"],
                    customer_id=customer_id, force_model=True):
                event_type = stream_event.get("type")
                last_source = stream_event.get("source", last_source)
                if event_type == "start":
                    yield _sse("start", {
                        "session_id": session_id,
                        "source": stream_event.get("source"),
                        "message_id": stream_event.get("message_id")})
                elif event_type == "token":
                    yield _sse("token", {
                        "session_id": session_id,
                        "delta": stream_event.get("delta", ""),
                        "source": stream_event.get("source")})
                elif event_type == "done" and stream_event.get("paused"):
                    yield _sse("error", {
                        "session_id": session_id,
                        "error": "action_required"})
                    return
                elif event_type == "error":
                    saw_error = True
                    yield _sse("error", {
                        "session_id": session_id,
                        "error": stream_event.get("error", "Streaming failed")})
                elif event_type == "done":
                    pass  # delivered below, once, with the final payload
            response = chat_agent.last_stream_response
            if response is not None:
                # Same post-delivery as the non-streaming endpoint: socket
                # emit to the room, push hook, lead capture, end-chat close.
                with SessionLocal() as fresh_db:
                    await deliver_ai_reply("", session_dict, response, fresh_db,
                                           chat_agent=chat_agent)
                final = reply_payload(response, session_id, chat_agent)
                final["source"] = last_source
                yield _sse("done", final)
            elif not saw_error:
                yield _sse("error", {
                    "session_id": session_id,
                    "error": "No reply was produced"})
        except Exception as gen_err:
            logger.error(f"Mobile stream failed for session {session_id}: {gen_err}")
            yield _sse("error", {"session_id": session_id, "error": str(gen_err)})
        finally:
            if chat_agent is not None:
                await chat_agent.safe_cleanup_mcp_tools()

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _stored_visitor_message(db: Session, session_id: str):
    """The latest visitor message the agent just stored, formatted. Used by
    the AI/workflow branches, where the agent (not this module) stores it."""
    from app.models.chat_history import ChatHistory
    row = db.query(ChatHistory).filter(
        ChatHistory.session_id == uuid.UUID(str(session_id)),
        ChatHistory.message_type == "user",
    ).order_by(ChatHistory.id.desc()).first()
    if row is None:
        return None
    return _format_message(await _reload_message(db, row.id))


async def _reload_message(db: Session, message_id: int):
    """The stored row with attachments/user/agent joined, for formatting.
    Attachment URLs are signed, as get_session_history does (via
    set_committed_value, so the signed URL never overwrites the stored path)."""
    from app.models.chat_history import ChatHistory
    from app.services.file_storage import resolve_public_url
    from sqlalchemy.orm import joinedload
    from sqlalchemy.orm.attributes import set_committed_value
    msg = db.query(ChatHistory).options(
        joinedload(ChatHistory.user),
        joinedload(ChatHistory.agent),
        joinedload(ChatHistory.attachments),
    ).filter(ChatHistory.id == message_id).first()
    if msg:
        for attachment in msg.attachments or []:
            if attachment.file_url:
                set_committed_value(
                    attachment, "file_url", await resolve_public_url(attachment.file_url))
    return msg


async def _relay_to_agents(session_id: str, message_id: int, message: str,
                           uploaded_files: list, user_id: Optional[str] = None):
    """Tell the inbox (and any agent socket in the room) about the visitor's
    message — the widget's human-taken-over branch, minus its own store."""
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    attachments = await _client_attachments(uploaded_files)
    payload = {
        "message": message, "message_id": message_id, "type": "user_message",
        "transfer_to_human": False, "session_id": session_id,
        "timestamp": timestamp, "created_at": timestamp,
        "attachments": attachments if attachments else None,
    }
    if user_id:
        await sio.emit("chat_reply", payload, room=f"user_{user_id}", namespace="/agent")
    await sio.emit("chat_reply", payload, room=session_id, namespace="/agent")


async def _transferred_notice(db, session_dict, session_id, org_id, customer_id, message):
    """The queued-for-human notice the widget writes when a transferred chat
    with no human yet gets another message."""
    chat_repo = ChatRepository(db)
    chat_history = await chat_repo.get_session_history(session_id)
    jira_repo = JiraRepository(db)
    agent_data = jira_repo.get_agent_with_jira_config(session_dict["agent_id"]) \
        if session_dict.get("agent_id") else None
    queued_notice_source = {}
    if agent_data is not None and getattr(agent_data, "groups", None):
        availability_response = await get_agent_availability_response(
            agent=agent_data, customer_id=customer_id, chat_history=chat_history, db=db,
            api_key=decrypt_api_key(session_dict["ai_config"].encrypted_api_key),
            model_name=session_dict["ai_config"].model_name,
            model_type=session_dict["ai_config"].model_type,
            base_url=(session_dict["ai_config"].settings or {}).get("base_url"),
            session_id=session_id)
    else:
        availability_response = {"message": QUEUED_FOR_HUMAN_NOTICE}
        queued_notice_source = {"source": MessageSource.SYSTEM.value}
    response = ChatResponse(
        message=availability_response["message"], transfer_to_human=True,
        transfer_reason=None, transfer_description=None, end_chat=False,
        request_rating=False, create_ticket=False)
    chat_repo.create_message({
        "message": response.message,
        "message_type": "bot",
        "session_id": session_id,
        "organization_id": org_id,
        "agent_id": session_dict["agent_id"],
        "customer_id": customer_id,
        "attributes": {
            "transfer_to_human": True,
            "transfer_reason": None,
            "transfer_description": None,
            "end_chat": False,
            "end_chat_reason": None,
            "end_chat_description": None,
            "request_rating": False,
            "shopify_output": None,
            **queued_notice_source,
        },
    })
    return response


# --------------------------------------------------------------------------
# 4. Register a device for push
# --------------------------------------------------------------------------

@router.post("/conversations/{conversation_id}/devices")
async def register_device(
    conversation_id: str,
    body: RegisterDeviceRequest,
    request: Request,
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    payload = _require_token(token)
    session, _scope = _load_own_session(db, conversation_id, payload, request)
    if body.platform not in ("android", "ios"):
        raise HTTPException(status_code=422, detail="platform must be 'android' or 'ios'")
    if not (body.push_token or "").strip():
        raise HTTPException(status_code=422, detail="push_token is required")
    device = db.query(MobileDeviceToken).filter(
        MobileDeviceToken.session_id == session.session_id,
        MobileDeviceToken.push_token == body.push_token.strip(),
    ).first()
    if device:
        device.platform = body.platform
        device.is_active = True
    else:
        device = MobileDeviceToken(
            session_id=session.session_id,
            organization_id=session.organization_id,
            push_token=body.push_token.strip(),
            platform=body.platform,
        )
        db.add(device)
    db.commit()
    db.refresh(device)
    return {"ok": True, "device_id": str(device.id), "platform": device.platform}


# --------------------------------------------------------------------------
# 5. Push attempts (debug / test-mode)
# --------------------------------------------------------------------------

@router.get("/push-attempts")
async def list_push_attempts(
    request: Request,
    conversation_id: str = Query(...),
    token: Optional[str] = Header(None, alias=TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    payload = _require_token(token)
    session, _scope = _load_own_session(db, conversation_id, payload, request)
    attempts = db.query(PushAttempt).filter(
        PushAttempt.session_id == session.session_id
    ).order_by(PushAttempt.created_at.desc(), PushAttempt.id.desc()).limit(100).all()
    return {
        "conversation_id": str(session.session_id),
        "attempts": [
            {
                "id": a.id,
                "conversation_id": str(a.session_id),
                "device_token_prefix": a.device_token_prefix,
                "title": a.title,
                "body": a.body,
                "status": a.status,
                "reason": a.reason,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in attempts
        ],
    }


# --------------------------------------------------------------------------
# 6. Report offensive AI content (Google Play AI-Generated Content policy)
# --------------------------------------------------------------------------

# Reports are low-frequency by nature, but the endpoint is public and
# unauthenticated, so it still gets a per-IP sliding window. Fail-open
# without Redis (sliding_window_allow allows), same as the message limits.
REPORTS_PER_IP_PER_HOUR = 10


@router.post("/reports")
async def report_ai_content(
    body: ReportRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """A visitor reports offensive AI content, without leaving the app.

    Google Play's AI-Generated Content policy requires apps with generative
    AI to offer in-app reporting of offensive AI content. Public, no login:
    the widget ID is public, the same trust level as the widget itself, so
    this takes no conversation token, no AI config and no admin auth — just
    a widget that exists. Stored in mobile_reports; org admins read them
    through GET /api/v1/admin/mobile/reports.
    """
    widget = db.query(Widget).filter(Widget.id == body.widget_id).first()
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")
    ip = mobile_security.client_ip(request)
    allowed, retry_after = mobile_security.sliding_window_allow(
        f"mobile:reports:ip:{ip}", REPORTS_PER_IP_PER_HOUR, 3600)
    if not allowed:
        mobile_security.record_abuse_event(
            db, event_type="rate_limited", organization_id=widget.organization_id,
            widget_id=widget.id, ip=ip,
            detail=f"Too many AI-content reports from this address "
                   f"({REPORTS_PER_IP_PER_HOUR}/hour)")
        raise HTTPException(
            status_code=429,
            detail="Too many reports from this address; please try again later",
            headers={"Retry-After": str(retry_after)})
    row = MobileReport(
        widget_id=widget.id,
        organization_id=widget.organization_id,
        reason=body.reason,
        details=(body.details or "").strip() or None,
        source=(body.source or "").strip() or None,
        ip_address=ip,
    )
    db.add(row)
    db.commit()
    return {"received": True}


