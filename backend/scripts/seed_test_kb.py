"""Seed TEST-ONLY help articles into the Media Test Support knowledge base.

Usage (from backend/):  ./venv/bin/python scripts/seed_test_kb.py

Creates three pasted-text knowledge sources (via page_editor.create_text_source,
the same path as the dashboard's "add text" — chunked + embedded into the
org's pgvector table and linked to the agent) and skips any title that
already exists, so it is safe to re-run.

TEST CONTENT ONLY: the wording below is sample text for verifying KB instant
answers and streaming. Real help articles must come from the owner.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")

from app.database import SessionLocal
from app.knowledge.page_editor import create_text_source
from app.repositories.knowledge import KnowledgeRepository
from sqlalchemy import text

ARTICLES = [
    (
        "How to reset your password",
        "To reset your password, click 'Forgot password' on the sign-in page "
        "and enter the email address for your account. We will send you a "
        "password reset link by email. The link is valid for 1 hour. Click the "
        "link, choose a new password with at least 8 characters, and save it. "
        "If the email does not arrive within 10 minutes, check your spam folder "
        "or contact our support team and we will help you reset it.",
    ),
    (
        "Support hours",
        "Our support team is available Monday to Friday, 9am to 6pm PKT, and "
        "Saturday, 10am to 2pm PKT. We are closed on Sundays and public holidays. "
        "Email messages are answered within one business day. For urgent issues "
        "outside support hours, leave us a message in the chat and we will reply "
        "as soon as we are back.",
    ),
    (
        "How to cancel your subscription",
        "You can cancel your subscription at any time from Settings, then "
        "Subscription, then Cancel subscription. Your subscription stays active "
        "until the end of the current billing period, so you can keep using it "
        "until then. After cancellation you will not be charged again. If you "
        "need a refund or have a billing question, please contact our support team.",
    ),
]

ORG_NAME = "Media Test Org"
AGENT_NAME = "Media Test Support"


def _dedupe(db, org_id) -> None:
    """Remove duplicate Knowledge rows for the seeded titles (keep the
    lowest id), including their agent links. Belt-and-braces for rows
    duplicated before the advisory lock below existed: on 2026-11-24 two
    seeder runs started in the same second, both passed the exists-check
    and both inserted, leaving 6 rows for 3 articles. The vector chunks
    are already deduped by content_hash (3 chunks, one per title), so
    nothing else needs removing."""
    for title, _ in ARTICLES:
        ids = [r[0] for r in db.execute(text(
            "select id from knowledge where organization_id = :o "
            "and source = :s order by id"),
            {"o": org_id, "s": title}).fetchall()]
        for dup_id in ids[1:]:
            db.execute(text(
                "delete from knowledge_to_agents where knowledge_id = :k"),
                {"k": dup_id})
            db.execute(text("delete from knowledge where id = :k"),
                       {"k": dup_id})
            print(f"DEDUPED id={dup_id}: {title}")
    db.commit()


def main() -> None:
    db = SessionLocal()
    try:
        org_id = db.execute(
            text("SELECT id FROM organizations WHERE name = :n"),
            {"n": ORG_NAME}).scalar()
        agent_id = db.execute(
            text("SELECT id FROM agents WHERE name = :n"),
            {"n": AGENT_NAME}).scalar()
        if not org_id or not agent_id:
            print(f"ERROR: org {ORG_NAME!r} or agent {AGENT_NAME!r} not found")
            raise SystemExit(1)
        # Serialize concurrent runs with a session-level advisory lock:
        # the exists-check below is only safe sequentially. Two runs
        # starting together both see nothing and both insert (that is how
        # the 6-for-3 duplicates happened). The lock makes the second run
        # wait; its check then sees the first run's committed rows and
        # skips. The lock is released on session close even if killed.
        db.execute(text(
            "select pg_advisory_lock(hashtext('seed_test_kb:' "
            "|| cast(:o as text)))"),
            {"o": str(org_id)})
        try:
            repo = KnowledgeRepository(db)
            for title, content in ARTICLES:
                if repo.get_by_sources(org_id, [title]):
                    print(f"SKIP (exists): {title}")
                    continue
                kb = create_text_source(db, org_id, title, content, agent_id)
                print(f"SEEDED id={kb.id}: {title}")
            _dedupe(db, org_id)
        finally:
            db.execute(text(
                "select pg_advisory_unlock(hashtext('seed_test_kb:' "
                "|| cast(:o as text)))"),
                {"o": str(org_id)})
    finally:
        db.close()


if __name__ == "__main__":
    main()
