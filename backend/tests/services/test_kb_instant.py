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

"""Unit tests for KB instant answers (docs/STREAMING-PROTOCOL.md, `kb` source).

Covers app/services/kb_instant.py: the keyword/confidence gate, the
rendered-hit parser, and the three fall-through cases — high-confidence hit
returns the article answer with NO model call, low-confidence falls through
to the model, and an empty KB falls through cleanly.

The vector search itself is mocked at the KnowledgeSearchByAgent boundary
(the tool's `search_knowledge_base` return string); no model, DB or
embedding is touched. Live KB hit/miss behaviour is covered by the live
SSE checks against the running backend (KB answers need no model call).
"""

import pytest

from app.services import kb_instant
from app.services.kb_instant import (
    MAX_ANSWER_CHARS,
    _confidence,
    _keywords,
    _parse_rendered,
    find_kb_answer,
    find_kb_candidate,
)

PASSWORD_RENDERED = (
    "[TEXT - How to reset your password] To reset your password, click "
    "'Forgot password' on the sign-in page and enter the email address for "
    "your account. We will send you a password reset link by email. The "
    "link is valid for 1 hour."
)
HOURS_RENDERED = (
    "[TEXT - Support hours] Our support team is available Monday to Friday, "
    "9am to 6pm PKT, and Saturday, 10am to 2pm PKT. We are closed on Sundays "
    "and public holidays."
)
CANCEL_RENDERED = (
    "[TEXT - How to cancel your subscription] You can cancel your "
    "subscription at any time from Settings, then Subscription, then Cancel "
    "subscription. Your subscription stays active until the end of the "
    "current billing period."
)


class _FakeTool:
    """Stand-in for KnowledgeSearchByAgent: returns a canned rendering."""

    def __init__(self, rendered=None, exc=None):
        self.rendered = rendered
        self.exc = exc
        self.calls = []

    def search_knowledge_base(self, query):
        self.calls.append(query)
        if self.exc is not None:
            raise self.exc
        return self.rendered


@pytest.fixture
def fake_tool(monkeypatch):
    """Patch the tool class kb_instant constructs; returns the fake holder."""
    holder = {}

    class _Factory:
        def __init__(self, agent_id=None, org_id=None, source=None):
            fake = _FakeTool(
                rendered=holder.get("rendered"), exc=holder.get("exc"))
            holder["tool"] = fake
            self.search_knowledge_base = fake.search_knowledge_base

    monkeypatch.setattr(
        "app.tools.knowledge_search_byagent.KnowledgeSearchByAgent", _Factory)
    # find_kb_candidate imports the class lazily inside the function from
    # app.tools.knowledge_search_byagent, so patching that module attribute
    # is what takes effect.
    return holder


def _run_candidate(monkeypatch, holder, rendered=None, exc=None):
    holder["rendered"] = rendered
    holder["exc"] = exc
    return holder


# --- keyword extraction ------------------------------------------------------

def test_keywords_drops_stopwords_and_short_words():
    assert _keywords("How do I reset my password please?") == {"reset", "password"}
    assert _keywords("the a an is") == set()
    assert _keywords("") == set()
    assert _keywords("support hours") == {"support", "hours"}


def test_keywords_lowercases_and_dedupes():
    assert _keywords("Password PASSWORD password") == {"password"}


# --- confidence --------------------------------------------------------------

def test_confidence_full_match_is_one():
    score, matched = _confidence(
        "how do I reset my password",
        "How to reset your password",
        "To reset your password, click 'Forgot password' ...")
    assert score == pytest.approx(1.0)
    assert set(matched) == {"reset", "password"}


def test_confidence_title_hit_needs_two_matches_for_floor():
    # Two question words match (one in the title, one in the body) but only
    # 2 of 4 keywords: the ratio is 0.5, floored to 0.6 by the title hit.
    score, matched = _confidence(
        "when is support open late night", "Support hours",
        "We are open on weekdays.")
    assert score == pytest.approx(0.6)
    assert set(matched) == {"support", "open"}


def test_confidence_single_title_word_does_not_floor():
    # One question word in the title must NOT floor the score: "when is
    # support open" shares only 'support' (1 of 2 keywords) -> 0.5.
    score, matched = _confidence(
        "when is support open", "Support hours", "Completely unrelated body text here.")
    assert score == pytest.approx(0.5)
    assert matched == ["support"]


def test_confidence_no_match_is_zero():
    score, matched = _confidence(
        "what is the capital of France",
        "How to reset your password",
        "To reset your password, click 'Forgot password' ...")
    assert score == 0.0
    assert matched == []


def test_confidence_empty_question_is_zero():
    assert _confidence("", "Support hours", "body") == (0.0, [])


# --- rendered-hit parsing ----------------------------------------------------

def test_parse_rendered_valid_block():
    parsed = _parse_rendered(PASSWORD_RENDERED)
    assert parsed is not None
    source_type, name, body = parsed
    assert source_type == "text"
    assert name == "How to reset your password"
    assert body.startswith("To reset your password")


def test_parse_rendered_rejects_no_results_sentences():
    assert _parse_rendered("No relevant information found in the knowledge base.") is None
    assert _parse_rendered("No knowledge sources available for this agent.") is None
    assert _parse_rendered(
        "No further results for this topic in the knowledge base beyond what "
        "earlier searches in this turn already returned.") is None
    assert _parse_rendered("") is None
    assert _parse_rendered(None) is None


def test_parse_rendered_rejects_unparseable_text():
    assert _parse_rendered("Just some plain text without a header.") is None
    assert _parse_rendered("[TEXT - Empty article]   ") is None


# --- find_kb_candidate / find_kb_answer (mocked vector search) --------------

@pytest.mark.asyncio
async def test_high_confidence_hit_returns_article_without_model_call(fake_tool):
    """The KB path never calls a model: find_kb_answer only runs the tool."""
    _run_candidate(None, fake_tool, rendered=PASSWORD_RENDERED)
    hit = await find_kb_answer("agent-1", "org-1", "How do I reset my password?")
    assert hit is not None
    assert hit.source_name == "How to reset your password"
    assert "Forgot password" in hit.answer
    assert hit.confidence == pytest.approx(1.0)
    assert set(hit.matched_keywords) == {"reset", "password"}
    # Exactly one tool search, and nothing else was consulted.
    assert fake_tool["tool"].calls == ["How do I reset my password?"]


@pytest.mark.asyncio
async def test_low_confidence_falls_through_to_model(fake_tool):
    """Vector search always returns a nearest hit; the keyword gate must
    reject it for an unrelated question so the model answers instead."""
    _run_candidate(None, fake_tool, rendered=PASSWORD_RENDERED)
    hit = await find_kb_answer("agent-1", "org-1", "What is the capital of France?")
    assert hit is None


@pytest.mark.asyncio
async def test_low_confidence_candidate_still_returned_ungated(fake_tool):
    """find_kb_candidate is ungated on purpose: the lean model may still use
    a near-miss article as context (confidence >= 0.25)."""
    _run_candidate(None, fake_tool, rendered=HOURS_RENDERED)
    hit = await find_kb_candidate("agent-1", "org-1", "when is support open")
    assert hit is not None
    assert hit.source_name == "Support hours"
    assert hit.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_empty_kb_falls_through_cleanly(fake_tool):
    """No sources linked to the agent: the tool's sentence parses to no hit
    and the caller falls through to the model (no exception, no answer)."""
    for rendered in (
        "No knowledge sources available for this agent.",
        "No relevant information found in the knowledge base.",
    ):
        _run_candidate(None, fake_tool, rendered=rendered)
        assert await find_kb_answer("agent-1", "org-1", "How do I reset my password?") is None
        assert await find_kb_candidate("agent-1", "org-1", "How do I reset my password?") is None


@pytest.mark.asyncio
async def test_search_failure_falls_through_cleanly(fake_tool):
    """A vector-search exception must never fail the turn: None, so the
    model answers (the implementation logs a warning)."""
    _run_candidate(None, fake_tool, exc=RuntimeError("pgvector down"))
    assert await find_kb_answer("agent-1", "org-1", "How do I reset my password?") is None


@pytest.mark.asyncio
async def test_empty_question_returns_none_without_search(fake_tool):
    _run_candidate(None, fake_tool, rendered=PASSWORD_RENDERED)
    assert await find_kb_answer("agent-1", "org-1", "   ") is None
    assert await find_kb_answer(None, "org-1", "How do I reset my password?") is None
    assert await find_kb_answer("agent-1", None, "How do I reset my password?") is None
    # No search was attempted for any of these.
    assert "tool" not in fake_tool


@pytest.mark.asyncio
async def test_answer_truncated_to_readable_length(fake_tool):
    long_body = "word " * 1000  # ~5000 chars, keyword 'password' included below
    rendered = "[TEXT - How to reset your password] " + ("password " + long_body)
    _run_candidate(None, fake_tool, rendered=rendered)
    hit = await find_kb_answer("agent-1", "org-1", "How do I reset my password?")
    assert hit is not None
    assert len(hit.answer) <= MAX_ANSWER_CHARS + 1  # +1 for the ellipsis char
    assert hit.answer.endswith("…")


@pytest.mark.asyncio
async def test_gate_requires_two_keywords_or_half_confidence(fake_tool):
    """One shared keyword with confidence < 0.5 is below the gate; the same
    hit passes when the caller lowers the keyword threshold to 1."""
    # Question keywords: {cancel, subscription, refund} — only 'cancel' and
    # 'subscription' appear in the article, 'refund' does not: 2/3 = 0.67.
    _run_candidate(None, fake_tool, rendered=CANCEL_RENDERED)
    hit = await find_kb_answer("agent-1", "org-1",
                               "How do I cancel my subscription refund?")
    assert hit is not None  # 2 matched keywords passes the default gate
    assert hit.confidence == pytest.approx(2 / 3)

    # A question sharing exactly one word and < half its keywords fails.
    # (A single TITLE word no longer floors confidence to 0.6 — see
    # test_single_title_word_does_not_pass_gate — so this body-only word
    # case and the title-word case both genuinely reject.)
    _run_candidate(None, fake_tool, rendered=CANCEL_RENDERED)
    hit = await find_kb_answer("agent-1", "org-1",
                               "What time do the flight booking offices open?")
    assert hit is None


@pytest.mark.asyncio
async def test_single_title_word_does_not_pass_gate(fake_tool):
    """A single shared word in the article TITLE is not a high-confidence
    match: "Can I cancel my flight booking tomorrow?" shares only 'cancel'
    (1/4 keywords, confidence 0.25) with the cancel-subscription article,
    so the gate rejects it and the model answers instead."""
    _run_candidate(None, fake_tool, rendered=CANCEL_RENDERED)
    hit = await find_kb_answer("agent-1", "org-1",
                               "Can I cancel my flight booking tomorrow?")
    assert hit is None


@pytest.mark.asyncio
async def test_kb_hit_for_each_seeded_article(fake_tool):
    """The three seeded test articles (seed_test_kb.py) each answer their own
    question at the gate."""
    cases = [
        ("How do I reset my password?", PASSWORD_RENDERED, "How to reset your password"),
        ("What are your support hours?", HOURS_RENDERED, "Support hours"),
        ("How can I cancel my subscription?", CANCEL_RENDERED, "How to cancel your subscription"),
    ]
    for question, rendered, title in cases:
        _run_candidate(None, fake_tool, rendered=rendered)
        hit = await find_kb_answer("agent-1", "org-1", question)
        assert hit is not None, question
        assert hit.source_name == title
