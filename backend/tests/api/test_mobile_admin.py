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

"""Tests for the mobile-admin router (app/api/mobile_admin.py): FCM
credentials, blocked visitors, per-widget security (kill switch), abuse log.
Dashboard auth only — the public mobile router must expose none of these.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mobile_admin
from app.core.auth import get_current_user
from app.core.security import decrypt_api_key
from app.database import get_db
from app.models.mobile import BlockedVisitor, MobileAbuseEvent, MobileFcmConfig, MobileWidgetSecurity

test_app = FastAPI()
test_app.include_router(mobile_admin.router, prefix="/api/v1/admin/mobile")


@pytest.fixture
def client(db, test_organization):
    def _override_db():
        yield db

    admin = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                            organization_id=test_organization.id)
    test_app.dependency_overrides[get_db] = _override_db
    test_app.dependency_overrides[get_current_user] = lambda: admin
    with TestClient(test_app) as c:
        yield c
    test_app.dependency_overrides.clear()


def test_fcm_config_roundtrip_masked(client, db):
    service_account = {"type": "service_account", "project_id": "demo-proj",
                       "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                       "client_email": "x@demo-proj.iam.gserviceaccount.com"}
    resp = client.post("/api/v1/admin/mobile/fcm-config",
                       json={"service_account_json": service_account})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": True, "project_id": "demo-proj"}

    got = client.get("/api/v1/admin/mobile/fcm-config")
    assert got.status_code == 200
    assert got.json()["configured"] is True
    assert got.json()["project_id"] == "demo-proj"
    assert "private_key" not in got.text
    assert "abc" not in got.text

    row = db.query(MobileFcmConfig).first()
    assert row is not None
    assert json.loads(decrypt_api_key(row.encrypted_service_account_json)) == service_account

    bad = client.post("/api/v1/admin/mobile/fcm-config",
                      json={"service_account_json": {"type": "not-a-service-account"}})
    assert bad.status_code == 422


def test_blocked_visitors_crud(client, db, test_organization):
    created = client.post("/api/v1/admin/mobile/blocked-visitors",
                          json={"ip_address": "203.0.113.9", "reason": "spam flood"})
    assert created.status_code == 200, created.text
    blocked_id = created.json()["id"]

    listed = client.get("/api/v1/admin/mobile/blocked-visitors").json()["blocked"]
    assert len(listed) == 1
    assert listed[0]["ip_address"] == "203.0.113.9"

    assert client.post("/api/v1/admin/mobile/blocked-visitors", json={}).status_code == 422

    assert client.delete(f"/api/v1/admin/mobile/blocked-visitors/{blocked_id}").status_code == 200
    assert client.get("/api/v1/admin/mobile/blocked-visitors").json()["blocked"] == []


def test_widget_security_get_put(client, db, test_widget):
    url = f"/api/v1/admin/mobile/widgets/{test_widget.id}/security"
    got = client.get(url)
    assert got.status_code == 200
    assert got.json()["chat_disabled"] is False
    assert got.json()["msg_per_min"] == 30  # the default

    updated = client.put(url, json={"chat_disabled": True, "msg_per_min": 5})
    assert updated.status_code == 200
    assert updated.json()["chat_disabled"] is True
    assert updated.json()["msg_per_min"] == 5

    row = db.query(MobileWidgetSecurity).filter(
        MobileWidgetSecurity.widget_id == test_widget.id).first()
    assert row is not None and row.chat_disabled is True


def test_widget_security_other_org_404(client, db, test_organization):
    from app.models.widget import Widget
    other_org_widget = Widget(id="99999999-9999-9999-9999-999999999999",
                              organization_id=uuid.UUID("88888888-8888-8888-8888-888888888888"),
                              agent_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
                              name="Other org widget")
    db.add(other_org_widget)
    db.commit()
    assert client.get(
        f"/api/v1/admin/mobile/widgets/{other_org_widget.id}/security").status_code == 404


def test_abuse_events_list(client, db, test_organization, test_widget):
    db.add(MobileAbuseEvent(organization_id=test_organization.id,
                            widget_id=test_widget.id, ip_address="203.0.113.9",
                            event_type="rate_limited", detail="burst"))
    db.add(MobileAbuseEvent(organization_id=test_organization.id,
                            widget_id=test_widget.id, ip_address="198.51.100.2",
                            event_type="blocked_visitor", detail="banned"))
    db.commit()
    events = client.get("/api/v1/admin/mobile/abuse-events").json()["events"]
    assert len(events) == 2
    only_blocked = client.get("/api/v1/admin/mobile/abuse-events",
                              params={"event_type": "blocked_visitor"}).json()["events"]
    assert len(only_blocked) == 1
    assert only_blocked[0]["event_type"] == "blocked_visitor"
