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

"""Tests for the Flutter SDK REST API (app/api/mobile_chat.py) and the push
service (app/services/push_service.py).

Same layout as the other API tests: a standalone FastAPI app with the mobile
router, get_db overridden to the shared sqlite `db` fixture. The AI turn is
mocked at the mobile_chat module boundary (create_widget_agent /
deliver_ai_reply): the fake agent answers, and the fake deliver stores the
bot message, so the polling path is exercised end to end without a model.
Live-model behaviour (image/PDF understanding) is verified against the real
backend with curl, not here.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.chat_agent import ChatResponse
from app.api import mobile_chat
from app.database import get_db
from app.models.file_attachment import FileAttachment
from app.models.mobile import BlockedVisitor, MobileDeviceToken, MobileWidgetSecurity, PushAttempt
from app.repositories.chat import ChatRepository
from app.services.media_ai import MediaTurn
from app.services.push_service import maybe_push_reply

test_app = FastAPI()
test_app.include_router(mobile_chat.router, prefix="/api/v1/mobile")


@pytest.fixture
def client(db):
    def _override_db():
        yield db

    test_app.dependency_overrides[get_db] = _override_db
    with TestClient(test_app) as c:
        yield c
    test_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def redis_ready(monkeypatch):
    """Initialise the shared Redis client BEFORE any test mints a token.

    Token minting stores the jti in Redis only when the client is already
    initialised; verification fails closed once it is. Without this, a token
    minted before the first get_redis() call would stop verifying the moment
    another test initialised the client. Redis-dependent tests skip when it
    is unavailable (fail-open in the implementation, but the test cannot
    observe a limit firing).
    """
    from app.core.config import settings
    from app.core.redis import get_redis
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    get_redis()


def _redis_available() -> bool:
    from app.core.redis import get_redis
    return get_redis() is not None


def _start(client, widget, **overrides):
    body = {"widget_id": str(widget.id), "visitor_name": "Mobile Tester"}
    body.update(overrides)
    resp = client.post("/api/v1/mobile/conversations", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(token):
    return {"X-Conversation-Token": token}


class FakeAgent:
    """Stand-in for ChatAgent: answers, stores nothing itself."""

    def __init__(self, session_id, db):
        self.agent = type("A", (), {"session_id": session_id})()
        self.last_bot_message_id = None
        self._db = db

    async def get_response(self, **kwargs):
        # Mirror the real ChatAgent: it stores the visitor message (and the
        # files that came with it) before answering — the route relies on it.
        db = self._db
        org = uuid.UUID(str(kwargs["org_id"]))
        cust = uuid.UUID(str(kwargs["customer_id"]))
        msg = ChatRepository(db).create_message({
            "message": kwargs["message"], "message_type": "user",
            "session_id": uuid.UUID(str(kwargs["session_id"])),
            "organization_id": org, "agent_id": kwargs["agent_id"],
            "customer_id": cust,
        })
        for f in kwargs.get("attachments") or []:
            db.add(FileAttachment(
                file_url=f["file_url"], filename=f["filename"],
                content_type=f["content_type"], file_size=f["size"],
                chat_history_id=msg.id, organization_id=org,
                uploaded_by_customer_id=cust))
        db.commit()
        return ChatResponse(message="Fake AI reply", transfer_to_human=False,
                            end_chat=False, request_rating=False, create_ticket=False)

    async def safe_cleanup_mcp_tools(self):
        return None


@pytest.fixture
def fake_ai(db):
    """Patch the AI turn: fake agent + a deliver that stores the bot reply,
    so GET /messages shows it like the real turn would."""

    async def _fake_deliver(sid, session, response, db_session, chat_agent=None):
        stored = ChatRepository(db_session).create_message({
            "message": response.message,
            "message_type": "bot",
            "session_id": session["session_id"],
            "organization_id": session["org_id"],
            "agent_id": session["agent_id"],
            "customer_id": session["customer_id"],
        })
        if chat_agent is not None:
            chat_agent.last_bot_message_id = stored.id

    async def _fake_create_agent(kwargs):
        return FakeAgent(kwargs["session_id"], db)

    with (patch.object(mobile_chat, "emit_bot_typing", new=AsyncMock()),
          patch.object(mobile_chat, "create_widget_agent", new=AsyncMock(side_effect=_fake_create_agent)),
          patch.object(mobile_chat, "deliver_ai_reply", new=AsyncMock(side_effect=_fake_deliver))):
        yield


# --- 1. Start / resume -------------------------------------------------------

def test_start_conversation_creates_and_resumes(client, test_widget, test_ai_config):
    first = _start(client, test_widget, visitor_email="mobile@example.com")
    assert first["created"] is True
    assert first["conversation_id"] and first["conversation_token"]
    assert first["agent_name"]

    resumed = _start(client, test_widget, conversation_token=first["conversation_token"])
    assert resumed["created"] is False
    assert resumed["conversation_id"] == first["conversation_id"]
    assert resumed["conversation_token"] == first["conversation_token"]


def test_start_unknown_widget_404(client):
    resp = client.post("/api/v1/mobile/conversations",
                       json={"widget_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 404


def test_start_without_ai_config_422(client, test_widget):
    # No test_ai_config fixture here: the widget cannot answer, so starting
    # a conversation is refused like the widget socket refuses.
    resp = client.post("/api/v1/mobile/conversations", json={"widget_id": str(test_widget.id)})
    assert resp.status_code == 422


# --- 2. Messages -------------------------------------------------------------

def test_messages_require_token(client, test_widget, test_ai_config):
    conv = _start(client, test_widget)
    assert client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages").status_code == 401
    assert client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers=_headers("bogus")).status_code == 401
    resp = client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers=_headers(conv["conversation_token"]))
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_send_text_runs_ai_turn_and_polls_back(client, test_widget, test_ai_config, fake_ai):
    conv = _start(client, test_widget)
    token = conv["conversation_token"]
    resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                       headers=_headers(token), data={"message": "Hello from the app"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"]["sender"] == "visitor"
    assert body["message"]["text"] == "Hello from the app"
    assert body["ai_reply_pending"] is True

    history = client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                         headers=_headers(token)).json()["messages"]
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    assert history[1]["text"] == "Fake AI reply"

    # `after` returns only newer messages.
    after = client.get(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        params={"after": history[0]["id"]}, headers=_headers(token)).json()["messages"]
    assert [m["id"] for m in after] == [history[1]["id"]]


def test_send_empty_message_422(client, test_widget, test_ai_config):
    conv = _start(client, test_widget)
    resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                       headers=_headers(conv["conversation_token"]), data={"message": "   "})
    assert resp.status_code == 422


def test_send_with_file_uses_media_context(client, test_widget, test_agent, test_ai_config, fake_ai, db):
    # The test agent is created with attachments off; the widget refuses
    # files until it is on.
    test_agent.allow_attachments = True
    db.commit()
    conv = _start(client, test_widget)
    token = conv["conversation_token"]
    seen = {}

    async def _fake_prepare(files, *, api_key, model_type, model_name=None):
        seen["files"] = files
        return MediaTurn(context_text="[Image 'order.png' attached below]", images=[])

    async def _fake_upload(*args, **kwargs):
        return {"file_url": "/uploads/test/order.png", "filename": "order.png",
                "content_type": "image/png", "size": 4}

    with (patch.object(mobile_chat, "prepare_media_context", new=AsyncMock(side_effect=_fake_prepare)),
          patch.object(mobile_chat.FileUploadService, "upload_file", new=AsyncMock(side_effect=_fake_upload))):
        resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                           headers=_headers(token),
                           data={"message": "What is my total?"},
                           files={"files": ("order.png", b"\x89PNG fake", "image/png")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"]["attachments"][0]["filename"] == "order.png"
    # The raw bytes reached media_ai (the widget's _media_inputs pairing).
    assert seen["files"][0]["filename"] == "order.png"
    assert seen["files"][0]["content_bytes"] == b"\x89PNG fake"


def test_send_to_other_customers_conversation_404(client, test_widget, test_ai_config):
    first = _start(client, test_widget, visitor_email="one@example.com")
    second = _start(client, test_widget, visitor_email="two@example.com")
    resp = client.post(f"/api/v1/mobile/conversations/{second['conversation_id']}/messages",
                       headers=_headers(first["conversation_token"]),
                       data={"message": "hijack"})
    assert resp.status_code == 404


# --- 3. Devices + push attempts ----------------------------------------------

def test_register_device_upserts(client, test_widget, test_ai_config, db):
    conv = _start(client, test_widget)
    url = f"/api/v1/mobile/conversations/{conv['conversation_id']}/devices"
    headers = _headers(conv["conversation_token"])
    first = client.post(url, json={"push_token": "fcm-token-abc", "platform": "android"}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["ok"] is True
    second = client.post(url, json={"push_token": "fcm-token-abc", "platform": "android"}, headers=headers)
    assert second.json()["device_id"] == first.json()["device_id"]
    assert db.query(MobileDeviceToken).count() == 1

    assert client.post(url, json={"push_token": "x", "platform": "web"}, headers=headers).status_code == 422
    assert client.post(url, json={"push_token": "x", "platform": "ios"},
                       headers=_headers("bogus")).status_code == 401


@pytest.mark.asyncio
async def test_push_recorded_when_no_fcm_credentials(db, test_widget, test_ai_config, test_customer):
    from app.repositories.session_to_agent import SessionToAgentRepository
    session_id = SessionToAgentRepository(db).create_session(
        session_id="11111111-2222-3333-4444-555555555555",
        agent_id=test_widget.agent_id, customer_id=test_customer.id,
        organization_id=test_widget.organization_id, channel="web")
    db.add(MobileDeviceToken(session_id=session_id.session_id,
                             organization_id=test_widget.organization_id,
                             push_token="fcm-test-token-1234567890", platform="android"))
    db.commit()
    await maybe_push_reply(db, session_id=str(session_id.session_id),
                           organization_id=test_widget.organization_id,
                           message_id=42, title="Test Agent", text="Hello from the AI")
    attempts = db.query(PushAttempt).all()
    assert len(attempts) == 1
    assert attempts[0].status == "recorded_no_credentials"
    assert attempts[0].device_token_prefix == "fcm-test-tok"
    assert attempts[0].message_id == 42

    # Second delivery of the same reply does not push twice.
    await maybe_push_reply(db, session_id=str(session_id.session_id),
                           organization_id=test_widget.organization_id,
                           message_id=42, title="Test Agent", text="Hello from the AI")
    assert db.query(PushAttempt).count() == 1


@pytest.mark.asyncio
async def test_push_skipped_when_live_socket(db, test_widget, test_ai_config, test_customer):
    from app.repositories.session_to_agent import SessionToAgentRepository
    session_id = SessionToAgentRepository(db).create_session(
        session_id="22222222-2222-3333-4444-555555555555",
        agent_id=test_widget.agent_id, customer_id=test_customer.id,
        organization_id=test_widget.organization_id, channel="web")
    db.add(MobileDeviceToken(session_id=session_id.session_id,
                             organization_id=test_widget.organization_id,
                             push_token="fcm-live-token-1234567890", platform="ios"))
    db.commit()
    with patch("app.services.push_service.is_live_socket", return_value=True):
        await maybe_push_reply(db, session_id=str(session_id.session_id),
                               organization_id=test_widget.organization_id,
                               message_id=7, title="Test Agent", text="live reply")
    attempts = db.query(PushAttempt).all()
    assert len(attempts) == 1
    assert attempts[0].status == "skipped_live_socket"


def test_push_attempts_endpoint(client, test_widget, test_ai_config, db):
    import uuid as _uuid
    conv = _start(client, test_widget)
    db.add(PushAttempt(session_id=_uuid.UUID(conv["conversation_id"]), message_id=1,
                       device_token_prefix="fcm-test-tok", title="T", body="B",
                       status="recorded_no_credentials", reason="test mode"))
    db.commit()
    resp = client.get("/api/v1/mobile/push-attempts",
                      params={"conversation_id": conv["conversation_id"]},
                      headers=_headers(conv["conversation_token"]))
    assert resp.status_code == 200
    attempts = resp.json()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "recorded_no_credentials"


# --- 4. Phase 1 security ------------------------------------------------------

def test_start_issues_short_lived_jwt(client, test_widget, test_ai_config):
    conv = _start(client, test_widget, device_id="device-123")
    assert conv["created"] is True
    assert conv["expires_in"] == 3600
    # The token carries the conversation + widget + device claims.
    from app.core.security import verify_conversation_token
    payload = verify_conversation_token(conv["conversation_token"])
    assert payload is not None
    assert payload["widget_id"] == str(test_widget.id)
    assert payload["session_id"] == conv["conversation_id"]
    assert payload["device_id"] == "device-123"
    assert payload["type"] == "conversation"


def test_rotate_revokes_old_token(client, test_widget, test_ai_config):
    if not _redis_available():
        pytest.skip("Redis unavailable; revocation cannot be observed")
    conv = _start(client, test_widget, device_id="device-123")
    old = conv["conversation_token"]
    resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/rotate",
                       headers={**_headers(old), "X-Device-Id": "device-123"})
    assert resp.status_code == 200, resp.text
    new = resp.json()["conversation_token"]
    assert new != old
    assert resp.json()["expires_in"] == 3600
    # The old token is revoked: it no longer verifies.
    assert client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers=_headers(old)).status_code == 401
    # The new token works.
    assert client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers={**_headers(new), "X-Device-Id": "device-123"}).status_code == 200


def test_device_mismatch_401(client, test_widget, test_ai_config):
    conv = _start(client, test_widget, device_id="device-123")
    resp = client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers={**_headers(conv["conversation_token"]), "X-Device-Id": "device-999"})
    assert resp.status_code == 401


def test_rate_limit_429_with_retry_after(client, test_widget, test_ai_config, fake_ai, db):
    if not _redis_available():
        pytest.skip("Redis unavailable; rate limits fail open")
    # Per-widget override (admin-settable): 2 messages/min for this widget.
    db.add(MobileWidgetSecurity(widget_id=test_widget.id,
                                organization_id=test_widget.organization_id,
                                msg_per_min=2))
    db.commit()
    conv = _start(client, test_widget)
    headers = _headers(conv["conversation_token"])
    url = f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages"
    assert client.post(url, headers=headers, data={"message": "one"}).status_code == 200
    assert client.post(url, headers=headers, data={"message": "two"}).status_code == 200
    third = client.post(url, headers=headers, data={"message": "three"})
    assert third.status_code == 429
    assert "retry-after" in third.headers
    assert int(third.headers["retry-after"]) >= 1


def test_identical_repeat_is_challenged(client, test_widget, test_ai_config, fake_ai, db):
    conv = _start(client, test_widget, device_id="test-device")
    headers = _headers(conv["conversation_token"])
    url = f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages"
    assert client.post(url, headers=headers, data={"message": "hello hello"}).status_code == 200
    # Same text again: spam signal, slowed down (429, not stored).
    assert client.post(url, headers=headers, data={"message": "hello hello"}).status_code == 429
    # The challenge row in the abuse log carries the device fingerprint
    # ([fp=...]) — the raw device id never lands in the log. (TestClient's
    # client IP is "testclient".)
    from app.models.mobile import MobileAbuseEvent
    from app.services import mobile_security
    row = db.query(MobileAbuseEvent).filter(
        MobileAbuseEvent.event_type == "challenge").order_by(
        MobileAbuseEvent.created_at.desc()).first()
    assert row is not None
    expected = mobile_security.device_fingerprint("test-device", "testclient")
    assert f"[fp={expected}]" in (row.detail or "")
    assert "test-device" not in (row.detail or "")


def test_blocked_visitor_403(client, test_widget, test_ai_config, db):
    import uuid as _uuid
    conv = _start(client, test_widget, visitor_email="blocked@example.com")
    db.add(BlockedVisitor(organization_id=test_widget.organization_id,
                          customer_id=_uuid.UUID(conv["customer_id"]), reason="spam"))
    db.commit()
    # Starting again as the same customer is refused...
    resp = client.post("/api/v1/mobile/conversations",
                       json={"widget_id": str(test_widget.id),
                             "visitor_email": "blocked@example.com"})
    assert resp.status_code == 403
    # ...and the existing conversation is closed to them too.
    assert client.get(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                      headers=_headers(conv["conversation_token"])).status_code == 403


def test_kill_switch_403(client, test_widget, test_ai_config, db):
    db.add(MobileWidgetSecurity(widget_id=test_widget.id,
                                organization_id=test_widget.organization_id,
                                chat_disabled=True))
    db.commit()
    resp = client.post("/api/v1/mobile/conversations", json={"widget_id": str(test_widget.id)})
    assert resp.status_code == 403


def test_oversize_image_422(client, test_widget, test_ai_config, fake_ai, db):
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == test_widget.agent_id).first()
    agent.allow_attachments = True
    db.commit()
    conv = _start(client, test_widget)
    # 6MB PNG: valid magic bytes, over the 5MB image cap.
    big_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024)
    resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                       headers=_headers(conv["conversation_token"]),
                       data={"message": "big image"},
                       files={"files": ("big.png", big_png, "image/png")})
    assert resp.status_code == 422
    assert "5" in resp.text  # the cap is named in the error


def test_executable_upload_422(client, test_widget, test_ai_config, fake_ai, db):
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == test_widget.agent_id).first()
    agent.allow_attachments = True
    db.commit()
    conv = _start(client, test_widget)
    resp = client.post(f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
                       headers=_headers(conv["conversation_token"]),
                       data={"message": "run this"},
                       files={"files": ("evil.exe", b"MZ" + b"\x00" * 100, "application/x-msdownload")})
    assert resp.status_code == 422
