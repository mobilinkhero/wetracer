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

"""Wildcard CORS for the public visitor API only.

The web widget is embedded on arbitrary customer sites and the Flutter web
build runs on any origin, so the public mobile/widget API must answer
cross-origin with no proxy: Access-Control-Allow-Origin: * on every response
and a 200 preflight. Admin and dashboard routes must never get the wildcard —
they authenticate with the dashboard's sign-in cookie, and a wildcard there
would let any site read a signed-in user's account.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cors
from app.core.cors import FirstPartyCredentialsCORSMiddleware, PublicApiCORSMiddleware
from app.database import get_db

ANY_ORIGIN = "http://localhost:8903"  # arbitrary origin, in no allow-list
DASHBOARD = "https://app.example.com"


def public_app():
    app = FastAPI()
    # Same order as app/main.py: origin-list middleware first, public wildcard
    # after it (outermost, so it runs first).
    app.add_middleware(
        FirstPartyCredentialsCORSMiddleware,
        credentialed_origins={DASHBOARD},
        allow_origins=[DASHBOARD],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(PublicApiCORSMiddleware)

    @app.post("/api/v1/mobile/conversations")
    def new_conversation():
        return {"conversation_id": "x"}

    @app.get("/api/v1/widgets/{widget_id}")
    def widget_config(widget_id: str):
        return {"widget_id": widget_id}

    @app.get("/api/v1/admin/mobile/fcm-config")
    def fcm_config():
        return {"configured": False}

    @app.get("/api/v1/organizations/me")
    def me():
        return {"org": "acme"}

    return app


@pytest.fixture
def client():
    return TestClient(public_app())


class TestPublicApiWildcard:
    def test_mobile_post_returns_wildcard_for_any_origin(self, client):
        resp = client.post(
            "/api/v1/mobile/conversations",
            json={"widget_id": str(uuid.uuid4())},
            headers={"Origin": ANY_ORIGIN},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in resp.headers

    def test_widgets_get_returns_wildcard_for_any_origin(self, client):
        resp = client.get(
            f"/api/v1/widgets/{uuid.uuid4()}",
            headers={"Origin": ANY_ORIGIN},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"

    def test_preflight_to_public_path_returns_200_with_allow_headers(self, client):
        resp = client.options(
            "/api/v1/mobile/conversations",
            headers={
                "Origin": ANY_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Conversation-Token, Content-Type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"
        methods = resp.headers["access-control-allow-methods"]
        for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
            assert method in methods
        allow_headers = resp.headers["access-control-allow-headers"]
        for header in (
            "Content-Type",
            "Authorization",
            "X-Conversation-Token",
            "X-Device-Id",
            "X-App-Version",
        ):
            assert header in allow_headers

    def test_listed_origin_on_public_path_gets_exactly_one_wildcard(self, client):
        # The dashboard origin is in the inner allow-list AND first-party, so
        # the inner middleware would answer ACAO: <dashboard> + credentials.
        # The public middleware must replace that with a single wildcard.
        resp = client.post(
            "/api/v1/mobile/conversations",
            json={"widget_id": str(uuid.uuid4())},
            headers={"Origin": DASHBOARD},
        )
        assert resp.status_code == 200
        assert resp.headers.get_list("access-control-allow-origin") == ["*"]
        assert "access-control-allow-credentials" not in resp.headers


class TestAdminAndDashboardStayListed:
    def test_admin_get_gets_no_wildcard(self, client):
        resp = client.get(
            "/api/v1/admin/mobile/fcm-config",
            headers={"Origin": ANY_ORIGIN},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") != "*"

    def test_preflight_to_admin_path_gets_no_wildcard(self, client):
        resp = client.options(
            "/api/v1/admin/mobile/fcm-config",
            headers={
                "Origin": ANY_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") != "*"

    def test_non_public_dashboard_route_gets_no_wildcard(self, client):
        resp = client.get(
            "/api/v1/organizations/me",
            headers={"Origin": ANY_ORIGIN},
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


class TestRealAppWiring:
    def test_real_app_registers_the_public_middleware(self):
        # app.main:app is the Socket.IO-wrapped ASGI app; the middleware lives
        # on the raw FastAPI app in app.core.application.
        from app.core.application import app as real_app

        assert any(
            isinstance(m.cls, type) and issubclass(m.cls, PublicApiCORSMiddleware)
            for m in real_app.user_middleware
        )

    def test_real_app_mobile_endpoint_returns_wildcard(self, db):
        """End to end against the real app: an unknown widget 404s, but the
        wildcard header is still on the response."""
        from app.core.application import app as real_app

        def _override_db():
            yield db

        real_app.dependency_overrides[get_db] = _override_db
        try:
            with TestClient(real_app) as client:
                resp = client.post(
                    "/api/v1/mobile/conversations",
                    json={"widget_id": str(uuid.uuid4())},
                    headers={"Origin": ANY_ORIGIN},
                )
                assert resp.headers.get("access-control-allow-origin") == "*"
                assert "access-control-allow-credentials" not in resp.headers
        finally:
            real_app.dependency_overrides.clear()

    def test_real_app_admin_endpoint_gets_no_wildcard(self, db):
        from app.core.application import app as real_app

        def _override_db():
            yield db

        real_app.dependency_overrides[get_db] = _override_db
        try:
            with TestClient(real_app) as client:
                resp = client.get(
                    "/api/v1/admin/mobile/reports",
                    headers={"Origin": ANY_ORIGIN},
                )
                assert resp.headers.get("access-control-allow-origin") != "*"
        finally:
            real_app.dependency_overrides.clear()
