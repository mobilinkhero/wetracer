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

"""Unit tests for ChatAgent.stream_response (docs/STREAMING-PROTOCOL.md v1).

Event protocol under test: start -> token* -> done, or error instead of
done; lean model turns (source "model-stream") emit one token per delta;
ineligible/failed lean turns fall back to the full non-streaming
turn (source "model-full": start + done, no tokens). There is no direct
KB answer path: every turn goes to the model, which answers from the KB
with its own search tool.

The agent is stubbed at the seams stream_response actually uses
(guardrail verdict, DB session, lean token generator,
full-turn runner) — no model, DB or embedding is touched. Live behaviour
is covered by the stub hosted-provider server in backend/tests/stub_provider.py
(verify_stub_stream.py) and the live SSE checks against the running backend.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.chat_agent import ChatAgent, ChatResponse


def _reply(text):
    return ChatResponse(
        message=text, transfer_to_human=False, end_chat=False,
        request_rating=False, create_ticket=False)


class _FakeRepo:
    """Stand-in for ChatRepository: user-message store only."""

    def __init__(self):
        self.created = []

    def create_message(self, data):
        self.created.append(data)
        return SimpleNamespace(id=101)

    def add(self, row):
        self.created.append(row)

    def commit(self):
        return None

    def flush(self):
        return None


class _FakeSessionCM:
    def __init__(self, repo):
        self.repo = repo

    def __enter__(self):
        return self.repo

    def __exit__(self, *exc):
        return False


@pytest.fixture
def agent(monkeypatch):
    """A ChatAgent with __init__ skipped and every external seam stubbed."""
    a = object.__new__(ChatAgent)
    a.session_id = None
    a.org_id = "org-1"
    a.agent_id = "agent-1"
    a.customer_id = "cust-1"
    a._guardrail_ctx = None
    a._guardrail_surface = None
    a.agent = SimpleNamespace(session_id=None)
    a.knowledge_tool = None
    a._groq_json_capture = {}
    a.tools = []
    a.extra_toolkits = None
    a._host_action_functions = None
    a.lead_capture_enabled = False
    a.lead_already_captured = False
    a.last_stream_response = None
    a.last_paused_turn = None
    a.last_bot_message_id = None
    a.last_handover = None
    a.last_ui_extras = {}
    a._use_groq_json_tool = False

    repo = _FakeRepo()
    monkeypatch.setattr("app.agents.chat_agent.SessionLocal",
                        lambda: _FakeSessionCM(repo))
    monkeypatch.setattr("app.agents.chat_agent.ChatRepository",
                        lambda db: repo)
    monkeypatch.setattr("app.agents.chat_agent.release_connection",
                        lambda db: None)
    monkeypatch.setattr(ChatAgent, "_compact_history",
                        staticmethod(AsyncMock(return_value=None)))
    verdict = SimpleNamespace(block=False, reply="",
                              as_attributes=lambda: {})
    monkeypatch.setattr("app.agents.chat_agent.check_inbound",
                        lambda *a, **k: verdict)
    monkeypatch.setattr("app.agents.chat_agent.check_output",
                        lambda msg, **k: (msg, None))
    a._verdict = verdict
    a._repo = repo

    def _store(chat_repo, data):
        a.last_bot_message_id = 777

    a._store_bot_message = _store
    a._finish_turn = AsyncMock(return_value=_reply("Finished reply"))
    a._arun = AsyncMock(return_value=SimpleNamespace())
    return a


async def _collect(agent, **kwargs):
    kwargs.setdefault("message", "Hello")
    kwargs.setdefault("session_id", "sess-1")
    kwargs.setdefault("org_id", "org-1")
    kwargs.setdefault("agent_id", "agent-1")
    kwargs.setdefault("customer_id", "cust-1")
    return [e async for e in agent.stream_response(**kwargs)]


def _types(events):
    return [e["type"] for e in events]


# --- lean streaming ----------------------------------------------------------

@pytest.mark.asyncio
async def test_lean_stream_token_order_and_concatenation(agent, monkeypatch):

    async def _lean(run_message, session_id, history_text, kb_context):
        for piece in ["Hello ", "streamed ", "world"]:
            yield piece

    agent._lean_stream_tokens = _lean

    async def _finish(*args, **kwargs):
        agent.last_bot_message_id = 777
        return _reply("Hello streamed world")

    agent._finish_turn = _finish

    events = await _collect(agent, message="Say hello")
    assert _types(events) == ["start", "token", "token", "token", "done"]
    assert events[0] == {"type": "start", "source": "model-stream",
                         "session_id": "sess-1", "message_id": None}
    assert "".join(e["delta"] for e in events if e["type"] == "token") == "Hello streamed world"
    assert all(e["source"] == "model-stream" for e in events)
    assert events[-1]["message_id"] == 777
    assert agent.last_stream_response.message == "Hello streamed world"


@pytest.mark.asyncio
async def test_lean_stream_failure_before_first_token_falls_back_silently(agent, monkeypatch):
    """No token ever arrived: no start event, no error — the full turn
    answers and the client just sees start(model-full) + done."""

    async def _lean(run_message, session_id, history_text, kb_context):
        raise RuntimeError("provider hiccup")
        yield "never"

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Full fallback reply"))

    events = await _collect(agent, message="Say hello")
    assert _types(events) == ["start", "done"]
    assert events[0]["source"] == "model-full"
    assert events[1]["source"] == "model-full"
    assert agent.last_stream_response.message == "Full fallback reply"


@pytest.mark.asyncio
async def test_lean_stream_failure_midway_keeps_partial_text_and_errors(agent, monkeypatch):
    """Tokens already sent: finish + store the partial text, emit error
    (no done). The widget then waits for chat_response with the finished
    reply; the SSE client treats error as terminal."""

    async def _lean(run_message, session_id, history_text, kb_context):
        yield "Partial "
        raise RuntimeError("connection reset")

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Partial finished"))

    events = await _collect(agent, message="Say hello")
    assert _types(events) == ["start", "token", "error"]
    assert events[1]["delta"] == "Partial "
    assert "Streaming interrupted" in events[2]["error"]
    assert agent.last_stream_response.message == "Partial finished"


@pytest.mark.asyncio
async def test_lean_stream_empty_falls_back_to_full_turn(agent, monkeypatch):

    async def _lean(run_message, session_id, history_text, kb_context):
        if False:
            yield "never"

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Full fallback reply"))

    events = await _collect(agent, message="Say hello")
    assert _types(events) == ["start", "done"]
    assert events[0]["source"] == "model-full"


# --- eligibility / fallback --------------------------------------------------

@pytest.mark.asyncio
async def test_human_request_is_ineligible_and_uses_full_turn(agent, monkeypatch):
    lean = AsyncMock()
    agent._lean_stream_tokens = lean
    agent._finish_turn = AsyncMock(return_value=_reply("Connecting you to a human."))

    events = await _collect(agent, message="Please speak to a human")
    assert _types(events) == ["start", "done"]
    assert events[0]["source"] == "model-full"
    lean.assert_not_called()


@pytest.mark.asyncio
async def test_goodbye_is_ineligible(agent, monkeypatch):
    agent._finish_turn = AsyncMock(return_value=_reply("Goodbye!"))
    events = await _collect(agent, message="goodbye")
    assert [e.get("source") for e in events] == ["model-full", "model-full"]


@pytest.mark.asyncio
async def test_attachments_are_ineligible(agent, monkeypatch):
    lean = AsyncMock()
    agent._lean_stream_tokens = lean
    agent._finish_turn = AsyncMock(return_value=_reply("Got the file."))
    events = await _collect(agent, message="See attached",
                            attachments=[{"filename": "x.png",
                                          "file_url": "/uploads/x.png",
                                          "content_type": "image/png",
                                          "size": 123}])
    assert events[0]["source"] == "model-full"
    lean.assert_not_called()


def test_stream_eligible_matrix(agent):
    assert agent._stream_eligible("What are your hours?") is True
    assert agent._stream_eligible("speak to a human") is False
    assert agent._stream_eligible("talk to a real person please") is False
    assert agent._stream_eligible("bye") is False
    assert agent._stream_eligible("hello", images=["img"]) is False
    agent.tools = ["some-toolkit"]
    assert agent._stream_eligible("hello") is False
    agent.tools = None
    agent._host_action_functions = ["act"]
    assert agent._stream_eligible("hello") is False


# --- guardrail block / paused / error ----------------------------------------

@pytest.mark.asyncio
async def test_guardrail_block_answers_without_model(agent, monkeypatch):
    agent._verdict.block = True
    agent._verdict.reply = "That message was blocked."
    agent._arun = AsyncMock(side_effect=AssertionError("model must not run"))

    events = await _collect(agent, message="bad message")
    assert _types(events) == ["start", "token", "done"]
    assert events[0]["source"] == "model-full"
    assert events[1]["delta"] == "That message was blocked."
    assert agent.last_stream_response.message == "That message was blocked."


@pytest.mark.asyncio
async def test_paused_turn_yields_done_paused_and_no_response(agent, monkeypatch):
    # Force the full-turn branch: lean ineligible via a host action.
    agent._host_action_functions = ["act"]
    paused = SimpleNamespace(action="ask_page")
    monkeypatch.setattr("app.agents.chat_agent.paused_turn",
                        lambda response: paused)

    events = await _collect(agent, message="Do the thing")
    assert events == [{"type": "done", "source": "model-full",
                       "session_id": "sess-1", "paused": True}]
    assert agent.last_paused_turn is paused
    assert agent.last_stream_response is None


@pytest.mark.asyncio
async def test_full_turn_failure_yields_error_event(agent, monkeypatch):

    async def _lean(run_message, session_id, history_text, kb_context):
        raise RuntimeError("lean down")
        yield "never"

    agent._lean_stream_tokens = _lean
    agent._arun = AsyncMock(side_effect=RuntimeError("model exploded"))

    events = await _collect(agent, message="Say hello")
    assert _types(events) == ["error"]
    assert "model exploded" in events[0]["error"]
    assert agent.last_stream_response is None
