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

"""KB-covered questions are answered by the MODEL, grounded in KB context.

Now that the instant path is gone, a question the knowledge base covers
must reach the model with the top article as background — not as an
article replay, and not as "I don't have...". These tests prove the
routing and the prompt: the lean stream receives short KB context for a
covered question (and none for an unrelated one), and the prompt sent to
the model carries that background plus the instruction to answer briefly
in its own words, never quoting or pasting it.

The model itself is the stub hosted provider (tests/stub_provider.py): it
replays scripted deltas, so the reply wording is the stub's, not a real
model's. What is asserted here is what the code controls — the context
the model is given, the instruction it is given, and that the turn goes
to the model at all. Live wording is covered by the live SSE checks
against the running backend.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.chat_agent import ChatAgent, ChatResponse
from app.services.kb_context import KBContext
from tests.stub_provider import StubProviderServer


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


def _kb_ctx():
    return KBContext(
        context="To reset your password, click 'Forgot password' on the "
                "sign-in page and enter your email address. We will send "
                "you a password reset link by email.",
        source_name="How to reset your password",
        source_type="text",
        confidence=1.0,
        matched_keywords=["reset", "password"])


@pytest.mark.asyncio
async def test_covered_question_goes_to_model_with_kb_context(agent, monkeypatch):
    """A KB-covered question is answered by the MODEL (source model-stream),
    with the article as background — no instant article replay, no "kb"
    source event, and the lean turn receives the context."""
    monkeypatch.setattr("app.services.kb_context.find_kb_context",
                        AsyncMock(return_value=_kb_ctx()))
    seen = {}

    async def _lean(run_message, session_id, history_text, kb_context):
        seen["kb_context"] = kb_context
        yield "Click 'Forgot password' "

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Click 'Forgot password'."))

    events = await _collect(agent, message="How do I reset my password?")
    assert _types(events) == ["start", "token", "done"]
    assert all(e.get("source") == "model-stream" for e in events)
    assert "Forgot password" in seen["kb_context"]
    # The context is background, not the whole article.
    assert len(seen["kb_context"]) <= 801
    assert "Support hours" not in seen["kb_context"]


@pytest.mark.asyncio
async def test_force_model_does_not_skip_kb_context(agent, monkeypatch):
    """Widget and mobile pass force_model=True on every streaming turn.
    That used to skip the KB lookup (it skipped the instant answer); the
    lean model has no search tool of its own, so skipping the pre-paste
    would leave it with no KB grounding at all. force_model must not
    strip the background."""
    monkeypatch.setattr("app.services.kb_context.find_kb_context",
                        AsyncMock(return_value=_kb_ctx()))
    seen = {}

    async def _lean(run_message, session_id, history_text, kb_context):
        seen["kb_context"] = kb_context
        yield "Click 'Forgot password' "

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Click 'Forgot password'."))

    events = await _collect(agent, message="How do I reset my password?",
                            force_model=True)
    assert _types(events) == ["start", "token", "done"]
    assert events[0]["source"] == "model-stream"
    assert seen["kb_context"] is not None
    assert "Forgot password" in seen["kb_context"]


@pytest.mark.asyncio
async def test_unrelated_question_gets_no_kb_context(agent, monkeypatch):
    """No KB hit: the model answers with no context pasted, so an unrelated
    question is never grounded in the wrong article."""
    monkeypatch.setattr("app.services.kb_context.find_kb_context",
                        AsyncMock(return_value=None))
    seen = {}

    async def _lean(run_message, session_id, history_text, kb_context):
        seen["kb_context"] = kb_context
        yield "Paris is the capital. "

    agent._lean_stream_tokens = _lean
    agent._finish_turn = AsyncMock(return_value=_reply("Paris is the capital."))

    events = await _collect(agent, message="What is the capital of France?")
    assert _types(events) == ["start", "token", "done"]
    assert events[0]["source"] == "model-stream"
    assert seen["kb_context"] is None


@pytest.mark.asyncio
async def test_lean_prompt_carries_background_and_own_words_instruction(monkeypatch):
    """The exact prompt the model receives for a KB-covered question: the
    background facts AND the instruction to answer in its own words, never
    quoting or pasting the background. Asserted on the request the stub
    provider recorded."""
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    stub = StubProviderServer(deltas=["Reset it from the sign-in page."],
                              chunk_delay=0.01).start()
    try:
        agent = object.__new__(ChatAgent)
        agent.model_type = "OPENAI_COMPATIBLE"
        agent.api_key = "stub-test-key"
        agent.model_name = "stub-model"
        agent.base_url = stub.base_url
        agent.agent_data = SimpleNamespace(name="Support")
        agent._guardrail_ctx = SimpleNamespace(org_name="Stub Org")

        kb_context = _kb_ctx().context
        parts = []
        async for delta in agent._lean_stream_tokens(
                "How do I reset my password?", "sess-stub", None, kb_context):
            parts.append(delta)
        assert "".join(parts) == "Reset it from the sign-in page."
        assert len(stub.requests) == 1
        sent = stub.requests[0]["messages"]
        # agno 1.7.6 sends the agent instructions as a "developer" message
        # (wrapped in <instructions>), not a "system" one — search every
        # message the model was sent.
        full_text = " ".join(str(m.get("content", "")) for m in sent)
        # The background facts reached the model.
        assert "Forgot password" in full_text
        # The model is told to answer in its own words, never to replay.
        assert "own words" in full_text
        assert "Never quote, copy or paste" in full_text
        assert "never say you do not have the information when the background covers" in full_text
        # And it is NOT told to dump the article: no verbatim instruction.
        assert "verbatim" not in full_text.lower()
    finally:
        stub.stop()


def test_lean_instructions_without_context_keep_the_say_so_fallback():
    """With no KB background the lean prompt keeps the honest fallback —
    say so and offer the team — instead of inventing an answer."""
    agent = object.__new__(ChatAgent)
    agent.agent_data = SimpleNamespace(name="Support")
    agent._guardrail_ctx = SimpleNamespace(org_name="Stub Org")
    instructions = agent._lean_instructions(None, None)
    assert "If you do not know the answer, say so" in instructions
    assert "Knowledge base background" not in instructions


def test_lean_instructions_with_context_forbid_replay():
    """With KB background the lean prompt tells the model to answer in its
    own words and never quote, copy or paste the background."""
    agent = object.__new__(ChatAgent)
    agent.agent_data = SimpleNamespace(name="Support")
    agent._guardrail_ctx = SimpleNamespace(org_name="Stub Org")
    instructions = agent._lean_instructions(None, _kb_ctx().context)
    assert "Knowledge base background" in instructions
    assert "Answer in your own words from the background above" in instructions
    assert "Never quote, copy or paste the background text" in instructions
    assert "never say you do not have the information when the background covers the question" in instructions
