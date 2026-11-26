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

"""Rule-based auto-tagging for visitor conversations.

No model call, no network: the tagger matches the visitor's own words
(English and Roman Urdu) against fixed keyword rules and returns the
slugs the inbox uses for priority, filtering and follow-up. It runs on
every stored visitor message (ChatRepository.create_message) and on the
retag backfill script — the same pure function in both places, so a tag
means the same thing everywhere it appears.

Two deliberate behaviours:
- A message that says the visitor already paid is never also tagged
  payment-pending: once payment-received fires, payment-pending is
  dropped, so a chat never carries both.
- Tags are only ever ADDED by this module. Removal is manual, through
  the API — merge_tags keeps every existing tag, even one this module
  would never suggest (a hand-added or legacy tag), and only appends
  new canonical slugs, without duplicates.
"""

import logging
import re
from typing import Any, Iterable, List, Optional
from uuid import UUID

from app.core.logger import get_logger

logger = get_logger(__name__)

# Canonical slugs, as specified for the tagging feature. The taxonomy
# module (app/services/tag_taxonomy.py) is the source of truth when it
# exists; this list is the fallback so the tagger works before it lands
# and keeps working if the import ever breaks.
_FALLBACK_TAGS = [
    "payment-received", "urgent", "angry", "needs-human", "refund-request",
    "payment-proof", "delivery-pending", "payment-pending", "vip",
    "follow-up-needed", "waiting-customer", "repeat-customer",
    "svc-sim-owner", "svc-cdr", "svc-imei", "svc-sim-location",
    "svc-cnic-copy", "svc-family-tree", "svc-travel-history",
    "svc-whatsapp-cdr", "svc-passport", "svc-all-network",
    "delivered", "spam",
]

# Slugs this tagger can suggest on its own. The rest of the canonical
# list (follow-up-needed, waiting-customer, delivered) are status tags a
# person sets — the rules below never emit them.
_SUGGESTABLE = {
    "payment-received", "urgent", "angry", "needs-human", "refund-request",
    "payment-proof", "delivery-pending", "payment-pending", "vip",
    "repeat-customer",
    "svc-sim-owner", "svc-cdr", "svc-imei", "svc-sim-location",
    "svc-cnic-copy", "svc-family-tree", "svc-travel-history",
    "svc-whatsapp-cdr", "svc-passport", "svc-all-network",
    "spam",
}


def _load_canonical_tags() -> List[str]:
    """The taxonomy module's list when it exists, else the fallback.

    The taxonomy module is written by a parallel piece of work, so its
    export name is not assumed: several likely names are tried. If none
    is found, or the list is missing slugs this tagger emits, the inline
    fallback is used and the mismatch is logged.
    """
    try:
        from app.services import tag_taxonomy as _taxonomy
    except Exception:
        return list(_FALLBACK_TAGS)
    for attr in ("CANONICAL_TAGS", "ALL_TAGS", "TAGS", "TAG_SLUGS"):
        candidate = getattr(_taxonomy, attr, None)
        if isinstance(candidate, (list, tuple)) and candidate:
            # Items may be bare slugs or Tag objects with a .slug.
            tags = [getattr(item, "slug", None) or str(item)
                    for item in candidate]
            missing = _SUGGESTABLE - set(tags)
            if missing:
                logger.warning(
                    "tag_taxonomy.%s is missing slugs this tagger emits "
                    "(%s); using the inline canonical list",
                    attr, sorted(missing),
                )
                return list(_FALLBACK_TAGS)
            return tags
    logger.warning(
        "tag_taxonomy has no recognised tag list export; "
        "using the inline canonical list"
    )
    return list(_FALLBACK_TAGS)


CANONICAL_TAGS = _load_canonical_tags()
_CANONICAL_SET = set(CANONICAL_TAGS)

# --- rule tables: phrase or word -> slug ---------------------------------

# Plain substrings on the normalised (lowercased, whitespace-collapsed)
# text. Multi-word phrases go here; single words that must not match
# inside another word go in _WORD_RULES instead.
_PHRASE_RULES = [
    # payment-pending
    ("how to pay", "payment-pending"),
    ("how do i pay", "payment-pending"),
    ("how can i pay", "payment-pending"),
    ("payment kaise", "payment-pending"),
    ("jazzcash", "payment-pending"),
    ("easypaisa", "payment-pending"),
    ("account number", "payment-pending"),
    ("pay karna", "payment-pending"),
    ("i will pay", "payment-pending"),
    ("payment karunga", "payment-pending"),
    ("payment karungi", "payment-pending"),
    # payment-received
    ("payment kar di", "payment-received"),
    ("pay kar diya", "payment-received"),
    ("paisay bhej diye", "payment-received"),
    ("paise bhej diye", "payment-received"),
    ("sent payment", "payment-received"),
    ("transaction id", "payment-received"),
    # refund-request
    ("refund", "refund-request"),
    ("money back", "refund-request"),
    ("paisay wapis", "refund-request"),
    ("paise wapis", "refund-request"),
    # urgent
    ("urgent", "urgent"),
    ("jaldi", "urgent"),
    ("emergency", "urgent"),
    ("foran", "urgent"),
    ("abhi chahiye", "urgent"),
    # angry
    ("fraud", "angry"),
    ("scam", "angry"),
    ("dhoka", "angry"),
    ("bekar", "angry"),
    ("bakwas", "angry"),
    ("cheat", "angry"),
    ("no reply", "angry"),
    # needs-human
    ("agent se baat", "needs-human"),
    ("call me", "needs-human"),
    ("insan", "needs-human"),
    ("operator", "needs-human"),
    ("staff", "needs-human"),
    # services — "whatsapp cdr" is checked before plain "cdr" so a
    # WhatsApp CDR question is tagged the WhatsApp service, not the
    # plain CDR one.
    ("whatsapp cdr", "svc-whatsapp-cdr"),
    ("sim owner", "svc-sim-owner"),
    ("sim details", "svc-sim-owner"),
    ("call history", "svc-cdr"),
    ("sms history", "svc-cdr"),
    ("cnic copy", "svc-cnic-copy"),
    ("cnic front back", "svc-cnic-copy"),
    ("family tree", "svc-family-tree"),
    ("travel history", "svc-travel-history"),
    ("all network", "svc-all-network"),
    # spam phrases
    ("earn money", "spam"),
    ("click here", "spam"),
    ("free recharge", "spam"),
]

# Single words that must match as whole words, so "tid" never fires on
# "tide" and "hide" never fires on "hidden".
_WORD_RULES = [
    ("paid", "payment-received"),
    ("tid", "payment-received"),
    ("trxid", "payment-received"),
    ("txn", "payment-received"),
    ("wapis", "refund-request"),
    ("human", "needs-human"),
    ("cdr", "svc-cdr"),
    ("imei", "svc-imei"),
    ("location", "svc-sim-location"),
    ("passport", "svc-passport"),
    ("hide", "svc-all-network"),
]

# "I have not paid" must not tag payment-received. A transaction id is
# stronger evidence than the negation, so it still tags when present.
_NEGATED_PAYMENT = (
    "not paid", "didn't pay", "didnt pay", "haven't paid", "havent paid",
    "hasn't paid", "hasnt paid", "payment nahi", "pay nahi",
    "nahi kiya", "nahi ki", "not yet paid",
)
_TXN_ID_WORDS = ("transaction id", "tid", "trxid", "txn")

# An amount that makes the chat high value. Numbers are only read when
# they stand alone: a phone number (03001234567) or a CNIC
# (35202-1234567-1) must never read as an amount, so a digit run next to
# another digit or a dash is skipped, and only 4-6 digit amounts count.
_AMOUNT_RE = re.compile(r"(?<![\d-])(\d[\d,]*)(?![\d-])")
_K_AMOUNT_RE = re.compile(r"\b(\d{1,3})\s*k\b")
VIP_AMOUNT_PKR = 20000

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_PDF_EXT = (".pdf",)


def _normalize(text: Optional[str]) -> str:
    """Lowercase and collapse whitespace, so "  Payment   KAR di " matches."""
    return " ".join((text or "").lower().split())


def _has_word(text: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def _attachment_is_image_or_pdf(attachment: Any) -> bool:
    """Whether one attachment is an image or a PDF.

    Accepts the dicts the upload paths hand around (content_type /
    filename / file_url), objects with those attributes, or a bare
    string (a content type or a filename).
    """
    content_type = ""
    filename = ""
    if isinstance(attachment, dict):
        content_type = str(attachment.get("content_type") or "")
        filename = str(
            attachment.get("filename") or attachment.get("file_url") or ""
        )
    elif isinstance(attachment, str):
        content_type = attachment
        filename = attachment
    else:
        content_type = str(getattr(attachment, "content_type", "") or "")
        filename = str(
            getattr(attachment, "filename", "")
            or getattr(attachment, "file_url", "")
            or ""
        )
    content_type = content_type.lower()
    filename = filename.lower()
    if content_type.startswith("image/") or content_type == "application/pdf":
        return True
    return filename.endswith(_IMAGE_EXTS) or filename.endswith(_PDF_EXT)


def _mentions_vip_amount(text: str) -> bool:
    """Whether the text names an amount of VIP_AMOUNT_PKR or more."""
    for match in _AMOUNT_RE.finditer(text):
        digits = match.group(1).replace(",", "")
        if 4 <= len(digits) <= 6 and int(digits) >= VIP_AMOUNT_PKR:
            return True
    for match in _K_AMOUNT_RE.finditer(text):
        if int(match.group(1)) * 1000 >= VIP_AMOUNT_PKR:
            return True
    return False


def merge_tags(existing_tags: Optional[Iterable[str]],
               new_tags: Optional[Iterable[str]]) -> List[str]:
    """Merge suggested tags into the tags a conversation already has.

    Existing tags keep their order and are never dropped — even one this
    module would never suggest, because it may be hand-added. New tags
    are appended in canonical order, skipping duplicates and anything
    outside the canonical list.
    """
    merged: List[str] = []
    for tag in existing_tags or []:
        tag = str(tag)
        if tag and tag not in merged:
            merged.append(tag)
    order = {slug: i for i, slug in enumerate(CANONICAL_TAGS)}
    for tag in sorted(
        {str(t) for t in (new_tags or [])} - set(merged),
        key=lambda t: order.get(t, len(order)),
    ):
        if tag in _CANONICAL_SET:
            merged.append(tag)
    return merged


def suggest_tags(message_text: str, *, attachments=None,
                 prior_conversation_count: int = 0,
                 existing_tags=None,
                 recent_texts=None) -> List[str]:
    """Suggest tags for one visitor message, from rules only.

    Args:
        message_text: the visitor's message.
        attachments: files sent with it (dicts/objects/strings — see
            _attachment_is_image_or_pdf). An image/PDF plus payment
            words is payment proof.
        prior_conversation_count: how many earlier conversations this
            visitor has had; above zero they are a repeat customer.
        existing_tags: tags the conversation already carries; the
            result merges into them without duplicates, never removing.
        recent_texts: the visitor's earlier texts in this conversation,
            NOT including this message. The current message counts as
            the next repeat, so two identical earlier texts plus this
            one is spam.

    Returns:
        The merged tag list: existing tags first, then new canonical
        slugs in canonical order.
    """
    text = _normalize(message_text)
    found = set()

    for phrase, slug in _PHRASE_RULES:
        if phrase in text:
            found.add(slug)
    for word, slug in _WORD_RULES:
        if _has_word(text, word):
            found.add(slug)

    # "I have not paid yet" is not a payment. A transaction id still
    # counts as proof even next to a negation.
    if "payment-received" in found and not any(w in text for w in _TXN_ID_WORDS):
        if any(neg in text for neg in _NEGATED_PAYMENT):
            found.discard("payment-received")

    # "WhatsApp CDR" is its own service, not plain CDR: the word rule
    # for "cdr" fires on it too, so drop the plain tag when the
    # WhatsApp one matched.
    if "svc-whatsapp-cdr" in found:
        found.discard("svc-cdr")

    # Paid beats pending: a chat that already paid is not waiting to pay.
    if "payment-received" in found:
        found.discard("payment-pending")
        # Money is in and the result has not been sent yet.
        found.add("delivery-pending")

    # Payment proof: an image or PDF sent with payment words.
    if attachments and ("payment-pending" in found
                        or "payment-received" in found):
        if any(_attachment_is_image_or_pdf(a) for a in attachments):
            found.add("payment-proof")

    # High value: the expensive services, or a big amount named.
    if "svc-whatsapp-cdr" in found or "svc-all-network" in found:
        found.add("vip")
    elif _mentions_vip_amount(text):
        found.add("vip")

    if (prior_conversation_count or 0) > 0:
        found.add("repeat-customer")

    # Same text three times running is spam, not a conversation.
    if text:
        repeats = sum(
            1 for earlier in (recent_texts or [])
            if _normalize(earlier) == text
        )
        if repeats >= 2:
            found.add("spam")

    return merge_tags(existing_tags, [t for t in found if t in _SUGGESTABLE])


def apply_tags_to_conversation(db, *, session_id, message_id,
                               message_text, attachments=None,
                               customer_id=None) -> List[str]:
    """Tag one conversation from one stored visitor message.

    The persistence half of the hook: reads the conversation's current
    tags, the visitor's earlier conversations and recent texts, runs
    suggest_tags, and writes the merged list back. Never raises —
    tagging must never break storing the message — and returns the
    merged list (empty when there is nothing to tag, or when the tags
    column has not landed yet).

    Args:
        db: an open SQLAlchemy session.
        session_id: the conversation (session_to_agents.session_id).
        message_id: the just-stored ChatHistory id, excluded from the
            recent-texts lookup so the current message is not counted
            as its own repeat.
        message_text: the visitor's message.
        attachments: files sent with it.
        customer_id: the visitor; read from the conversation when omitted.
    """
    try:
        from app.models.chat_history import ChatHistory
        from app.models.session_to_agent import SessionToAgent

        # The tags column is added by the model/migration piece of this
        # feature. Until it lands, there is nowhere to write: log once
        # and leave the conversation untagged instead of failing.
        if "tags" not in SessionToAgent.__table__.columns:
            logger.warning(
                "Conversation tagging skipped for session %s: "
                "SessionToAgent has no tags column yet", session_id,
            )
            return []

        row = db.query(SessionToAgent).filter(
            SessionToAgent.session_id == UUID(str(session_id)),
        ).first()
        if row is None:
            return []

        visitor_id = customer_id or row.customer_id
        prior_count = 0
        if visitor_id is not None:
            prior_count = db.query(SessionToAgent).filter(
                SessionToAgent.customer_id == visitor_id,
                SessionToAgent.session_id != row.session_id,
            ).count()

        recent_rows = (
            db.query(ChatHistory.message)
            .filter(
                ChatHistory.session_id == row.session_id,
                ChatHistory.message_type == "user",
                ChatHistory.id != message_id,
            )
            .order_by(ChatHistory.id.desc())
            .limit(5)
            .all()
        )
        recent_texts = [r[0] for r in recent_rows if r and r[0]]

        merged = suggest_tags(
            message_text,
            attachments=attachments,
            prior_conversation_count=prior_count,
            existing_tags=list(getattr(row, "tags", None) or []),
            recent_texts=recent_texts,
        )
        row.tags = merged
        db.commit()
        return merged
    except Exception as exc:
        logger.warning(
            "Conversation tagging skipped for session %s: %s", session_id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return []
