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

"""KB context for the model: the top matching article as background for the
model's own answer — never an article replay.

This replaces the removed instant-answer path: it used to send article text
to the visitor verbatim, with no model call, so a KB question got back
almost half an article whether or not that was what the visitor asked.
The model now answers KB questions itself, using the top matching article
as background context only: it understands what the visitor is asking,
answers briefly in its own words, and never quotes or pastes the article.

Relevance gate: vector search always returns a nearest hit, even for an
unrelated question, so a hit alone is not enough. The context needs
confidence >= 0.25 — the share of the question's distinctive words found
in the article — AND at least 2 matched keywords: a lone shared word (e.g.
"cancel" in a flight-booking question) once pasted the
subscription-cancellation article as context and the model answered the
flight question with subscription steps. Below the gate the question goes
to the model with no KB context, which may still search the KB with its
own tool (the full agent carries KnowledgeSearchByAgent).
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from app.core.logger import get_logger

logger = get_logger(__name__)

# Words too generic to count as evidence that an article answers a question.
_STOPWORDS = frozenset("""
a an the is are was were be been do does did how what when where who why
can could should would will i you we they he she it its my your our their
me him her us them this that these those there here and or but if then so
to of in on for with by at from as about into over after before between
please tell me get give want need know like just very really also
""".split())

# A KB context is background for the model, not text to repeat: keep it to
# one short passage so the model answers from it instead of echoing it.
MAX_CONTEXT_CHARS = 800

# Relevance gate for pasting context (see module docstring).
MIN_CONFIDENCE = 0.25
MIN_MATCHED_KEYWORDS = 2


@dataclass
class KBContext:
    """The top KB article, trimmed to background for the model's answer."""
    context: str
    source_name: str
    source_type: str
    confidence: float  # share of the question's keywords found in the article
    matched_keywords: list


def _keywords(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _confidence(question: str, title: str, body: str) -> tuple:
    """(confidence, matched keywords) for a candidate article.

    Confidence is the share of the question's distinctive words that appear
    in the article title or text. Two or more title words floor the score
    at 0.6: a question that names the article's subject is a match even if
    the body words differ.
    """
    q = _keywords(question)
    if not q:
        return 0.0, []
    title_words = _keywords(title)
    body_words = _keywords(body)
    matched = [w for w in q if w in title_words or w in body_words]
    # Title hit: question words in the title are strong evidence — but the
    # floor needs at least TWO title words. A single title word (e.g.
    # "cancel" in "Can I cancel my flight booking tomorrow?") must NOT floor
    # the score to 0.6, even when a second, incidental body word also
    # matches: "What time do the flight booking offices cancel?" matches
    # 'cancel' (title) + 'time' (body), 2/5 = 0.4 — a near miss, not a
    # grounded answer. The question has to name the article's subject, not
    # share one word with it.
    title_hits = [w for w in q if w in title_words]
    score = len(matched) / len(q)
    if len(title_hits) >= 2:
        score = max(score, 0.6)
    return score, matched


def _parse_rendered(rendered: str) -> Optional[tuple]:
    """Split the knowledge tool's rendered hit into (source_type, name, body).

    Format (see KnowledgeSearchByAgent._page_block): "[TYPE - Name] chunk
    text...". Returns None for the tool's no-results sentences.
    """
    if not rendered or "No relevant information" in rendered \
            or "No further results" in rendered:
        return None
    # The renderer joins several article blocks with "\n\n" (_PAGE_SEPARATOR
    # in knowledge_search_byagent.py), but only the FIRST block is the top
    # hit. Without this cut the (.*) body below swallows every following
    # block, so the context contains unrelated articles. Stop at the next
    # block header: a blank line followed by "[TYPE - Name]".
    first_block = re.split(
        r"\n\n\[(?=[A-Za-z]+\s*-\s)", rendered.strip(), maxsplit=1)[0]
    match = re.match(r"\[([A-Za-z]+)\s*-\s*([^\]|]+)(?:\s*\|[^]]*)?\]\s*(.*)",
                     first_block, re.DOTALL)
    if not match:
        return None
    source_type, name, body = match.group(1).lower(), match.group(2).strip(), match.group(3).strip()
    if not body:
        return None
    return source_type, name, body


def _trim_context(body: str) -> str:
    """Cap the context at MAX_CONTEXT_CHARS, cut at a word boundary."""
    if len(body) <= MAX_CONTEXT_CHARS:
        return body.strip()
    return body[:MAX_CONTEXT_CHARS].rsplit(" ", 1)[0].strip() + "…"


async def find_kb_context(agent_id, org_id, question: str,
                          knowledge_tool=None) -> Optional[KBContext]:
    """The top KB article as background for the model's answer, or None.

    Returns None when there is no KB, no hit, the search fails, or the hit
    fails the relevance gate — the model then answers without KB context
    (and may still search the KB with its own tool on the full-agent path).
    When a tool is passed, the search runs on it so its citations
    (collected_sources) cover what the model was shown; otherwise a
    throwaway tool is built for the lookup.
    """
    if not agent_id or not org_id or not (question or "").strip():
        return None
    try:
        tool = knowledge_tool
        if tool is None:
            from app.tools.knowledge_search_byagent import KnowledgeSearchByAgent
            tool = KnowledgeSearchByAgent(agent_id=agent_id, org_id=org_id)
        rendered = await asyncio.to_thread(tool.search_knowledge_base, question.strip())
    except Exception as e:
        logger.warning(f"KB context search failed, model answers without it: {e}")
        return None
    parsed = _parse_rendered(rendered)
    if parsed is None:
        return None
    source_type, name, body = parsed
    confidence, matched = _confidence(question, name, body)
    if confidence < MIN_CONFIDENCE or len(matched) < MIN_MATCHED_KEYWORDS:
        logger.debug(
            f"KB context '{name}' rejected: confidence {confidence:.2f}, "
            f"matched {matched} for question {question[:60]!r}")
        return None
    logger.info(f"KB context from '{name}' "
                f"(confidence {confidence:.2f}, matched {matched})")
    return KBContext(context=_trim_context(body), source_name=name,
                     source_type=source_type, confidence=confidence,
                     matched_keywords=matched)
