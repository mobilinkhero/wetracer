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

"""Streaming transport tests (docs/STREAMING-PROTOCOL.md v1).

Two transports, same event protocol (start / token / done, or error):

1. Mobile SSE: POST /api/v1/mobile/conversations/{id}/messages/stream
   (app/api/mobile_chat.py). The AI turn is mocked at the mobile_chat module
   boundary (create_widget_agent returns a fake agent whose stream_response
   yields canned events); the SSE framing, fallback and error mapping are
   exercised for real. No model, no embedding.
2. Widget Socket.IO: app/api/widget_chat.py handle_widget_chat, driven with
   the same mock-socket harness as tests/api/test_widget_chat.py. The fake
   agent yields canned stream events; the emitted socket events and the
   final chat_response are asserted.

Live end-to-end behaviour (stub hosted-provider streaming server, real KB,
token rotation, reconnect) is covered by backend/tests/stub_provider.py
plus backend/tests_live/test_streaming_live.py. No live model is used: the
owner's hosted provider key is never present in test mode.
"""

import contextlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.chat_agent import ChatResponse
from app.api import mobile_chat, widget_chat
from app.database import get_db
from app.models.session_to_agent import SessionStatus
from app.repositories.chat import ChatRepository
from app.services import host_action_runtime, message_delivery, widget_reply
from tests.socket_db import serve_socket_db

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
    from app.core.config import settings
    from app.core.redis import get_redis
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    get_redis()


def _start(client, widget, **overrides):
    body = {"widget_id": str(widget.id), "visitor_name": "Stream Tester"}
    body.update(overrides)
    resp = client.post("/api/v1/mobile/conversations", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(token):
    return {"X-Conversation-Token": token}


def _parse_sse(body: str):
    """[(event_name, data_dict)] in wire order."""
    events = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events


class FakeStreamAgent:
    """Stand-in for ChatAgent on the streaming path."""

    def __init__(self, session_id, db, events, response_text,
                 message_id=None):
        self.agent = SimpleNamespace(session_id=session_id)
        self.last_bot_message_id = message_id
        self.last_stream_response = None
        self.last_paused_turn = None
        self.last_handover = None
        self.last_ui_extras = {}
        self._db = db
        self._events = events
        self._response_text = response_text

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
        if self._response_text is not None:
            self.last_stream_response = ChatResponse(
                message=self._response_text, transfer_to_human=False,
                end_chat=False, request_rating=False, create_ticket=False)

    async def safe_cleanup_mcp_tools(self):
        return None


@pytest.fixture
def fake_stream(monkeypatch):
    """Patch the SSE turn: fake agent + a deliver that stores the bot reply."""
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

    def _install(events, response_text, message_id=None):
        async def _fake_create_agent(kwargs):
            return FakeStreamAgent(kwargs["session_id"], state["db"],
                                   events, response_text, message_id)

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
def stream_client(client, db, fake_stream):
    fake_stream["db"] = db
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


# --- SSE framing -------------------------------------------------------------

def test_sse_event_format():
    assert mobile_chat._sse("token", {"delta": "hi"}) == \
        'event: token\ndata: {"delta": "hi"}\n\n'
    assert mobile_chat._sse("done", {"message": "x", "source": "model-stream"}).endswith("\n\n")


# --- SSE: model stream ---------------------------------------------------------

def test_sse_model_stream_token_order(stream_client, test_widget, test_ai_config, fake_stream):
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([
        {"type": "start", "source": "model-stream", "session_id": conv["conversation_id"],
         "message_id": None},
        {"type": "token", "delta": "Hello ", "source": "model-stream"},
        {"type": "token", "delta": "streamed ", "source": "model-stream"},
        {"type": "token", "delta": "world", "source": "model-stream"},
        {"type": "done", "source": "model-stream", "session_id": conv["conversation_id"],
         "message_id": None},
    ], "Hello streamed world")

    events = _stream(stream_client, conv, "Say hello")

    assert [name for name, _ in events] == ["start", "token", "token", "token", "done"]
    assert events[0][1]["source"] == "model-stream"
    assert "".join(d[1]["delta"] for d in events if d[0] == "token") == "Hello streamed world"
    done = events[-1][1]
    assert done["message"] == "Hello streamed world"
    assert done["type"] == "chat_response"
    assert done["source"] == "model-stream"
    assert done["message_id"] is not None

    # The streamed reply is stored exactly once, like a non-streamed one.
    history = stream_client.get(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        headers=_headers(conv["conversation_token"])).json()["messages"]
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    assert history[1]["text"] == "Hello streamed world"


# --- SSE: welcome message (mirrors the widget socket handler) -----------------

WELCOME_TEXT = "Hi! I can track orders or start a return — pick one."


def _set_welcome(db, test_agent, welcome):
    """Give the agent a customization row carrying this welcome message."""
    from app.models.agent import AgentCustomization
    db.add(AgentCustomization(agent_id=test_agent.id, welcome_message=welcome))
    db.commit()


def _stream_events(conv):
    return [
        {"type": "start", "source": "model-stream", "session_id": conv["conversation_id"],
         "message_id": None},
        {"type": "token", "delta": "Hello ", "source": "model-stream"},
        {"type": "token", "delta": "streamed ", "source": "model-stream"},
        {"type": "token", "delta": "world", "source": "model-stream"},
        {"type": "done", "source": "model-stream", "session_id": conv["conversation_id"],
         "message_id": None},
    ]


@pytest.mark.asyncio
async def test_sse_first_message_inserts_welcome_message(
        stream_client, db, test_widget, test_agent, test_ai_config, fake_stream):
    """The streamed first turn stores the welcome message too, ahead of the
    visitor message the stream stores — the AI's history starts with what
    the visitor is picking from, on both send endpoints."""
    from app.api.widget_chat import WELCOME_MESSAGE_ATTRIBUTE

    _set_welcome(db, test_agent, WELCOME_TEXT)
    conv = _start(stream_client, test_widget)
    fake_stream["install"](_stream_events(conv), "Hello streamed world")

    events = _stream(stream_client, conv, "Say hello")
    assert [name for name, _ in events] == ["start", "token", "token", "token", "done"]
    assert events[-1][1]["message"] == "Hello streamed world"

    history = stream_client.get(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        headers=_headers(conv["conversation_token"])).json()["messages"]
    assert [m["text"] for m in history] == [
        WELCOME_TEXT, "Say hello", "Hello streamed world"]
    assert [m["sender"] for m in history] == ["ai", "visitor", "ai"]

    stored = await ChatRepository(db).get_session_history(conv["conversation_id"])
    assert len(stored) == 3
    assert stored[0].message == WELCOME_TEXT
    assert stored[0].message_type == "bot"
    assert stored[0].attributes.get(WELCOME_MESSAGE_ATTRIBUTE) is True
    assert stored[0].attributes.get("source") == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("welcome", [None, "", "   "])
async def test_sse_first_message_without_welcome_message_works_as_before(
        stream_client, db, test_widget, test_agent, test_ai_config, fake_stream, welcome):
    """No welcome message set: the streamed turn works exactly as before —
    a normal streamed reply, nothing extra stored."""
    from app.api.widget_chat import WELCOME_MESSAGE_ATTRIBUTE

    _set_welcome(db, test_agent, welcome)
    conv = _start(stream_client, test_widget)
    fake_stream["install"](_stream_events(conv), "Hello streamed world")

    events = _stream(stream_client, conv, "Say hello")
    assert [name for name, _ in events] == ["start", "token", "token", "token", "done"]
    assert events[-1][1]["message"] == "Hello streamed world"

    history = stream_client.get(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        headers=_headers(conv["conversation_token"])).json()["messages"]
    assert [m["sender"] for m in history] == ["visitor", "ai"]
    stored = await ChatRepository(db).get_session_history(conv["conversation_id"])
    assert not [m for m in stored
                if (m.attributes or {}).get(WELCOME_MESSAGE_ATTRIBUTE) is True]


def test_sse_model_full_fallback_has_no_tokens(stream_client, test_widget, test_ai_config, fake_stream):
    """A turn the lean agent cannot take: single start (model-full), no
    tokens, then done with the complete reply. A client that only handles
    done still works."""
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([
        {"type": "start", "source": "model-full", "session_id": conv["conversation_id"],
         "message_id": 99},
        {"type": "done", "source": "model-full", "session_id": conv["conversation_id"],
         "message_id": 99},
    ], "Complete reply from the full agent.", message_id=99)

    events = _stream(stream_client, conv, "Please speak to a human")
    assert [name for name, _ in events] == ["start", "done"]
    assert events[0][1]["source"] == "model-full"
    assert events[-1][1]["message"] == "Complete reply from the full agent."
    assert events[-1][1]["source"] == "model-full"


def test_sse_mid_stream_error_then_done_with_finished_reply(stream_client, test_widget, test_ai_config, fake_stream):
    """Mid-stream failure AFTER the agent finished the partial text: the
    stream yields `error`, and — because a finished reply exists — the
    handler ALSO yields `done` with it (the widget analogue is
    chat_response_stream_error followed by chat_response).
    NOTE (finding, see final report): docs/STREAMING-PROTOCOL.md says
    'No `done` follows an `error`' for SSE, but the implementation sends
    done after error whenever last_stream_response is set. A client that
    stops listening on error never sees the done;
    a client that only handles done still gets the finished reply."""
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([
        {"type": "start", "source": "model-stream", "session_id": conv["conversation_id"],
         "message_id": None},
        {"type": "token", "delta": "Partial ", "source": "model-stream"},
        {"type": "error", "session_id": conv["conversation_id"],
         "error": "Streaming interrupted: boom"},
    ], "Partial finished")

    events = _stream(stream_client, conv, "Say hello")
    assert [name for name, _ in events] == ["start", "token", "error", "done"]
    assert events[1][1]["delta"] == "Partial "
    assert "boom" in events[2][1]["error"]
    assert events[3][1]["message"] == "Partial finished"


def test_sse_hard_error_has_no_done(stream_client, test_widget, test_ai_config, fake_stream):
    """Turn fails with no finished reply at all: `error` is terminal and
    no `done` follows (the protocol's stated rule)."""
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([
        {"type": "error", "session_id": conv["conversation_id"],
         "error": "Streaming failed: model exploded"},
    ], None)

    events = _stream(stream_client, conv, "Say hello")
    assert [name for name, _ in events] == ["error"]
    assert "model exploded" in events[0][1]["error"]


def test_sse_paused_turn_reports_action_required(stream_client, test_widget, test_ai_config, fake_stream):
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([
        {"type": "done", "source": "model-full", "session_id": conv["conversation_id"],
         "paused": True},
    ], "unused")

    events = _stream(stream_client, conv, "Do the thing")
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["error"] == "action_required"


# --- SSE: pre-stream errors ----------------------------------------------------

def test_sse_rejects_files_422(stream_client, test_widget, test_ai_config, fake_stream):
    conv = _start(stream_client, test_widget)
    fake_stream["install"]([], "unused")
    resp = stream_client.post(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream",
        headers=_headers(conv["conversation_token"]),
        data={"message": "See attached"},
        files={"files": ("x.png", b"fake", "image/png")})
    assert resp.status_code == 422
    assert "not supported on the streaming endpoint" in resp.json()["detail"]


def test_sse_rejects_empty_message_422(stream_client, test_widget, test_ai_config):
    conv = _start(stream_client, test_widget)
    resp = stream_client.post(
        f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream",
        headers=_headers(conv["conversation_token"]), data={"message": "   "})
    assert resp.status_code == 422


def test_sse_requires_valid_token(stream_client, test_widget, test_ai_config):
    conv = _start(stream_client, test_widget)
    url = f"/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream"
    assert stream_client.post(url, data={"message": "hi"}).status_code == 401
    assert stream_client.post(
        url, headers=_headers("bogus"), data={"message": "hi"}).status_code == 401


def test_sse_unknown_conversation_404(stream_client, test_widget, test_ai_config):
    conv = _start(stream_client, test_widget)
    resp = stream_client.post(
        "/api/v1/mobile/conversations/00000000-0000-0000-0000-000000000000/messages/stream",
        headers=_headers(conv["conversation_token"]), data={"message": "hi"})
    assert resp.status_code == 404


# --- Widget Socket.IO ----------------------------------------------------------

@pytest.fixture
def mock_sio():
    mock = MagicMock()
    mock.enter_room = AsyncMock()
    mock.emit = AsyncMock()
    mock.save_session = AsyncMock()
    mock.get_environ = MagicMock()
    mock.get_session = AsyncMock()
    mock.leave_room = AsyncMock()
    return mock


@pytest.fixture
def widget_session(db, test_widget, test_ai_config, test_customer, mock_sio, monkeypatch):
    """An open widget conversation wired to the mock socket (same harness
    as tests/api/test_widget_chat.py::test_widget_chat_message)."""
    from app.repositories.session_to_agent import SessionToAgentRepository

    monkeypatch.setattr(widget_chat, "sio", mock_sio)
    monkeypatch.setattr(widget_reply, "sio", mock_sio)
    monkeypatch.setattr(host_action_runtime, "sio", mock_sio)
    monkeypatch.setattr(message_delivery, "sio", mock_sio)
    serve_socket_db(monkeypatch, db)

    session_id = uuid.uuid4()
    conversation_token = "test-token"
    monkeypatch.setattr(
        widget_chat, "authenticate_socket_conversation_token",
        AsyncMock(return_value=(str(test_widget.id), str(test_widget.organization_id),
                                str(test_customer.id), conversation_token)))
    SessionToAgentRepository(db).create_session(
        session_id=session_id, agent_id=test_widget.agent_id,
        customer_id=test_customer.id, organization_id=test_widget.organization_id)

    mock_sio.get_session.return_value = {
        "widget_id": str(test_widget.id),
        "org_id": str(test_widget.organization_id),
        "agent_id": str(test_widget.agent_id),
        "customer_id": str(test_customer.id),
        "session_id": str(session_id),
        "ai_config": test_ai_config,
        "conversation_token": conversation_token,
        "ai_replies_enabled": True,
    }
    mock_sio.get_environ.return_value = {}

    mock_session = MagicMock()
    mock_session.session_id = session_id
    mock_session.status = SessionStatus.OPEN
    mock_session.user_id = None
    mock_session.workflow_id = None
    monkeypatch.setattr(widget_chat, "_own_session",
                        lambda session_repo, session, active_only=True: mock_session)
    return {"session_id": session_id, "db": db}


class FakeWidgetStreamAgent:
    def __init__(self, session_id, events, response):
        self.agent = SimpleNamespace(session_id=session_id)
        self.last_bot_message_id = 555
        self.last_stream_response = response
        self.last_paused_turn = None
        self.last_handover = None
        self.last_ui_extras = {}
        self._events = events

    async def stream_response(self, **kwargs):
        for event in self._events:
            yield event

    async def safe_cleanup_mcp_tools(self):
        return None


def _emitted(mock_sio):
    return [(c[0][0], c[0][1] if len(c[0]) > 1 else None)
            for c in mock_sio.emit.call_args_list]


@pytest.mark.asyncio
async def test_widget_socket_streams_tokens_then_final_reply(
        widget_session, mock_sio, monkeypatch):
    session_id = widget_session["session_id"]
    response = ChatResponse(message="Hello streamed world", transfer_to_human=False,
                            end_chat=False, request_rating=False, create_ticket=False)
    fake = FakeWidgetStreamAgent(str(session_id), [
        {"type": "start", "source": "model-stream", "session_id": str(session_id),
         "message_id": None},
        {"type": "token", "delta": "Hello ", "source": "model-stream"},
        {"type": "token", "delta": "streamed world", "source": "model-stream"},
        {"type": "done", "source": "model-stream", "session_id": str(session_id),
         "message_id": 555},
    ], response)
    monkeypatch.setattr(widget_chat, "create_widget_agent",
                        AsyncMock(return_value=fake))

    await widget_chat.handle_widget_chat("test_sid", {"message": "Say hello"})

    emitted = _emitted(mock_sio)
    names = [name for name, _ in emitted]
    assert names.index("bot_typing") < names.index("chat_response_stream_start")
    assert names == ["bot_typing", "chat_response_stream_start",
                     "chat_response_stream_token", "chat_response_stream_token",
                     "chat_response_stream_done", "chat_response"]
    start = emitted[1][1]
    assert start["source"] == "model-stream"
    assert start["session_id"] == str(session_id)
    assert emitted[2][1]["delta"] == "Hello "
    assert emitted[3][1]["delta"] == "streamed world"
    final = emitted[-1][1]
    assert final["message"] == "Hello streamed world"
    assert final["type"] == "chat_response"
    # One bubble: the streamed text is confirmed by chat_response, not doubled.
    assert [n for n in names if n == "chat_response"] == ["chat_response"]


@pytest.mark.asyncio
async def test_widget_socket_model_full_fallback_emits_no_stream_events(
        widget_session, mock_sio, monkeypatch):
    session_id = widget_session["session_id"]
    response = ChatResponse(message="Complete reply from the full agent.",
                            transfer_to_human=False, end_chat=False,
                            request_rating=False, create_ticket=False)
    fake = FakeWidgetStreamAgent(str(session_id), [
        {"type": "start", "source": "model-full", "session_id": str(session_id),
         "message_id": 555},
        {"type": "done", "source": "model-full", "session_id": str(session_id),
         "message_id": 555},
    ], response)
    monkeypatch.setattr(widget_chat, "create_widget_agent",
                        AsyncMock(return_value=fake))

    await widget_chat.handle_widget_chat(
        "test_sid", {"message": "Please speak to a human"})

    emitted = _emitted(mock_sio)
    names = [name for name, _ in emitted]
    assert "chat_response_stream_start" in names
    assert "chat_response_stream_token" not in names
    assert names == ["bot_typing", "chat_response_stream_start",
                     "chat_response_stream_done", "chat_response"]
    assert emitted[-1][1]["message"] == "Complete reply from the full agent."


@pytest.mark.asyncio
async def test_widget_socket_mid_stream_error_still_delivers_final_reply(
        widget_session, mock_sio, monkeypatch):
    """Protocol rule: after chat_response_stream_error the client keeps the
    partial text and waits for chat_response, which still arrives."""
    session_id = widget_session["session_id"]
    response = ChatResponse(message="Partial finished", transfer_to_human=False,
                            end_chat=False, request_rating=False, create_ticket=False)
    fake = FakeWidgetStreamAgent(str(session_id), [
        {"type": "start", "source": "model-stream", "session_id": str(session_id),
         "message_id": None},
        {"type": "token", "delta": "Partial ", "source": "model-stream"},
        {"type": "error", "session_id": str(session_id),
         "error": "Streaming interrupted: boom"},
    ], response)
    monkeypatch.setattr(widget_chat, "create_widget_agent",
                        AsyncMock(return_value=fake))

    await widget_chat.handle_widget_chat("test_sid", {"message": "Say hello"})

    emitted = _emitted(mock_sio)
    names = [name for name, _ in emitted]
    assert names == ["bot_typing", "chat_response_stream_start",
                     "chat_response_stream_token", "chat_response_stream_error",
                     "chat_response"]
    assert emitted[3][1]["error"] == "Streaming interrupted: boom"
    assert emitted[-1][1]["message"] == "Partial finished"
