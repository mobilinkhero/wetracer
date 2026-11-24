# ChatterMate Chat API — AI Integration Guide

> For an AI code builder or anyone integrating chat into a mobile app,
> over the raw REST API (any stack, no SDK, no WebView). Machine-readable
> spec: `frontend/src/assets/openapi.yaml` (OpenAPI 3.0.3, validated).
> Compact variant: `frontend/src/assets/ai-integration-compact.md`.
> Dashboard page:
> Settings → API Docs (shows your own widget ids, copyable).
>
> Verified 2026-11-23 against the live backend (dev, `http://localhost:8000`)
> with the test widget below and the backend test suite
> (`backend/tests/api/test_mobile_chat.py`: 21 passed,
> `backend/tests/api/test_mobile_admin.py`: 5 passed). Every endpoint,
> example and error string here matches the implementation
> (`backend/app/api/mobile_chat.py`, `backend/app/api/mobile_admin.py`).
> Where the earlier API contract (v1) disagrees with the code, the code
> wins — see "Contract corrections" at the end.

## 0. Facts you need first

- **Base URL (dev):** `http://localhost:8000`. Production: the same origin
  that serves the dashboard API. All routes live under `/api/v1`.
- **Test widget id:** `72e0025a-d062-40a2-a1d5-6598e84bf4a2`
  ("Media Test Widget", agent **Media Test Support**). The widget id is
  **public** — find your own in the dashboard under Settings → API Docs.
- **Auth:** the mobile client is a *widget visitor*, not a dashboard user.
  `POST /api/v1/mobile/conversations` mints a short-lived signed visitor
  JWT (1 hour, `expires_in: 3600`). Send it as `X-Conversation-Token` on
  every later call. No dashboard login, no API key, no session cookie is
  accepted on the visitor endpoints.
- **IDs:** `conversation_id` is the widget `session_id` (UUID) — the same id
  the web widget and the dashboard inbox use, so a chat started in the app
  continues in the inbox and vice versa. All timestamps are ISO-8601 UTC.
- **Live replies** come over Socket.IO, namespace `/widget`, auth
  `{"conversation_token": "<token>"}`. The socket is **receive-only** —
  chat is never sent over the socket, only over REST.

## 1. Auth flow

1. `POST /api/v1/mobile/conversations` with `{"widget_id": "<id>"}` (+
   optional `visitor_name`, `visitor_email`, `device_id`).
2. Store `conversation_id` + `conversation_token` securely on device.
   They survive app restarts.
3. Send the token as `X-Conversation-Token`. If you sent `device_id` at
   start, send the same id as `X-Device-Id` on every call — a token
   presented from another device gets `401 "Conversation token was issued
   to another device"`.
4. Before the hour is up, rotate: `POST
   /api/v1/mobile/conversations/{id}/rotate` → fresh token, same
   conversation. The old token is revoked immediately (401 after).
   A lapsed token cannot be rotated — start/resume via step 1 instead.
5. To resume later: `POST /conversations` with the stored
   `conversation_token`. A live token naming an active conversation for
   this widget returns it with `created: false` and the **same** token;
   otherwise a new conversation starts.

```bash
curl -X POST http://localhost:8000/api/v1/mobile/conversations \
  -H 'Content-Type: application/json' \
  -d '{"widget_id":"72e0025a-d062-40a2-a1d5-6598e84bf4a2","visitor_name":"Ahtisham","visitor_email":"visitor@example.com","device_id":"550e8400-e29b-41d4-a716-446655440000"}'
# {"conversation_id":"1bf5844c-…","conversation_token":"eyJhbG…","customer_id":"…",
#  "widget_id":"72e0025a-…","agent_name":"Media Test Support","created":true,"expires_in":3600}
```

## 2. Endpoints (raw API)

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/v1/mobile/conversations` | Start / resume. Returns token + `expires_in: 3600`. |
| `POST` | `/api/v1/mobile/conversations/{id}/rotate` | Revoke + re-mint the token for the same conversation. |
| `GET` | `/api/v1/mobile/conversations/{id}/messages?after=&limit=` | History / polling fallback. `after` = last message id seen; `limit` default 50, max 100. |
| `POST` | `/api/v1/mobile/conversations/{id}/messages` | Send text and/or files (`multipart/form-data`). |
| `POST` | `/api/v1/mobile/conversations/{id}/devices` | Register an FCM token for push. |
| `GET` | `/api/v1/mobile/push-attempts?conversation_id=` | Push decisions (debug / test mode). |
| `POST`/`GET` | `/api/v1/admin/mobile/fcm-config` | **Admin only** (dashboard auth): store / check FCM service-account JSON. Never from the app. |
| `GET`/`POST`/`DELETE` | `/api/v1/admin/mobile/blocked-visitors[/…]` | **Admin only**: blocked-visitor list. |
| `GET`/`PUT` | `/api/v1/admin/mobile/widgets/{id}/security` | **Admin only**: per-widget limits + kill switch. |
| `GET` | `/api/v1/admin/mobile/abuse-events` | **Admin only**: abuse-event log. |

### 2.1 Read messages

```bash
TOKEN="<token from §1>"; CID="<conversation_id>"
curl "http://localhost:8000/api/v1/mobile/conversations/$CID/messages?after=0&limit=50" \
  -H "X-Conversation-Token: $TOKEN"
# {"conversation_id":"…","messages":[
#   {"id":20,"sender":"visitor","text":"Hello, what is my order total?","message_type":"user",
#    "attachments":[],"created_at":"2026-11-23T00:05:00+00:00","user_name":null,
#    "agent_name":"Media Test Support"},
#   {"id":21,"sender":"ai","text":"Your order ORD-78412 totals $129.99.","message_type":"bot",
#    "attachments":[],"created_at":"2026-11-23T00:05:04+00:00","user_name":null,
#    "agent_name":"Media Test Support"}]}
```

`sender`: `visitor` (`message_type: "user"`), `ai` (`"bot"`), `agent`
(`"agent"` — a human in the inbox, with `user_name` set). Attachment
`file_url`s are signed and time-limited: fetch promptly, do not cache as
permanent. Poll at most once every 3 s while a reply is pending, and stop
polling once the `/widget` socket reconnects. Dedupe socket vs polling by
message `id`.

### 2.2 Send a message (+ files)

```bash
# Text only
curl -X POST "http://localhost:8000/api/v1/mobile/conversations/$CID/messages" \
  -H "X-Conversation-Token: $TOKEN" -F "message=Hello, what is my order total?"
# {"message":{"id":20,"sender":"visitor","text":"Hello, what is my order total?",
#   "message_type":"user","attachments":[],"created_at":"…","user_name":null,
#   "agent_name":"Media Test Support"},"ai_reply_pending":true}

# Text + image (repeat -F "files=@…" for more files; "files[]" also accepted)
curl -X POST "http://localhost:8000/api/v1/mobile/conversations/$CID/messages" \
  -H "X-Conversation-Token: $TOKEN" \
  -F "message=What is the total on this order?" \
  -F "files=@order-image.png;type=image/png"
# Same shape; the stored message carries
# "attachments":[{"filename":"order-image.png","file_url":"…","content_type":"image/png","file_size":23117}]
```

Rules, all enforced server-side:

- `message` may be empty **only** when `files` is present; otherwise 422.
- Allowed extensions (the live 422 lists them): `.csv .doc .docx .gif
  .jpeg .jpg .m4a .mp3 .mp4 .ogg .pdf .png .txt .wav .webm .webp .xls
  .xlsx`. Images ≤ 5 MB, other files ≤ 10 MB, magic bytes checked. A file
  that fails validation returns 422 and **no** message is stored.
- The response holds the stored visitor message only — the AI reply is
  **not** in it. It arrives over the socket (`chat_response`) or via
  polling `GET .../messages?after=<your message id>`.
- `ai_reply_pending: true` = a reply is being produced/awaited. `false` =
  a human has taken over (or the agent is human-only): no AI reply is
  coming; the reply arrives as a human message instead.
- The turn is the web widget's turn: images are seen by the model,
  PDF/text/DOCX/XLSX text is extracted (capped at 12,000 chars), voice
  notes are transcribed with Whisper.
- Do **not** retry on timeout — retrying sends the message twice. On
  timeout, poll `GET .../messages` to see whether the visitor message
  was stored.
- If the server fails the AI turn after storing the message, the send
  returns 500 and the message stays stored — poll for it, do not resend.
  (Observed live 2026-11-23: the seeded test AI config's stored key
  failed to decrypt, so the turn 500'd while the message stored fine.
  The endpoint logic itself is covered by the backend tests with a
  mocked model.)

### 2.3 Live replies (Socket.IO `/widget`)

Same host, default Socket.IO path (`/socket.io/`), namespace `/widget`,
connect auth `{"conversation_token": "<token>"}`. The server joins the
socket to the conversation room and emits `session_initialized`
(`session_id` == `conversation_id`).

| Event | Meaning |
|---|---|
| `session_initialized` | Connected. `{session_id, customer_id, agent_id, authenticated, created}` |
| `bot_typing` | Show the typing indicator until the reply arrives. |
| `chat_response` (`type: "chat_response"`) | **The AI reply.** `message` is the text; `message_id` is the stored row id. Also `transfer_to_human`, `end_chat`, `request_rating`, `quick_replies`, `sources`, `shopify_output`. |
| `chat_response` (`type: "agent_message"`) | **A human agent's reply.** `message_type: "agent"`, `attachments` when present. Same event name — `type` distinguishes it. |
| `error` | `token_invalid` → re-auth via §1; `conversation_gone` → start a new conversation. |

Client → server: only `get_chat_history` (ask the server to re-send
history). Never send chat over the socket. `quick_replies` render as
tappable chips; tapping one sends its text via §2.2.
`transfer_to_human: true` means the next reply is human. `end_chat:
true` means the conversation was closed — offer "Start a new chat".

### 2.4 Register a device for push

```bash
curl -X POST "http://localhost:8000/api/v1/mobile/conversations/$CID/devices" \
  -H "X-Conversation-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"push_token":"<FCM registration token>","platform":"android"}'
# {"ok":true,"device_id":"af5b4fd1-…","platform":"android"}
```

`platform` is `android` or `ios` (anything else: 422). Upsert by
(`conversation_id`, `push_token`) — re-registering refreshes, never
duplicates. When a reply is delivered to a conversation with registered
devices **and no live `/widget` socket is connected**, the backend sends
one FCM push per device; when a socket is connected, no push is sent (no
duplicates). Push payload: `notification: {title: "<agent display
name>", body: "<first ~120 chars of the reply>"}`, `data:
{conversation_id, message_id, type: "chat_response"}`. On notification
tap: resume via §1 with the stored token, then §2.1 to load messages —
never trust the truncated payload body.

### 2.5 Push attempts (debug / test mode)

```bash
curl "http://localhost:8000/api/v1/mobile/push-attempts?conversation_id=$CID" \
  -H "X-Conversation-Token: $TOKEN"
# {"conversation_id":"…","attempts":[
#   {"id":1,"conversation_id":"…","device_token_prefix":"fcm-test-tok",
#    "title":"Media Test Support","body":"Your order ORD-78412 …",
#    "status":"recorded_no_credentials",
#    "reason":"No FCM credentials configured — test mode, push recorded only",
#    "created_at":"2026-11-23T00:06:00+00:00"}]}
```

`status`: `sent` (FCM accepted), `failed` (FCM rejected — `reason` has
the error), `recorded_no_credentials` (test mode: no FCM credentials
configured, so the would-be push is recorded instead of sent),
`skipped_live_socket` (a socket was connected, so no push was needed).
`device_token_prefix` is the first 12 characters — the full token is
never exposed. Test mode (no FCM credentials) is the current state.

### 2.6 FCM credentials (dashboard admin only — NOT the app)

```
POST /api/v1/admin/mobile/fcm-config   (dashboard auth, org-scoped)
{"service_account_json": {"type": "service_account", "project_id": "…", "private_key": "…", …}}
→ {"configured": true, "project_id": "…"}

GET /api/v1/admin/mobile/fcm-config    (dashboard auth, org-scoped)
→ {"configured": true, "project_id": "…", "updated_at": "…"}   (masked status only)
```

The JSON is stored encrypted and is **never** returned by any GET. A mobile app never handles FCM service-account keys. No real FCM
service-account JSON has been provided yet (test mode until then).

## 3. Errors

| Status | Meaning | What to do |
|---|---|---|
| `400/422` | Bad body / bad file / empty message / AI config missing | Fix the request. `422 "AI configuration required"` = the org has no active AI config; nothing the app can do. |
| `401` | Missing, invalid or expired token; token from another device | Resume via §1 (or rotate before expiry). |
| `403` | Blocked visitor, temp ban (15 min), or widget kill switch | Show "chat unavailable". The detail never says which list the visitor is on. |
| `404` | Unknown widget; conversation not found **or not yours** (cross-customer ids are 404, not 403, by design) | Start a new conversation. |
| `409` | Waiting for the app to finish the last host-action step | Wait; do not resend. |
| `410` | Conversation is closed | Start a new conversation (§1). |
| `429` | Rate limit / AI quota / spam slow-down. Always carries `Retry-After` (seconds) | Back off for `Retry-After` seconds. Never retry in a tight loop. |
| `500` | Server failed the AI turn (message already stored) | Poll `GET .../messages`; do not resend. |

## 4. Rate limits & security (defaults; per-widget overridable by an admin)

- Messages: **30/min per conversation, 120/min per IP, 600/min per
  widget** (sliding window). AI replies: **200/day per conversation,
  2000/day per widget**. Uploads: **20 files / 50 MB per day per
  conversation**. AI/upload counters reset at UTC midnight.
- Spam scoring on each message: identical repeat of the previous message
  +50; 3–5 links +20; 6+ links +40; message over 4000 chars +10. Score
  ≥ 40 → slowed down (429); ≥ 70 → temp-banned 15 minutes (403).
- Redis being down **fails open** — a broken counter never stops the
  chat. The DB-backed checks (blocked list, kill switch) fail closed.
- **Widget id is public; secret API keys are server-side only, never in
  an app binary.** The visitor JWT is short-lived (1 h), tied to the
  conversation + widget + device, and revocable (rotate / logout).
- **Blocked-visitor list + kill switch:** an admin blocks a customer/IP
  or disables chat for a widget; both refuse with 403.
- **Device attestation is explicitly NOT used:** no Play Integrity
  (Android), no App Attest (iOS) — by decision 2026-11-23. The chosen
  approach is visitor JWT + rate limits + device binding + CAPTCHA
  challenge for suspicious traffic (challenge path is the slow-down
  above until a provider is wired in).
- **Do not** call the web widget's `GET /api/v1/widgets/{id}` or the
  dashboard inbox APIs from the app — §2 is the whole mobile surface.
  Do not send chat over Socket.IO. Do not store FCM service-account
  JSON or any AI key in the app. Do not assume a push means a new
  message — always re-fetch on tap.
- Full security design (when published): `docs/API-SECURITY.md`.

## 5. Troubleshooting

- **401 on a call that worked:** token expired (1 h) — resume via §1
  with the stored token, or rotate before expiry. After `rotate`, the
  old token is dead: use the new one.
- **401 "issued to another device":** the token was bound to a
  `device_id` and this call sent a different `X-Device-Id` (or none
  after one was bound). Send the original device id, or start a new
  conversation.
- **404 on a conversation id:** the id is not this token's conversation
  (or it never existed). Start a new conversation.
- **422 "AI configuration required":** the widget's org has no active AI
  config. Nothing the app can do — an admin must configure AI.
- **429:** read the `Retry-After` header and wait. Identical repeats and
  link floods trigger the slow-down; stop resending the same text.
- **Send returns 500:** the visitor message was stored; the AI turn
  failed server-side. Poll `GET .../messages` — do not resend.
- **No push arrives:** check `GET /push-attempts` — `skipped_live_socket`
  means the socket was connected (reply arrived live instead);
  `recorded_no_credentials` means no FCM credentials are configured
  (test mode). Push only fires when no live socket is connected.
- **Web host (not mobile):** the host origin must be in the backend's
  `CORS_ORIGINS` (the backend allows `localhost:3000`/`localhost:8000`
  by default). Mobile apps are unaffected.

## 6. Contract corrections (code vs the earlier API contract v1)

1. **Token lifetime is 1 hour, not 30 days.** `expires_in: 3600` in the
   start response; `rotate` exists for renewal.
2. **`POST /conversations/{id}/rotate` exists** and is the renewal path.
   The contract does not mention it.
3. **FCM config is at `/api/v1/admin/mobile/fcm-config`**
   (dashboard auth), not `/api/v1/mobile/fcm-config` as the contract
   said. The contract's path 404s.
4. **`device_id` / `X-Device-Id` binding exists** in the implementation
   (token claim + 401 on mismatch). The contract does not mention it.
   Clients that do not send a `device_id` simply skip device binding.
5. **Allowed upload extensions** are the live list in §2.2 (includes
   `.mp3`/`.jpg`, which the contract omitted).
6. **`POST /conversations` returns `expires_in`** (3600), which the
   contract's example omitted.
7. **Admin surface is wider than the contract's §7:** blocked visitors,
   per-widget security (kill switch + limits) and the abuse-event log
   all live under `/api/v1/admin/mobile/*` (see §2 table and the OpenAPI
   spec).
