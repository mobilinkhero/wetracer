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

"""Re-run the rule-based conversation tagger over recent conversations.

New visitor messages are tagged as they arrive (ChatRepository.
create_message calls app.services.conversation_tagger); this script
backfills the inbox that already exists, so old chats get the same
priority/payment/service tags.

Usage (from backend/):
    ./venv/bin/python scripts/retag_conversations.py [--days 30] [--limit 500] [--dry-run]

--days caps how far back to look (default 30); --limit caps how many
conversations are processed; --dry-run prints what would change and
writes nothing. Tags are only ever added, never removed, and a
conversation is only written when the retag actually adds a tag.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import FileAttachment
from app.models.chat_history import ChatHistory
from app.models.session_to_agent import SessionToAgent
from app.services.conversation_tagger import merge_tags, suggest_tags

# A conversation's whole history is not needed: the tagger reads the
# visitor's words, and the last few turns carry the intent.
MAX_MESSAGES_PER_CONVERSATION = 20


def _retaggable_sessions(db, since, limit):
    """Conversations updated since `since`, most recent first."""
    query = (
        db.query(SessionToAgent)
        .filter(SessionToAgent.updated_at >= since)
        .order_by(SessionToAgent.updated_at.desc())
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def retag_session(db, session, *, dry_run=False):
    """Tag one conversation from its own visitor messages.

    Messages are replayed oldest-first, so the recent-texts rule (the
    same text three times is spam) and the merge see them in the order
    the visitor sent them. Returns (added, merged) — the tags this run
    added, and the conversation's full tag list afterwards.
    """
    messages = (
        db.query(ChatHistory)
        .filter(
            ChatHistory.session_id == session.session_id,
            ChatHistory.message_type == "user",
        )
        .order_by(ChatHistory.id.asc())
        .limit(MAX_MESSAGES_PER_CONVERSATION)
        .all()
    )
    if not messages:
        return [], list(getattr(session, "tags", None) or [])

    prior_count = (
        db.query(SessionToAgent)
        .filter(
            SessionToAgent.customer_id == session.customer_id,
            SessionToAgent.session_id != session.session_id,
        )
        .count()
    )

    merged = list(getattr(session, "tags", None) or [])
    preceding_texts = []
    for message in messages:
        attachments = (
            db.query(FileAttachment)
            .filter(FileAttachment.chat_history_id == message.id)
            .all()
        )
        merged = suggest_tags(
            message.message or "",
            attachments=[
                {
                    "content_type": a.content_type,
                    "filename": a.filename,
                }
                for a in attachments
            ],
            prior_conversation_count=prior_count,
            existing_tags=merged,
            recent_texts=preceding_texts,
        )
        preceding_texts.append(message.message or "")

    added = [t for t in merged if t not in (getattr(session, "tags", None) or [])]
    if added and not dry_run:
        session.tags = merged
        db.commit()
    return added, merged


def main():
    parser = argparse.ArgumentParser(
        description="Backfill conversation tags with the rule-based tagger."
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Only conversations updated in the last N days (default 30).",
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Maximum conversations to process (default 500).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing anything.",
    )
    args = parser.parse_args()

    if "tags" not in SessionToAgent.__table__.columns:
        print("ERROR: SessionToAgent has no tags column yet — the "
              "model/migration piece of the tagging feature has not landed.")
        raise SystemExit(1)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    db = SessionLocal()
    try:
        sessions = _retaggable_sessions(db, since, args.limit)
        changed = 0
        tagged_total = 0
        for session in sessions:
            added, merged = retag_session(db, session, dry_run=args.dry_run)
            if added:
                changed += 1
                print(f"{session.session_id}: +{', '.join(added)}"
                      f"  (now: {', '.join(merged)})")
            if merged:
                tagged_total += 1
        print(f"\n{len(sessions)} conversations scanned, {changed} gained "
              f"tags, {tagged_total} carry tags"
              f"{' (dry run — nothing written)' if args.dry_run else ''}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
