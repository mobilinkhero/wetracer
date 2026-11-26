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

"""End-to-end API tests for AI auto-tagging of conversations (QA agent).

Covers the whole loop the inbox depends on: a visitor message goes in
through the public mobile API, the tagger hook (ChatRepository.create_message
-> conversation_tagger.apply_tags_to_conversation) tags the conversation,
and the admin inbox shows the tags, filters by them and sorts by priority.
Manual add/remove go through the admin tags API.

CONTRACT — the builders' landed implementation (read from
app/services/tag_taxonomy.py, app/services/conversation_tagger.py,
app/api/conversation_tags.py, app/api/chat.py, app/repositories/chat.py):

Storage: SessionToAgent.tags, a JSON list of slugs.
Taxonomy slugs + weights (heaviest first):
    payment-received 100, urgent 90, angry 85, needs-human 80,
    refund-request 75, payment-proof 70, delivery-pending 65,
    payment-pending 60, vip 55, follow-up-needed 50, waiting-customer 40,
    repeat-customer 20, svc-* 10 (svc-sim-owner, svc-cdr, svc-imei,
    svc-sim-location, svc-cnic-copy, svc-family-tree, svc-travel-history,
    svc-whatsapp-cdr, svc-passport, svc-all-network),
    delivered 0, spam 0.
Priority = the heaviest tag's weight; sort=priority orders by
(priority_score, updated_at) descending.

Tagger rules (verified against suggest_tags): a message that already paid
is never also payment-pending — payment-received drops payment-pending and
adds delivery-pending. "I have not paid" does not tag payment-received
(unless a transaction id is present). WhatsApp CDR tags svc-whatsapp-cdr
(and vip), never plain svc-cdr. An image/PDF sent with payment words adds
payment-proof. Service tags use the svc-* slugs; the status tag for a
waiting customer is waiting-customer.

Endpoints:
    GET  /api/v1/chats/recent?tag=<slug> — ONE tag; unknown tag -> 422
    GET  /api/v1/chats/recent?sort=priority — priority sort
    GET  /api/v1/chats/recent — rows carry "tags" + "priority_score"
    GET  /api/v1/admin/tags — every defined tag (no auth)
    GET  /api/v1/admin/conversations/{id}/tags — {"tags", "priority_score"}
    POST /api/v1/admin/conversations/{id}/tags {"tag"|"slug"} -> 200
    DELETE /api/v1/admin/conversations/{id}/tags/{slug} -> 200
    DELETE /api/v1/admin/conversations/{id}/tags {"slug"} -> 200
    Unknown tag -> 422; another org's conversation -> 404 (never a leak).
    Chat detail (GET /api/v1/chats/{id}) does NOT carry tags — the admin
    get-tags endpoint is the per-conversation read.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.chat_agent import ChatResponse
from app.api import chat as chat_router
from app.api import conversation_tags as admin_tags_router
from app.api import mobile_chat
from app.core.auth import get_current_user, get_unified_chat_auth
from app.core.security import encrypt_api_key
from app.database import get_db
from app.models.agent import AgentType
from app.models.ai_config import AIConfig, AIModelType
from app.models.file_attachment import FileAttachment
from app.models.organization import Organization
from app.models.schemas.widget import WidgetCreate
from app.repositories.agent import AgentRepository
from app.repositories.chat import ChatRepository
from app.repositories.widget import WidgetRepository
from app.services.media_ai import MediaTurn

# One app with all three routers: a conversation created through the public
# mobile API is read back through the admin inbox and the admin tags API.
test_app = FastAPI()
test_app.include_router(mobile_chat.router, prefix="/api/v1/mobile")
test_app.include_router(chat_router.router, prefix="/api/v1/chats")
test_app.include_router(admin_tags_router.router, prefix="/api/v1/admin")

# --- taxonomy under test (slugs + weights from tag_taxonomy.py) ---------------

PAYMENT_RECEIVED = "payment-received"
PAYMENT_PENDING = "payment-pending"
PAYMENT_PROOF = "payment-proof"
DELIVERY_PENDING = "delivery-pending"
REFUND_REQUEST = "refund-request"
URGENT = "urgent"
ANGRY = "angry"
NEEDS_HUMAN = "needs-human"
SVC_SIM_OWNER = "svc-sim-owner"
SVC_CDR = "svc-cdr"
SVC_IMEI = "svc-imei"
SVC_WHATSAPP_CDR = "svc-whatsapp-cdr"
VIP = "vip"
SPAM = "spam"


@pytest.fixture
def client(db, test_organization, test_user):
    """Test client with the shared sqlite db, an admin inbox identity (for
    GET /recent) and a dashboard user (for /api/v1/admin/...)."""
    org_id = test_organization.id

    async def override_get_unified_chat_auth():
        return {
            "auth_type": "jwt",
            "organization_id": org_id,
            "user_id": test_user.id,
            "current_user": test_user,
            "can_view_all": True,
            "can_view_assigned": True,
            "can_view_unassigned": True,
        }

    async def override_get_current_user():
        return test_user

    def _override_db():
        yield db

    test_app.dependency_overrides[get_db] = _override_db
    test_app.dependency_overrides[get_unified_chat_auth] = override_get_unified_chat_auth
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    with TestClient(test_app) as c:
        yield c
    test_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def redis_ready(monkeypatch):
    """Same as tests/api/test_mobile_chat.py: initialise the shared Redis
    client before any test mints a conversation token."""
    from app.core.config import settings
    from app.core.redis import get_redis
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    get_redis()


class FakeAgent:
    """Stand-in for ChatAgent (from tests/api/test_mobile_chat.py): stores
    the visitor message and answers with a fixed line. Forwards the
    attachments through to create_message exactly like the real ChatAgent
    does, so the tagger sees files sent with the message (payment proof)."""

    def __init__(self, session_id, db):
        self.agent = type("A", (), {"session_id": session_id})()
        self.last_bot_message_id = None
        self._db = db

    async def get_response(self, **kwargs):
        db = self._db
        org = uuid.UUID(str(kwargs["org_id"]))
        cust = uuid.UUID(str(kwargs["customer_id"]))
        ChatRepository(db).create_message({
            "message": kwargs["message"], "message_type": "user",
            "session_id": uuid.UUID(str(kwargs["session_id"])),
            "organization_id": org, "agent_id": kwargs["agent_id"],
            "customer_id": cust,
            "attachments": kwargs.get("attachments"),
        })
        db.commit()
        return ChatResponse(message="Fake AI reply", transfer_to_human=False,
                            end_chat=False, request_rating=False, create_ticket=False)

    async def safe_cleanup_mcp_tools(self):
        return None


@pytest.fixture
def fake_ai(db):
    """Patch the AI turn so sends complete without a model."""
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


# --- helpers -----------------------------------------------------------------

def _start(client, widget, **overrides):
    body = {"widget_id": str(widget.id), "visitor_name": "Tag Tester",
            "visitor_email": f"tags-{uuid.uuid4().hex[:8]}@example.com"}
    body.update(overrides)
    resp = client.post("/api/v1/mobile/conversations", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _send(client, conv, text, files=None):
    token = conv["conversation_token"]
    kwargs = {"headers": {"X-Conversation-Token": token},
              "data": {"message": text}}
    if files is not None:
        kwargs["files"] = files
    resp = client.post(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _inbox(client, **params):
    resp = client.get("/api/v1/chats/recent", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tags_for(client, session_id):
    """Tags on one conversation, read back through the inbox list."""
    for row in _inbox(client, limit=100):
        if str(row["session_id"]) == str(session_id):
            assert "tags" in row, "inbox row carries no 'tags' field"
            return list(row["tags"])
    raise AssertionError(f"session {session_id} not in inbox")


def _admin_tags(client, session_id):
    """(tags, priority_score) via GET /api/v1/admin/conversations/{id}/tags."""
    resp = client.get(f"/api/v1/admin/conversations/{session_id}/tags")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return list(body["tags"]), body["priority_score"]


def _start_and_tag(client, widget, text):
    conv = _start(client, widget)
    _send(client, conv, text)
    return conv


def _other_org_widget(db):
    """A second org + agent + widget + AI config, for the cross-org test."""
    org2 = Organization(name="Tags Other Org",
                        domain=f"tags-other-{uuid.uuid4().hex[:8]}.example.com",
                        timezone="UTC")
    db.add(org2)
    db.commit()
    db.refresh(org2)
    agent2 = AgentRepository(db).create_agent(
        name="Other Agent", agent_type=AgentType.CUSTOMER_SUPPORT,
        instructions=["You are a test agent."], org_id=org2.id)
    widget2 = WidgetRepository(db).create_widget(
        widget=WidgetCreate(name="Other Widget", agent_id=agent2.id),
        organization_id=org2.id)
    db.add(AIConfig(organization_id=org2.id, model_type=AIModelType.OPENAI,
                    model_name="gpt-4",
                    encrypted_api_key=encrypt_api_key("test_key"),
                    is_active=True))
    db.commit()
    return widget2


# --- 1. Tagger rules: visitor wording -> tags --------------------------------

def test_payment_received_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "I paid via JazzCash, transaction id 123456")
    tags = _tags_for(client, conv["conversation_id"])
    assert PAYMENT_RECEIVED in tags
    # Paid beats pending: the same message also says JazzCash, but a chat
    # that already paid is never also payment-pending — and the result has
    # not been sent yet, so delivery-pending is added.
    assert PAYMENT_PENDING not in tags
    assert DELIVERY_PENDING in tags


def test_payment_pending_and_service_tags(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "how to pay for SIM owner details?")
    tags = _tags_for(client, conv["conversation_id"])
    assert PAYMENT_PENDING in tags
    assert SVC_SIM_OWNER in tags


def test_not_paid_yet_is_pending_not_received(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "I have not paid yet, how to pay?")
    tags = _tags_for(client, conv["conversation_id"])
    assert PAYMENT_PENDING in tags
    assert PAYMENT_RECEIVED not in tags


def test_urgent_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "urgent, jaldi karo")
    assert URGENT in _tags_for(client, conv["conversation_id"])


def test_service_tag_imei(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "I want IMEI tracking")
    assert SVC_IMEI in _tags_for(client, conv["conversation_id"])


def test_refund_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "I want a refund, paisay wapas karo")
    assert REFUND_REQUEST in _tags_for(client, conv["conversation_id"])


def test_needs_human_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "I want to talk to a human agent please")
    assert NEEDS_HUMAN in _tags_for(client, conv["conversation_id"])


def test_vip_and_service_tags_for_big_order(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "I want WhatsApp CDR for 3 months")
    tags = _tags_for(client, conv["conversation_id"])
    assert SVC_WHATSAPP_CDR in tags
    assert VIP in tags
    # WhatsApp CDR is its own service, never plain CDR.
    assert SVC_CDR not in tags


def test_spam_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "FREE crypto double your money click here now")
    assert SPAM in _tags_for(client, conv["conversation_id"])


def test_angry_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget,
                          "you are a scam, this is fraud, no reply for days")
    assert ANGRY in _tags_for(client, conv["conversation_id"])


def test_payment_proof_with_receipt_image(client, test_widget, test_agent,
                                          test_ai_config, fake_ai, db):
    test_agent.allow_attachments = True
    db.commit()
    conv = _start(client, test_widget)

    async def _fake_prepare(files, *, api_key, model_type, model_name=None):
        return MediaTurn(context_text="[Image 'receipt.png' attached below]", images=[])

    async def _fake_upload(*args, **kwargs):
        return {"file_url": "/uploads/test/receipt.png", "filename": "receipt.png",
                "content_type": "image/png", "size": 4}

    with (patch.object(mobile_chat, "prepare_media_context", new=AsyncMock(side_effect=_fake_prepare)),
          patch.object(mobile_chat.FileUploadService, "upload_file", new=AsyncMock(side_effect=_fake_upload))):
        _send(client, conv, "I paid, here is the receipt",
              files={"files": ("receipt.png", b"\x89PNG fake", "image/png")})
    tags = _tags_for(client, conv["conversation_id"])
    assert PAYMENT_RECEIVED in tags
    assert PAYMENT_PROOF in tags
    assert DELIVERY_PENDING in tags


def test_tags_accumulate_without_duplicates(client, test_widget, test_ai_config, fake_ai):
    conv = _start(client, test_widget)
    _send(client, conv, "I paid via JazzCash, transaction id 123456")
    _send(client, conv, "urgent, jaldi karo")
    _send(client, conv, "I paid via JazzCash, transaction id 123456")
    tags = _tags_for(client, conv["conversation_id"])
    assert PAYMENT_RECEIVED in tags
    assert URGENT in tags
    assert tags.count(PAYMENT_RECEIVED) == 1


def test_plain_greeting_gets_no_tags(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "hi, how are you")
    assert _tags_for(client, conv["conversation_id"]) == []


# --- 2. Inbox: filter + priority sort ------------------------------------------

def test_filter_by_single_tag(client, test_widget, test_ai_config, fake_ai):
    paid = _start_and_tag(client, test_widget, "I paid via JazzCash, transaction id 1")
    _start_and_tag(client, test_widget, "I want IMEI tracking")
    rows = _inbox(client, tag=PAYMENT_RECEIVED)
    assert [str(r["session_id"]) for r in rows] == [paid["conversation_id"]]
    rows = _inbox(client, tag=SVC_IMEI)
    assert len(rows) == 1
    assert rows[0]["session_id"] != paid["conversation_id"]


def test_filter_unknown_tag_422(client, test_widget, test_ai_config, fake_ai):
    resp = client.get("/api/v1/chats/recent", params={"tag": "not-a-real-tag"})
    assert resp.status_code == 422, resp.text


def test_priority_sort_orders_by_heaviest_tag(client, test_widget, test_ai_config, fake_ai):
    paid = _start_and_tag(client, test_widget, "I paid via JazzCash, transaction id 1")
    urgent = _start_and_tag(client, test_widget, "urgent, jaldi karo")
    plain = _start_and_tag(client, test_widget, "hi, how are you")
    rows = _inbox(client, sort="priority")
    order = [str(r["session_id"]) for r in rows]
    # payment-received (100) > urgent (90) > no tags (0).
    assert order.index(paid["conversation_id"]) < order.index(urgent["conversation_id"])
    assert order.index(urgent["conversation_id"]) < order.index(plain["conversation_id"])


def test_recent_rows_carry_tags_and_priority_score(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "I paid via JazzCash, transaction id 1")
    rows = _inbox(client)
    row = next(r for r in rows if str(r["session_id"]) == conv["conversation_id"])
    assert PAYMENT_RECEIVED in row["tags"]
    assert row["priority_score"] == 100


# --- 3. Admin tags API: list / get / add / remove ------------------------------

def test_list_tags(client):
    resp = client.get("/api/v1/admin/tags")
    assert resp.status_code == 200, resp.text
    by_slug = {t["slug"]: t for t in resp.json()}
    assert by_slug[PAYMENT_RECEIVED]["weight"] == 100
    assert by_slug[SVC_IMEI]["weight"] == 10
    assert by_slug[URGENT]["label"] == "Urgent"
    # Heaviest first.
    weights = [t["weight"] for t in resp.json()]
    assert weights == sorted(weights, reverse=True)


def test_get_tags_and_priority_score(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "I paid via JazzCash, transaction id 1")
    tags, score = _admin_tags(client, conv["conversation_id"])
    assert PAYMENT_RECEIVED in tags
    assert score == 100
    plain = _start_and_tag(client, test_widget, "hi, how are you")
    _, plain_score = _admin_tags(client, plain["conversation_id"])
    assert plain_score == 0


def test_manual_add_tag(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "hi, how are you")
    sid = conv["conversation_id"]
    resp = client.post(f"/api/v1/admin/conversations/{sid}/tags", json={"tag": VIP})
    assert resp.status_code == 200, resp.text
    assert VIP in resp.json()["tags"]
    assert resp.json()["priority_score"] == 55
    tags, _ = _admin_tags(client, sid)
    assert VIP in tags


def test_manual_add_accepts_slug_shape(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "hi, how are you")
    resp = client.post(f"/api/v1/admin/conversations/{conv['conversation_id']}/tags",
                       json={"slug": VIP})
    assert resp.status_code == 200, resp.text
    assert VIP in resp.json()["tags"]


def test_manual_add_unknown_tag_422(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "hi, how are you")
    resp = client.post(f"/api/v1/admin/conversations/{conv['conversation_id']}/tags",
                       json={"tag": "not-a-real-tag"})
    assert resp.status_code == 422, resp.text


def test_manual_remove_tag_by_path(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "I paid via JazzCash, transaction id 1")
    sid = conv["conversation_id"]
    assert PAYMENT_RECEIVED in _tags_for(client, sid)
    resp = client.delete(f"/api/v1/admin/conversations/{sid}/tags/{PAYMENT_RECEIVED}")
    assert resp.status_code == 200, resp.text
    assert PAYMENT_RECEIVED not in resp.json()["tags"]
    tags, _ = _admin_tags(client, sid)
    assert PAYMENT_RECEIVED not in tags


def test_manual_remove_tag_by_body(client, test_widget, test_ai_config, fake_ai):
    conv = _start_and_tag(client, test_widget, "hi, how are you")
    sid = conv["conversation_id"]
    client.post(f"/api/v1/admin/conversations/{sid}/tags", json={"tag": VIP})
    # Old Starlette TestClient rejects json= on DELETE — send the body raw.
    resp = client.request(
        "DELETE", f"/api/v1/admin/conversations/{sid}/tags",
        content=json.dumps({"slug": VIP}),
        headers={"Content-Type": "application/json"})
    assert resp.status_code == 200, resp.text
    assert VIP not in resp.json()["tags"]


def test_tags_unknown_session_404(client):
    sid = str(uuid.uuid4())
    assert client.get(f"/api/v1/admin/conversations/{sid}/tags").status_code == 404
    assert client.post(f"/api/v1/admin/conversations/{sid}/tags",
                       json={"tag": VIP}).status_code == 404
    assert client.delete(f"/api/v1/admin/conversations/{sid}/tags/{VIP}").status_code == 404


def test_tags_other_org_conversation_404(client, test_widget, test_ai_config,
                                         fake_ai, db):
    widget2 = _other_org_widget(db)
    conv2 = _start(client, widget2)
    sid = conv2["conversation_id"]
    # The admin user belongs to the first org: the other org's conversation
    # is a 404, never a leak.
    assert client.get(f"/api/v1/admin/conversations/{sid}/tags").status_code == 404
    assert client.post(f"/api/v1/admin/conversations/{sid}/tags",
                       json={"tag": VIP}).status_code == 404
