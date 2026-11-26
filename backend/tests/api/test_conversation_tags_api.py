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

"""Admin conversation-tags API: definitions, add/remove, list filter + sort."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import chat as chat_router
from app.api import conversation_tags as tags_router
from app.core.auth import get_current_user, get_unified_chat_auth
from app.database import get_db
from app.models.agent import Agent
from app.models.chat_history import ChatHistory
from app.models.customer import Customer
from app.models.organization import Organization
from app.models.permission import Permission, role_permissions
from app.models.role import Role
from app.models.session_to_agent import SessionToAgent, SessionStatus
from app.models.user import User, UserGroup
from datetime import datetime, timedelta, timezone
from uuid import uuid4

# The two routers under test, mounted the way backend/app/main.py mounts
# them: the inbox list at /api/v1/chats, the tags API at /api/v1/admin.
app = FastAPI()
app.include_router(chat_router.router, prefix="/api/v1/chats", tags=["chats"])
app.include_router(tags_router.router, prefix="/api/v1/admin", tags=["conversation-tags"])


@pytest.fixture
def test_permissions(db) -> list[Permission]:
    permissions = []
    for name in ["view_all_chats", "view_assigned_chats"]:
        perm = Permission(name=name, description=f"Test permission for {name}")
        db.add(perm)
        permissions.append(perm)
    db.commit()
    for p in permissions:
        db.refresh(p)
    return permissions


@pytest.fixture
def test_role(db, test_organization_id, test_permissions) -> Role:
    role = Role(
        name="Test Role",
        description="Test Role Description",
        organization_id=test_organization_id,
        is_default=True,
    )
    db.add(role)
    db.commit()
    for perm in test_permissions:
        db.execute(
            role_permissions.insert().values(role_id=role.id, permission_id=perm.id)
        )
    db.commit()
    db.refresh(role)
    return role


@pytest.fixture
def test_user(db, test_organization_id, test_role) -> User:
    user = User(
        id=uuid4(),
        email="test@example.com",
        hashed_password="hashed",
        is_active=True,
        organization_id=test_organization_id,
        full_name="Test User",
        role_id=test_role.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_agent(db, test_user) -> Agent:
    agent = Agent(
        id=uuid4(),
        name="Test Agent",
        display_name="Test Display Name",
        description="Test Description",
        agent_type="CUSTOMER_SUPPORT",
        instructions=["Test instruction"],
        is_active=True,
        organization_id=test_user.organization_id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@pytest.fixture
def test_customer(db, test_user) -> Customer:
    customer = Customer(
        id=uuid4(),
        email="customer@example.com",
        full_name="Test Customer",
        organization_id=test_user.organization_id,
        is_active=True,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _make_session(db, test_user, test_agent, test_customer, *, tags=None, at=None):
    """One chat session with one message (the inbox list only shows
    sessions that have messages)."""
    session = SessionToAgent(
        session_id=uuid4(),
        user_id=test_user.id,
        agent_id=test_agent.id,
        customer_id=test_customer.id,
        organization_id=test_user.organization_id,
        status=SessionStatus.OPEN,
        assigned_at=datetime.now(timezone.utc),
    )
    if tags is not None:
        session.tags = tags
    db.add(session)
    db.commit()
    message = ChatHistory(
        organization_id=test_user.organization_id,
        user_id=test_user.id,
        customer_id=test_customer.id,
        agent_id=test_agent.id,
        session_id=session.session_id,
        message="Test message",
        message_type="user",
        created_at=at or datetime.now(timezone.utc),
    )
    db.add(message)
    db.commit()
    db.refresh(session)
    return session


@pytest.fixture
def client(db, test_user) -> TestClient:
    async def override_get_current_user():
        return test_user

    async def override_get_unified_chat_auth():
        db.refresh(test_user)
        db.refresh(test_user.role)
        user_permissions = {p.name for p in test_user.role.permissions}
        return {
            "auth_type": "jwt",
            "organization_id": test_user.organization_id,
            "user_id": test_user.id,
            "current_user": test_user,
            "can_view_all": "view_all_chats" in user_permissions,
            "can_view_assigned": "view_assigned_chats" in user_permissions,
        }

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_unified_chat_auth] = override_get_unified_chat_auth
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/tags
# ---------------------------------------------------------------------------

def test_list_tags_returns_the_taxonomy(client):
    response = client.get("/api/v1/admin/tags")
    assert response.status_code == 200
    tags = response.json()
    assert isinstance(tags, list)
    by_slug = {t["slug"]: t for t in tags}
    assert by_slug["payment-received"]["weight"] == 100
    assert by_slug["urgent"]["weight"] == 90
    assert by_slug["angry"]["weight"] == 85
    assert by_slug["needs-human"]["weight"] == 80
    assert by_slug["refund-request"]["weight"] == 75
    assert by_slug["payment-proof"]["weight"] == 70
    assert by_slug["delivery-pending"]["weight"] == 65
    assert by_slug["payment-pending"]["weight"] == 60
    assert by_slug["vip"]["weight"] == 55
    assert by_slug["follow-up-needed"]["weight"] == 50
    assert by_slug["waiting-customer"]["weight"] == 40
    assert by_slug["repeat-customer"]["weight"] == 20
    assert by_slug["svc-sim-owner"]["weight"] == 10
    assert by_slug["delivered"]["weight"] == 0
    assert by_slug["spam"]["weight"] == 0
    for tag in tags:
        assert set(tag) == {"slug", "label", "group", "weight", "color"}
        assert tag["group"] in {"payment", "priority", "status", "customer", "service"}


# ---------------------------------------------------------------------------
# GET /api/v1/chats/recent — tags, priority_score, filter, sort
# ---------------------------------------------------------------------------

def test_list_includes_tags_and_priority_score(
    client, db, test_user, test_agent, test_customer
):
    session = _make_session(
        db, test_user, test_agent, test_customer, tags=["urgent", "svc-cdr"]
    )
    response = client.get("/api/v1/chats/recent")
    assert response.status_code == 200
    assert len(response.json()) == 1
    row = response.json()[0]
    assert row["session_id"] == str(session.session_id)
    assert row["tags"] == ["urgent", "svc-cdr"]
    # The score is the heaviest tag's weight, not the sum.
    assert row["priority_score"] == 90


def test_list_untagged_conversation_scores_zero(
    client, db, test_user, test_agent, test_customer
):
    _make_session(db, test_user, test_agent, test_customer)
    row = client.get("/api/v1/chats/recent").json()[0]
    assert row["tags"] == []
    assert row["priority_score"] == 0


def test_list_filter_by_tag(client, db, test_user, test_agent, test_customer):
    paid = _make_session(
        db, test_user, test_agent, test_customer, tags=["payment-received"]
    )
    _make_session(db, test_user, test_agent, test_customer, tags=["urgent"])
    response = client.get("/api/v1/chats/recent", params={"tag": "payment-received"})
    assert response.status_code == 200
    rows = response.json()
    assert [r["session_id"] for r in rows] == [str(paid.session_id)]
    assert client.get(
        "/api/v1/chats/recent", params={"tag": "waiting-customer"}
    ).json() == []


def test_list_filter_by_two_tags_matches_either(
    client, db, test_user, test_agent, test_customer
):
    """Repeated ?tag= params are OR, not last-one-wins: a conversation
    matches when it carries ANY of the given tags."""
    paid = _make_session(
        db, test_user, test_agent, test_customer, tags=["payment-received"]
    )
    urgent = _make_session(
        db, test_user, test_agent, test_customer, tags=["urgent"]
    )
    _make_session(db, test_user, test_agent, test_customer, tags=["vip"])
    response = client.get(
        "/api/v1/chats/recent",
        params={"tag": ["payment-received", "urgent"]},
    )
    assert response.status_code == 200
    assert {r["session_id"] for r in response.json()} == {
        str(paid.session_id),
        str(urgent.session_id),
    }


def test_list_filter_two_tags_unknown_is_422(client):
    response = client.get(
        "/api/v1/chats/recent", params={"tag": ["urgent", "bogus"]}
    )
    assert response.status_code == 422
    assert "Unknown tag" in response.json()["detail"]


def test_human_path_receipt_image_tags_payment_proof(
    db, test_user, test_agent, test_customer
):
    """The human/transferred branches store the visitor message with the
    uploaded files attached (the same dict shape they now build), so a
    payment receipt image sent while a human has taken over yields the
    payment-proof tag."""
    from app.repositories.chat import ChatRepository

    session = _make_session(db, test_user, test_agent, test_customer)
    ChatRepository(db).create_message({
        "message": "payment kar di hai, receipt attach kar raha hoon",
        "message_type": "user",
        "session_id": session.session_id,
        "organization_id": test_user.organization_id,
        "agent_id": test_agent.id,
        "customer_id": test_customer.id,
        "user_id": test_user.id,
        "attachments": [{
            "filename": "receipt.jpg",
            "content_type": "image/jpeg",
            "file_url": "https://example.invalid/receipt.jpg",
            "size": 12345,
        }],
    })
    db.refresh(session)
    assert "payment-received" in session.tags
    assert "payment-proof" in session.tags


def test_list_filter_unknown_tag_is_422(client):
    response = client.get("/api/v1/chats/recent", params={"tag": "bogus"})
    assert response.status_code == 422
    assert "Unknown tag" in response.json()["detail"]


def test_list_sort_priority_orders_by_score_then_recency(
    client, db, test_user, test_agent, test_customer
):
    now = datetime.now(timezone.utc)
    low = _make_session(
        db, test_user, test_agent, test_customer,
        tags=["vip"], at=now - timedelta(minutes=3),
    )
    high = _make_session(
        db, test_user, test_agent, test_customer,
        tags=["urgent"], at=now - timedelta(minutes=2),
    )
    mid = _make_session(
        db, test_user, test_agent, test_customer,
        tags=["payment-pending"], at=now - timedelta(minutes=1),
    )
    rows = client.get(
        "/api/v1/chats/recent", params={"sort": "priority"}
    ).json()
    assert [r["session_id"] for r in rows] == [
        str(high.session_id), str(mid.session_id), str(low.session_id)
    ]
    assert [r["priority_score"] for r in rows] == [90, 60, 55]


def test_list_sort_priority_breaks_ties_by_recency(
    client, db, test_user, test_agent, test_customer
):
    now = datetime.now(timezone.utc)
    older = _make_session(
        db, test_user, test_agent, test_customer,
        tags=["urgent"], at=now - timedelta(minutes=5),
    )
    newer = _make_session(
        db, test_user, test_agent, test_customer,
        tags=["urgent"], at=now,
    )
    rows = client.get(
        "/api/v1/chats/recent", params={"sort": "priority"}
    ).json()
    assert [r["session_id"] for r in rows] == [
        str(newer.session_id), str(older.session_id)
    ]


def test_list_sort_priority_respects_pagination(
    client, db, test_user, test_agent, test_customer
):
    now = datetime.now(timezone.utc)
    for i, tag in enumerate(["vip", "urgent", "payment-pending"]):
        _make_session(
            db, test_user, test_agent, test_customer,
            tags=[tag], at=now - timedelta(minutes=i),
        )
    rows = client.get(
        "/api/v1/chats/recent", params={"sort": "priority", "limit": 2}
    ).json()
    assert [r["priority_score"] for r in rows] == [90, 60]
    rows = client.get(
        "/api/v1/chats/recent", params={"sort": "priority", "limit": 1, "skip": 1}
    ).json()
    assert [r["priority_score"] for r in rows] == [60]


# ---------------------------------------------------------------------------
# GET / POST / DELETE /api/v1/admin/conversations/{id}/tags
# ---------------------------------------------------------------------------

def test_get_conversation_tags(client, db, test_user, test_agent, test_customer):
    session = _make_session(
        db, test_user, test_agent, test_customer, tags=["payment-pending"]
    )
    response = client.get(
        f"/api/v1/admin/conversations/{session.session_id}/tags"
    )
    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": str(session.session_id),
        "tags": ["payment-pending"],
        "priority_score": 60,
    }


def test_add_tag(client, db, test_user, test_agent, test_customer):
    session = _make_session(db, test_user, test_agent, test_customer)
    url = f"/api/v1/admin/conversations/{session.session_id}/tags"

    response = client.post(url, json={"tag": "urgent"})
    assert response.status_code == 200
    assert response.json()["tags"] == ["urgent"]
    assert response.json()["priority_score"] == 90

    # The {"slug": ...} body the inbox client sends works too, and the
    # score stays the heaviest tag's weight.
    response = client.post(url, json={"slug": "vip"})
    assert response.status_code == 200
    assert response.json()["tags"] == ["urgent", "vip"]
    assert response.json()["priority_score"] == 90

    # Adding the same tag again is a no-op, not a duplicate.
    response = client.post(url, json={"tag": "urgent"})
    assert response.status_code == 200
    assert response.json()["tags"] == ["urgent", "vip"]
    assert response.json()["priority_score"] == 90

    db.refresh(session)
    assert session.tags == ["urgent", "vip"]


def test_add_tag_unknown_tag_is_422(client, db, test_user, test_agent, test_customer):
    session = _make_session(db, test_user, test_agent, test_customer)
    response = client.post(
        f"/api/v1/admin/conversations/{session.session_id}/tags",
        json={"tag": "bogus"},
    )
    assert response.status_code == 422
    assert "Unknown tag" in response.json()["detail"]


def test_add_tag_unknown_conversation_is_404(client):
    response = client.post(
        f"/api/v1/admin/conversations/{uuid4()}/tags", json={"tag": "urgent"}
    )
    assert response.status_code == 404


def test_remove_tag_by_path(client, db, test_user, test_agent, test_customer):
    session = _make_session(
        db, test_user, test_agent, test_customer, tags=["urgent", "vip"]
    )
    url = f"/api/v1/admin/conversations/{session.session_id}/tags"
    response = client.delete(f"{url}/urgent")
    assert response.status_code == 200
    assert response.json()["tags"] == ["vip"]
    assert response.json()["priority_score"] == 55

    # Removing a tag that is not there is a no-op, not an error.
    response = client.delete(f"{url}/urgent")
    assert response.status_code == 200
    assert response.json()["tags"] == ["vip"]
    assert response.json()["priority_score"] == 55

    db.refresh(session)
    assert session.tags == ["vip"]


def test_remove_tag_by_body(client, db, test_user, test_agent, test_customer):
    """The inbox client DELETEs with the slug in the body, not the path.
    (This TestClient's delete() takes no json kwarg, so the body goes in
    as raw content, the way the client sends it.)"""
    session = _make_session(
        db, test_user, test_agent, test_customer, tags=["urgent", "vip"]
    )
    response = client.request(
        "DELETE",
        f"/api/v1/admin/conversations/{session.session_id}/tags",
        content=json.dumps({"slug": "urgent"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["tags"] == ["vip"]
    assert response.json()["priority_score"] == 55


def test_remove_tag_unknown_tag_is_422(
    client, db, test_user, test_agent, test_customer
):
    session = _make_session(db, test_user, test_agent, test_customer)
    response = client.delete(
        f"/api/v1/admin/conversations/{session.session_id}/tags/bogus"
    )
    assert response.status_code == 422


def test_remove_tag_unknown_conversation_is_404(client):
    response = client.delete(f"/api/v1/admin/conversations/{uuid4()}/tags/urgent")
    assert response.status_code == 404


def test_conversation_from_another_org_is_404(
    client, db, test_user, test_agent, test_customer
):
    other_org = Organization(name="Other Org", domain="other.com", timezone="UTC")
    db.add(other_org)
    db.commit()
    db.refresh(other_org)
    other = SessionToAgent(
        session_id=uuid4(),
        agent_id=test_agent.id,
        customer_id=test_customer.id,
        organization_id=other_org.id,
        status=SessionStatus.OPEN,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(other)
    db.commit()
    base = f"/api/v1/admin/conversations/{other.session_id}/tags"
    assert client.get(base).status_code == 404
    assert client.post(base, json={"tag": "urgent"}).status_code == 404
    assert client.delete(f"{base}/urgent").status_code == 404
