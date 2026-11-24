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

"""Phase 1 security for the public mobile Chat API (/api/v1/mobile).

What was REUSED (not re-invented):
- Conversation tokens: app/services/conversation_token.py + verify_conversation_token
  (JWT, Redis-backed revocation via jti). The mobile visitor JWT is one of
  these, minted short-lived (1h) with device + conversation claims.
- Per-customer AI reply limits: app/services/reply_limits.py `check`
  (per-minute / per-day, Redis, fail-open).
- Socket rate limiting: app/services/socket_rate_limit.py (per-IP daily +
  per-second event limits, localhost skipped). The REST limits below follow
  the same fail-open-on-Redis-failure rule.
- Magic-byte upload validation + size caps: app/services/file_upload_service.py
  (5MB images / 10MB files, magic bytes incl. executables rejected by the
  allow-list — only listed MIME types ever pass).
- Socket heartbeat / idle timeout: app/core/socketio.py already runs
  ping_interval=25s, ping_timeout=60s (a socket that stops answering pongs is
  dropped), max_http_buffer_size=15MB, and an origin policy.

What was BUILT NEW here (no equivalent existed in this repo):
- The blocked-visitor list (app/models/mobile.py BlockedVisitor). The list in
  the project memory belonged to the Laravel replica, not this repo.
- Redis sliding-window rate limits (per conversation / IP / widget), AI daily
  quotas, per-conversation daily upload limits, spam/risk scoring, temp bans,
  the per-widget kill switch, and the abuse-event log.
- Device fingerprinting (SHA-256 of device_id + IP — the raw values are never
  stored in the abuse log) and IP reputation (blocked IPs + temp bans).

Explicitly OUT OF SCOPE (user decision 2026-11-23): device attestation —
Play Integrity (Android) and App Attest (iOS) are NOT used, NOT planned, and
no code or dependency for them exists anywhere in this backend. The chosen
approach is the visitor JWT + rate limits + fingerprint + CAPTCHA challenge
below. See docs/API-SECURITY.md.

Fail-open rule: every Redis check here allows the request when Redis is down
or errors (a broken counter must not stop the chat — same rule as
reply_limits). The DB-backed checks (blocked list, kill switch) fail CLOSED
only when the DB itself errors, which the caller turns into a 500, not a pass.
"""

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.models.mobile import BlockedVisitor, MobileAbuseEvent, MobileWidgetSecurity

logger = get_logger(__name__)

# --- Defaults (overridable per widget via MobileWidgetSecurity) -------------
MSG_PER_CONVERSATION_PER_MIN = 30
MSG_PER_IP_PER_MIN = 120
MSG_PER_WIDGET_PER_MIN = 600
AI_PER_CONVERSATION_PER_DAY = 200
AI_PER_WIDGET_PER_DAY = 2000
UPLOAD_FILES_PER_CONVERSATION_PER_DAY = 20
UPLOAD_BYTES_PER_CONVERSATION_PER_DAY = 50 * 1024 * 1024  # 50 MB

# Visitor JWT lifetime: short-lived by design; the client rotates it (see
# POST /api/v1/mobile/conversations/{id}/rotate) at least this often.
VISITOR_TOKEN_TTL_SECONDS = 3600

# WebSocket concurrent-connection caps (enforced at /widget connect).
WS_MAX_CONNECTIONS_PER_IP = 20
WS_MAX_CONNECTIONS_PER_WIDGET = 500
# How long a connect entry lives in Redis: a missed disconnect can only
# inflate the count until this expires.
WS_CONN_TTL_SECONDS = 300

# Risk scoring: >= CHALLENGE_SCORE slows the visitor down (429), >= BAN_SCORE
# temp-bans them (403). See risk_score().
CHALLENGE_SCORE = 40
BAN_SCORE = 70
TEMP_BAN_SECONDS = 900  # 15 minutes

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


@dataclass
class WidgetLimits:
    """Effective limits for one widget: row overrides, else the defaults."""

    msg_per_min: int = MSG_PER_CONVERSATION_PER_MIN
    msg_per_ip_per_min: int = MSG_PER_IP_PER_MIN
    msg_per_widget_per_min: int = MSG_PER_WIDGET_PER_MIN
    ai_per_conversation_per_day: int = AI_PER_CONVERSATION_PER_DAY
    ai_per_widget_per_day: int = AI_PER_WIDGET_PER_DAY
    upload_files_per_day: int = UPLOAD_FILES_PER_CONVERSATION_PER_DAY
    upload_bytes_per_day: int = UPLOAD_BYTES_PER_CONVERSATION_PER_DAY
    chat_disabled: bool = False


def _as_uuid(value):
    """UUID objects for UUID columns: the SQLite test schema only accepts
    UUID objects (its CHAR(36) compiler calls .hex), while callers usually
    pass strings. Postgres coerces strings, SQLite does not — coerce here so
    both behave the same."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def widget_limits(db: Session, widget_id) -> WidgetLimits:
    """The widget's security row merged onto the defaults. Absent row = defaults."""
    limits = WidgetLimits()
    try:
        row = db.query(MobileWidgetSecurity).filter(
            MobileWidgetSecurity.widget_id == widget_id).first()
    except Exception as e:
        logger.warning(f"Could not read widget security for {widget_id}: {e}")
        return limits
    if not row:
        return limits
    if row.msg_per_min is not None:
        limits.msg_per_min = row.msg_per_min
    if row.msg_per_ip_per_min is not None:
        limits.msg_per_ip_per_min = row.msg_per_ip_per_min
    if row.msg_per_widget_per_min is not None:
        limits.msg_per_widget_per_min = row.msg_per_widget_per_min
    if row.ai_daily_quota_per_conversation is not None:
        limits.ai_per_conversation_per_day = row.ai_daily_quota_per_conversation
    if row.ai_daily_quota_per_widget is not None:
        limits.ai_per_widget_per_day = row.ai_daily_quota_per_widget
    if row.upload_files_per_day is not None:
        limits.upload_files_per_day = row.upload_files_per_day
    if row.upload_bytes_per_day is not None:
        limits.upload_bytes_per_day = row.upload_bytes_per_day
    limits.chat_disabled = bool(row.chat_disabled)
    return limits


def client_ip(request) -> str:
    """The caller's IP, X-Forwarded-For first (same order as socket_rate_limit)."""
    if request is None:
        return "unknown"
    headers = getattr(request, "headers", {}) or {}
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"


def device_fingerprint(device_id: Optional[str], ip: str) -> str:
    """A stable fingerprint for abuse logging: SHA-256, so the raw device id
    and IP never land in the abuse log."""
    return hashlib.sha256(f"{device_id or ''}|{ip}".encode()).hexdigest()[:32]


def _redis():
    try:
        from app.core.redis import get_redis
        return get_redis()
    except Exception as e:
        logger.warning(f"Redis unavailable for mobile rate limits: {e}")
        return None


def ws_connect_allowed(ip: str, widget_id: str, sid: str) -> bool:
    """Per-IP / per-widget concurrent Socket.IO connection caps (Phase 1).
    Redis sets hold the live sids; a missed disconnect can only inflate the
    count until WS_CONN_TTL_SECONDS. Fail-open without Redis."""
    client = _redis()
    if client is None:
        return True
    try:
        ip_key = f"mobile:ws:ip:{ip}"
        widget_key = f"mobile:ws:widget:{widget_id}"
        results = client.pipeline().sadd(ip_key, sid).expire(
            ip_key, WS_CONN_TTL_SECONDS).sadd(widget_key, sid).expire(
            widget_key, WS_CONN_TTL_SECONDS).scard(ip_key).scard(widget_key).execute()
        ip_count, widget_count = results[4], results[5]
        if ip_count > WS_MAX_CONNECTIONS_PER_IP or widget_count > WS_MAX_CONNECTIONS_PER_WIDGET:
            client.srem(ip_key, sid)
            client.srem(widget_key, sid)
            logger.warning(f"WS connection cap hit: ip {ip}={ip_count}, widget {widget_id}={widget_count}")
            return False
        return True
    except Exception as e:
        logger.warning(f"WS cap check failed, allowing: {e}")
        return True


def ws_disconnect(ip: Optional[str], widget_id: Optional[str], sid: str) -> None:
    """Drop a sid from the connection-cap sets. Best-effort."""
    client = _redis()
    if client is None:
        return
    try:
        if ip:
            client.srem(f"mobile:ws:ip:{ip}", sid)
        if widget_id:
            client.srem(f"mobile:ws:widget:{widget_id}", sid)
    except Exception:
        pass


def sliding_window_allow(key: str, limit: int, window_seconds: int):
    """Sliding-window check (Redis sorted set). Returns (allowed, retry_after).

    Fail-open: without Redis, or on any Redis error, the request is allowed.
    """
    client = _redis()
    if client is None:
        return True, 0
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex}"
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        _, count = pipe.execute()
        if count < limit:
            pipe = client.pipeline()
            pipe.zadd(key, {member: now})
            pipe.expire(key, window_seconds)
            pipe.execute()
            return True, 0
        oldest = client.zrange(key, 0, 0, withscores=True)
        retry_after = 1
        if oldest:
            retry_after = max(1, int(window_seconds - (now - oldest[0][1])) + 1)
        return False, retry_after
    except Exception as e:
        logger.warning(f"Sliding-window check failed for {key}, allowing: {e}")
        return True, 0


def _day_key_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def today_suffix() -> str:
    """UTC day (YYYYMMDD) for daily-counter keys."""
    return _day_key_suffix()


def _seconds_to_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    from datetime import timedelta
    midnight += timedelta(days=1)
    return max(1, int((midnight - now).total_seconds()))


def daily_counter_check(key: str, limit: int):
    """Fixed day-window counter (INCR + expire at UTC midnight).
    Returns (allowed, retry_after_seconds). Fail-open without Redis."""
    client = _redis()
    if client is None:
        return True, 0
    try:
        count = client.incr(key)
        if count == 1:
            client.expire(key, _seconds_to_midnight_utc())
        if count <= limit:
            return True, 0
        return False, _seconds_to_midnight_utc()
    except Exception as e:
        logger.warning(f"Daily counter check failed for {key}, allowing: {e}")
        return True, 0


def daily_counter_value(key: str) -> int:
    client = _redis()
    if client is None:
        return 0
    try:
        value = client.get(key)
        return int(value) if value is not None else 0
    except Exception:
        return 0


def daily_counter_incr(key: str, amount: int = 1) -> int:
    client = _redis()
    if client is None:
        return 0
    try:
        count = client.incrby(key, amount)
        if count == amount:
            client.expire(key, _seconds_to_midnight_utc())
        return count
    except Exception as e:
        logger.warning(f"Daily counter incr failed for {key}: {e}")
        return 0


# --- Blocking / bans ---------------------------------------------------------

def _temp_ban_key(kind: str, value: str) -> str:
    return f"mobile:ban:{kind}:{value}"


def is_temp_banned(kind: str, value: str) -> bool:
    client = _redis()
    if client is None or not value:
        return False
    try:
        return bool(client.exists(_temp_ban_key(kind, value)))
    except Exception:
        return False


def temp_ban(kind: str, value: str, seconds: int = TEMP_BAN_SECONDS) -> None:
    client = _redis()
    if client is None or not value:
        return
    try:
        client.setex(_temp_ban_key(kind, value), seconds, "1")
    except Exception as e:
        logger.warning(f"Could not set temp ban for {kind}:{value}: {e}")


def is_blocked(db: Session, organization_id, customer_id=None, ip: Optional[str] = None) -> Optional[BlockedVisitor]:
    """The blocked-visitor row banning this customer or IP, if any — plus
    Redis temp bans. Returns the row (or a sentinel) or None."""
    if ip and is_temp_banned("ip", ip):
        return "temp_ban_ip"
    if customer_id and is_temp_banned("customer", str(customer_id)):
        return "temp_ban_customer"
    try:
        org_uuid = _as_uuid(organization_id)
        cust_uuid = _as_uuid(customer_id)
        query = db.query(BlockedVisitor).filter(
            BlockedVisitor.organization_id == org_uuid)
        if cust_uuid:
            row = query.filter(BlockedVisitor.customer_id == cust_uuid).first()
            if row:
                return row
        if ip:
            row = query.filter(BlockedVisitor.ip_address == ip).first()
            if row:
                return row
    except Exception as e:
        logger.error(f"Blocked-visitor lookup failed: {e}")
        raise
    return None


# --- Spam / risk scoring -----------------------------------------------------

def count_links(text: str) -> int:
    return len(_URL_RE.findall(text or ""))


def risk_score(text: str, last_visitor_text: Optional[str], link_count: Optional[int] = None) -> tuple[int, list]:
    """Spam signals on one message. Identical repeats and link floods are the
    two signals the widget sees most; each adds to the score, and the caller
    decides the consequence (slow-down vs temp ban)."""
    reasons = []
    score = 0
    normalized = (text or "").strip().lower()
    if normalized and last_visitor_text and normalized == last_visitor_text.strip().lower():
        score += 50
        reasons.append("identical repeat of previous message")
    links = link_count if link_count is not None else count_links(text)
    if links >= 6:
        score += 40
        reasons.append(f"link flood ({links} links)")
    elif links >= 3:
        score += 20
        reasons.append(f"many links ({links})")
    if len(text or "") > 4000:
        score += 10
        reasons.append("overlong message")
    return score, reasons


# --- Abuse log ---------------------------------------------------------------

def record_abuse_event(db: Session, *, event_type: str, organization_id=None,
                       widget_id=None, session_id=None, ip: Optional[str] = None,
                       device_id: Optional[str] = None,
                       detail: Optional[str] = None) -> None:
    """Append-only abuse log. Never raises into the request path.

    When the caller's device id is known it is stamped as a fingerprint
    (``[fp=...]``) — the SHA-256 of device id + IP — so a repeat offender can
    be recognised across rows without the raw device id or IP in the detail.
    """
    if device_id is not None:
        fp = device_fingerprint(device_id, ip or "")
        detail = f"[fp={fp}] {detail}" if detail else f"[fp={fp}]"
    try:
        db.add(MobileAbuseEvent(
            organization_id=_as_uuid(organization_id),
            widget_id=widget_id,
            session_id=_as_uuid(session_id), ip_address=ip,
            event_type=event_type, detail=detail))
        db.commit()
    except Exception as e:
        logger.warning(f"Could not record abuse event {event_type}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
