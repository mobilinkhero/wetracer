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

"""Firebase push for mobile clients (no WebView).

When an AI or human reply is delivered to a conversation, `maybe_push_reply`
decides whether the visitor needs a push:

- No registered mobile devices -> nothing to do.
- A live Socket.IO client is connected to the conversation room -> no push
  (the reply already arrived live; a push would double-notify).
- FCM credentials configured for the org -> send one FCM message per device
  via firebase-admin (named app per org, so several orgs can each hold their
  own service account).
- No FCM credentials (test mode) -> NEVER fail the chat. The would-be push is
  recorded in `mobile_push_attempts` with status `recorded_no_credentials`,
  queryable via GET /api/v1/mobile/push-attempts.

Every decision is recorded per device, so the push-attempts endpoint tells
the truth about what happened. Nothing here ever raises into the chat path:
callers wrap it, and this module itself swallows errors after logging them.

Hook points (the only two places a reply reaches a visitor):
- AI replies: app/services/widget_reply.py `deliver_ai_reply` (after the
  `chat_response` socket emit).
- Human replies: app/services/message_delivery.py `deliver_to_customer`
  (after the `chat_response` socket emit; the inbox agent-message handler in
  app/api/widget_chat.py, channel replies and ticket replies all flow
  through here). Dedupe on (session_id, message_id) keeps an AI reply that
  travelled both paths from pushing twice.
"""

import json
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.core.security import decrypt_api_key
from app.core.socketio import sio
from app.models.mobile import MobileDeviceToken, MobileFcmConfig, PushAttempt

logger = get_logger(__name__)

# Statuses recorded in mobile_push_attempts.status (see API_CONTRACT.md §6).
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_RECORDED_NO_CREDENTIALS = "recorded_no_credentials"
STATUS_SKIPPED_LIVE_SOCKET = "skipped_live_socket"

# The notification body carries the start of the reply, not all of it.
BODY_PREVIEW_CHARS = 120
TOKEN_PREFIX_CHARS = 12


def is_live_socket(session_id: str) -> bool:
    """True when at least one Socket.IO client is in the conversation's room.

    The web widget and a mobile app both join room `session_id` on the
    `/widget` namespace when they connect. On any doubt (manager shape
    changed, server not started) assume live: a missed push is better than a
    push that duplicates a reply the visitor already saw live.
    """
    try:
        rooms = sio.manager.rooms.get("/widget", {})
        members = rooms.get(str(session_id)) or set()
        return len(members) > 0
    except Exception as e:
        logger.warning(f"Could not check live sockets for {session_id}: {e}")
        return True


def _record(db: Session, *, session_id, message_id, device: MobileDeviceToken,
            title: str, body: str, status: str, reason: Optional[str] = None) -> PushAttempt:
    attempt = PushAttempt(
        session_id=session_id,
        message_id=message_id,
        device_token_prefix=(device.push_token or "")[:TOKEN_PREFIX_CHARS],
        title=title,
        body=body,
        status=status,
        reason=reason,
    )
    db.add(attempt)
    return attempt


def _already_recorded(db: Session, session_id, message_id: Optional[int]) -> bool:
    """A push for this exact reply was already decided (sent or recorded).

    Guards the AI path, which can reach both hook points: deliver_ai_reply
    emits directly, while channel/ticket delivery also passes through
    deliver_to_customer. Without this the visitor would get two pushes.
    """
    if message_id is None:
        return False
    return db.query(PushAttempt).filter(
        PushAttempt.session_id == session_id,
        PushAttempt.message_id == message_id,
        PushAttempt.status.in_([STATUS_SENT, STATUS_FAILED, STATUS_RECORDED_NO_CREDENTIALS]),
    ).first() is not None


def _fcm_config(db: Session, organization_id) -> Optional[dict]:
    """The org's decrypted FCM service-account JSON, or None when unconfigured."""
    if not organization_id:
        return None
    row = db.query(MobileFcmConfig).filter(
        MobileFcmConfig.organization_id == organization_id
    ).first()
    if not row:
        return None
    try:
        return json.loads(decrypt_api_key(row.encrypted_service_account_json))
    except Exception as e:
        logger.error(f"Could not decrypt FCM config for org {organization_id}: {e}")
        return None


def _send_via_fcm(service_account: dict, organization_id, device: MobileDeviceToken,
                  title: str, body: str, session_id: str, message_id: Optional[int]) -> None:
    """Send one FCM message. Raises on failure; the caller records it."""
    import firebase_admin
    from firebase_admin import credentials, messaging

    app_name = f"chattermate-mobile-{organization_id}"
    try:
        app = firebase_admin.get_app(app_name)
    except ValueError:
        app = firebase_admin.initialize_app(credentials.Certificate(service_account), name=app_name)
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={
            "conversation_id": str(session_id),
            "message_id": str(message_id) if message_id is not None else "",
            "type": "chat_response",
        },
        token=device.push_token,
    )
    messaging.send(message, app=app)


async def maybe_push_reply(db: Session, *, session_id, organization_id,
                           message_id: Optional[int], title: str, text: str) -> None:
    """Push a delivered reply to the conversation's devices, if it needs one.

    Never raises: push is best-effort and must never break the chat turn
    that just delivered the reply.
    """
    try:
        # Callers pass the id as a string; the columns hold UUIDs, and the
        # SQLite test schema only accepts UUID objects — coerce once, here.
        session_id = uuid.UUID(str(session_id))
        devices = db.query(MobileDeviceToken).filter(
            MobileDeviceToken.session_id == session_id,
            MobileDeviceToken.is_active.is_(True),
        ).all()
        if not devices:
            return
        if _already_recorded(db, session_id, message_id):
            return

        body = (text or "").strip()
        preview = body[:BODY_PREVIEW_CHARS]
        title = (title or "ChatterMate").strip() or "ChatterMate"

        if is_live_socket(session_id):
            for device in devices:
                _record(db, session_id=session_id, message_id=message_id, device=device,
                        title=title, body=preview, status=STATUS_SKIPPED_LIVE_SOCKET,
                        reason="A live socket client is connected to the conversation")
            db.commit()
            return

        service_account = _fcm_config(db, organization_id)
        if not service_account:
            for device in devices:
                _record(db, session_id=session_id, message_id=message_id, device=device,
                        title=title, body=preview, status=STATUS_RECORDED_NO_CREDENTIALS,
                        reason="No FCM credentials configured — test mode, push recorded only")
            db.commit()
            logger.info(f"Push recorded (no FCM credentials) for session {session_id}: {len(devices)} device(s)")
            return

        for device in devices:
            try:
                _send_via_fcm(service_account, organization_id, device, title, preview,
                              str(session_id), message_id)
                _record(db, session_id=session_id, message_id=message_id, device=device,
                        title=title, body=preview, status=STATUS_SENT)
            except Exception as send_err:
                logger.warning(f"FCM send failed for session {session_id}: {send_err}")
                _record(db, session_id=session_id, message_id=message_id, device=device,
                        title=title, body=preview, status=STATUS_FAILED, reason=str(send_err))
        db.commit()
    except Exception as e:
        # Push must never fail the chat: roll back the attempt rows and move on.
        logger.error(f"Push handling failed for session {session_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
