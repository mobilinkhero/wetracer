<!--
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
-->

<!--
API Docs — the public Chat API (`/api/v1/mobile`) for the org's own
developers. Route: /settings/api-docs.

The bundled doc files below are verbatim copies of the canonical docs —
keep them in sync when the canonical files change:
  src/assets/openapi.yaml            <- frontend/src/assets/openapi.yaml (this repo)
  src/assets/ai-integration.md       <- docs/AI-INTEGRATION.md
  src/assets/ai-integration-compact.md <- frontend/src/assets/ai-integration-compact.md
  cp <canonical> frontend/src/assets/<name> && npm run build
-->
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { toast } from 'vue-sonner'
import DashboardLayout from '@/layouts/DashboardLayout.vue'
import CopyField from '@/components/integrations/CopyField.vue'
import api from '@/services/api'
import { getApiUrl } from '@/config/api'
import { widgetService } from '@/services/widget'
import { agentService } from '@/services/agent'
import type { Widget } from '@/types/widget'
import openApiSpec from '@/assets/openapi.yaml?raw'
import aiIntegrationDoc from '@/assets/ai-integration.md?raw'
import aiCompactDoc from '@/assets/ai-integration-compact.md?raw'

const apiBase = getApiUrl()

const widgets = ref<Widget[]>([])
const agentNames = ref<Record<string, string>>({})
const selectedWidgetId = ref('')
const loading = ref(true)
const loadError = ref('')
const fcmStatus = ref<{ configured: boolean; project_id: string | null } | null>(null)
const copyAiState = ref('Copy for AI')

const selectedWidget = computed(
  () => widgets.value.find((w) => w.id === selectedWidgetId.value) ?? widgets.value[0],
)

const agentNameFor = (widget: Widget | undefined): string => {
  if (!widget) return ''
  return agentNames.value[widget.agent_id] ?? ''
}

// GET /api/v1/widgets never returns a widget name (WidgetResponse has no
// `name` field — the DB row has one, the serializer drops it), so the widget
// is labelled by the agent it chats with. Two widgets on one agent get the
// short id appended so the picker can tell them apart.
const widgetLabel = (widget: Widget): string => {
  const agent = agentNameFor(widget) || 'Widget'
  const siblings = widgets.value.filter((w) => agentNameFor(w) === agentNameFor(widget)).length
  return siblings > 1 ? `${agent} · ${widget.id.slice(0, 8)}` : agent
}

onMounted(async () => {
  try {
    const [foundWidgets, roster] = await Promise.all([
      widgetService.getWidgets(),
      agentService.getAgentRoster().catch(() => []),
    ])
    widgets.value = foundWidgets
    agentNames.value = Object.fromEntries(
      roster.map((a) => [a.id, a.display_name || a.name]),
    )
    if (foundWidgets.length > 0) selectedWidgetId.value = foundWidgets[0].id
  } catch {
    loadError.value = 'Could not load your widgets. The endpoint list below still applies.'
  } finally {
    loading.value = false
  }
  try {
    const response = await api.get('/admin/mobile/fcm-config')
    fcmStatus.value = response.data
  } catch {
    fcmStatus.value = null
  }
})

const copyForAi = async () => {
  try {
    await navigator.clipboard.writeText(aiCompactDoc)
    copyAiState.value = 'Copied!'
    toast.success('AI integration doc copied — paste it to your AI code builder')
    window.setTimeout(() => { copyAiState.value = 'Copy for AI' }, 2000)
  } catch {
    toast.error('Could not copy. Use the download links below instead.')
  }
}

const download = (filename: string, content: string, mime: string) => {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

const startExample = computed(() => `curl -X POST ${apiBase}/mobile/conversations \\
  -H 'Content-Type: application/json' \\
  -d '{"widget_id":"${selectedWidget.value?.id ?? 'YOUR_WIDGET_ID'}","visitor_name":"Ahtisham"}'`)

const sendExample = `curl -X POST ${apiBase}/mobile/conversations/<conversation_id>/messages \\
  -H "X-Conversation-Token: <token>" \\
  -F "message=Hello, what is my order total?"
# {"message":{"id":20,"sender":"visitor","text":"Hello, what is my order total?",
#   "message_type":"user","attachments":[],"created_at":"…",
#   "agent_name":"Media Test Support"},"ai_reply_pending":true}`
</script>

<template>
  <DashboardLayout>
    <div class="dashboard-container">
      <div class="settings-section">
        <div class="page-header">
          <h1>API Docs</h1>
          <p class="description">
            Chat with your AI agents from a mobile app — over the public Chat API, no WebView. Your widget id is public; secret keys never belong in an app.
          </p>
          <div class="actions">
            <button class="action-btn primary" type="button" @click="copyForAi">{{ copyAiState }}</button>
            <button
              class="action-btn"
              type="button"
              @click="download('openapi.yaml', openApiSpec, 'application/yaml')"
            >
              Download OpenAPI spec
            </button>
            <button
              class="action-btn"
              type="button"
              @click="download('AI-INTEGRATION.md', aiIntegrationDoc, 'text/markdown')"
            >
              Download AI integration doc
            </button>
          </div>
          <p class="hint">
            “Copy for AI” copies the compact integration doc — paste it to your AI code builder
            and it can write the integration for you. The full guide is
            <code>docs/AI-INTEGRATION.md</code>; the machine-readable spec is OpenAPI 3.
          </p>
        </div>

        <section class="card">
          <h2>Your widgets</h2>
          <p v-if="loading">Loading your widgets…</p>
          <p v-else-if="loadError" class="error">{{ loadError }}</p>
          <template v-else-if="selectedWidget">
            <label v-if="widgets.length > 1" class="field-label" for="api-docs-widget">Widget</label>
            <select
              v-if="widgets.length > 1"
              id="api-docs-widget"
              v-model="selectedWidgetId"
              class="widget-select"
            >
              <option v-for="w in widgets" :key="w.id" :value="w.id">{{ widgetLabel(w) }}</option>
            </select>
            <p class="widget-line">
              <strong>{{ widgetLabel(selectedWidget) }}</strong>
            </p>
            <CopyField
              field-id="api-docs-widget-id"
              label="Widget ID (public — this is what the app sends)"
              name="Widget ID"
              :value="selectedWidget.id"
            />
            <CopyField
              field-id="api-docs-base-url"
              label="API base URL"
              name="API base URL"
              :value="apiBase"
            />
            <p class="widget-line">
              Push (FCM):
              <span v-if="fcmStatus === null">status unavailable</span>
              <span v-else-if="fcmStatus.configured">
                configured (project {{ fcmStatus.project_id }}) — pushes go out for real
              </span>
              <span v-else>
                not configured — test mode, pushes are recorded, not sent
              </span>
            </p>
          </template>
          <p v-else>No widgets yet — create one from an AI agent first.</p>
        </section>

        <section class="card">
          <h2>Authentication</h2>
          <ol class="steps">
            <li>
              <code>POST /api/v1/mobile/conversations</code> with
              <code>{"widget_id": "&lt;your widget id&gt;"}</code> mints a visitor JWT.
            </li>
            <li>Store the <code>conversation_id</code> + <code>conversation_token</code> on device.</li>
            <li>Send the token as the <code>X-Conversation-Token</code> header on every call.</li>
            <li>
              The token lasts <strong>1 hour</strong>. Rotate it before it lapses:
              <code>POST /api/v1/mobile/conversations/{id}/rotate</code> — the old token is
              revoked at once. A lapsed token cannot be rotated; start/resume instead.
            </li>
            <li>
              To resume later, <code>POST /conversations</code> with the stored token — a live
              token returns its conversation with <code>created: false</code>.
            </li>
          </ol>
          <p>
            No dashboard login, no API key and no session cookie is accepted on these endpoints.
            If you sent a <code>device_id</code> at start, send the same id as
            <code>X-Device-Id</code> on every call — a token presented from another device gets
            401.
          </p>
          <pre class="code">{{ startExample }}</pre>
          <pre class="code"># {"conversation_id":"…","conversation_token":"eyJhbG…","customer_id":"…",
#  "widget_id":"…","agent_name":"Media Test Support","created":true,"expires_in":3600}</pre>
        </section>

        <section class="card">
          <h2>Endpoints</h2>
          <table class="endpoint-table">
            <thead>
              <tr><th>Method</th><th>Path</th><th>What it does</th></tr>
            </thead>
            <tbody>
              <tr><td><code>POST</code></td><td><code>/api/v1/mobile/conversations</code></td><td>Start / resume a conversation (mints the token)</td></tr>
              <tr><td><code>POST</code></td><td><code>/api/v1/mobile/conversations/{id}/rotate</code></td><td>Revoke + re-mint the token for the same conversation</td></tr>
              <tr><td><code>GET</code></td><td><code>/api/v1/mobile/conversations/{id}/messages</code></td><td>History / polling fallback (<code>?after=&amp;limit=</code>, default 50, max 100)</td></tr>
              <tr><td><code>POST</code></td><td><code>/api/v1/mobile/conversations/{id}/messages</code></td><td>Send text and/or files (multipart)</td></tr>
              <tr><td><code>POST</code></td><td><code>/api/v1/mobile/conversations/{id}/devices</code></td><td>Register an FCM token for push</td></tr>
              <tr><td><code>GET</code></td><td><code>/api/v1/mobile/push-attempts</code></td><td>Push decisions (debug / test mode)</td></tr>
              <tr><td><code>POST/GET</code></td><td><code>/api/v1/admin/mobile/fcm-config</code></td><td>Admin only: store / check FCM service-account JSON</td></tr>
              <tr><td><code>GET/POST/DELETE</code></td><td><code>/api/v1/admin/mobile/blocked-visitors</code></td><td>Admin only: blocked-visitor list</td></tr>
              <tr><td><code>GET/PUT</code></td><td><code>/api/v1/admin/mobile/widgets/{id}/security</code></td><td>Admin only: per-widget limits + kill switch</td></tr>
              <tr><td><code>GET</code></td><td><code>/api/v1/admin/mobile/abuse-events</code></td><td>Admin only: abuse-event log</td></tr>
            </tbody>
          </table>
          <h3>Send a message</h3>
          <pre class="code">{{ sendExample }}</pre>
          <p>
            The response holds the stored visitor message only — the AI reply is not in it. It
            arrives over the Socket.IO <code>/widget</code> namespace
            (<code>chat_response</code>) or via polling
            <code>GET .../messages?after=&lt;your message id&gt;</code>. Images
            (jpeg/png/gif/webp ≤ 5 MB), documents (pdf/doc/docx/txt/csv/xls/xlsx) and audio
            (webm/mp4/mpeg/m4a/ogg/wav/mp3 ≤ 10 MB) are understood by the model; a file that
            fails validation returns 422 and no message is stored. Never retry a send on
            timeout — poll instead, or the message goes twice.
          </p>
          <h3>Live replies</h3>
          <p>
            Same host, namespace <code>/widget</code>, connect auth
            <code>{"conversation_token": "&lt;token&gt;"}</code>. The socket is receive-only —
            chat is never sent over it. Events: <code>session_initialized</code>,
            <code>bot_typing</code>, <code>chat_response</code> (the AI reply, or with
            <code>type: "agent_message"</code> a human agent's reply) and <code>error</code>.
          </p>
        </section>

        <section class="card">
          <h2>Rate limits &amp; errors</h2>
          <table class="endpoint-table">
            <thead>
              <tr><th>Limit</th><th>Default</th></tr>
            </thead>
            <tbody>
              <tr><td>Messages per conversation</td><td>30 / minute</td></tr>
              <tr><td>Messages per IP</td><td>120 / minute</td></tr>
              <tr><td>Messages per widget</td><td>600 / minute</td></tr>
              <tr><td>AI replies per conversation</td><td>200 / day</td></tr>
              <tr><td>AI replies per widget</td><td>2000 / day</td></tr>
              <tr><td>Uploads per conversation</td><td>20 files / 50 MB / day</td></tr>
            </tbody>
          </table>
          <p>
            Every 429 carries a <code>Retry-After</code> header (seconds) — back off for that
            long. Identical repeats and link floods are slowed down (429) and, past the
            threshold, temp-banned for 15 minutes (403). Common statuses: 401 token
            invalid/expired (resume or rotate), 403 blocked or kill switch, 404 unknown
            widget or a conversation that is not yours, 410 conversation closed (start a
            new one), 422 bad body/file/empty message.
          </p>
        </section>

        <section class="card">
          <h2>Build your own client</h2>
          <p>
            Any HTTP client can use the endpoints above — start a conversation, send
            messages over REST, and receive live replies on the Socket.IO
            <code>/widget</code> namespace. The full guide
            (<code>docs/AI-INTEGRATION.md</code>) and the OpenAPI spec are available
            to download above. Play Integrity / App Attest are intentionally not
            used; the API relies on the visitor JWT, rate limits and device
            binding instead.
          </p>
        </section>
      </div>
    </div>
  </DashboardLayout>
</template>

<style scoped>
.dashboard-container {
  display: flex;
  height: 100%;
  position: relative;
}

.settings-section {
  flex: 1;
  padding: var(--space-lg);
  max-width: 960px;
}

.page-header {
  margin-bottom: var(--space-xl);
}

.page-header h1 {
  font-size: var(--text-2xl);
  color: var(--text-color);
  margin-bottom: var(--space-sm);
}

.description {
  color: var(--text-color);
  opacity: 0.7;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: var(--space-md);
}

.action-btn {
  padding: 9px 14px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-btn);
  background: var(--background-soft);
  color: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.action-btn.primary {
  background: var(--accent-ink);
  border-color: var(--accent-ink);
  color: #ffffff;
}

.action-btn:hover {
  border-color: var(--muted);
}

.hint {
  margin-top: var(--space-sm);
  font-size: 13px;
  opacity: 0.75;
}

.card {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background: var(--background-soft);
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}

.card h2 {
  font-size: 17px;
  margin: 0 0 var(--space-sm);
}

.card h3 {
  font-size: 14px;
  margin: var(--space-md) 0 var(--space-sm);
}

.card p {
  font-size: 14px;
  line-height: 1.55;
}

.card code {
  font-size: 12.5px;
}

.steps {
  padding-left: 20px;
  font-size: 14px;
  line-height: 1.6;
}

.steps li {
  margin-bottom: 6px;
}

.code {
  background: #0b0c10;
  color: #e8e8e8;
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 12.5px;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre;
}

.endpoint-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13.5px;
  margin-bottom: var(--space-sm);
}

.endpoint-table th,
.endpoint-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-color);
  vertical-align: top;
}

.endpoint-table th {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  opacity: 0.7;
}

.endpoint-table td code {
  white-space: nowrap;
}

.field-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.widget-select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-btn);
  background: var(--background-soft);
  color: inherit;
  font-size: 13px;
  margin-bottom: 10px;
}

.widget-line {
  margin: var(--space-sm) 0;
}

.error {
  color: #b91c1c;
}
</style>
