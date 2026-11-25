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

"""Tests for app.services.push_service — the FCM push delivery flow.

firebase_admin is always mocked here: no test ever talks to real FCM.
The fake records every send/initialise call so the tests can assert exactly
which device tokens a push was attempted for.
"""

import json

import firebase_admin
import pytest

from app.core.security import encrypt_api_key
from app.models.mobile import MobileDeviceToken, MobileFcmConfig, PushAttempt
from app.services.push_service import (
    STATUS_FAILED,
    STATUS_RECORDED_NO_CREDENTIALS,
    STATUS_SENT,
    maybe_push_reply,
)


# Fake service-account JSON: clearly fake, never a real credential. Only
# type / project_id / private_key are read anywhere in the send path (and
# only by the mocked Certificate call); the rest mirrors the real shape.
FAKE_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "fake-key-id-for-tests-only",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKEKEYFORTESTSONLY\n-----END PRIVATE KEY-----\n",
    "client_email": "firebase-adminsdk-test@test-project.iam.gserviceaccount.com",
    "client_id": "123456789012345678901",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class _FakeNotification:
    def __init__(self, title=None, body=None):
        self.title = title
        self.body = body


class _FakeMessage:
    def __init__(self, notification=None, data=None, token=None):
        self.notification = notification
        self.data = data or {}
        self.token = token


@pytest.fixture
def fake_fcm(monkeypatch):
    """Replace firebase_admin with recording fakes.

    Returns a dict of call recorders. ``send`` can be made to fail for
    chosen tokens by adding them to ``fail_tokens``.
    """
    calls = {"send": [], "init": [], "cert": [], "fail_tokens": set()}

    def _get_app(name):
        # No app ever pre-exists in tests: every send initialises one.
        raise ValueError(f"No app '{name}'")

    def _initialize_app(cred, name=None):
        calls["init"].append({"cred": cred, "name": name})
        return object()

    def _certificate(service_account):
        calls["cert"].append(service_account)
        return "fake-cert"

    def _send(message, app=None):
        if message.token in calls["fail_tokens"]:
            raise Exception("FCM unavailable (fake)")
        calls["send"].append({"message": message, "app": app})
        return "fake-message-id"

    monkeypatch.setattr(firebase_admin, "get_app", _get_app)
    monkeypatch.setattr(firebase_admin, "initialize_app", _initialize_app)
    monkeypatch.setattr(firebase_admin.credentials, "Certificate", _certificate)
    monkeypatch.setattr(firebase_admin.messaging, "Message", _FakeMessage)
    monkeypatch.setattr(firebase_admin.messaging, "Notification", _FakeNotification)
    monkeypatch.setattr(firebase_admin.messaging, "send", _send)
    # The visitor is never on a live socket in these tests: pushes are decided
    # purely on credentials/devices, deterministically.
    monkeypatch.setattr(
        "app.services.push_service.is_live_socket", lambda session_id: False
    )
    return calls


@pytest.fixture
def push_session(db, test_widget, test_customer):
    """A real conversation row the device tokens can point at."""
    from app.repositories.session_to_agent import SessionToAgentRepository

    session = SessionToAgentRepository(db).create_session(
        session_id="11111111-2222-3333-4444-555555555555",
        agent_id=test_widget.agent_id, customer_id=test_customer.id,
        organization_id=test_widget.organization_id, channel="web")
    return session


def _add_device(db, session_id, organization_id, token, platform="android"):
    db.add(MobileDeviceToken(
        session_id=session_id, organization_id=organization_id,
        push_token=token, platform=platform, is_active=True))
    db.commit()


def _add_fcm_config(db, organization_id, service_account=None):
    db.add(MobileFcmConfig(
        organization_id=organization_id,
        encrypted_service_account_json=encrypt_api_key(
            json.dumps(service_account or FAKE_SERVICE_ACCOUNT)),
        project_id=(service_account or FAKE_SERVICE_ACCOUNT)["project_id"]))
    db.commit()


def _attempts(db):
    return db.query(PushAttempt).order_by(PushAttempt.id).all()


# --- 1. No credentials: test mode, recorded, never an exception -------------


@pytest.mark.asyncio
async def test_no_credentials_records_test_mode_and_never_raises(
        db, test_widget, push_session, fake_fcm):
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-android-001")
    long_text = "Hello from the AI. " * 20  # > 120 chars: body must be previewed
    await maybe_push_reply(
        db, session_id=str(push_session.session_id),
        organization_id=test_widget.organization_id,
        message_id=42, title="Test Agent", text=long_text)

    assert fake_fcm["send"] == []  # no FCM attempt at all without credentials
    assert fake_fcm["init"] == []
    attempts = _attempts(db)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == STATUS_RECORDED_NO_CREDENTIALS
    assert attempt.message_id == 42
    assert attempt.device_token_prefix == "fcm-test-tok"
    assert attempt.title == "Test Agent"
    assert attempt.body == long_text.strip()[:120]
    assert "test mode" in (attempt.reason or "").lower()


# --- 2. Credentials: one FCM message per device ------------------------------


@pytest.mark.asyncio
async def test_credentials_send_one_message_per_device(
        db, test_widget, push_session, fake_fcm):
    _add_fcm_config(db, test_widget.organization_id)
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-android-001", platform="android")
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-ios-002", platform="ios")
    await maybe_push_reply(
        db, session_id=str(push_session.session_id),
        organization_id=test_widget.organization_id,
        message_id=43, title="Test Agent", text="Hello from the AI")

    sent_tokens = [call["message"].token for call in fake_fcm["send"]]
    assert sent_tokens == ["fcm-test-token-android-001", "fcm-test-token-ios-002"]
    for call in fake_fcm["send"]:
        assert call["message"].data["conversation_id"] == str(push_session.session_id)
        assert call["message"].data["message_id"] == "43"
        assert call["message"].notification.title == "Test Agent"
    # The named per-org app was initialised with the org's service account —
    # once per device send, because the fake get_app always reports "no app"
    # (real firebase_admin would return the already-initialised app).
    assert fake_fcm["cert"] == [FAKE_SERVICE_ACCOUNT, FAKE_SERVICE_ACCOUNT]
    assert len(fake_fcm["init"]) == 2
    assert all(call["name"] == f"chattermate-mobile-{test_widget.organization_id}"
               for call in fake_fcm["init"])
    attempts = _attempts(db)
    assert len(attempts) == 2
    assert {a.status for a in attempts} == {STATUS_SENT}
    assert {a.device_token_prefix for a in attempts} == {"fcm-test-tok"}
    assert {a.message_id for a in attempts} == {43}


# --- 3. Send failure: recorded, other devices still served -------------------


@pytest.mark.asyncio
async def test_fcm_failure_recorded_without_breaking_chat(
        db, test_widget, push_session, fake_fcm):
    _add_fcm_config(db, test_widget.organization_id)
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-android-001", platform="android")
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-ios-002", platform="ios")
    fake_fcm["fail_tokens"].add("fcm-test-token-android-001")

    # Must return normally: a failed send never breaks the chat turn.
    await maybe_push_reply(
        db, session_id=str(push_session.session_id),
        organization_id=test_widget.organization_id,
        message_id=44, title="Test Agent", text="Hello from the AI")

    assert [call["message"].token for call in fake_fcm["send"]] == [
        "fcm-test-token-ios-002"]
    attempts = _attempts(db)
    assert len(attempts) == 2
    failed = [a for a in attempts if a.status == STATUS_FAILED]
    assert len(failed) == 1
    assert "FCM unavailable (fake)" in (failed[0].reason or "")
    assert [a for a in attempts if a.status == STATUS_SENT] != []


# --- 4. No devices: nothing sent, nothing recorded ----------------------------


@pytest.mark.asyncio
async def test_no_devices_sends_nothing(
        db, test_widget, push_session, fake_fcm):
    _add_fcm_config(db, test_widget.organization_id)
    await maybe_push_reply(
        db, session_id=str(push_session.session_id),
        organization_id=test_widget.organization_id,
        message_id=45, title="Test Agent", text="Hello from the AI")

    assert fake_fcm["send"] == []
    assert fake_fcm["init"] == []
    assert _attempts(db) == []


# --- 5. Dedupe: the same reply is never pushed twice --------------------------


@pytest.mark.asyncio
async def test_same_reply_not_pushed_twice(
        db, test_widget, push_session, fake_fcm):
    _add_fcm_config(db, test_widget.organization_id)
    _add_device(db, push_session.session_id, test_widget.organization_id,
                "fcm-test-token-android-001")
    kwargs = dict(session_id=str(push_session.session_id),
                  organization_id=test_widget.organization_id,
                  message_id=46, title="Test Agent", text="Hello from the AI")
    await maybe_push_reply(db, **kwargs)
    await maybe_push_reply(db, **kwargs)

    assert len(fake_fcm["send"]) == 1
    assert len(_attempts(db)) == 1
    assert _attempts(db)[0].status == STATUS_SENT
