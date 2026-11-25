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

"""Tests for the visitor AI-content report endpoint
(POST /api/v1/mobile/reports, app/api/mobile_chat.py) and the admin list
(GET /api/v1/admin/mobile/reports, app/api/mobile_admin.py).

Google Play's AI-Generated Content policy requires in-app reporting of
offensive AI content. The public endpoint takes no auth and no AI config —
the widget ID is public, the same trust level as the widget itself.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mobile_admin, mobile_chat
from app.core.auth import get_current_user
from app.database import get_db
from app.models.mobile import MobileReport
from app.models.organization import Organization

public_app = FastAPI()
public_app.include_router(mobile_chat.router, prefix="/api/v1/mobile")

admin_app = FastAPI()
admin_app.include_router(mobile_admin.router, prefix="/api/v1/admin/mobile")


@pytest.fixture
def public_client(db):
    def _override_db():
        yield db

    public_app.dependency_overrides[get_db] = _override_db
    with TestClient(public_app) as c:
        yield c
    public_app.dependency_overrides.clear()


@pytest.fixture
def admin_client(db, test_organization):
    def _override_db():
        yield db

    admin = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                            organization_id=test_organization.id)
    admin_app.dependency_overrides[get_db] = _override_db
    admin_app.dependency_overrides[get_current_user] = lambda: admin
    with TestClient(admin_app) as c:
        yield c
    admin_app.dependency_overrides.clear()


def _report_body(test_widget, **overrides):
    body = {"widget_id": str(test_widget.id),
            "reason": "Offensive content",
            "details": "The AI reply was insulting.",
            "source": "mobile"}
    body.update(overrides)
    return body


def test_report_success_stores_row(public_client, db, test_widget):
    resp = public_client.post("/api/v1/mobile/reports",
                              json=_report_body(test_widget))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}

    row = db.query(MobileReport).one()
    assert str(row.widget_id) == str(test_widget.id)
    assert row.organization_id == test_widget.organization_id
    assert row.reason == "Offensive content"
    assert row.details == "The AI reply was insulting."
    assert row.source == "mobile"
    assert row.ip_address == "testclient"
    assert row.created_at is not None


def test_report_minimal_body_ok(public_client, db, test_widget):
    resp = public_client.post(
        "/api/v1/mobile/reports",
        json={"widget_id": str(test_widget.id), "reason": "Spam or scam"})
    assert resp.status_code == 200, resp.text
    row = db.query(MobileReport).one()
    assert row.details is None
    assert row.source is None


def test_report_unknown_widget_404(public_client, test_widget):
    resp = public_client.post(
        "/api/v1/mobile/reports",
        json={"widget_id": "no-such-widget", "reason": "Spam or scam"})
    assert resp.status_code == 404


def test_report_missing_widget_id_422(public_client, test_widget):
    resp = public_client.post("/api/v1/mobile/reports",
                              json={"reason": "Spam or scam"})
    assert resp.status_code == 422


@pytest.mark.parametrize("reason", [
    "Offensive content",
    "Wrong or misleading info",
    "Spam or scam",
    "Something else",
])
def test_report_each_reason_accepted(public_client, db, test_widget, reason):
    resp = public_client.post(
        "/api/v1/mobile/reports",
        json={"widget_id": str(test_widget.id), "reason": reason})
    assert resp.status_code == 200, resp.text


def test_report_bad_reason_422(public_client, test_widget):
    resp = public_client.post(
        "/api/v1/mobile/reports",
        json=_report_body(test_widget, reason="I just don't like it"))
    assert resp.status_code == 422


def test_report_details_too_long_422(public_client, test_widget):
    resp = public_client.post(
        "/api/v1/mobile/reports",
        json=_report_body(test_widget, details="x" * 1001))
    assert resp.status_code == 422


def test_report_rate_limited_429(public_client, db, test_widget, monkeypatch):
    import app.services.mobile_security as mobile_security
    monkeypatch.setattr(mobile_security, "sliding_window_allow",
                        lambda key, limit, window: (False, 45))
    resp = public_client.post("/api/v1/mobile/reports",
                              json=_report_body(test_widget))
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "45"
    assert db.query(MobileReport).count() == 0


def test_admin_list_reports(public_client, admin_client, db, test_widget,
                            test_organization):
    public_client.post("/api/v1/mobile/reports",
                       json=_report_body(test_widget))
    public_client.post(
        "/api/v1/mobile/reports",
        json=_report_body(test_widget, reason="Spam or scam",
                          details=None, source=None))

    # A report from another org must never leak into this org's list.
    other_org = Organization(name="Other Org", domain="other.example")
    db.add(other_org)
    db.flush()
    db.add(MobileReport(widget_id=str(test_widget.id),
                        organization_id=other_org.id,
                        reason="Offensive content", details="other org"))
    db.commit()

    got = admin_client.get("/api/v1/admin/mobile/reports")
    assert got.status_code == 200, got.text
    reports = got.json()["reports"]
    assert len(reports) == 2
    assert reports[0]["reason"] == "Spam or scam"  # newest first
    assert reports[0]["details"] is None
    assert reports[1]["reason"] == "Offensive content"
    assert reports[1]["details"] == "The AI reply was insulting."
    assert reports[1]["source"] == "mobile"
    assert "other org" not in got.text

    filtered = admin_client.get("/api/v1/admin/mobile/reports",
                                params={"reason": "Spam or scam"})
    assert [r["reason"] for r in filtered.json()["reports"]] == ["Spam or scam"]

    by_widget = admin_client.get(
        "/api/v1/admin/mobile/reports",
        params={"widget_id": str(test_widget.id)})
    assert len(by_widget.json()["reports"]) == 2

    unknown_widget = admin_client.get(
        "/api/v1/admin/mobile/reports",
        params={"widget_id": "no-such-widget"})
    assert unknown_widget.status_code == 404
