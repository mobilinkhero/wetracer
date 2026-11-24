# ChatterMate Streaming Protocol (v1)

Live token-by-token AI replies + KB instant answers. Implemented 2026-11-25
(test mode). The non-streaming paths are unchanged and remain the fallback
everywhere.

## Provider: hosted only, configured by the owner

The AI comes only from the hosted provider the owner configures in the
dashboard. There is no local or self-hosted model anywhere in this work:
nothing downloads, serves, warms or keeps alive a model on this machine,
and no model name, URL or keep-alive setting for a local model appears in
the code or this doc.

How the provider is resolved: the dashboard stores the owner's AI config
(`ai_configs` — provider/model, base URL where applicable, encrypted key);
`ChatAgent` receives `model_type`, `model_name`, `base_url` and the decrypted
key and builds the model with `app/utils/agno_utils.create_model(...)`
(the single choke point for every provider: OpenAI, OpenAI-compatible
gateways, Groq, Anthropic, DeepSeek, Google, Mistral, xAI, …). The streaming
path uses that same `create_model` result — no provider-specific code, no
key handling here. The owner adds their own key in the dashboard; this
implementation never adds, asks for, or stores any key.

Testing without a live key: verification runs against a stub that mimics
the hosted provider's OpenAI-compatible chat-completions streaming API
(`backend/tests/stub_provider.py`, tests in
`backend/tests/agent/test_lean_stream_stub.py`). No real provider is called
and nothing is charged.

## Why streaming is a separate path

agno 1.7.6 `Agent.arun(stream=True)` **does** stream content tokens for a
plain agent — but **not** when the agent uses `response_model` +
`structured_outputs` (the structured turn arrives as one chunk at the end;
verified against the stub hosted-provider server). The main `ChatAgent`
needs structured output for transfer / ticket / lead-capture / end-chat
flags, so streaming runs on a **lean plain-text agent** (no `response_model`,
no tools, short system prompt, compact history) for simple turns only.
Anything that needs structured actions uses the existing non-streaming turn
and the client sees a normal complete reply instead.

## Transports

### 1. Widget — Socket.IO namespace `/widget` (existing connection)

New events, all emitted to the conversation room (`session_id`):

| Event | When | Payload |
|---|---|---|
| `chat_response_stream_start` | First token is about to be sent (or a KB instant answer is ready). If the client never sees this event for a turn, the turn fell back to non-streaming — just wait for `chat_response`. | `{"session_id": str, "source": "model-stream" \| "kb", "message_id": int \| null}` (`message_id` is set only for `kb`; for `model-stream` it arrives with the final `chat_response`) |
| `chat_response_stream_token` | Each model token chunk (or the whole KB answer as one chunk). Append `delta` to the in-progress bubble. | `{"session_id": str, "delta": str, "source": "model-stream" \| "kb"}` |
| `chat_response_stream_done` | Stream finished; the full reply is stored. The existing `chat_response` event follows immediately with the final payload (message id, quick replies, handover, sources, flags) — clients should keep the streamed text and let `chat_response` replace/confirm it, not render a second bubble. | `{"session_id": str, "source": "model-stream" \| "kb", "message_id": int \| null}` |
| `chat_response_stream_error` | Streaming failed mid-turn after `start` was sent. The server finishes and stores the partial text and the normal `chat_response` still arrives with it — the client should keep the partial text and wait for `chat_response`. If `chat_response` never arrives, show the generic retry error as before. | `{"session_id": str, "error": str}` |

Existing events are unchanged: `bot_typing` still fires first, `chat_response`
still carries the final reply, `error` still covers turn failures.

### 2. Mobile — SSE endpoint (new)

```
POST /api/v1/mobile/conversations/{conversation_id}/messages/stream
Headers: X-Conversation-Token: <visitor JWT>, X-Device-Id: <device id>
Body: multipart form, same as POST .../messages (field `message`; NO files —
      uploads get 422 "use the non-streaming endpoint for files")
Query: ?force_model=true skips the KB instant answer (testing only)
Response: 200 text/event-stream
```

SSE lines (`event:` + `data:` JSON, blank line terminated):

| SSE event | `data` JSON |
|---|---|
| `start` | `{"session_id": str, "source": "model-stream" \| "kb" \| "model-full", "message_id": int \| null}` |
| `token` | `{"session_id": str, "delta": str, "source": "model-stream" \| "kb"}` |
| `done` | Full reply payload, same shape as `reply_payload` in `app/services/widget_reply.py`: `{"message_id", "message", "type": "chat_response", "session_id", "transfer_to_human", "end_chat", "end_chat_reason", "end_chat_description", "request_rating", "shopify_output", "sources", "quick_replies", "handover", ...}` plus `"source": "model-stream" \| "kb" \| "model-full"` |
| `error` | `{"session_id": str, "error": str}` — sent when the turn fails. If a finished reply exists (mid-stream failure after partial text was completed and stored), `done` follows `error` with that reply; if no reply exists, `error` is terminal and no `done` follows. The HTTP status stays 200 because headers were already sent. |

Non-SSE errors (before streaming starts) use normal HTTP codes, same as
`POST .../messages`: 401 bad token, 404 unknown conversation, 403 blocked /
chat disabled, 409 waiting on a host action, 422 no message / files attached,
429 rate / quota limited.

Fallbacks on the SSE endpoint:

- Turn needs the full structured agent (transfer/ticket/lead/shopify/host
  action, attachments, workflow, human takeover): the endpoint answers with a
  single `start` (`source: "model-full"`), no `token` events, then `done`
  with the complete reply. A client that only handles `done` still works.
- Human-taken-over / transferred / workflow conversations: `409` with
  `detail` telling the client to use the non-streaming `POST .../messages`
  endpoint (those branches have no streamed reply).

The existing `POST .../messages` (JSON, whole reply) and
`GET .../messages` (polling) are unchanged.

## KB instant answers (`source: "kb"`)

Before any model call, the server searches the agent's knowledge base for the
visitor's question. On a high-confidence match it answers **from the article
itself, with no model call**:

- Delivered through the same streaming channel as a complete message:
  `start` (`source: "kb"`) → one `token` with the full answer text →
  `done` (payload includes `"sources": [{"name": <article title>, "type": <source type>}]`).
  The client renders it like any streamed reply; it may show a "from help
  article" affordance when `source == "kb"`.
- Confidence gate (see `app/services/kb_instant.py`): the vector search must
  return a chunk **and** confidence must be at least 0.5 — the share of the
  question's distinctive words found in the article title/text (a title word
  counts extra, but only when at least 2 words match overall, so a single
  shared word can never pass the gate on its own). This stops the vector
  search's always-nearest hit from answering unrelated questions.
  Low-confidence questions go to the model, which may still search the KB
  with its own tool. A near miss (confidence ≥ 0.25 with at least 2 matched
  keywords) is pasted as context for the lean model instead of being
  answered verbatim.
- KB answers never set `transfer_to_human`, `end_chat`, `request_rating`,
  `create_ticket` or lead-capture flags.
- **The knowledge base contains ONLY articles the owner adds** (plus the
  seeded test samples below). There is no self-learning from conversations:
  nothing mines past chats, messages or transcripts into KB entries, and no
  "learn from chats" feature exists or is planned in this work.

Seeded test articles only (for "Media Test Support"; real articles must come
from the owner): "How to reset your password", "Support hours",
"How to cancel your subscription" — see `backend/scripts/seed_test_kb.py`.

## Lean streaming turns (`source: "model-stream"`)

Eligibility — all must hold, otherwise the turn silently uses the full
non-streaming agent (`source: "model-full"` on SSE; no stream events on the
widget, just `chat_response`):

- No attachments/images, no workflow, no host actions offered.
- No Jira / Shopify / extension toolkits on the agent, no lead capture
  pending, no transfer/closing intent in the message (e.g. "speak to a human",
  "goodbye"). Merely having the native ticket toolkit attached does not
  disqualify a turn — only a message that actually asks for a ticket
  ("open a ticket", "report a problem", …) is routed to the full agent.
- Session OPEN, AI replies enabled for the agent and the plan.

Lean turn construction (speed trims, answer quality kept):

- Short system prompt (~6 lines: identity, concise 2–4 sentence answers,
  answer only from the given context, never invent URLs/prices/dates/details).
  The full agent prompt (operator instructions, guardrail policy, lead,
  ticket, transfer, end-chat, choice rules) is **not** sent.
- No tools attached — the KB is pre-searched and, when useful, pasted in as
  short context text instead of a tool call round-trip.
- History is compact: last 3 exchanges, truncated (~1,200 chars), inlined in
  the system prompt — no agno storage/memory round-trip on this path.
- `max_tokens`: unchanged from the agent config path.
- After the stream completes, the full text goes through the normal finish:
  output guardrail, citation attach, bot-message store, then the normal
  `chat_response` / push / lead / end-chat handling. A streamed reply is
  therefore stored exactly like a non-streamed one. If the provider stream
  breaks mid-turn, the partial text is finished and stored the same way and
  the client gets the finished reply via `chat_response` / `done`.

What a lean turn **cannot** do (by design): set `end_chat`, `transfer_to_human`,
`create_ticket`, `request_rating`, lead-capture fields, or `quick_replies`
choices. If the visitor's message needs any of those, the eligibility gate
sends it to the full agent instead.

## Client rules

1. Show the typing indicator on `bot_typing` (widget) / on request start
   (SSE) until `start` or `done` arrives.
2. On `start`, open an in-progress bubble. On each `token`, append `delta`.
   On `done`, replace the bubble's content with `done.message` (identical
   text, plus the final `message_id` and flags) — never append a second bubble.
3. If a turn produces `done` with no preceding `start`/`token`, render it as
   a normal complete reply (fallback path).
4. If `error` arrives after partial tokens, keep the partial text and wait
   for `chat_response` (widget) / `done` (SSE, when one follows) — the server
   retries non-streaming / finishes the partial reply. On SSE, an `error`
   with no following `done` is terminal for that request; the client may
   retry the POST.
5. Never render `thinking`/reasoning content: the stream carries only the
   visitor-facing reply text.
6. `source == "kb"` replies may be labelled as coming from a help article;
   do not present them as live-agent/model answers.

## Verification (stub hosted provider — no live model, no key, no charges)

- Lean streaming through the real code path
  (`ChatAgent._lean_stream_tokens` → `create_model("OPENAI_COMPATIBLE")` →
  agno `arun(stream=True)`) against the stub OpenAI-compatible SSE server
  (`backend/tests/stub_provider.py`, `tests/agent/test_lean_stream_stub.py`):
  exact scripted text arrived token by token, the stub observed one request
  with `stream: true` and the configured model id, **TTFT 0.31 s, total
  0.46 s** (stub replay delay 50 ms/chunk; a hosted provider's own latency
  adds on top — no live-model timing is claimed here, because no live key
  exists in test mode).
- KB instant answers verified live against the running backend over SSE and
  the widget Socket.IO connection: all three seeded questions answered from
  their articles with `source: "kb"` and TTFT ~0.3 s once the embedding
  model was loaded in the backend process (the first KB search after a
  restart takes ~5–16 s to load the embedding model on this box;
  subsequent searches ~0.3 s).
  A question sharing only one word with an article ("Can I cancel my flight
  booking tomorrow?") correctly does NOT get a KB answer.
- Unit tests (mocked at the seams, no model/DB/embedding):
  `tests/services/test_kb_instant.py`, `tests/agent/test_chat_agent_streaming.py`,
  `tests/agent/test_lean_stream_stub.py`, `tests/api/test_mobile_chat.py`,
  `tests/api/test_streaming.py` — 71 passed.

## Files

- Protocol: `docs/STREAMING-PROTOCOL.md` (this file)
- KB instant answers: `backend/app/services/kb_instant.py`
- Lean streaming + `ChatAgent.stream_response`: `backend/app/agents/chat_agent.py`
- Widget wiring: `backend/app/api/widget_chat.py` (`handle_widget_chat`)
- Mobile SSE: `backend/app/api/mobile_chat.py`
  (`POST /conversations/{id}/messages/stream`)
- Test KB seed: `backend/scripts/seed_test_kb.py`
- Stub hosted-provider server + tests: `backend/tests/stub_provider.py`,
  `backend/tests/agent/test_lean_stream_stub.py`
- Stack start (no local model started): `~/workspace/chattermate-pgdata/start-all.sh`
