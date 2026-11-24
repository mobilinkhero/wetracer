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

"""Unit tests for KB context for the model (app/services/kb_context.py).

The model answers KB questions itself now that the instant path is gone:
the top matching article is pasted into the lean prompt as background
only, and the model answers briefly in its own words — never an article
replay. Covers the keyword/confidence gate, the rendered-hit parser, and
the fall-through cases — a covered question gets short background context,
an unrelated question gets none, and a one-word overlap must not ground
the answer in the wrong article.

The vector search itself is mocked at the KnowledgeSearchByAgent boundary
(the tool's `search_knowledge_base` return string); no model, DB or
embedding is touched. Live KB grounding is covered by the stub
hosted-provider checks (the lean prompt sent to the model carries the
context) and the live SSE checks against the running backend.
"""

import asyncio

import pytest

from app.services import kb_context
from app.services.kb_context import (
    MAX_CONTEXT_CHARS,
    _confidence,
    _keywords,
    _parse_rendered,
    _trim_context,
    find_kb_context,
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
# Two blocks: the parser must return the FIRST one only, so context never
# carries an unrelated article.
TWO_BLOCKS_RENDERED = PASSWORD_RENDERED + "\n\n" + HOURS_RENDERED


class _FakeTool:
    """Stand-in for KnowledgeSearchByAgent: returns a canned rendering."""

    def __init__(self, rendered=None, exc=None):
        self.rendered = rendered
        self.exc = exc
        self.calls = []
        self.collected_sources = []

    def search_knowledge_base(self, query):
        self.calls.append(query)
        if self.exc is not None:
            raise self.exc
        return self.rendered


@pytest.fixture
def fake_tool(monkeypatch):
    """Patch the tool class find_kb_context constructs; returns the holder."""
    holder = {}

    class _Factory:
        def __init__(self, agent_id=None, org_id=None, source=None):
            fake = _FakeTool(
                rendered=holder.get("rendered"), exc=holder.get("exc"))
            holder["tool"] = fake
            self.search_knowledge_base = fake.search_knowledge_base

    monkeypatch.setattr(
        "app.tools.knowledge_search_byagent.KnowledgeSearchByAgent", _Factory)
    return holder


def _run(holder, rendered=None, exc=None, question="How do I reset my password?"):
    holder["rendered"] = rendered
    holder["exc"] = exc
    return asyncio.run(find_kb_context("agent-1", "org-1", question))


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
        "To reset your password, click 'Forgot password' on the sign-in page.")
    assert score == 1.0
    assert set(matched) == {"reset", "password"}


def test_confidence_two_title_words_floor_at_point_six():
    # Question names the article's subject (reset + password in the title)
    # even though only half its words match: 2/4 = 0.5 floors to 0.6.
    score, matched = _confidence(
        "reset password now please help", "How to reset your password",
        "Something else entirely.")
    assert score == 0.6
    assert set(matched) == {"reset", "password"}


def test_confidence_single_title_word_does_not_floor():
    # "cancel" alone in the title must not floor: a flight-booking question
    # sharing one word with the cancellation article is not grounded in it.
    score, matched = _confidence(
        "Can I cancel my flight booking tomorrow?",
        "How to cancel your subscription",
        "You can cancel your subscription at any time from Settings.")
    assert score < 0.6
    assert "cancel" in matched


# --- rendered parser ---------------------------------------------------------

def test_parse_rendered_first_block_only():
    parsed = _parse_rendered(TWO_BLOCKS_RENDERED)
    assert parsed is not None
    source_type, name, body = parsed
    assert source_type == "text"
    assert name == "How to reset your password"
    assert "Forgot password" in body
    assert "Support hours" not in body
    assert "Monday to Friday" not in body


def test_parse_rendered_rejects_no_results():
    assert _parse_rendered("No relevant information found in the knowledge base.") is None
    assert _parse_rendered("No further results for this topic in the knowledge base beyond what earlier searches in this turn already returned.") is None
    assert _parse_rendered("") is None
    assert _parse_rendered(None) is None


# --- context trim ------------------------------------------------------------

def test_trim_context_caps_at_800_chars_on_word_boundary():
    long_body = "word " * 300  # 1500 chars
    trimmed = _trim_context(long_body)
    assert len(trimmed) <= MAX_CONTEXT_CHARS + 1  # +1 for the ellipsis
    assert trimmed.endswith("…")
    assert not trimmed.endswith(" …")  # cut at a word boundary, no dangling space


def test_trim_context_leaves_short_body_alone():
    assert _trim_context("Short answer.") == "Short answer."


# --- find_kb_context ---------------------------------------------------------

def test_covered_question_returns_short_background_context(fake_tool):
    ctx = _run(fake_tool, rendered=PASSWORD_RENDERED)
    assert ctx is not None
    assert ctx.source_name == "How to reset your password"
    assert ctx.source_type == "text"
    assert ctx.confidence == 1.0
    # Background for the model, not an article dump: short, first block only.
    assert "Forgot password" in ctx.context
    assert len(ctx.context) <= MAX_CONTEXT_CHARS + 1
    assert "Support hours" not in ctx.context


def test_near_miss_still_grounds_the_model(fake_tool):
    # 0.5 confidence (>= 0.25) with 2 matched keywords (support, holidays):
    # context, not silence. The article lists hours but never says outright
    # whether support is open on weekends — a near miss, not a full match.
    ctx = _run(fake_tool, rendered=HOURS_RENDERED,
               question="Is support open on weekends and holidays?")
    assert ctx is not None
    assert ctx.source_name == "Support hours"
    assert "9am to 6pm" in ctx.context


def test_unrelated_question_gets_no_context(fake_tool):
    ctx = _run(fake_tool, rendered=CANCEL_RENDERED,
               question="What is the capital of France?")
    assert ctx is None


def test_single_keyword_overlap_gets_no_context(fake_tool):
    # "cancel" is the only shared word with the cancellation article: the
    # flight question must not be grounded in subscription steps.
    ctx = _run(fake_tool, rendered=CANCEL_RENDERED,
               question="Can I cancel my flight booking tomorrow?")
    assert ctx is None


def test_empty_kb_gets_no_context(fake_tool):
    ctx = _run(fake_tool,
               rendered="No relevant information found in the knowledge base.")
    assert ctx is None


def test_search_failure_gets_no_context(fake_tool):
    ctx = _run(fake_tool, exc=RuntimeError("vector store down"))
    assert ctx is None


def test_missing_ids_or_blank_question_get_no_context(fake_tool):
    assert asyncio.run(find_kb_context(None, "org-1", "How do I reset my password?")) is None
    assert asyncio.run(find_kb_context("agent-1", None, "How do I reset my password?")) is None
    assert asyncio.run(find_kb_context("agent-1", "org-1", "   ")) is None
    assert fake_tool.get("tool") is None  # no search was even attempted


def test_passed_tool_is_used_for_the_search(fake_tool):
    tool = _FakeTool(rendered=PASSWORD_RENDERED)
    ctx = asyncio.run(find_kb_context(
        "agent-1", "org-1", "How do I reset my password?",
        knowledge_tool=tool))
    assert ctx is not None
    assert tool.calls == ["How do I reset my password?"]
    assert ctx.source_name == "How to reset your password"
