"""Re-embed knowledge-base chunks with the organization's CURRENT embedder.

Usage (from backend/):
    ./venv/bin/python scripts/reembed_kb.py [--org "Media Test Org"] [--source TITLE] [--dry-run]

Why this exists: the embeddings switch (local model -> hosted provider,
see app/knowledge/embedder.py) left previously stored rows on old-model
vectors. Both models write 384-dim vectors, so nothing errors — but cosine
similarity between a new hosted query vector and a stored old-model vector
is meaningless, and vector search returns the wrong top article until the
row is re-embedded. New rows are fine; only stored rows need this backfill.

What it does, per chunk: re-embeds the chunk's stored content with the
org's current embedder (via app.knowledge.page_editor.reembed_chunk, the
same helper the knowledge-edit API uses) and updates the stored vector in
place. Content is not changed. No API key is handled here: the embedder is
built from the org's own dashboard-configured AI config, exactly as live
search does.

Scope defaults to the Media Test Org's three seeded test articles
(backend/scripts/seed_test_kb.py), so a default run is a handful of
embedding calls. Pass --source to limit to one article, --org to target a
different org (name or UUID), or --all-orgs to re-embed every org's KB —
the last one is NOT the default because it embeds every stored chunk.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")

from sqlalchemy import text

from app.database import SessionLocal
from app.knowledge.page_editor import reembed_chunk
from app.repositories.knowledge import KnowledgeRepository

SEEDED_TITLES = [
    "How to reset your password",
    "Support hours",
    "How to cancel your subscription",
]
DEFAULT_ORG = "Media Test Org"


def _resolve_org(db, org_arg):
    if org_arg is None:
        org_arg = DEFAULT_ORG
    row = db.execute(
        text("SELECT id, name FROM organizations WHERE id::text = :v OR name = :v"),
        {"v": org_arg},
    ).fetchone()
    if row is None:
        print(f"ERROR: no organization matching {org_arg!r}")
        raise SystemExit(1)
    return row[0], row[1]


def _chunks_for_source(db, knowledge):
    """All stored chunk rows of one knowledge source (id, content)."""
    return db.execute(
        text(
            f'SELECT id, content FROM {knowledge.schema}."{knowledge.table_name}" '
            "WHERE name = :source ORDER BY id"
        ),
        {"source": knowledge.source},
    ).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=None,
                        help="org name or UUID (default: %(default)s)" % {"default": DEFAULT_ORG})
    parser.add_argument("--source", default=None,
                        help="re-embed only this source title (default: the 3 seeded test articles)")
    parser.add_argument("--all-sources", action="store_true",
                        help="re-embed every knowledge source of the org (overrides the seeded default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be re-embedded without calling the embedder")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        org_id, org_name = _resolve_org(db, args.org)
        repo = KnowledgeRepository(db)
        if args.source:
            sources = repo.get_by_sources(org_id, [args.source])
            if not sources:
                print(f"ERROR: no knowledge source {args.source!r} in org {org_name!r}")
                raise SystemExit(1)
        elif args.all_sources:
            sources = repo.get_by_organization(org_id) if hasattr(repo, "get_by_organization") else []
            if not sources:
                rows = db.execute(
                    text("SELECT id FROM knowledge WHERE organization_id = :o ORDER BY source"),
                    {"o": org_id}).fetchall()
                sources = [repo.get_by_id(r[0]) for r in rows]
        else:
            sources = repo.get_by_sources(org_id, SEEDED_TITLES)
        print(f"Org: {org_name} ({org_id}) — {len(sources)} source(s)")

        total = updated = skipped = 0
        for knowledge in sources:
            chunks = _chunks_for_source(db, knowledge)
            print(f"Source {knowledge.source!r}: {len(chunks)} chunk(s)")
            for chunk_id, content in chunks:
                total += 1
                if args.dry_run:
                    print(f"  DRY-RUN chunk {chunk_id!r} ({len(content or '')} chars)")
                    continue
                rows = reembed_chunk(db, knowledge, chunk_id, content)
                if rows:
                    updated += 1
                    print(f"  RE-EMBEDDED chunk {chunk_id!r}")
                else:
                    skipped += 1
                    print(f"  SKIP (chunk gone): {chunk_id!r}")
        if not args.dry_run:
            db.commit()
        print(f"DONE: {updated}/{total} chunk(s) re-embedded, {skipped} skipped"
              + (" (dry run, nothing written)" if args.dry_run else ""))
    finally:
        db.close()


if __name__ == "__main__":
    main()
