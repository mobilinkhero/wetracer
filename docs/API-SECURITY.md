# ChatterMate Public Chat API — Security Plan

**Status 2026-11-23:** Phase 1 is implemented and live-verified against the
test widget. Phases 2–3 are documented-only follow-ups. Device attestation
(Play Integrity / App Attest) is **explicitly out of scope** — not a
follow-up, not planned.

The public surface is `POST/GET /api/v1/mobile/*` (visitor) plus the
`/widget` Socket.IO namespace. Admin operations live only under
`/api/v1/admin/mobile/*` (dashboard auth, org-scoped); the public router
exposes **zero** admin operations.

Related docs: `docs/AI-INTEGRATION.md` (§6, contract corrections),
`CHAT_API_REFERENCE.md` (§6), `CHAT_API_OPENAPI.json`,
`CHAT_API_LLM.md`.

---

## Threat model (what Phase 1 defends against)

- **Token theft / replay:** a leaked visitor token must be short-lived,
  scoped, revocable, and bound to one device.
- **Spam / flooding:** one visitor or botnet flooding the chat or the
  model (AI calls are the expensive part).
- **Abuse of the AI:** prompt-injection via media, link floods, mass
  uploads.
- **Harassment:** a banned visitor re-entering; a compromised widget
  used to spam visitors.
- **Credential exposure:** AI keys / FCM service-account keys must never
  reach an app binary or a public response.

Out of scope for Phase 1: botnets with fully valid tokens at scale (see
Phase 2/3), account takeover of the dashboard (covered by dashboard auth),
and model-level prompt injection (the agent's guardrails, not this API).

---

## Phase 1 — IMPLEMENTED

### 1. Auth: public widget key + short-lived visitor JWT

- The widget id is **public** (embeddable, not a secret).
- On conversation start the backend mints a signed visitor JWT
  (`app/api/mobile_chat.py::_mint_visitor_token`, via
  `app/services/conversation_token.py::mint` — the same signing,
  verification and Redis revocation as the web widget's conversation
  token): 1 hour (`VISITOR_TOKEN_TTL_SECONDS = 3600`), claims
  `widget_id`, `sub`/`customer_id`, `customer_email`, `ckey`,
  `device_id`, `session_id`, `type: "conversation"`, `jti`.
- The token is bound to one device: calls must send the same device id
  as `X-Device-Id`, or they get
  `401 "Conversation token was issued to another device"`.
- Rotation: `POST /conversations/{id}/rotate` issues a fresh token and
  revokes the old `jti` at once (verified live: old token → 401).
- Server-side secret API keys (per agent/widget, revocable,
  dashboard-issued) are for admin/server-to-server use only and must
  never ship in an app binary.
- The same JWT is the Socket.IO `/widget` auth
  (`auth: {"conversation_token": …}`).

### 2. Rate limiting (Redis sliding window)

`app/services/mobile_security.py` — reuses `reply_limits.check` (the
agent's own per-customer limits) and `socket_rate_limit.py` where they
fit; the mobile windows are new, not duplicated. Fail-open on Redis
failure (a broken counter must not stop the chat), same rule as
`reply_limits`.

| Limit | Default | Overridable per widget |
|---|---|---|
| Messages per conversation | 30 / min | `MobileWidgetSecurity.msg_per_min` |
| Messages per IP (per widget) | 120 / min | `msg_per_ip_per_min` |
| Messages per widget (global) | 600 / min | `msg_per_widget_per_min` |
| AI replies per conversation | 200 / day | `ai_daily_quota_per_conversation` |
| AI replies per widget | 2,000 / day | `ai_daily_quota_per_widget` |
| Uploads per conversation | 20 files / 50 MB per day | `upload_files_per_day` / `upload_bytes_per_day` |

- Every limit returns **429 with a `Retry-After` header** (seconds; UTC
  midnight for the AI daily quotas). Verified live: `429` +
  `retry-after: 56` + `{"detail": "Too many messages for this
  conversation (2/min)"}` (test override of 2/min).
- WebSocket: per-IP (20) / per-widget (500) concurrent-connection caps
  enforced at `/widget` connect via Redis sets
  (`ws_connect_allowed` / `ws_disconnect`); heartbeat `ping_interval=25s`,
  `ping_timeout=60s` in `app/core/socketio.py` is the idle timeout.
- AI quotas are checked **before** the turn (the model is the expensive
  part); at 80% of the widget quota a once-per-day `ai_spike` abuse event
  fires.

### 3. Abuse control

- **Blocked-visitor list** (`app/models/mobile.py::BlockedVisitor`,
  migration `mobile_sdk_002`): org-scoped rows by `customer_id` and/or
  `ip_address`. The mobile API refuses a blocked visitor with 403 on
  start **and** on later calls; the `/widget` socket refuses the same
  way at connect (`visitor_blocked`). Verified live on both paths.
  Managed by org admins via `/api/v1/admin/mobile/blocked-visitors`.
- **Spam signals / risk scoring** (`risk_score`): identical repeat +50,
  link flood +20/+40, overlong +10. Score ≥ 40 → slow-down (429) +
  `challenge` abuse event; score ≥ 70 → temp ban 15 min
  (`TEMP_BAN_SECONDS = 900`) + `temp_ban` event + 403.
- **CAPTCHA challenge — plumbing implemented, provider documented-only:**
  the challenge path exists (risk score ≥ 40 raises the `challenge`
  event and applies the slow-down), but verifying a Turnstile/hCaptcha
  token against a provider secret is **not wired** — no provider is
  configured in test mode. Do not claim CAPTCHA is enforced until a
  provider is wired (see Phase 2).
- **Device fingerprint** (`device_fingerprint`): SHA-256 of
  `device_id + IP`, truncated to 32 hex chars. It is stamped on abuse-log
  rows as `[fp=…]` (wired into `record_abuse_event` via the `device_id`
  parameter, threaded through `_guard_chat_allowed`, `_rate_limited`,
  `_enforce_rate_limit`, `_enforce_risk`, `_enforce_ai_quota`); the raw
  device id and IP never land in the detail. IP reputation beyond this
  (blocklists, ASN checks) is Phase 2.
- **Per-widget kill switch** (`MobileWidgetSecurity.chat_disabled`):
  when true, start and every later call return 403
  (`"Chat is disabled for this widget"`), and the socket refuses at
  connect. Managed via `/api/v1/admin/mobile/widgets/{id}/security`.
- **Admin abuse panel:** `GET /api/v1/admin/mobile/abuse-events`
  (dashboard auth) lists the append-only `mobile_abuse_events` log
  (`kill_switch`, `blocked_visitor`, `rate_limited`, `challenge`,
  `temp_ban`, `ai_quota`, `ai_spike`). It never raises into the request
  path.
- **Anomaly alerts:** the `ai_spike` event (80% of the daily widget AI
  quota) is the Phase 1 alert; there is no paging/escalation yet (Phase 2).

### 4. Media

- The existing magic-byte type checks from `file_upload_service` apply
  unchanged: images `jpeg/png/gif/webp` ≤ 5 MB; documents
  `pdf/doc/docx/txt/csv/xls/xlsx`; audio `webm/mp4/mpeg/m4a/ogg/wav` ≤
  10 MB. No executable uploads. A file that fails validation returns 422
  and stores no message (verified live with a 6 MB PNG).
- Per-conversation daily upload count/size limits (§2) sit on top.

### 5. What was reused vs built new

Reused: `conversation_token.py` + `verify_conversation_token` (token
mint/verify/revoke), `reply_limits.check`, `socket_rate_limit.py`
patterns, `file_upload_service` magic-byte + size validation,
Socket.IO heartbeat, `encrypt_api_key`/`decrypt_api_key` (FCM config
stored encrypted; GET returns masked status only).
Built new: `app/api/mobile_chat.py` (public REST + JWT issue/rotate),
`app/api/mobile_admin.py` (dashboard-only admin router),
`app/services/push_service.py`, `app/services/mobile_security.py`
(Redis windows, AI quotas, risk scoring, temp bans, kill switch,
fingerprint, abuse log), `app/models/mobile.py` tables
(`MobileDeviceToken`, `PushAttempt`, `MobileFcmConfig`,
`BlockedVisitor`, `MobileWidgetSecurity`, `MobileAbuseEvent`),
migrations `mobile_sdk_001` / `mobile_sdk_002`, and the blocked-visitor
enforcement in `widget_chat.py` (the original repo had no blocked list).

### 6. Tests (live + unit)

- Unit: `backend/tests/api/test_mobile_chat.py` +
  `test_mobile_admin.py` — **26 passed, 0 failed** (2026-11-23,
  `NO_ENTERPRISE=1 pytest --no-cov`). Covers JWT issue/rotate/revoke,
  device mismatch 401, 429 + `Retry-After`, identical-repeat challenge
  (incl. the `[fp=…]` stamp), blocked 403, kill-switch 403, oversize
  422, executable 422, device upsert, push recorded/skipped/dedupe.
- Live (curl, test widget `72e0025a-…`): start + JWT claims, rotate +
  old-token 401, wrong-device 401, text turn with a real AI reply,
  PDF turn returning `$249.50`, device registration, push attempt
  `recorded_no_credentials`, 429 + `retry-after: 56`, blocked 403 on
  start and send, oversize 422, `/widget` socket `session_initialized`
  with the mobile JWT. See `CHAT_API_REFERENCE.md` for the verbatim
  outputs.

---

## Device attestation — EXPLICITLY OUT OF SCOPE

**Play Integrity (Android) and App Attest (iOS) are NOT used, NOT
planned, and NOT a follow-up.** No attestation code or dependency exists
in the mobile API (verified by grep for `integrity|attest` over the
touched files — no matches). The chosen approach is the Phase 1 set
above: public widget key + short-lived signed visitor JWT (tied to
conversation + widget + device, rotated) + Redis rate limits per
visitor/IP/widget + AI quotas + device fingerprint + IP reputation (via
the fingerprint and the blocked list) + CAPTCHA challenge for suspicious
traffic + the blocked-visitor list. If attestation is ever reconsidered,
that is a new user decision, not a standing plan.

---

## Phase 2 — DOCUMENTED ONLY (do not implement yet)

- **CAPTCHA provider wiring:** verify Turnstile/hCaptcha tokens against
  the provider secret (server-side only); on success, clear the
  challenge state for that fingerprint; on failure, keep the slow-down.
- **Per-plan quotas:** Free / Starter / Pro / Business AI and message
  quotas, enforced per org plan (the per-widget overrides in
  `MobileWidgetSecurity` are the mechanism; the plan mapping is the new
  work). Pricing: Free Rs 0 / Starter Rs 5,500 / Pro Rs 14,000 /
  Business Rs 28,000 (from the paying-SaaS build).
- **WAF / CDN in front:** rate-limit and bot rules at the edge (e.g.
  Cloudflare) before traffic reaches the API; geo/IP blocklists feeding
  the blocked-visitor list.
- **Audit logs:** who changed FCM config / blocked list / kill switch /
  per-widget limits, and when (admin user + timestamp + diff).
- **Anomaly alerts:** page/escalate on `ai_spike`, sudden 429 bursts, or
  a spike in `temp_ban` events (webhook or email to org admins).

## Phase 3 — DOCUMENTED ONLY (do not implement yet)

- **Data retention + deletion:** retention windows for
  `mobile_push_attempts` and `mobile_abuse_events`; a visitor-data
  deletion path (delete a customer's conversations, devices, and abuse
  rows on request) with proof of deletion.
- **Key rotation:** scheduled rotation of the Fernet encryption key and
  of the conversation-token signing secret, with a re-encryption pass.
- **Abuse analytics:** per-widget abuse dashboards (429s, challenges,
  bans over time) and export for review.

---

## Operational notes

- **Encryption key:** the backend's `ENCRYPTION_KEY` (Fernet, double
  base64 in `.env`) must be a valid persistent key. On 2026-11-23 it was
  a 31-character invalid value, so the server generated a throwaway key
  per process and every AI key / encrypted chat row became unreadable
  after a restart (the `ValueError: Failed to decrypt API key` 500).
  It was replaced with a valid generated key; do not revert it. Any
  deployment without a valid persistent key will break AI replies the
  same way.
- **Test-only AI config:** the live verification used a test-only
  `ai_configs` row (hosted-provider placeholder, placeholder key
  `test-placeholder-key`, encrypted at rest). The temporary OpenAI key
  (`gpt-6-luna`) is **not** stored anywhere in the AI config (verified
  2026-11-23: exactly one `ai_configs` row, placeholder key only) — the
  user must still revoke it in the OpenAI dashboard. Add a real hosted
  provider key before any production use; the test row is for local
  verification only.
- **Timeouts:** `AGENT_RUN_TIMEOUT` defaults to 90s; a slow hosted model
  can need longer per turn, so a local test server may run with
  `AGENT_RUN_TIMEOUT=300`. That is a local test env var, not a code
  change; the default stays 90s.
