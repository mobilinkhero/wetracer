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

"""Live streaming + KB integration tests (docs/STREAMING-PROTOCOL.md v1).

Runs against the REAL backend (HTTP + Socket.IO, real Postgres/Redis, real
KB vector search) with the model layer stubbed: the Media Test Org AI config
is switched for the duration of this module to model type
OPENAI_COMPATIBLE pointed at tests/stub_provider.py — a local server that
mimics the hosted provider's OpenAI-compatible chat-completions streaming
API — and restored to its original values afterwards. No live model, no
paid calls, no key: the stub replays scripted deltas, and the tests mutate
``stub.deltas`` per test for short / long / structured replies.

LIVE-AI VERIFICATION IS BLOCKED pending the owner's hosted provider key in
the dashboard: nothing here calls a real provider, and a green run proves
the plumbing (transports, events, tokens, reconnect, storage) — not the
quality of a real provider's replies.

Run explicitly (outside `testpaths`, like the other live tests):
    cd backend && ./venv/bin/python -m pytest tests_live/test_streaming_live.py -v
"""

import json
import threading
import time
import uuid

import pytest
import requests
import socketio
from sqlalchemy import text

from app.database import SessionLocal
from app.models.agent import AgentType
from app.models.schemas.widget import WidgetCreate
from app.repositories.agent import AgentRepository
from app.repositories.widget import WidgetRepository
from tests.stub_provider import StubProviderServer

BASE = "http://localhost:8000"
WIDGET_ID = "72e0025a-d062-40a2-a1d5-6598e84bf4a2"

# Scripted stub replies (the stub replays `stub.deltas` verbatim).
SHORT_DELTAS = ["Hi there! ", "How can I help?"]
PARIS_DELTAS = ["The capital ", "of France ", "is Paris."]
LONG_TEXT = (
    "Our support works in four steps. First, you describe the problem in the chat and "
    "we confirm we understand it. Second, we check the help articles for an instant "
    "answer, so simple questions are resolved in seconds. Third, if the articles do "
    "not cover it, a support agent picks up the conversation and works through it "
    "with you, asking for screenshots or order details only when they are needed. "
    "Fourth, we confirm the fix with you and close the chat, and you can rate the "
    "help you received. Outside support hours the chat still takes your message and "
    "we reply as soon as we are back. If your issue is urgent, say so in your first "
    "message and it is flagged for the team. Billing questions go to the same chat, "
    "and refunds are handled case by case. You can reopen any closed chat from your "
    "history, and the agent sees the full earlier conversation, so you never have to "
    "repeat yourself. Fifth, if the fix needs a change to your account, the agent "
    "explains exactly what will change before anything is touched, and nothing is "
    "changed without your confirmation. Sixth, after the chat ends you receive a "
    "short summary of what was done, so you have a record of the solution and the "
    "steps to repeat it yourself next time. Seventh, if the same problem comes back, "
    "tell us it is the same issue and we pick up where the last chat left off, with "
    "the full history in front of us, instead of starting the diagnosis from zero. "
    "Eighth, you can ask for a human at any point in the conversation, and the chat "
    "is handed over with everything you already said attached, so nothing is lost "
    "in the transfer. That is the whole flow, end to end, "
    "and it works the same on the website widget and in the mobile app."
)
LONG_DELTAS = [w + " " for w in LONG_TEXT.split(" ")[:-1]] + [LONG_TEXT.split(" ")[-1]]
FULL_TURN_JSON = json.dumps({
    "message": "Stub full-turn reply: the capital of France is Paris.",
    "transfer_to_human": False,
    "end_chat": False,
    "request_rating": False,
    "create_ticket": False,
})

_CREATED = []  # (conversation_id, customer_id) for teardown cleanup


# --- fixtures ---------------------------------------------------------------

# Fixed stub port: whoever owns the AI config points the test org at this
# URL in advance (OPENAI_COMPATIBLE / stub-model, settings.base_url below).
# The live tests NEVER write ai_configs themselves — see stub_ai_config.
STUB_PORT = 18099


@pytest.fixture(scope="module")
def stub():
    server = StubProviderServer(
        deltas=list(SHORT_DELTAS), chunk_delay=0.05, port=STUB_PORT)
    try:
        server.start()
    except OSError:
        pytest.skip(
            f"stub port {STUB_PORT} already in use — another stub server "
            "is running; refusing to share it")
    yield server
    server.stop()


@pytest.fixture(scope="module")
def media_org():
    """Media Test Org id by name (no AI config needed to discover it —
    tests_live's live_org fixture requires one, and the org currently has
    none)."""
    with SessionLocal() as db:
        org_id = db.execute(text(
            "select id from organizations where name = 'Media Test Org'"),
            ).scalar()
    assert org_id is not None, "Media Test Org not found"
    return str(org_id)


@pytest.fixture(scope="module")
def stub_ai_config(stub, media_org):
    """READ-ONLY guard: verify the test org already points at this run's
    stub provider, and skip otherwise. This fixture NEVER writes
    ai_configs — no update, no insert, no delete.

    Why: the previous version of this fixture switched the org's config to
    the stub and restored it afterwards (and deleted the stub row it had
    inserted when none existed). That mutates shared live state: under
    concurrent runs it could clobber another run's config, and its
    teardown deleted the only active config present, which is what broke
    live widget chat at 2026-11-24 19:56 UTC (the regression saw
    ai_config_missing right after this fixture's teardown ran). The AI
    config belongs to the owner / the live-verification agent alone.

    To run this suite: the live-verification agent points Media Test Org's
    active AI config at the stub — model_type OPENAI_COMPATIBLE,
    model_name 'stub-model', settings {"base_url": stub.base_url}, i.e.
    http://127.0.0.1:18099/v1 — and points it back afterwards.
    """
    org_id = media_org
    with SessionLocal() as db:
        row = db.execute(text(
            "select model_type, model_name, settings "
            "from ai_configs where organization_id = :o and is_active"),
            {"o": org_id}).first()
    if row is None:
        pytest.skip(
            "no active AI config for Media Test Org — the live-verification "
            "agent owns AI config; point it at the stub to run the live suite")
    model_type, model_name, settings = row
    base_url = (settings or {}).get("base_url")
    if model_name != "stub-model" or base_url != stub.base_url:
        pytest.skip(
            f"active AI config is {model_type}/{model_name} "
            f"(base_url={base_url}), not this run's stub provider "
            f"({stub.base_url}) — refusing to switch it; the "
            "live-verification agent owns AI config")
    yield stub
    # Teardown intentionally does nothing: this fixture writes nothing,
    # so there is nothing to restore and nothing that can delete a config.


@pytest.fixture(scope="module", autouse=True)
def _cleanup_created(stub_ai_config):
    yield
    if not _CREATED:
        return
    conv_ids = [c for c, _ in _CREATED]
    cust_ids = [c for _, c in _CREATED if c]
    with SessionLocal() as db:
        try:
            db.execute(text(
                "delete from chat_history where session_id = any(cast(:ids as uuid[]))"),
                {"ids": conv_ids})
            db.execute(text(
                "delete from session_to_agents where session_id = any(cast(:ids as uuid[]))"),
                {"ids": conv_ids})
            if cust_ids:
                db.execute(text(
                    "delete from customers where id = any(cast(:ids as uuid[]))"),
                    {"ids": cust_ids})
            db.commit()
        except Exception as e:  # noqa: BLE001 - best-effort test cleanup
            print(f"[cleanup] leftover test rows ({e}); see _CREATED ids")


@pytest.fixture
def empty_kb_widget(media_org):
    """A temp agent + widget in the test org with NO KB linked (empty KB)."""
    org_id = media_org
    with SessionLocal() as db:
        agent = AgentRepository(db).create_agent(
            name="Temp Empty KB Agent",
            agent_type=AgentType.CUSTOMER_SUPPORT,
            instructions=["You are a helpful test support agent. Answer concisely."],
            org_id=org_id)
        widget = WidgetRepository(db).create_widget(
            widget=WidgetCreate(name="Temp Empty KB Widget",
                                agent_id=str(agent.id)),
            organization_id=org_id)
        agent_id, widget_id = str(agent.id), str(widget.id)
    yield widget_id
    with SessionLocal() as db:
        db.execute(text("delete from widgets where id = :w"), {"w": widget_id})
        db.execute(text("delete from agents where id = :a"), {"a": agent_id})
        db.commit()


# --- helpers ----------------------------------------------------------------

def _start(widget_id=None, stub_ai_config=None):
    tag = uuid.uuid4().hex[:8]
    resp = requests.post(
        f"{BASE}/api/v1/mobile/conversations",
        json={"widget_id": widget_id or WIDGET_ID,
              "visitor_name": "Stream Tester",
              "visitor_email": f"stream-{tag}@example.com"},
        headers={"X-Device-Id": f"stream-device-{tag}"},
        timeout=15)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _CREATED.append((body["conversation_id"], body.get("customer_id")))
    return body, f"stream-device-{tag}"


def _stream_sse(conv, token, device_id, message, timeout=120, on_first_token=None):
    """POST .../messages/stream; returns (events, ttft_seconds, total)."""
    t0 = time.time()
    ttft = None
    events = []
    name, data = None, []

    def _flush():
        nonlocal name, data
        if name is not None or data:
            try:
                events.append((name, json.loads("\n".join(data))))
            except json.JSONDecodeError:
                pass
        name, data = None, []

    with requests.post(
            f"{BASE}/api/v1/mobile/conversations/{conv['conversation_id']}/messages/stream",
            headers={"X-Conversation-Token": token, "X-Device-Id": device_id},
            data={"message": message}, stream=True, timeout=(10, timeout)) as resp:
        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")
        for line in resp.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if line == "":
                _flush()
                continue
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
                if name == "token" and ttft is None:
                    ttft = time.time() - t0
                    if on_first_token is not None:
                        on_first_token()
    _flush()
    return events, ttft, time.time() - t0


def _messages(conv_id, token, device_id=None):
    headers = {"X-Conversation-Token": token}
    if device_id:
        headers["X-Device-Id"] = device_id
    resp = requests.get(
        f"{BASE}/api/v1/mobile/conversations/{conv_id}/messages",
        headers=headers, timeout=15)
    return resp


def _wait_for_bot(conv_id, token, device_id, timeout=90):
    """Poll GET /messages until a bot reply appears; returns the messages."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _messages(conv_id, token, device_id)
        assert resp.status_code == 200, resp.text
        msgs = resp.json()["messages"]
        bots = [m for m in msgs if m["sender"] == "ai"]
        if bots:
            return msgs
        time.sleep(2)
    raise AssertionError(f"no bot reply within {timeout}s for {conv_id}")


def _socket_client(token):
    """A connected widget-socket client; returns (client, collected dict)."""
    collected = {"events": []}
    client = socketio.Client(logger=False, engineio_logger=False)

    def _record(event):
        def _handler(data=None):
            collected["events"].append((event, data))
        return _handler

    for ev in ("session_initialized", "bot_typing",
               "chat_response_stream_start", "chat_response_stream_token",
               "chat_response_stream_done", "chat_response_stream_error",
               "chat_response", "error"):
        client.on(ev, _record(ev), namespace="/widget")
    client.connect(BASE, namespaces=["/widget"],
                   auth={"conversation_token": token}, wait_timeout=15)
    return client, collected


def _wait_for(collected, event, timeout=90, count=1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in collected["events"] if e[0] == event]
        if len(found) >= count:
            return found
        time.sleep(0.2)
    raise AssertionError(
        f"event {event!r} not seen ({len([e for e in collected['events'] if e[0] == event])}/{count}) "
        f"in {timeout}s; got {[e[0] for e in collected['events']]}")


# --- mobile SSE -------------------------------------------------------------

def test_mobile_sse_kb_hit_no_model_call(stub, stub_ai_config):
    """KB hit: start(kb) -> one token with the article -> done; the stub
    provider is never called (no model call at all)."""
    stub.deltas = list(SHORT_DELTAS)
    before = len(stub.requests)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    events, ttft, total = _stream_sse(
        conv, conv["conversation_token"], device_id,
        "How do I reset my password?")

    assert [n for n, _ in events] == ["start", "token", "done"]
    assert events[0][1]["source"] == "kb"
    assert events[0][1]["message_id"] is not None
    assert "Forgot password" in events[1][1]["delta"]
    done = events[-1][1]
    assert done["source"] == "kb"
    assert "Forgot password" in done["message"]
    assert any(s.get("name") == "How to reset your password"
               for s in (done.get("sources") or [])), done
    assert ttft is not None and ttft < 15, f"KB TTFT too slow: {ttft}"
    assert total < 30
    assert len(stub.requests) == before, "KB hit must not call the model"


def test_mobile_sse_kb_miss_streams_from_stub(stub, stub_ai_config):
    """KB miss: the question shares no keywords with any article, so the
    lean model streams token by token from the stub provider."""
    stub.deltas = list(PARIS_DELTAS)
    before = len(stub.requests)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    events, ttft, total = _stream_sse(
        conv, conv["conversation_token"], device_id,
        "What is the capital of France?")

    names = [n for n, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert names.count("token") == len(PARIS_DELTAS)
    assert events[0][1]["source"] == "model-stream"
    text = "".join(d[1]["delta"] for d in events if d[0] == "token")
    assert text == "The capital of France is Paris."
    assert events[-1][1]["message"] == text
    assert ttft is not None and ttft < 10, f"TTFT too slow: {ttft}"
    assert total < 60
    assert len(stub.requests) > before
    streamed = [r for r in stub.requests[before:] if r.get("stream") is True]
    assert streamed and streamed[0].get("model") == "stub-model"


def test_mobile_sse_empty_kb_falls_through_to_model(stub, stub_ai_config, empty_kb_widget):
    """Empty KB (agent with no linked sources): even a question that WOULD
    match an article elsewhere goes to the model — no kb source, no crash."""
    stub.deltas = list(PARIS_DELTAS)
    conv, device_id = _start(widget_id=empty_kb_widget,
                             stub_ai_config=stub_ai_config)
    events, ttft, total = _stream_sse(
        conv, conv["conversation_token"], device_id,
        "How do I reset my password?")

    names = [n for n, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert events[0][1]["source"] == "model-stream", events[0]
    text = "".join(d[1]["delta"] for d in events if d[0] == "token")
    assert text == "The capital of France is Paris."
    assert events[-1][1]["message"] == text


def test_mobile_sse_long_reply_streams_progressively(stub, stub_ai_config):
    """Long reply: many tokens, first token long before done (progressive,
    not buffered until the end)."""
    stub.deltas = list(LONG_DELTAS)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    events, ttft, total = _stream_sse(
        conv, conv["conversation_token"], device_id,
        "Explain in detail how your support works, step by step")

    tokens = [d[1]["delta"] for d in events if d[0] == "token"]
    assert len(tokens) > 40, f"only {len(tokens)} tokens"
    done = events[-1][1]
    assert len(done["message"]) > 1500
    assert "".join(tokens) == done["message"]
    assert ttft is not None and ttft < 10
    assert total > ttft * 2, f"reply arrived all at once (ttft={ttft}, total={total})"


def test_mobile_sse_short_reply(stub, stub_ai_config):
    """Very short reply: few tokens, fast done."""
    stub.deltas = list(SHORT_DELTAS)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    events, ttft, total = _stream_sse(
        conv, conv["conversation_token"], device_id, "Hi")

    tokens = [d for d in events if d[0] == "token"]
    assert len(tokens) == len(SHORT_DELTAS)
    assert events[-1][1]["message"] == "Hi there! How can I help?"
    assert total < 30


def test_mobile_sse_token_rotation_mid_stream(stub, stub_ai_config):
    """Rotate the 1-hour visitor token while a long reply is streaming: the
    stream must finish, the old token must stop working, the new one must
    work and show the stored reply."""
    stub.deltas = list(LONG_DELTAS)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    conv_id = conv["conversation_id"]
    old_token = conv["conversation_token"]

    result = {}
    first_token = threading.Event()

    def _read():
        try:
            result["events"], result["ttft"], result["total"] = _stream_sse(
                conv, old_token, device_id,
                "Explain in detail how your support works, step by step",
                on_first_token=first_token.set)
        except Exception as e:  # noqa: BLE001 - surfaced by the assertions
            result["error"] = e

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    # Rotate WHILE the reply is still streaming (first token in, ~100 more
    # to come): the stream must survive the rotation.
    assert first_token.wait(timeout=30), "no token arrived to rotate under"
    resp = requests.post(
        f"{BASE}/api/v1/mobile/conversations/{conv_id}/rotate",
        headers={"X-Conversation-Token": old_token,
                 "X-Device-Id": device_id}, timeout=15)
    assert resp.status_code == 200, resp.text
    new_token = resp.json()["conversation_token"]
    assert new_token != old_token

    thread.join(timeout=120)
    assert "error" not in result, result["error"]
    events = result["events"]
    assert [n for n, _ in events][-1] == "done"
    assert len([n for n, _ in events if n == "token"]) > 40
    assert events[-1][1]["message"] == LONG_TEXT

    # Old token revoked, new token works and shows the one stored reply.
    assert _messages(conv_id, old_token, device_id).status_code == 401
    msgs = _messages(conv_id, new_token, device_id)
    assert msgs.status_code == 200, msgs.text
    bots = [m for m in msgs.json()["messages"] if m["sender"] == "ai"]
    assert len(bots) == 1
    assert bots[0]["text"] == LONG_TEXT


def test_mobile_nonstreaming_fallback_still_works(stub, stub_ai_config):
    """POST .../messages (non-streaming) still answers via the full
    structured turn against the stub provider."""
    stub.deltas = [FULL_TURN_JSON]
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    resp = requests.post(
        f"{BASE}/api/v1/mobile/conversations/{conv['conversation_id']}/messages",
        headers={"X-Conversation-Token": conv["conversation_token"],
                 "X-Device-Id": device_id},
        data={"message": "What is the capital of France?"},
        timeout=120)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ai_reply_pending"] is True

    msgs = _wait_for_bot(conv["conversation_id"],
                         conv["conversation_token"], device_id)
    bots = [m for m in msgs if m["sender"] == "ai"]
    assert len(bots) == 1
    assert bots[0]["text"] == (
        "Stub full-turn reply: the capital of France is Paris.")
    non_streamed = [r for r in stub.requests if r.get("stream") is not True]
    assert non_streamed, "full turn should make a non-streamed call"
    assert non_streamed[-1].get("response_format") is not None


# --- widget Socket.IO -------------------------------------------------------

def test_widget_socket_kb_hit_streams(stub, stub_ai_config):
    """Widget socket end to end: KB question -> bot_typing, stream start /
    token / done (source kb), then exactly one chat_response with sources."""
    stub.deltas = list(SHORT_DELTAS)
    before = len(stub.requests)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    client, collected = _socket_client(conv["conversation_token"])
    try:
        _wait_for(collected, "session_initialized", timeout=15)
        client.emit("chat", {"message": "How do I reset my password?"},
                    namespace="/widget")
        _wait_for(collected, "chat_response", timeout=60)
    finally:
        client.disconnect()

    names = [n for n, _ in collected["events"]]
    assert "bot_typing" in names
    assert names.index("bot_typing") < names.index("chat_response_stream_start")
    assert names.index("chat_response_stream_start") < names.index(
        "chat_response_stream_token") < names.index(
        "chat_response_stream_done") < names.index("chat_response")
    assert names.count("chat_response") == 1
    start = next(d for n, d in collected["events"]
                 if n == "chat_response_stream_start")
    assert start["source"] == "kb"
    assert start["message_id"] is not None
    final = next(d for n, d in collected["events"] if n == "chat_response")
    assert "Forgot password" in final["message"]
    assert any(s.get("name") == "How to reset your password"
               for s in (final.get("sources") or [])), final
    assert len(stub.requests) == before, "KB hit must not call the model"


def test_widget_socket_drop_mid_stream_then_reconnect(stub, stub_ai_config):
    """Connection drop mid-stream: disconnect after the first token, then
    reconnect with the same token. The reply must still be stored exactly
    once, with no duplicate and no crash."""
    stub.deltas = list(LONG_DELTAS)
    conv, device_id = _start(stub_ai_config=stub_ai_config)
    token = conv["conversation_token"]
    conv_id = conv["conversation_id"]

    client, collected = _socket_client(token)
    try:
        _wait_for(collected, "session_initialized", timeout=15)
        client.emit("chat",
                    {"message": "Explain in detail how your support works, step by step"},
                    namespace="/widget")
        _wait_for(collected, "chat_response_stream_token", timeout=60)
    finally:
        client.disconnect()

    # The server finishes the turn without the socket: the stored history
    # must hold exactly one bot reply with the full text.
    msgs = _wait_for_bot(conv_id, token, device_id, timeout=90)
    bots = [m for m in msgs if m["sender"] == "ai"]
    assert len(bots) == 1, msgs
    assert bots[0]["text"] == LONG_TEXT

    # Reconnect with the same token: same conversation, still healthy.
    client2, collected2 = _socket_client(token)
    try:
        inits = _wait_for(collected2, "session_initialized", timeout=15)
        assert inits[0][1]["session_id"] == conv_id
    finally:
        client2.disconnect()
    assert requests.get(f"{BASE}/health", timeout=5).json()["status"] == "healthy"
    msgs2 = _messages(conv_id, token, device_id).json()["messages"]
    assert len([m for m in msgs2 if m["sender"] == "ai"]) == 1
