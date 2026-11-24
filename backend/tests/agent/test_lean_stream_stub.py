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

"""Lean streaming against a stub hosted provider (no live model, no key).

The streaming path is provider-agnostic by construction — ChatAgent resolves
the provider/model from the dashboard AI config and builds the model with
app/utils/agno_utils.create_model — so these tests run the REAL lean path
(ChatAgent._lean_stream_tokens -> create_model -> agno Agent.arun(stream=True))
against tests/stub_provider.py, which mimics the OpenAI-compatible
chat-completions streaming API the hosted providers use. The API key here
is a dummy string that never leaves localhost; no real provider is called
and nothing is charged.
"""

import time
from types import SimpleNamespace

import pytest
from agno.agent import Agent

from app.agents.chat_agent import ChatAgent
from app.utils.agno_utils import create_model
from tests.stub_provider import StubProviderServer


@pytest.fixture
def stub(monkeypatch):
    """Stub provider up for one test; localhost bypasses any proxy."""
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    server = StubProviderServer(deltas=["Hello ", "streamed ", "world"],
                                chunk_delay=0.05).start()
    yield server
    server.stop()


def test_agno_openai_compatible_model_streams_from_stub(stub):
    """agno's OpenAI-compatible model yields content deltas token by token
    from the stub's SSE stream, and asks for a streaming completion."""
    model = create_model("OPENAI_COMPATIBLE", api_key="stub-test-key",
                         model_name="stub-model", max_tokens=100,
                         base_url=stub.base_url)
    agent = Agent(name="Stub", model=model,
                  instructions="Answer concisely.", markdown=False)
    import asyncio

    async def _run():
        t0 = time.time()
        ttft = None
        parts = []
        stream = await agent.arun("Say hello", stream=True)
        async for event in stream:
            content = getattr(event, "content", None)
            if isinstance(content, str) and content:
                if ttft is None:
                    ttft = time.time() - t0
                parts.append(content)
        return ttft, time.time() - t0, "".join(parts)

    ttft, total, text = asyncio.run(_run())
    assert text == "Hello streamed world"
    assert ttft is not None and ttft < 5.0
    assert total < 10.0
    assert len(stub.requests) == 1
    assert stub.requests[0].get("stream") is True
    assert stub.requests[0].get("model") == "stub-model"


def test_lean_stream_tokens_uses_stub_hosted_provider(stub):
    """ChatAgent._lean_stream_tokens — the exact code the widget/mobile
    streaming paths call — streams through the hosted-provider client."""
    agent = object.__new__(ChatAgent)
    agent.model_type = "OPENAI_COMPATIBLE"
    agent.api_key = "stub-test-key"
    agent.model_name = "stub-model"
    agent.base_url = stub.base_url
    agent.agent_data = SimpleNamespace(name="Support")
    agent._guardrail_ctx = SimpleNamespace(org_name="Stub Org")
    import asyncio

    async def _run():
        t0 = time.time()
        ttft = None
        parts = []
        async for delta in agent._lean_stream_tokens(
                "Say hello", "sess-stub", None, None):
            if ttft is None:
                ttft = time.time() - t0
            parts.append(delta)
        return ttft, time.time() - t0, "".join(parts)

    ttft, total, text = asyncio.run(_run())
    assert text == "Hello streamed world"
    assert ttft is not None and ttft < 5.0
    assert total < 10.0
    assert len(stub.requests) == 1
    assert stub.requests[0].get("stream") is True
    assert stub.requests[0].get("model") == "stub-model"
    # The lean prompt (not the full agent prompt) is what was sent.
    sent = stub.requests[0]["messages"]
    assert any("Answer concisely" in str(m.get("content", ""))
               for m in sent)
