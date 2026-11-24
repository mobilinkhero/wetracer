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
        repo = KnowledgeRepository(db)
        for title, content in ARTICLES:
            if repo.get_by_sources(org_id, [title]):
                print(f"SKIP (exists): {title}")
                continue
            kb = create_text_source(db, org_id, title, content, agent_id)
            print(f"SEEDED id={kb.id}: {title}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
