# ChatterMate Real-Time Replies — Setup Guide

Fast, real-time AI replies in the chat widget and the mobile API: answers
stream token by token so the visitor sees the first words in about a second.
Every turn is answered by the model, which searches your help articles and
answers from them in its own words — the server never sends article text
back as the reply.

This is the operator's setup guide: how to connect your AI provider, how to
add the help articles that ground the model's answers, what timings to expect,
and what to do when something is slow or silent. The wire-level protocol
(Socket.IO events, the mobile SSE endpoint, the lean
agent) is documented in `docs/STREAMING-PROTOCOL.md`.

## 1. Connect your hosted AI provider

The AI answers come only from the hosted provider you configure in the
dashboard. Until you add a provider key, the AI has no model and cannot
reply — set this up first.

1. In the dashboard, go to **Settings → AI Configuration**
   (route `/settings/ai-config`).
2. Pick a **Provider**. The dashboard offers OpenAI, Anthropic, DeepSeek,
   Google, Google Vertex, Groq, Mistral, Hugging Face, xAI, and
   OpenAI-compatible providers. (The ChatterMate provider entry is for
   enterprise customers.)
3. Pick a **Model** from the provider's suggested list, or choose the custom
   model ID option and type the model ID yourself.
4. If the provider needs it, fill in the **Base URL** (shown only for
   providers that require one).
5. Paste your **API key** for that provider. The key is stored encrypted
   in the organization's AI config and is never shown back in the UI.
6. Save. The setup is per organization and applies to every agent in it.

You can check or change the saved config later at the same page, or via
the API:

| Action | Endpoint |
|---|---|
| List providers and their suggested models | `GET /api/v1/ai/providers` |
| Create the AI config | `POST /api/v1/ai/setup` |
| Read the current config | `GET /api/v1/ai/config` |
| Update the config (provider, model, key) | `PUT /api/v1/ai/config` |

Notes:

- Use a key from the provider's own dashboard, with enough quota for your
  chat volume. If the key is invalid, expired, or out of quota, visitors
  get the generic error reply — see Troubleshooting below.
- Changing the provider or model takes effect for new turns immediately;
  an in-flight reply finishes with the config it started with.
- Keep the key private. It lives only in the server-side AI config
  (`ai_configs` table, encrypted). Never paste it into a chat message, a
  help article, or the widget embed code.

## 2. How streaming replies work

When a visitor sends a message, the server answers in this order:

1. **Streamed model reply.** A lean plain-text agent (short
   system prompt, no tools, last 3 exchanges of history) streams the
   reply token by token from your configured provider. The visitor sees
   the first words in about a second and the rest as it is generated.
   Before the model runs, the server searches the agent's knowledge
   base for the question and, when a matching article is found, pastes
   it into the model prompt as background context — the model answers
   in its own words, it never repeats article text verbatim. See section 3.
2. **Full agent reply (fallback).** Some turns need the full agent —
   attachments, a human handover, opening a ticket, lead capture, a
   workflow, or a shop/host action. Those run the normal non-streaming
   agent, which searches the knowledge base with its own tool, and the
   client receives one complete reply instead of a stream.
   This is by design, not an error.

Where the stream arrives:

- **Website widget** — the existing Socket.IO connection, namespace
  `/widget`. The server emits `chat_response_stream_start`, then one
  `chat_response_stream_token` per chunk, then `chat_response_stream_done`,
  and the normal `chat_response` event still follows with the final
  stored reply (message id, sources, flags). If a turn falls back to the
  full agent, no stream events are sent — the client just waits for
  `chat_response` as before.
- **Mobile / custom clients** — the SSE endpoint
  `POST /api/v1/mobile/conversations/{conversation_id}/messages/stream`
  (headers `X-Conversation-Token`, `X-Device-Id`; same multipart body as
  `POST .../messages`, but no file uploads). It sends `start`, `token`,
  and `done` events; a turn that needs the full agent answers with a
  single `start` (`source: "model-full"`) and then `done` with the
  complete reply, so a client that only handles `done` still works.

A streamed reply is stored exactly like a non-streamed one: it goes
through the same output guardrail and citation step, is saved as a bot
message, and then triggers the normal follow-up handling (push, lead,
end-chat). If the provider stream breaks mid-turn, the partial text is
finished and stored the same way and the client still gets the finished
reply.

## 3. Knowledge base — owner-added articles only

The model's answers are grounded only in help articles **you add** to the
knowledge base. There is no automatic learning from past chats: nothing
mines old conversations, messages, or transcripts into knowledge-base
entries, and no "learn from chats" feature exists. If an article is not
in the knowledge base, it can never ground an answer.

### Add articles in the dashboard

1. Open the **Knowledge base** page (route `/knowledge`).
2. Add a source with the add-source dialog. It supports:
   - **Website** — crawl a single page or a whole host.
   - **Sitemap** — crawl every URL in a sitemap.
   - **Document** — upload a PDF file (up to 25 MB).
   - **Text** — paste the content directly with a title.
3. **Link the source to the agent.** The model searches only the
   sources linked to that agent: in the knowledge base, attach the
   source to the agent (the link dialog says "Attach knowledge from your
   organization to this agent so it can use it"). An article that is
   added but never linked to the agent will never be used to answer.
4. Wait for processing to finish. Pasted text is ready almost at once;
   crawled pages and PDFs are queued and processed in the background —
   check the queue status on the knowledge-base page before testing.

The same operations exist as API calls (`/api/v1/knowledge/...`):

| Action | Endpoint |
|---|---|
| Add pasted text | `POST /api/v1/knowledge/add/text` |
| Add website / sitemap URLs to the crawl queue | `POST /api/v1/knowledge/add/urls` |
| Upload PDF files | `POST /api/v1/knowledge/upload/pdf` |
| Link a source to an agent | `POST /api/v1/knowledge/link` |
| List an agent's linked sources | `GET /api/v1/knowledge/agent/{agent_id}` |
| Check a queue item's status | `GET /api/v1/knowledge/queue/{queue_id}` |

### How matching works

Before the model runs, the server searches the agent's linked sources for
the visitor's question. When a match is found, the article is pasted into
the model prompt as background context — the model answers in its own
words, it never repeats article text verbatim. A match needs at least
2 shared distinctive words (confidence at least 0.25); a single shared
word is never enough to ground an answer. The full agent also searches
the knowledge base with its own tool.

Because the gate needs at least 2 shared words, an unrelated question
never gets grounded in the wrong article: for example, "Can I cancel my
flight booking tomorrow?" does not match the "How to cancel your
subscription" article, even though they share the word "cancel".

### Writing articles that match well

- Give each article a clear, question-like title ("How to reset your
  password", "Support hours", "How to cancel your subscription").
- Put the answer in the first paragraph, in plain words, with the exact
  facts (links, times, prices, deadlines). The model answers from the
  article in its own words.
- One topic per article. A long page covering five topics matches worse
  than five focused articles.
- Keep facts current. The model repeats the article's facts, so an
  out-of-date article gives an out-of-date answer. Edit the article in
  the knowledge base when the facts change.

### Test-only seeded articles

For testing only, `backend/scripts/seed_test_kb.py` adds three sample
articles ("How to reset your password", "Support hours", "How to cancel
your subscription") to the Media Test Support agent. Run it from
`backend/` with `./venv/bin/python scripts/seed_test_kb.py`. It skips
titles that already exist, so it is safe to re-run. These are sample
texts for verifying the model answers grounded in the KB — real articles
must come from the owner.

## 4. Expected timings

Measured in test mode against the stub hosted-provider server and the
live backend (see `docs/STREAMING-PROTOCOL.md` for the full verification
notes). A real provider adds its own latency on top of these numbers.

| Path | Time to first token / answer | Notes |
|---|---|---|
| Streamed model reply | ~0.3 s to first token against the stub provider (0.46 s total for a short scripted reply) | With a real provider, expect roughly 1–2 s to the first token. Total time depends on reply length and the provider. The first KB search after a backend restart takes ~5–16 s while the search index warms up; later searches are ~0.3 s. |
| Full agent reply (fallback) | Longer — no streaming | Runs the full agent with tools and structured output; first words arrive only when the whole reply is ready. |

Every question goes to the model, which answers from the knowledge base
with its own search tool. If the knowledge base is empty, there are no
articles to ground it, and the model may invent facts (prices, links,
hours). Add real articles before judging answer quality.

## 5. Troubleshooting

**Visitors get no AI reply at all, or the generic "I encountered an
error" reply.**

- Check Settings → AI Configuration: is a provider, model, and key
  saved? Without a configured provider the AI cannot reply.
- Check the key itself in the provider's dashboard: is it still valid,
  not revoked, and does the account have quota/credits left? A revoked
  or exhausted key produces the generic error reply for every turn.
- Check the backend log for the provider's error (401 = bad key,
  429 = rate/quota limit, 5xx = provider outage) and fix that side.
  The key is never the visitor's problem to solve.

**Replies arrive, but never stream — the visitor waits, then gets the
whole reply at once.**

- This is the fallback path, and it is usually correct behavior: the
  turn needed the full agent (attachment, handover, ticket, lead
  capture, workflow, or shop/host action). Send a plain question with
  no attachment and no handover/ticket wording and check again.
- On the widget, a turn that streams always emits
  `chat_response_stream_start` first. If the client never sees that
  event for a turn, the turn fell back — the client should just wait
  for `chat_response`, which still arrives.

**The model answers without using the articles, or invents facts.**

- Is the article linked to the agent? The model searches only the
  agent's linked sources. Open the Knowledge base page and confirm the
  source is attached to that agent.
- Has the source finished processing? Crawled pages and PDFs sit in
  the queue first — check the queue status (`GET
  /api/v1/knowledge/queue/{queue_id}`) or the knowledge-base page.
- Is the knowledge base empty for this agent? Then nothing can ground
  the answer — add and link at least one article first.

**The first KB search after a restart is slow (~5–16 s), then fast.**

- Expected. The first search warms up the search index; later searches
  are ~0.3 s. If every KB search stays slow, check backend CPU/memory
  and the backend log for embedding/search errors.

**Mobile SSE stream returns an error status instead of a stream.**

- `401` — bad or expired visitor token; create a fresh conversation
  (tokens last 1 hour and rotate).
- `404` — unknown conversation id.
- `403` — visitor blocked, or chat disabled for the agent.
- `409` — the conversation is waiting on a host action, transferred,
  or in a workflow; use the non-streaming `POST
  /api/v1/mobile/conversations/{conversation_id}/messages` endpoint.
- `422` — no message text, or files were attached (uploads must use
  the non-streaming endpoint).
- `429` — rate or quota limit hit; retry after a short wait.
- An `error` event mid-stream is not fatal by itself: keep any partial
  text and wait — a `done` event with the finished reply follows when
  one exists. Only an `error` with no following `done` is terminal for
  that request, and the client may retry the POST.

**The widget shows a send timeout but the reply still arrives.**

- The message was stored and the reply was generated; the send
  acknowledgement and the AI reply travel separately, so a slow
  acknowledgement does not mean the message was lost. Do not resend —
  check whether the reply arrived first.

**Nothing above helps.**

- Confirm the backend is healthy: `GET /health` should return
  `{"status": "healthy"}`.
- Check the backend log for the conversation's session id — streaming,
  KB, and provider errors are all logged with it.
- Reproduce with one plain question ("How do I reset my password?")
  against the seeded test articles, and note whether the reply source
  is `model-stream` or `model-full` (shown in the stream events
  and the SSE `done` payload). That tells you exactly which path ran.

## Reference

- Protocol detail (events, SSE format, eligibility rules, verification
  results): `docs/STREAMING-PROTOCOL.md`
- Lean streaming agent: `backend/app/agents/chat_agent.py`
  (`ChatAgent.stream_response`)
- Widget wiring: `backend/app/api/widget_chat.py`
- Mobile SSE endpoint: `backend/app/api/mobile_chat.py`
  (`POST /conversations/{conversation_id}/messages/stream`)
- AI config API: `backend/app/api/ai_setup.py`
- Knowledge API: `backend/app/api/knowledge.py`
- Test KB seed (test-only): `backend/scripts/seed_test_kb.py`
