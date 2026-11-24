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

"""Streaming edge-case tests (docs/STREAMING-PROTOCOL.md v1).

The failure modes the happy-path streaming tests do not cover:

1. Visitor token revoked mid-stream — auth is checked once, before the
   SSE generator starts, so an in-flight stream still completes with
   `done` and the reply is stored; a NEW request with the same token then
   gets 401, proving the token really was dead while the stream ran.
2. Client disconnect mid-stream — the generator is cancelled, the partial
   reply is NOT stored as a bot message, and agent cleanup still runs.
3. Empty knowledge base — the KB lookup returns no hit and the turn falls
   through to the model stream (service level + SSE wire level).
4. Very long reply — hundreds of tokens arrive in order and the stored
   reply is exactly their concatenation.
5. Very short reply — a one-character reply still streams as one token
   plus `done` and is stored verbatim.

Same harness as tests/api/test_streaming.py: the AI turn is mocked at the
mobile_chat module boundary (create_widget_agent returns a fake agent);
SSE framing, delivery and error mapping are exercised for real. No model
call, no embedding, no external provider.
"""

import asyncio
import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agents.chat_agent import ChatResponse
from app.api import mobile_chat
from app.core.security import revoke_token, verify_conversation_token
from app.database import get_db
from app.models.chat_history import ChatHistory
from app.repositories.chat import ChatRepository
from tests.api.test_streaming import (
    FakeStreamAgent,
    _headers,
    _parse_sse,
    _start,
    test_app,
)


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
    from app.core.config import settings
    from app.core.redis import get_redis
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    get_redis()


@pytest.fixture
def edge_stream(monkeypatch):
    """Like tests/api/test_streaming.py::fake_stream, but the agent class
    is selectable so edge-case agents can hook the stream."""
    state = {}

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

    def _install(events, response_text, message_id=None,
                 agent_cls=FakeStreamAgent, agent_kwargs=None):
        async def _fake_create_agent(kwargs):
            agent = agent_cls(kwargs["session_id"], state["db"], events,
                              response_text, message_id,
                              **(agent_kwargs or {}))
            state["agent"] = agent
            return agent

        # The SSE generator delivers the finished reply through a FRESH
        # SessionLocal (not the request-scoped db): in the unit harness
        # that is the real Postgres URL, so point it at the sqlite `db`
        # fixture instead. Production behaviour is unchanged.
        @contextlib.contextmanager
        def _fake_session_local():
            yield state["db"]

        patches = (
            patch.object(mobile_chat, "emit_bot_typing", new=AsyncMock()),
            patch.object(mobile_chat, "create_widget_agent",
                         new=AsyncMock(side_effect=_fake_create_agent)),
            patch.object(mobile_chat, "deliver_ai_reply",
                         new=AsyncMock(side_effect=_fake_deliver)),
            patch.object(mobile_chat, "SessionLocal",
                         new=_fake_session_local),
        )
        for p in patches:
            p.start()
        state["patches"] = patches

    state["install"] = _install
    yield state
    for p in state.get("patches", ()):
        p.stop()


@pytest.fixture
def stream_client(client, db, edge_stream):
    edge_stream["db"] = db
    return client


def _stream(client, conv, message="Hello"):
    with client.stream(
            "POST",
            f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream",
            headers=_headers(conv["conversation_token"]),
            data={"message": message}) as resp:
        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers["content-type"]
        return _parse_sse(resp.read().decode())


def _history(client, conv):
    return client.get(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        headers=_headers(conv["conversation_token"])).json()["messages"]


# --- 1. Token revoked mid-stream ---------------------------------------------

class RevokingStreamAgent(FakeStreamAgent):
    """Fake agent that revokes its own conversation token right after the
    first token event — the credential dies while the stream is in flight."""

    def __init__(self, *args, revoke_jti=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._revoke_jti = revoke_jti

    async def stream_response(self, **kwargs):
        db = self._db
        ChatRepository(db).create_message({
            "message": kwargs["message"], "message_type": "user",
            "session_id": uuid.UUID(str(kwargs["session_id"])),
            "organization_id": uuid.UUID(str(kwargs["org_id"])),
            "agent_id": kwargs["agent_id"],
            "customer_id": uuid.UUID(str(kwargs["customer_id"])),
        })
        db.commit()
        for event in self._events:
            yield event
            if event.get("type") == "token" and self._revoke_jti is not None:
                revoke_token(self._revoke_jti)
                self._revoke_jti = None
        if self._response_text is not None:
            self.last_stream_response = ChatResponse(
                message=self._response_text, transfer_to_human=False,
                end_chat=False, request_rating=False, create_ticket=False)


def _stored_messages(db, conv):
    """Rows stored for a conversation, read straight from the test DB.

    Used where the API history endpoint cannot be called — e.g. after the
    visitor token was revoked mid-stream, the GET would 401."""
    db.expire_all()
    return (db.query(ChatHistory)
              .filter(ChatHistory.session_id == uuid.UUID(conv["conversation_id"]))
              .order_by(ChatHistory.id).all())


def test_sse_token_revoked_mid_stream_still_completes(
        stream_client, test_widget, test_ai_config, edge_stream, db):
    """Auth is checked once, before the SSE generator starts: a token that
    dies mid-stream must not cut the in-flight stream short, and the
    finished reply must still be stored. The same token is dead for any
    NEW request afterwards."""
    conv = _start(stream_client, test_widget)
    token = conv["conversation_token"]
    payload = verify_conversation_token(token)
    assert payload is not None and payload.get("jti")

    edge_stream["install"]([
        {"type": "start", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
        {"type": "token", "delta": "Part one ", "source": "model-stream"},
        {"type": "token", "delta": "part two.", "source": "model-stream"},
        {"type": "done", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
    ], "Part one part two.",
        agent_cls=RevokingStreamAgent,
        agent_kwargs={"revoke_jti": payload["jti"]})

    events = _stream(stream_client, conv, "Say hello")

    assert [name for name, _ in events] == ["start", "token", "token", "done"]
    assert "".join(d[1]["delta"] for d in events if d[0] == "token") == \
        "Part one part two."
    assert events[-1][1]["message"] == "Part one part two."

    # The finished reply was stored even though the token died mid-stream.
    # Read from the DB directly: the API history endpoint would 401 with
    # the revoked token.
    rows = _stored_messages(db, conv)
    assert [r.message_type for r in rows] == ["user", "bot"]
    assert rows[1].message == "Part one part two."

    # ... but the token really is dead now: a new request with it gets 401.
    assert verify_conversation_token(token) is None
    resp = stream_client.post(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream",
        headers=_headers(token), data={"message": "hi again"})
    assert resp.status_code == 401


# --- 2. Client disconnect mid-stream -----------------------------------------

class BlockingStreamAgent(FakeStreamAgent):
    """Yields start + one token, then stalls: the test closes the
    connection while the agent is stalled, so the stream can never
    complete on its own."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup_called = False

    async def stream_response(self, **kwargs):
        db = self._db
        ChatRepository(db).create_message({
            "message": kwargs["message"], "message_type": "user",
            "session_id": uuid.UUID(str(kwargs["session_id"])),
            "organization_id": uuid.UUID(str(kwargs["org_id"])),
            "agent_id": kwargs["agent_id"],
            "customer_id": uuid.UUID(str(kwargs["customer_id"])),
        })
        db.commit()
        yield {"type": "start", "source": "model-stream",
               "session_id": str(kwargs["session_id"]), "message_id": None}
        yield {"type": "token", "delta": "Partial ",
               "source": "model-stream"}
        # Stall: only a disconnect cancels this. If the stall ever ends
        # on its own the rest of the turn would run and the test below
        # (no bot message stored) would fail, so this is self-checking.
        await asyncio.sleep(10)
        yield {"type": "token", "delta": "rest.",
               "source": "model-stream"}
        yield {"type": "done", "source": "model-stream",
               "session_id": str(kwargs["session_id"]), "message_id": None}
        self.last_stream_response = ChatResponse(
            message="Partial rest.", transfer_to_human=False,
            end_chat=False, request_rating=False, create_ticket=False)

    async def safe_cleanup_mcp_tools(self):
        self.cleanup_called = True
        return None


def test_sse_client_disconnect_mid_stream_stores_no_partial_reply(
        stream_client, test_widget, test_ai_config, edge_stream, db):
    """The visitor closes the connection after the first token: the
    generator is cancelled, the partial text is never stored as a bot
    message (the visitor can re-send), and agent cleanup still runs.

    NOTE: this cannot be driven through TestClient — its transport runs
    the whole request to completion and buffers the body, so closing the
    response early never reaches the server. The ASGI app is driven
    directly instead and the server task is cancelled after the first
    token, exactly what a real server does when the client goes away.
    """
    conv = _start(stream_client, test_widget)
    edge_stream["install"]([], "Partial rest.",
                           agent_cls=BlockingStreamAgent)
    body = b"message=Say+hello"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": (f"/api/v1/mobile/conversations/{conv['conversation_id']}"
                 "/messages/stream"),
        "raw_path": (f"/api/v1/mobile/conversations/{conv['conversation_id']}"
                     "/messages/stream").encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"x-conversation-token",
             conv["conversation_token"].encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body,
                    "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    messages = []
    token_seen = asyncio.Event()

    async def send(message):
        messages.append(message)
        if (message["type"] == "http.response.body"
                and b"event: token" in message.get("body", b"")):
            token_seen.set()

    async def _run():
        task = asyncio.create_task(test_app(scope, receive, send))
        await asyncio.wait_for(token_seen.wait(), timeout=10)
        # Client goes away mid-stream: cancel the server task.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    agent = edge_stream["agent"]
    assert agent.cleanup_called, \
        "agent cleanup did not run after the client disconnected"

    # The stream never produced `done`: only start + one token arrived.
    wire = b"".join(m.get("body", b"") for m in messages
                    if m["type"] == "http.response.body").decode()
    events = _parse_sse(wire)
    assert [name for name, _ in events] == ["start", "token"]
    assert "done" not in [name for name, _ in events]

    # Only the visitor message is stored — no partial bot reply.
    rows = _stored_messages(db, conv)
    assert [r.message_type for r in rows] == ["user"]
    assert rows[0].message == "Say hello"


# --- 3. Empty knowledge base -------------------------------------------------

def test_sse_empty_kb_falls_through_to_model_stream(
        stream_client, test_widget, test_ai_config, edge_stream):
    """Wire level for a KB miss (empty KB): no `kb`-sourced event appears,
    the full model stream runs instead, and the reply is stored. There is
    no direct KB answer path: every turn goes to the model."""
    conv = _start(stream_client, test_widget)
    edge_stream["install"]([
        {"type": "start", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
        {"type": "token", "delta": "Reset it from the sign-in page.",
         "source": "model-stream"},
        {"type": "done", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
    ], "Reset it from the sign-in page.")

    events = _stream(stream_client, conv, "How do I reset my password?")

    assert [name for name, _ in events] == ["start", "token", "done"]
    assert all(data.get("source") != "kb" for _, data in events)
    assert events[-1][1]["source"] == "model-stream"
    assert events[-1][1]["message"] == "Reset it from the sign-in page."
    history = _history(stream_client, conv)
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    assert history[1]["text"] == "Reset it from the sign-in page."


# --- 4. Very long reply ------------------------------------------------------

def test_sse_very_long_reply_streams_in_order(
        stream_client, test_widget, test_ai_config, edge_stream):
    """A 100,000-character reply in 400 tokens: every token arrives in
    order, the deltas concatenate to the full text exactly, and the
    stored bot message is the full text — nothing truncated or reordered."""
    chunk = "word " * 50  # 250 chars
    full = chunk * 400  # 100,000 chars
    assert len(full) == 100_000

    events_in = (
        [{"type": "start", "source": "model-stream",
          "session_id": None, "message_id": None}]
        + [{"type": "token", "delta": chunk, "source": "model-stream"}
           for _ in range(400)]
        + [{"type": "done", "source": "model-stream",
            "session_id": None, "message_id": None}]
    )
    conv = _start(stream_client, test_widget)
    for event in events_in:
        event["session_id"] = conv["conversation_id"]
    edge_stream["install"](events_in, full)

    events = _stream(stream_client, conv, "Tell me everything")

    assert [name for name, _ in events] == (
        ["start"] + ["token"] * 400 + ["done"])
    assert "".join(d[1]["delta"] for d in events if d[0] == "token") == full
    assert events[-1][1]["message"] == full
    history = _history(stream_client, conv)
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    assert history[1]["text"] == full


# --- 5. Very short reply -----------------------------------------------------

def test_sse_single_character_reply(
        stream_client, test_widget, test_ai_config, edge_stream):
    """A one-character reply is a real reply: exactly one token event,
    then `done` with the character, and it is stored verbatim — not
    dropped as empty or mistaken for a missing reply."""
    conv = _start(stream_client, test_widget)
    edge_stream["install"]([
        {"type": "start", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
        {"type": "token", "delta": "K", "source": "model-stream"},
        {"type": "done", "source": "model-stream",
         "session_id": conv["conversation_id"], "message_id": None},
    ], "K")

    events = _stream(stream_client, conv, "Are you there?")

    assert [name for name, _ in events] == ["start", "token", "done"]
    assert events[1][1]["delta"] == "K"
    assert events[-1][1]["message"] == "K"
    assert events[-1][1]["message_id"] is not None
    history = _history(stream_client, conv)
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    assert history[1]["text"] == "K"
