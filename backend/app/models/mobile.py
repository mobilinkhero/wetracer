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

"""Mobile models: device push tokens, push attempts, FCM config.

A mobile app chats over the public REST API in app/api/mobile_chat.py and
receives live replies on the web widget's Socket.IO namespace. When no live
socket is connected to a conversation, a reply is pushed via Firebase Cloud
Messaging instead. These tables hold the registered devices, a record of
every push decision (including test-mode "would-be" pushes, when no FCM
credentials are configured), and the org's encrypted FCM service-account JSON.
"""

import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text, TIMESTAMP, UniqueConstraint, func, UUID

from app.database import Base


class MobileDeviceToken(Base):
    """One FCM registration token for one conversation.

    Registered by a mobile app (POST /api/v1/mobile/conversations/{id}/devices).
    Upserted on (session_id, push_token): re-registering the same token refreshes
    it instead of duplicating the row. The full token is stored because FCM
    needs it to send; it is never returned by any API (push-attempts only expose
    a 12-character prefix).
    """

    __tablename__ = "mobile_device_tokens"
    __table_args__ = (
        UniqueConstraint("session_id", "push_token", name="uq_mobile_device_session_token"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("session_to_agents.session_id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    push_token = Column(Text, nullable=False)
    platform = Column(String(16), nullable=False)  # 'android' | 'ios'
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class PushAttempt(Base):
    """Every push decision for a reply, sent or not.

    Test-mode rule: with no FCM credentials configured the chat must never
    fail, so the would-be push is recorded here with status
    'recorded_no_credentials' instead of being sent. Queryable via
    GET /api/v1/mobile/push-attempts?conversation_id=...
    """

    __tablename__ = "mobile_push_attempts"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(UUID(as_uuid=True), ForeignKey("session_to_agents.session_id", ondelete="CASCADE"), nullable=False, index=True)
    message_id = Column(Integer, nullable=True)  # chat_history row delivered, when known
    device_token_prefix = Column(String(16), nullable=False)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    # 'sent' | 'failed' | 'recorded_no_credentials' | 'skipped_live_socket'
    status = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class MobileFcmConfig(Base):
    """One org's FCM service-account JSON, encrypted at rest.

    Saved by an org admin (POST /api/v1/admin/mobile/fcm-config) with the same
    Fernet encrypt_api_key/decrypt_api_key utils as the AI keys. No GET ever
    returns the JSON — only masked status (configured / project_id).
    """

    __tablename__ = "mobile_fcm_configs"

    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    encrypted_service_account_json = Column(Text, nullable=False)
    project_id = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class BlockedVisitor(Base):
    """Phase 1 abuse control: visitors banned from starting or continuing chats.

    NOTE: the original repo had no blocked-visitor list (the one in the
    project memory belonged to the Laravel replica). This table IS the list:
    one row per banned customer and/or IP address, org-scoped. The mobile API
    refuses a blocked visitor with 403 (see app/services/mobile_security.py);
    the web widget socket refuses the same way at connect.
    """

    __tablename__ = "blocked_visitors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    ip_address = Column(String(64), nullable=True, index=True)
    reason = Column(Text, nullable=True)
    blocked_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class MobileWidgetSecurity(Base):
    """Per-widget security settings for the public mobile API: the kill switch
    and per-widget limit overrides. Absent rows mean defaults (see
    app/services/mobile_security.py). Managed by org admins through
    /api/v1/admin/mobile/widgets/{widget_id}/security — never by the app."""

    __tablename__ = "mobile_widget_security"

    widget_id = Column(String, ForeignKey("widgets.id", ondelete="CASCADE"), primary_key=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # Kill switch: when true, the mobile API refuses to start or continue
    # chats on this widget (403). The web widget is unaffected.
    chat_disabled = Column(Boolean, nullable=False, default=False, server_default="false")
    msg_per_min = Column(Integer, nullable=True)  # per-conversation msgs/min (default 30)
    msg_per_ip_per_min = Column(Integer, nullable=True)  # per-IP msgs/min (default 120)
    msg_per_widget_per_min = Column(Integer, nullable=True)  # widget-wide msgs/min (default 600)
    ai_daily_quota_per_conversation = Column(Integer, nullable=True)  # default 200
    ai_daily_quota_per_widget = Column(Integer, nullable=True)  # default 2000
    upload_files_per_day = Column(Integer, nullable=True)  # per-conversation (default 20)
    upload_bytes_per_day = Column(Integer, nullable=True)  # per-conversation (default 50MB)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class MobileAbuseEvent(Base):
    """Append-only log of abuse-control decisions on the public mobile API
    (rate limits, blocks, temp bans, spam signals, AI quota/spike). Read by
    org admins through GET /api/v1/admin/mobile/abuse-events — the Phase 1
    abuse panel's backend. No PII beyond a hashed fingerprint is stored here;
    see app/services/mobile_security.py."""

    __tablename__ = "mobile_abuse_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    widget_id = Column(String, ForeignKey("widgets.id", ondelete="CASCADE"), nullable=True, index=True)
    session_id = Column(UUID(as_uuid=True), ForeignKey("session_to_agents.session_id", ondelete="SET NULL"), nullable=True, index=True)
    ip_address = Column(String(64), nullable=True)
    # rate_limited | blocked_visitor | temp_ban | spam_repeat | link_flood |
    # ai_quota | ai_spike | kill_switch | challenge
    event_type = Column(String(64), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


# Reasons a visitor can pick when reporting AI content. Keep in sync with
# the ReportRequest reason Literal in app/api/mobile_chat.py.
REPORT_REASONS = (
    "Offensive content",
    "Wrong or misleading info",
    "Spam or scam",
    "Something else",
)


class MobileReport(Base):
    """Visitor report of offensive AI content.

    Google Play's AI-Generated Content policy requires apps with generative
    AI to let users report or flag offensive AI content without leaving the
    app — this table is where those reports land. Public, no login: the
    widget ID is public, the same trust level as the widget itself, so the
    endpoint (POST /api/v1/mobile/reports) takes no auth and no AI config.
    Read by org admins through GET /api/v1/admin/mobile/reports.
    """

    __tablename__ = "mobile_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    widget_id = Column(String, ForeignKey("widgets.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    reason = Column(String(64), nullable=False)  # one of REPORT_REASONS
    details = Column(Text, nullable=True)  # free text, max 1000 chars at the API
    source = Column(String(64), nullable=True)  # e.g. 'widget' | 'mobile' | 'web'
    ip_address = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
