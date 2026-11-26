#!/usr/bin/env python3
"""
Live smoke test for AI auto-tagging of conversations (QA agent).

Drives the REAL local stack (http://localhost:8000 by default) end to end,
no mocks: start a conversation through the public mobile API, send visitor
messages with payment / urgent / service wording, and assert the expected
tags appear through the admin inbox API (filter, priority sort, manual
add/remove, detail).

No real keys, no charges, no network beyond localhost: the AI turn runs
against tests/stub_provider.py (an OpenAI-compatible stub server on
127.0.0.1), and every fixture lives in a TEMPORARY org created for this
run and deleted afterwards. It never touches Media Test Org or any existing
AI config — the live-verification owner owns those (see
backend/tests_live/test_streaming_live.py: the stub_ai_config fixture).

Run with the backend venv, from the repo root:
    backend/venv/bin/python scripts/verify_tags_live.py [--keep]

Exit code 0 = every check passed; 1 = at least one check failed; 2 = the
environment is not ready (stack down, stub port busy, contract missing).

CONTRACT (same as backend/tests/test_conversation_tags_e2e.py):
    Tags: payment-received + delivery-pending, urgent, svc-imei (from the
    three messages — paid also drops payment-pending and adds
    delivery-pending; IMEI tracking tags svc-imei, not imei-tracking)
    GET  /api/v1/admin/tags — every defined tag
    GET  /api/v1/admin/conversations/{id}/tags — {"tags", "priority_score"}
    GET  /api/v1/chats/recent?tag=<slug> / ?sort=priority, rows carry
    "tags" + "priority_score"
    POST /api/v1/admin/conversations/{id}/tags {"tag": slug}
    DELETE /api/v1/admin/conversations/{id}/tags/{slug}
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))
sys.path.insert(0, os.path.join(REPO, "backend", "tests"))
os.chdir(os.path.join(REPO, "backend"))

STUB_PORT = 18099
BASE_URL = "http://localhost:8000"

# The three smoke messages and the tags each one must produce.
MESSAGES = [
    ("I paid via JazzCash, transaction id 123",
     {"payment-received", "delivery-pending"}),
    ("urgent, jaldi karo", {"urgent"}),
    ("I want IMEI tracking", {"svc-imei"}),
]
EXPECTED_TAGS = {"payment-received", "delivery-pending", "urgent", "svc-imei"}

RESULTS = []  # (name, ok, detail)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(method, url, body=None, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, dict) and hdrs.get("Content-Type") == "application/x-www-form-urlencoded":
            data = urllib.parse.urlencode(body).encode()
        else:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"raw": raw}
    except urllib.error.URLError as e:
        return None, {"error": str(e)}


def wait_for_health(base, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", f"{base}/health", timeout=5)
        if status == 200:
            return True
        time.sleep(3)
    return False


def ensure_stack(base):
    status, _ = http("GET", f"{base}/health", timeout=5)
    if status == 200:
        print("[info] backend already up")
        return True
    print("[info] backend down — starting stack via start-all.sh")
    subprocess.run(["bash", os.path.expanduser("~/workspace/chattermate-pgdata/start-all.sh")],
                   check=False, timeout=300)
    if wait_for_health(base):
        print("[info] backend is up")
        return True
    print("[error] backend did not come up within 120s (see /tmp/chattermate-backend.log)")
    return False


def provision_temp_fixtures(stub_base_url, ids):
    """Temp org + agent + widget + stub-backed AI config + temp admin user.
    Fills in ``ids`` in place (so a failure half-way still leaves enough
    for cleanup) and returns it. Everything here is deleted in teardown."""
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.models.organization import Organization
    from app.models.agent import Agent, AgentType
    from app.models.role import Role
    from app.models.permission import Permission, role_permissions
    from app.models.user import User
    from app.models.schemas.widget import WidgetCreate
    from app.repositories.agent import AgentRepository
    from app.repositories.widget import WidgetRepository
    from app.repositories.ai_config import AIConfigRepository
    from app.core.security import get_password_hash

    tag = uuid.uuid4().hex[:8]
    ids["tag"] = tag
    with SessionLocal() as db:
        org = Organization(name=f"Tags Smoke Test Org {tag}",
                           domain=f"tags-smoke-{tag}.example.com", timezone="UTC")
        db.add(org)
        db.commit()
        db.refresh(org)
        ids["org_id"] = str(org.id)

        agent = AgentRepository(db).create_agent(
            name="Tags Smoke Agent", agent_type=AgentType.CUSTOMER_SUPPORT,
            instructions=["You are a helpful test support agent. Answer concisely."],
            org_id=str(org.id))
        ids["agent_id"] = str(agent.id)

        widget = WidgetRepository(db).create_widget(
            widget=WidgetCreate(name="Tags Smoke Widget", agent_id=str(agent.id)),
            organization_id=str(org.id))
        ids["widget_id"] = str(widget.id)

        AIConfigRepository(db).create_config(
            org_id=str(org.id), model_type="OPENAI_COMPATIBLE",
            model_name="stub-model", api_key="stub-test-key",
            base_url=stub_base_url)
        # create_config commits; confirm the config is active.
        row = db.execute(text(
            "select count(*) from ai_configs "
            "where organization_id = :o and is_active"),
            {"o": str(org.id)}).scalar()
        assert row == 1, "stub AI config not active after provisioning"

        # Temp admin in the temp org, for the inbox API read-back.
        # view_all_chats is a seeded permission in the dev DB: reuse the
        # existing row when it is there, and only delete a permission this
        # script itself created (cleanup honours ids["perm_created"]).
        perm = db.query(Permission).filter(
            Permission.name == "view_all_chats").first()
        if perm is None:
            perm = Permission(name="view_all_chats")
            db.add(perm)
            db.commit()
            db.refresh(perm)
            ids["perm_created"] = True
        else:
            ids["perm_created"] = False
        role = Role(name="Tags Smoke Admin", organization_id=org.id)
        db.add(role)
        db.commit()
        db.refresh(role)
        db.execute(role_permissions.insert().values(
            role_id=role.id, permission_id=perm.id))
        user = User(id=uuid.uuid4(), organization_id=org.id,
                    email=f"tags-smoke-{tag}@example.com",
                    full_name="Tags Smoke Admin",
                    hashed_password=get_password_hash("tags-smoke-password"),
                    is_active=True, role_id=role.id)
        db.add(user)
        db.commit()
        db.refresh(user)
        ids["user_id"] = str(user.id)
        ids["perm_id"] = perm.id
        ids["role_id"] = role.id
    return ids


def cleanup_temp_fixtures(ids):
    """Delete everything provision_temp_fixtures created. Tolerant of a
    half-provisioned run: a key that was never filled in is skipped, and
    a pre-existing permission is never deleted."""
    from sqlalchemy import text
    from app.database import SessionLocal
    with SessionLocal() as db:
        try:
            conv_ids = ids.get("conversation_ids", [])
            if conv_ids:
                db.execute(text(
                    "delete from chat_history where session_id = any(cast(:ids as uuid[]))"),
                    {"ids": conv_ids})
                db.execute(text(
                    "delete from session_to_agents where session_id = any(cast(:ids as uuid[]))"),
                    {"ids": conv_ids})
            if ids.get("customer_ids"):
                db.execute(text(
                    "delete from customers where id = any(cast(:ids as uuid[]))"),
                    {"ids": ids["customer_ids"]})
            for table, key in (("widgets", "widget_id"), ("agents", "agent_id"),
                               ("ai_configs", None), ("users", "user_id")):
                if key:
                    if ids.get(key):
                        db.execute(text(f"delete from {table} where id = :v"),
                                   {"v": ids[key]})
                elif table == "ai_configs" and ids.get("org_id"):
                    db.execute(text(
                        "delete from ai_configs where organization_id = :o"),
                        {"o": ids["org_id"]})
            if ids.get("role_id"):
                db.execute(text("delete from role_permissions where role_id = :r"),
                           {"r": ids["role_id"]})
                db.execute(text("delete from roles where id = :r"), {"r": ids["role_id"]})
            if ids.get("perm_id") and ids.get("perm_created"):
                db.execute(text("delete from permissions where id = :p"), {"p": ids["perm_id"]})
            if ids.get("org_id"):
                db.execute(text("delete from organizations where id = :o"),
                           {"o": ids["org_id"]})
            db.commit()
            print("[info] temp fixtures deleted")
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            print(f"[warn] cleanup incomplete ({e}); temp org id {ids.get('org_id')}")


def db_stored_tags(session_id):
    """Cross-check: read the tags straight from Postgres, auto-detecting
    where the builders stored them. Returns (where, tags) or (None, None)."""
    from sqlalchemy import text
    from app.database import SessionLocal
    with SessionLocal() as db:
        col = db.execute(text(
            "select 1 from information_schema.columns "
            "where table_name = 'session_to_agents' and column_name = 'tags'")).first()
        if col:
            row = db.execute(text(
                "select tags from session_to_agents where session_id = :s"),
                {"s": session_id}).first()
            return "session_to_agents.tags", list(row[0] or []) if row else []
        tbl = db.execute(text(
            "select 1 from information_schema.tables "
            "where table_name = 'conversation_tags'")).first()
        if tbl:
            rows = db.execute(text(
                "select tag from conversation_tags where session_id = :s"),
                {"s": session_id}).fetchall()
            return "conversation_tags", [r[0] for r in rows]
        return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--stub-port", type=int, default=STUB_PORT)
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the temp org/fixtures")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    if not ensure_stack(base):
        return 2

    # --- stub provider (no real model, localhost only) -----------------------
    from tests.stub_provider import StubProviderServer  # noqa: E402
    stub = StubProviderServer(
        deltas=["Salam! ", "Main Ayesha hoon. How can I help?"],
        chunk_delay=0.02, port=args.stub_port)
    try:
        stub.start()
    except OSError:
        print(f"[error] stub port {args.stub_port} already in use — "
              "another stub server is running; refusing to share it")
        return 2
    print(f"[info] stub provider at {stub.base_url}")

    ids = {}
    try:
        provision_temp_fixtures(stub.base_url, ids)
    except Exception:
        # Provisioning died half-way: remove whatever it managed to
        # create, then fail loudly instead of leaking a temp org.
        if ids.get("org_id"):
            cleanup_temp_fixtures(ids)
        raise
    try:
        print(f"[info] temp org {ids['org_id']} widget {ids['widget_id']}")

        # Admin token for the temp org (real auth path, not an override).
        from app.core.security import create_access_token  # noqa: E402
        admin_token = create_access_token(
            {"sub": ids["user_id"], "org": ids["org_id"]})
        auth = {"Authorization": f"Bearer {admin_token}"}

        # --- start a conversation + send the three messages ------------------
        status, conv = http("POST", f"{base}/api/v1/mobile/conversations",
                            {"widget_id": ids["widget_id"],
                             "visitor_name": "Tags Smoke",
                             "visitor_email": f"smoke-{ids['tag']}@example.com"},
                            timeout=30)
        if not check("start conversation", status == 200,
                     f"HTTP {status}: {conv}"):
            return 1
        conv_id = conv["conversation_id"]
        token = conv["conversation_token"]
        ids["conversation_ids"] = [conv_id]
        ids["customer_ids"] = [conv["customer_id"]]
        msg_headers = {"X-Conversation-Token": token,
                       "Content-Type": "application/x-www-form-urlencoded"}
        for text, _want in MESSAGES:
            sent = False
            for attempt in range(2):
                status, body = http(
                    "POST",
                    f"{base}/api/v1/mobile/conversations/{conv_id}/messages",
                    {"message": text}, headers=msg_headers, timeout=60)
                if status == 200:
                    sent = True
                    break
                if status == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                break
            if not check(f'send "{text[:40]}"', sent, f"HTTP {status}: {body}"):
                return 1
            time.sleep(2)  # stay under per-minute reply limits

        # A second, plain conversation for the sort comparison.
        status, plain = http("POST", f"{base}/api/v1/mobile/conversations",
                             {"widget_id": ids["widget_id"],
                              "visitor_name": "Tags Smoke Plain",
                              "visitor_email": f"plain-{ids['tag']}@example.com"},
                             timeout=30)
        if status == 200:
            ids["conversation_ids"].append(plain["conversation_id"])
            ids["customer_ids"].append(plain["customer_id"])
            http("POST",
                 f"{base}/api/v1/mobile/conversations/{plain['conversation_id']}/messages",
                 {"message": "hi, how are you"},
                 headers={"X-Conversation-Token": plain["conversation_token"],
                          "Content-Type": "application/x-www-form-urlencoded"},
                 timeout=60)
            plain_id = plain["conversation_id"]
        else:
            plain_id = None
            print(f"[warn] plain comparison conversation failed: HTTP {status}")

        # --- read back through the admin tags API + inbox API ---------------
        status, tag_defs = http("GET", f"{base}/api/v1/admin/tags", timeout=30)
        by_slug = {t["slug"]: t for t in tag_defs} if status == 200 else {}
        check("tag list endpoint",
              status == 200 and by_slug.get("payment-received", {}).get("weight") == 100
              and by_slug.get("svc-imei", {}).get("weight") == 10,
              f"HTTP {status}, {len(by_slug)} tags")

        status, body = http("GET", f"{base}/api/v1/admin/conversations/{conv_id}/tags",
                            headers=auth, timeout=30)
        if not check("get conversation tags", status == 200, f"HTTP {status}: {body}"):
            return 1
        tags = list(body.get("tags") or [])
        check("expected tags present",
              EXPECTED_TAGS <= set(tags),
              f"expected {sorted(EXPECTED_TAGS)}, got {tags}")
        check("priority score is 100 (payment-received)",
              body.get("priority_score") == 100,
              f"priority_score={body.get('priority_score')}")
        where, stored = db_stored_tags(conv_id)
        if where:
            check(f"tags persisted ({where})", EXPECTED_TAGS <= set(stored or []),
                  f"stored={stored}")
        else:
            print("[info] no tags column/table auto-detected in Postgres — "
                  "API read-back is the check")

        status, rows = http("GET", f"{base}/api/v1/chats/recent?limit=100",
                            headers=auth, timeout=30)
        if not check("inbox list returns 200", status == 200, f"HTTP {status}"):
            return 1
        row = next((r for r in rows if str(r.get("session_id")) == conv_id), None)
        if not check("conversation appears in inbox", row is not None):
            return 1
        check("inbox row carries tags + priority score",
              "tags" in row and row.get("priority_score") == 100,
              f"tags={row.get('tags')}, score={row.get('priority_score')}")

        status, rows = http("GET",
                            f"{base}/api/v1/chats/recent?tag=payment-received",
                            headers=auth, timeout=30)
        check("filter tag=payment-received finds the conversation",
              status == 200 and any(str(r.get("session_id")) == conv_id for r in rows),
              f"HTTP {status}, {len(rows) if isinstance(rows, list) else rows} rows")

        if plain_id:
            status, rows = http("GET", f"{base}/api/v1/chats/recent?sort=priority",
                                headers=auth, timeout=30)
            order = [str(r.get("session_id")) for r in rows] if status == 200 else []
            check("priority sort puts tagged conversation first",
                  status == 200 and conv_id in order and plain_id in order
                  and order.index(conv_id) < order.index(plain_id),
                  f"HTTP {status}, order={order[:4]}")

        status, body = http("POST", f"{base}/api/v1/admin/conversations/{conv_id}/tags",
                            {"tag": "vip"}, headers=auth, timeout=30)
        check("manual add tag vip", status == 200 and "vip" in (body.get("tags") or []),
              f"HTTP {status}: {body}")
        status, body = http("DELETE", f"{base}/api/v1/admin/conversations/{conv_id}/tags/vip",
                            headers=auth, timeout=30)
        check("manual remove tag vip",
              status == 200 and "vip" not in (body.get("tags") or []),
              f"HTTP {status}: {body}")
        status, body = http("GET", f"{base}/api/v1/admin/conversations/{conv_id}/tags",
                            headers=auth, timeout=30)
        check("tags still present after manual add/remove",
              status == 200 and EXPECTED_TAGS <= set(body.get("tags") or []),
              f"HTTP {status}: {body.get('tags') if isinstance(body, dict) else body}")
    finally:
        stub.stop()
        if ids and not args.keep:
            cleanup_temp_fixtures(ids)
        elif ids:
            print(f"[info] --keep: temp org {ids['org_id']} left in place")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print("LIVE SMOKE: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
