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

"""KB instant answers: answer from a help article without a model call.

Before the agent runs, search the agent's knowledge base (the same vector
search the agent's knowledge tool uses) for the visitor's question. On a
high-confidence match, return the article text as the reply — no model call,
so the answer arrives in under a second instead of after a 20s–3min
generation on this CPU-only box.

Confidence gate: vector search always returns a nearest hit, even for an
unrelated question, so a hit alone is not enough. The question must also
share distinctive words with the article (title or text): at least 2 shared
keywords, or at least half of the question's keywords. Anything below the
gate goes to the model, which may still search the KB with its own tool.

Seeded test articles live in backend/scripts/seed_test_kb.py (test-only).
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

# A KB answer is rendered as-is, so keep it to a readable length.
MAX_ANSWER_CHARS = 1500


@dataclass
class KBHit:
    """A high-confidence KB answer, ready to send as the reply."""
    answer: str
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
    in the article title or text. Title words count double: a question that
    names the article's subject is a match even if the body words differ.
    """
    q = _keywords(question)
    if not q:
        return 0.0, []
    title_words = _keywords(title)
    body_words = _keywords(body)
    matched = [w for w in q if w in title_words or w in body_words]
    # Title hit: a question word in the title is strong evidence — but only
    # when at least two question words match overall. A single shared word
    # (e.g. "cancel" in "Can I cancel my flight booking tomorrow?") must
    # NOT floor the score to 0.6 and pass the gate for an unrelated article.
    title_hits = [w for w in q if w in title_words]
    score = len(matched) / len(q)
    if title_hits and len(matched) >= 2:
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
    match = re.match(r"\[([A-Za-z]+)\s*-\s*([^\]|]+)(?:\s*\|[^]]*)?\]\s*(.*)",
                     rendered.strip(), re.DOTALL)
    if not match:
        return None
    source_type, name, body = match.group(1).lower(), match.group(2).strip(), match.group(3).strip()
    if not body:
        return None
    return source_type, name, body


async def find_kb_candidate(agent_id, org_id, question: str) -> Optional[KBHit]:
    """The best KB article for `question` with its confidence, ungated.

    Returns None when there is no KB, no hit, or the search fails. Callers
    decide the gate: instant answer, context for the lean model, or nothing.
    """
    if not agent_id or not org_id or not (question or "").strip():
        return None
    try:
        from app.tools.knowledge_search_byagent import KnowledgeSearchByAgent
        tool = KnowledgeSearchByAgent(agent_id=agent_id, org_id=org_id)
        rendered = await asyncio.to_thread(tool.search_knowledge_base, question.strip())
    except Exception as e:
        logger.warning(f"KB instant search failed, falling back to model: {e}")
        return None
    parsed = _parse_rendered(rendered)
    if parsed is None:
        return None
    source_type, name, body = parsed
    confidence, matched = _confidence(question, name, body)
    answer = body.strip()
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS].rsplit(" ", 1)[0].strip() + "…"
    return KBHit(answer=answer, source_name=name, source_type=source_type,
                  confidence=confidence, matched_keywords=matched)


async def find_kb_answer(agent_id, org_id, question: str,
                         min_keywords: int = 2,
                         min_confidence: float = 0.5) -> Optional[KBHit]:
    """High-confidence KB answer for `question`, or None to use the model.

    The vector search always returns a nearest hit, even for an unrelated
    question, so a hit alone is not enough: the question must share at
    least `min_keywords` distinctive words with the article, or at least
    `min_confidence` of its keywords. Anything below the gate goes to the
    model, which may still search the KB with its own tool.
    """
    hit = await find_kb_candidate(agent_id, org_id, question)
    if hit is None:
        return None
    if len(hit.matched_keywords) < min_keywords and hit.confidence < min_confidence:
        logger.debug(
            f"KB hit '{hit.source_name}' rejected: confidence {hit.confidence:.2f}, "
            f"matched {hit.matched_keywords} for question {question[:60]!r}")
        return None
    logger.info(f"KB instant answer from '{hit.source_name}' "
                f"(confidence {hit.confidence:.2f}, matched {hit.matched_keywords})")
    return hit
