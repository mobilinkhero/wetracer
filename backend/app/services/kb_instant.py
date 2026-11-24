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
unrelated question, so a hit alone is not enough. The instant answer needs
confidence >= 0.5 — the share of the question's distinctive words found in
the article (two or more title words count extra, see _confidence). Below 0.5 the
question goes to the model, which may still search the KB with its own tool;
a near miss (confidence >= 0.25 with at least 2 matched keywords) is pasted
as context for the lean model instead of being answered verbatim.

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
    # 'cancel' (title) + 'time' (body), 2/5 = 0.4 — a near miss, not an
    # instant answer. The question has to name the article's subject, not
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
    # block, so the answer (and the near-miss context built from it) contains
    # unrelated articles. Stop at the next block header: a blank line
    # followed by "[TYPE - Name]".
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
                         min_confidence: float = 0.5) -> Optional[KBHit]:
    """High-confidence KB answer for `question`, or None to use the model.

    The gate is confidence only: the share of the question's distinctive
    words found in the article must be at least `min_confidence` (default
    0.5). A hit with 2 matched words out of 5 (0.4) is NOT an instant
    answer — it is a near miss the lean model may use as context — while
    2 out of 3 (0.67) is. Anything below the gate goes to the model, which
    may still search the KB with its own tool.
    """
    hit = await find_kb_candidate(agent_id, org_id, question)
    if hit is None:
        return None
    if hit.confidence < min_confidence:
        logger.debug(
            f"KB hit '{hit.source_name}' rejected: confidence {hit.confidence:.2f}, "
            f"matched {hit.matched_keywords} for question {question[:60]!r}")
        return None
    logger.info(f"KB instant answer from '{hit.source_name}' "
                f"(confidence {hit.confidence:.2f}, matched {hit.matched_keywords})")
    return hit
