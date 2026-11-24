# ChatterMate Chat API — llms.txt

> Compact AI-readable integration doc for the public Chat API (raw REST). Full version: `docs/AI-INTEGRATION.md` in the repo. OpenAPI 3
> spec: `openapi.yaml` next to this file. Dashboard: Settings → API Docs.
> Verified 2026-11-23 against the live backend + backend test suite
> (test_mobile_chat 21 passed, test_mobile_admin 5 passed).

## ChatterMate Chat API

> Chat with a ChatterMate AI agent from a mobile app without a WebView.
> Base URL (dev): `http://localhost:8000`. All routes under `/api/v1`.
> Test widget id: `72e0025a-d062-40a2-a1d5-6598e84bf4a2` (agent "Media Test
> Support"). The widget id is PUBLIC. Secret API keys are server-side only,
> never in an app binary.

## Auth

- `POST /api/v1/mobile/conversations` with `{"widget_id": "<id>"}` (optional
  `visitor_name`, `visitor_email`, `device_id`) mints a visitor JWT (1 hour,
  `expires_in: 3600`). Store `conversation_id` + `conversation_token`.
- Send it as `X-Conversation-Token` on every call. If `device_id` was sent at
  start, send the same id as `X-Device-Id` every call (mismatch → 401).
- Rotate before expiry: `POST /api/v1/mobile/conversations/{id}/rotate` →
  fresh token, same conversation; old token revoked at once (401 after).
- Resume: `POST /conversations` with stored `conversation_token` →
  `created: false` + same token when live, else a new conversation.
- `conversation_id` is the widget `session_id` (UUID), shared with the web
  widget and the inbox. No dashboard login / API key / cookie on visitor
  endpoints.

## Endpoints

- `POST /api/v1/mobile/conversations` — start/resume.
- `POST /api/v1/mobile/conversations/{id}/rotate` — rotate token.
- `GET /api/v1/mobile/conversations/{id}/messages?after=&limit=` — history /
  polling. `after` = last message id; `limit` default 50, max 100. Poll at
  most once every 3 s while a reply is pending; stop when the socket
  reconnects. `sender`: `visitor`/`ai`/`agent` (`message_type`:
  `user`/`bot`/`agent`). Attachment `file_url`s are signed, time-limited.
- `POST /api/v1/mobile/conversations/{id}/messages` — send, multipart
  (`message` + repeatable `files`/`files[]`). Response is the stored visitor
  message + `ai_reply_pending` only; the AI reply arrives over the socket or
  via polling `GET .../messages?after=<id>`. `message` may be empty only with
  files. Allowed: `.csv .doc .docx .gif .jpeg .jpg .m4a .mp3 .mp4 .ogg .pdf
  .png .txt .wav .webm .webp .xls .xlsx`; images ≤ 5 MB, files ≤ 10 MB.
  A failed file → 422, no message stored. Do not retry on timeout (would
  double-send); poll instead. 500 after send = message stored, AI turn
  failed server-side — poll, do not resend.
- `POST /api/v1/mobile/conversations/{id}/devices` — register FCM token:
  `{"push_token": "…", "platform": "android"|"ios"}` → `{"ok": true,
  "device_id": "…", "platform": "android"}`. Upsert by (conversation,
  token). Push fires only when no live socket is connected; payload
  `notification: {title: <agent>, body: <first ~120 chars>}`, `data:
  {conversation_id, message_id, type: "chat_response"}`. On tap: resume
  (§Auth) then fetch messages — never trust the truncated body.
- `GET /api/v1/mobile/push-attempts?conversation_id=` — push decisions:
  `sent` / `failed` / `recorded_no_credentials` (test mode, no FCM creds) /
  `skipped_live_socket`. `device_token_prefix` = first 12 chars only.
- Admin only (dashboard auth, never from the app):
  `POST|GET /api/v1/admin/mobile/fcm-config` (store encrypted FCM
  service-account JSON; GET returns masked `{configured, project_id,
  updated_at}` only), `GET|POST|DELETE
  /api/v1/admin/mobile/blocked-visitors[/…]`, `GET|PUT
  /api/v1/admin/mobile/widgets/{id}/security` (limits + kill switch),
  `GET /api/v1/admin/mobile/abuse-events`.

## Live replies (Socket.IO)

- Same host, default path, namespace `/widget`, auth
  `{"conversation_token": "<token>"}`. Receive-only: never send chat over
  the socket. Events: `session_initialized`, `bot_typing`,
  `chat_response` (`type: "chat_response"` = AI reply; `type:
  "agent_message"` = human reply), `error` (`token_invalid` → re-auth;
  `conversation_gone` → new conversation). Dedupe by `message_id`.
  `quick_replies` = tappable chips (send text via REST).
  `transfer_to_human: true` → next reply is human. `end_chat: true` →
  conversation closed, offer a new chat.

## Errors

- `401` invalid/expired token or wrong device → resume/rotate. `403`
  blocked / temp ban 15 min / kill switch. `404` unknown widget or
  conversation not yours (cross-customer ids are 404 by design). `409`
  waiting on a host-action step. `410` closed → start new. `422` bad body /
  file / empty message / no AI config. `429` + `Retry-After` header → back
  off. `500` after send → poll, do not resend.

## Rate limits & security (defaults)

- Messages 30/min per conversation, 120/min per IP, 600/min per widget;
  AI 200/day per conversation, 2000/day per widget; uploads 20 files /
  50 MB per day per conversation. Spam: identical repeat +50, 3–5 links
  +20, 6+ links +40, >4000 chars +10; ≥ 40 → 429 slow-down, ≥ 70 → 403
  temp ban 15 min. Redis down fails open. Widget id public; visitor JWT
  1 h, tied to conversation + widget + device. No Play Integrity / App
  Attest (explicit decision 2026-11-23): JWT + rate limits + device
  binding + challenge slow-down instead. Do not call widget/inbox
  dashboard APIs from the app; do not store FCM service-account JSON or
  AI keys in the app.

## Contract corrections (code beats `API_CONTRACT.md` v1)

- Token lifetime 1 hour, not 30 days; `rotate` exists. FCM config is at
  `/api/v1/admin/mobile/fcm-config`, not `/api/v1/mobile/fcm-config`.
  `device_id`/`X-Device-Id` binding exists. Upload list includes
  `.mp3`/`.jpg`. Start returns `expires_in`. Admin surface also covers
  blocked visitors, per-widget security and abuse events.
